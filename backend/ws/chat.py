"""WebSocket chat handler and sheet watch loop."""
from __future__ import annotations

import asyncio
import json
import secrets
import traceback

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.agent import Agent
from backend.core.helpers import friendly_error
from backend.core.state import sessions, touch, watchers
from backend import db as ssdb
from backend.logging_config import get_logger
from backend.rate_limit import check_limit
from backend.smartsheet_client import SmartsheetClient

log = get_logger(__name__)
router = APIRouter()


async def watcher_loop(session_id: str, interval: int, ws_ref: list):
    session = sessions.get(session_id)
    if not session:
        return

    ss_client: SmartsheetClient = session["smartsheet"]
    sheet_id = session["sheet_id"]
    last_snapshot: dict | None = None

    while True:
        await asyncio.sleep(interval)
        try:
            sheet = await ss_client.get_sheet(sheet_id, page_size=100, max_rows=100)
            rows = sheet.get("rows", [])
            snapshot = {str(r["id"]): len(r.get("cells", [])) for r in rows}

            if last_snapshot is not None and snapshot != last_snapshot:
                added = set(snapshot.keys()) - set(last_snapshot.keys())
                removed = set(last_snapshot.keys()) - set(snapshot.keys())
                changes = []
                if added:
                    changes.append(f"{len(added)} new row(s)")
                if removed:
                    changes.append(f"{len(removed)} removed row(s)")
                if not changes:
                    changes.append("cell data changed")

                for w in ws_ref:
                    try:
                        await w.send_json({
                            "type": "notification",
                            "changes": changes,
                        })
                    except Exception:
                        pass

            last_snapshot = snapshot
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.debug(f"Watcher error: {e}", extra={"session_id": session_id})


@router.websocket("/ws/{session_id}")
async def websocket_chat(ws: WebSocket, session_id: str):
    await ws.accept()
    session = sessions.get(session_id)
    if not session:
        await ws.send_json({"type": "error", "content": "Invalid session"})
        await ws.close()
        return

    expected_token = session.get("ws_token")
    provided_token = ws.query_params.get("token", "")
    if expected_token and not secrets.compare_digest(expected_token, provided_token):
        log.warning("Rejected WS with bad ws_token", extra={"session_id": session_id})
        await ws.send_json({"type": "error", "content": "Unauthorized: missing or invalid ws_token."})
        await ws.close(code=1008)
        return

    touch(session_id)
    agent: Agent = session["agent"]
    messages: list = session["messages"]
    agent_task: asyncio.Task | None = None
    pending_confirmations: dict[str, asyncio.Future] = {}

    ws_list: list = session.setdefault("_ws_list", [])
    ws_list.append(ws)

    log.info("WS connected", extra={"session_id": session_id})

    async def _persist_assistant(content: str):
        cid = session.get("active_conversation_id")
        uid = session.get("db_user_id")
        if cid and uid and content:
            try:
                await ssdb.append_message(cid, "assistant", content)
            except Exception as e:
                log.debug(f"persist assistant failed: {e}")

    async def send_event(event):
        await ws.send_json(event)
        if event.get("type") in ("response", "stream_end") and event.get("content"):
            await _persist_assistant(event["content"])

    async def confirm_callback(tool_name: str, arguments: dict, tool_call_id: str) -> bool:
        future = asyncio.get_event_loop().create_future()
        pending_confirmations[tool_call_id] = future
        await ws.send_json({
            "type": "confirm_action",
            "tool_call_id": tool_call_id,
            "tool": tool_name,
            "arguments": arguments,
        })
        try:
            approved = await future
            uid = session.get("db_user_id")
            if uid:
                try:
                    await ssdb.log_audit(
                        user_id=uid,
                        sheet_id=str(arguments.get("sheet_id") or session.get("sheet_id") or ""),
                        tool_name=tool_name,
                        arguments=arguments,
                        before=None,
                        after=None,
                        status="approved" if approved else "rejected",
                    )
                except Exception as e:
                    log.debug(f"Audit log write failed: {e}")
            return approved
        finally:
            pending_confirmations.pop(tool_call_id, None)

    async def run_agent_safe(user_msg: str):
        messages.append({"role": "user", "content": user_msg})
        # Persist user message
        cid = session.get("active_conversation_id")
        uid = session.get("db_user_id")
        if cid and uid:
            try:
                await ssdb.append_message(cid, "user", user_msg)
            except Exception as e:
                log.debug(f"persist user failed: {e}")
        try:
            await agent.run(messages, on_event=send_event, confirm_callback=confirm_callback)
        except asyncio.CancelledError:
            await ws.send_json({"type": "cancelled", "content": "Request interrupted."})
        except Exception as e:
            log.error(f"Agent error: {traceback.format_exc()}", extra={"session_id": session_id})
            await ws.send_json({"type": "response", "content": friendly_error(e)})

    recv_task: asyncio.Task | None = None

    def _handle_control(payload: dict) -> bool:
        nonlocal agent_task
        msg_type = payload.get("type", "")
        if msg_type == "cancel":
            for fut in pending_confirmations.values():
                if not fut.done():
                    fut.set_result(False)
            if agent_task and not agent_task.done():
                agent_task.cancel()
            return True
        if msg_type == "confirm":
            tcid = payload.get("tool_call_id")
            if tcid in pending_confirmations and not pending_confirmations[tcid].done():
                pending_confirmations[tcid].set_result(True)
            return True
        if msg_type == "reject":
            tcid = payload.get("tool_call_id")
            if tcid in pending_confirmations and not pending_confirmations[tcid].done():
                pending_confirmations[tcid].set_result(False)
            return True
        return False

    async def _handle_watch(payload: dict):
        enabled = payload.get("enabled", True)
        if enabled:
            interval = max(15, int(payload.get("interval", 60)))
            if session_id in watchers:
                watchers[session_id].cancel()
            watchers[session_id] = asyncio.create_task(
                watcher_loop(session_id, interval, ws_list)
            )
            await ws.send_json({"type": "response", "content": f"Watch mode enabled (every {interval}s)."})
        else:
            task = watchers.pop(session_id, None)
            if task:
                task.cancel()
            await ws.send_json({"type": "response", "content": "Watch mode disabled."})

    try:
        while True:
            if recv_task is None or recv_task.done():
                recv_task = asyncio.ensure_future(ws.receive_text())

            wait_set = {recv_task}
            if agent_task and not agent_task.done():
                wait_set.add(agent_task)

            done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

            if recv_task in done:
                try:
                    payload = json.loads(recv_task.result())
                except (json.JSONDecodeError, Exception):
                    recv_task = None
                    continue
                recv_task = None
                touch(session_id)

                ok, retry_after = check_limit(session_id, "ws")
                if not ok:
                    await ws.send_json({
                        "type": "response",
                        "content": f"Slow down! Please wait {retry_after:.1f}s before sending more messages.",
                    })
                    continue

                if _handle_control(payload):
                    continue
                if payload.get("type") == "watch":
                    await _handle_watch(payload)
                    continue

                user_msg = payload.get("message", "")
                if user_msg:
                    llm_ok, llm_retry = check_limit(session_id, "llm")
                    if not llm_ok:
                        await ws.send_json({
                            "type": "response",
                            "content": f"LLM rate limit reached. Retry in {llm_retry:.1f}s.",
                        })
                        continue
                    if agent_task and not agent_task.done():
                        agent_task.cancel()
                        try:
                            await agent_task
                        except asyncio.CancelledError:
                            pass
                    agent_task = asyncio.create_task(run_agent_safe(user_msg))

            if agent_task and agent_task in done:
                try:
                    agent_task.result()
                except (asyncio.CancelledError, Exception):
                    pass
                agent_task = None

    except WebSocketDisconnect:
        log.info("WS disconnected", extra={"session_id": session_id})
        if agent_task and not agent_task.done():
            agent_task.cancel()
        for fut in pending_confirmations.values():
            if not fut.done():
                fut.cancel()
        if ws in ws_list:
            ws_list.remove(ws)
        if recv_task and not recv_task.done():
            recv_task.cancel()
