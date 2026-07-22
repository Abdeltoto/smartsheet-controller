"""Auto-split from backend/app.py — S12 router."""
from __future__ import annotations

import json
import os
import secrets
import time
import traceback
import uuid
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from backend import db as ssdb
from backend.agent import Agent
from backend.core.helpers import detect_available_providers, friendly_error, resolve_api_key
from backend.core.state import (
    APP_START_TIME,
    resolve_session,
    session_secrets,
    sessions,
    touch,
    watchers,
)
from backend.llm_router import LLMRouter, PROVIDERS, get_provider_info
from backend.logging_config import get_logger
from backend.rate_limit import rate_limiter
from backend.services.session import (
    build_sheet_context,
    build_welcome,
    create_session as bootstrap_session,
)
from backend.smartsheet_client import SmartsheetClient

log = get_logger(__name__)
router = APIRouter()

class ValidateTokenRequest(BaseModel):
    smartsheet_token: str


@router.post("/api/validate-token")
async def validate_token(req: ValidateTokenRequest):
    """Validate a Smartsheet token and return user info + sheet list in one call.

    Used by the BYOT connect form: user pastes their token, we confirm who
    they are and populate the sheet browser in step 2.
    """
    token = req.smartsheet_token.strip()
    if not token or len(token) < 16:
        return JSONResponse({"error": "Token looks too short. Please paste your full Smartsheet API token."}, status_code=400)

    ss_client = SmartsheetClient(token)
    try:
        user = await ss_client.get_current_user()
        sheets = await ss_client.list_sheets()
    except Exception as e:
        await ss_client.close()
        log.warning(f"validate-token failed: {traceback.format_exc().splitlines()[-1]}")
        return JSONResponse({"error": friendly_error(e)}, status_code=400)

    try:
        await ss_client.close()
    except Exception:
        pass

    available = detect_available_providers()
    return {
        "user": {
            "id": user.get("id"),
            "email": user.get("email"),
            "firstName": user.get("firstName"),
            "lastName": user.get("lastName"),
            "account": (user.get("account") or {}).get("name"),
        },
        "sheets": sheets,
        "available_providers": available,
    }


class LookupSheetRequest(BaseModel):
    smartsheet_token: str
    sheet_id: str


@router.post("/api/lookup-sheet")
async def lookup_sheet(req: LookupSheetRequest):
    """Resolve a sheet ID against the given token.

    Used by the connect wizard "By ID" tab : user pastes a sheet ID and we
    confirm the sheet exists and is accessible before letting them start a
    session on it. Returns the sheet name and a quick summary on success.
    """
    token = req.smartsheet_token.strip()
    sheet_id = req.sheet_id.strip()
    if not token or len(token) < 16:
        return JSONResponse({"error": "Smartsheet token missing or too short."}, status_code=400)
    if not sheet_id.isdigit():
        return JSONResponse({"error": "Sheet ID must be numeric (e.g. 7340597274509188)."}, status_code=400)

    ss_client = SmartsheetClient(token)
    try:
        summary = await ss_client.get_sheet_summary(sheet_id)
    except Exception as e:
        await ss_client.close()
        log.warning(f"lookup-sheet failed for {sheet_id}: {traceback.format_exc().splitlines()[-1]}")
        return JSONResponse({"error": friendly_error(e)}, status_code=400)

    try:
        await ss_client.close()
    except Exception:
        pass

    return {
        "id": sheet_id,
        "name": summary.get("name", ""),
        "row_count": summary.get("totalRowCount", 0),
        "column_count": summary.get("columnCount", 0),
    }


class CreateBlankSheetRequest(BaseModel):
    smartsheet_token: str
    name: str
    columns: list[dict] | None = None


@router.post("/api/create-sheet")
async def create_blank_sheet(req: CreateBlankSheetRequest):
    """Create a brand-new sheet with sensible starter columns.

    Used by the connect wizard "Create new" tab. The user gives a name, the
    server creates a blank sheet (Task / Status / Due Date / Notes by default
    or caller-provided columns), and the new ID flows back to the wizard so
    the session opens directly on it.
    """
    token = req.smartsheet_token.strip()
    name = (req.name or "").strip()
    if not token or len(token) < 16:
        return JSONResponse({"error": "Smartsheet token missing or too short."}, status_code=400)
    if not name:
        return JSONResponse({"error": "Please give the new sheet a name."}, status_code=400)
    if len(name) > 50:
        return JSONResponse({"error": "Sheet name must be 50 characters or fewer."}, status_code=400)

    columns = req.columns or [
        {"title": "Task", "primary": True, "type": "TEXT_NUMBER"},
        {"title": "Status", "type": "PICKLIST", "options": ["Not Started", "In Progress", "Done"]},
        {"title": "Due Date", "type": "DATE"},
        {"title": "Notes", "type": "TEXT_NUMBER"},
    ]

    ss_client = SmartsheetClient(token)
    try:
        created = await ss_client.create_sheet(name, columns)
    except Exception as e:
        await ss_client.close()
        log.warning(f"create-sheet failed for '{name}': {traceback.format_exc().splitlines()[-1]}")
        return JSONResponse({"error": friendly_error(e)}, status_code=400)

    try:
        await ss_client.close()
    except Exception:
        pass

    payload = created.get("result") or created.get("data") or created
    new_id = payload.get("id") or created.get("id")
    if not new_id:
        return JSONResponse({"error": "Sheet created but Smartsheet did not return an ID."}, status_code=500)

    log.info(f"Blank sheet '{name}' created (id={new_id})")
    return {
        "id": str(new_id),
        "name": payload.get("name") or name,
        "permalink": payload.get("permalink", ""),
    }


class SessionConfig(BaseModel):
    smartsheet_token: str
    sheet_id: str
    llm_provider: str = "openai"
    llm_model: str = ""
    llm_api_key: str = ""


@router.post("/api/session")
async def create_session(config: SessionConfig):
    provider = config.llm_provider.lower()
    model = config.llm_model.strip()
    if not model:
        info = PROVIDERS.get(provider, {})
        model = info.get("default_model", "gpt-4o-mini") if info else "gpt-4o-mini"

    api_key = resolve_api_key(provider, config.llm_api_key)
    if not api_key:
        return JSONResponse(
            {"error": f"No API key for {provider}. Provide one or set {PROVIDERS.get(provider, {}).get('env_key', '???')} in .env"},
            status_code=400,
        )

    token = config.smartsheet_token.strip()
    if not token:
        return JSONResponse({"error": "Smartsheet token is required."}, status_code=400)

    sheet_id = config.sheet_id.strip()
    if not sheet_id.isdigit():
        return JSONResponse({"error": "Sheet ID must be a numeric Smartsheet sheet ID."}, status_code=400)

    ss_client = SmartsheetClient(token)
    try:
        result = await bootstrap_session(ss_client, sheet_id, LLMRouter(provider, model, api_key), smartsheet_token=token)
    except Exception as e:
        await ss_client.close()
        log.warning(f"Session creation failed: {traceback.format_exc().splitlines()[-1]}")
        return JSONResponse({"error": friendly_error(e)}, status_code=400)

    return result


@router.post("/api/quick-connect")
async def quick_connect():
    ss_token = os.getenv("SMARTSHEET_TOKEN", "").strip()
    sheet_id = os.getenv("SHEET_ID", "").strip()

    if not ss_token:
        return JSONResponse({"error": "SMARTSHEET_TOKEN not set in .env"}, status_code=400)
    if not sheet_id:
        return JSONResponse({"error": "SHEET_ID not set in .env"}, status_code=400)

    available = detect_available_providers()
    if not available:
        return JSONResponse({"error": "No LLM API key in .env"}, status_code=400)

    provider = next(iter(available))
    info = PROVIDERS[provider]
    api_key = os.getenv(info["env_key"], "").strip()
    model = info["default_model"]

    ss_client = SmartsheetClient(ss_token)
    try:
        result = await bootstrap_session(ss_client, sheet_id, LLMRouter(provider, model, api_key), smartsheet_token=ss_token)
    except Exception as e:
        await ss_client.close()
        log.warning(f"Quick-connect failed: {traceback.format_exc().splitlines()[-1]}")
        return JSONResponse({"error": friendly_error(e)}, status_code=400)

    return result


class SwitchSheetRequest(BaseModel):
    session_id: str
    sheet_id: str


@router.post("/api/switch-sheet")
async def switch_sheet(req: SwitchSheetRequest):
    session = sessions.get(req.session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)

    touch(req.session_id)

    ss_client: SmartsheetClient = session["smartsheet"]
    try:
        ctx = await build_sheet_context(ss_client, req.sheet_id)
    except Exception as e:
        log.warning(f"switch-sheet failed: {e}")
        return JSONResponse({"error": friendly_error(e)}, status_code=400)

    summary = ctx["summary"]
    session["agent"] = Agent(session["llm"], ss_client, req.sheet_id, ctx)
    session["messages"] = []
    session["sheet_id"] = req.sheet_id
    session["sheet_name"] = summary["name"]
    session["context"] = ctx

    return {"sheet": summary, "welcome": build_welcome(summary)}


class PinSheetRequest(BaseModel):
    session_id: str
    sheet_id: str


@router.post("/api/pin-sheet")
async def pin_sheet(req: PinSheetRequest):
    session = sessions.get(req.session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)

    touch(req.session_id)
    pinned: list = session.setdefault("pinned_sheets", [])
    if any(str(p["id"]) == str(req.sheet_id) for p in pinned):
        return JSONResponse({"error": "Sheet already pinned"}, status_code=400)
    if len(pinned) >= 3:
        return JSONResponse({"error": "Max 3 pinned sheets"}, status_code=400)

    ss_client: SmartsheetClient = session["smartsheet"]
    try:
        summary = await ss_client.get_sheet_summary(req.sheet_id)
    except Exception as e:
        return JSONResponse({"error": friendly_error(e)}, status_code=400)

    pinned.append({"id": req.sheet_id, "name": summary["name"], "summary": summary})
    session["agent"].pinned_sheets = pinned

    return {"pinned": [{"id": p["id"], "name": p["name"]} for p in pinned]}


@router.post("/api/unpin-sheet")
async def unpin_sheet(req: PinSheetRequest):
    session = sessions.get(req.session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)

    touch(req.session_id)
    pinned: list = session.get("pinned_sheets", [])
    pinned[:] = [p for p in pinned if str(p["id"]) != str(req.sheet_id)]
    session["agent"].pinned_sheets = pinned

    return {"pinned": [{"id": p["id"], "name": p["name"]} for p in pinned]}


class SwitchModelRequest(BaseModel):
    session_id: str
    provider: str
    model: str
    api_key: str | None = None


@router.post("/api/switch-model")
async def switch_model(req: SwitchModelRequest):
    session = sessions.get(req.session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)

    touch(req.session_id)
    provider = req.provider.lower()
    model = req.model.strip()

    if provider not in PROVIDERS:
        return JSONResponse({"error": f"Unknown provider: {provider}"}, status_code=400)

    # Priority: explicit api_key from request > env var
    api_key = (req.api_key or "").strip() or resolve_api_key(provider)
    if not api_key:
        return JSONResponse(
            {
                "error": f"No API key for {provider}. Provide one in the request body "
                f"or set {PROVIDERS[provider]['env_key']} in .env",
                "needs_key": True,
                "provider": provider,
            },
            status_code=400,
        )

    old_llm = session["llm"]
    if old_llm.provider == provider:
        old_llm.switch_model(model)
    else:
        new_llm = LLMRouter(provider, model, api_key)
        session["llm"] = new_llm
        session["agent"].llm = new_llm

    return {"provider": provider, "model": model}


class DisconnectRequest(BaseModel):
    session_id: str


@router.get("/api/usage")
async def get_usage(session_id: str):
    """Token usage and cache stats for a session — exposed in Settings."""
    session = sessions.get(session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)
    touch(session_id)
    llm = session.get("llm")
    ss_client = session.get("smartsheet")
    agent = session.get("agent")
    return {
        "tokens": llm.usage if llm else None,
        "provider": llm.provider if llm else None,
        "current_model": llm.model if llm else None,
        "cache": ss_client.cache_stats() if ss_client else None,
        # Agent reliability metrics: each counter ticks every time a safety
        # net (loop killer, schema-guard, parse recovery) catches a model
        # mistake, so the user can see in Settings how often the harness
        # actually saved the day.
        "agent_metrics": agent.metrics if agent and hasattr(agent, "metrics") else None,
    }


@router.post("/api/disconnect")
async def disconnect(
    req: DisconnectRequest,
    x_ws_token: str | None = Header(None, alias="X-WS-Token"),
    x_auth_cookie: str | None = Header(None, alias="X-Auth-Cookie"),
):
    session = sessions.get(req.session_id)
    if not session:
        return {"status": "ok"}

    resolve_session(
        req.session_id,
        x_ws_token,
        x_auth_cookie,
        require_secret=True,
    )
    session = sessions.pop(req.session_id, None)

    task = watchers.pop(req.session_id, None)
    if task and not task.done():
        task.cancel()

    ss_client = session.get("smartsheet")
    if ss_client:
        try:
            await ss_client.close()
        except Exception:
            pass

    rate_limiter.clear(req.session_id)
    log.info("Session disconnected", extra={"session_id": req.session_id})
    return {"status": "ok"}


class CsvImportRequest(BaseModel):
    session_id: str
    name: str
    headers: list[str]
    rows: list[list[str]]


@router.post("/api/csv-to-sheet")
async def csv_to_sheet(
    req: CsvImportRequest,
    x_ws_token: str | None = Header(None, alias="X-WS-Token"),
    x_auth_cookie: str | None = Header(None, alias="X-Auth-Cookie"),
):
    session = resolve_session(req.session_id, x_ws_token, x_auth_cookie, require_secret=True)

    name = (req.name or "").strip()
    if not name:
        return JSONResponse({"error": "Sheet name is required"}, status_code=400)
    if not req.headers:
        return JSONResponse({"error": "At least one column is required"}, status_code=400)
    if len(req.headers) > 200:
        return JSONResponse({"error": "Smartsheet supports max 200 columns"}, status_code=400)

    ss_client = session["smartsheet"]
    columns = []
    for i, h in enumerate(req.headers):
        title = (h or f"Column {i + 1}").strip()[:50] or f"Column {i + 1}"
        col = {"title": title, "type": "TEXT_NUMBER"}
        if i == 0:
            col["primary"] = True
        columns.append(col)

    try:
        created = await ss_client.create_sheet(name, columns)
        sheet_payload = created.get("result") or created.get("data") or created
        new_sheet_id = sheet_payload.get("id") or created.get("id")
        if not new_sheet_id:
            return JSONResponse({"error": "Sheet created but ID not returned"}, status_code=500)

        rows_to_add = []
        for row in req.rows[:5000]:
            mapped = {}
            for i, val in enumerate(row[: len(req.headers)]):
                col_title = columns[i]["title"]
                mapped[col_title] = val
            if mapped:
                rows_to_add.append(mapped)

        added = 0
        if rows_to_add:
            BATCH = 250
            for i in range(0, len(rows_to_add), BATCH):
                chunk = rows_to_add[i : i + BATCH]
                await ss_client.add_rows(str(new_sheet_id), chunk, to_bottom=True)
                added += len(chunk)

        log.info(
            f"CSV imported as sheet '{name}' ({added} rows, {len(req.headers)} cols)",
            extra={"session_id": req.session_id},
        )
        return {
            "status": "ok",
            "sheet_id": str(new_sheet_id),
            "name": name,
            "rows_added": added,
            "columns": len(req.headers),
        }
    except Exception as e:
        log.error(f"CSV import failed: {e}", extra={"session_id": req.session_id})
        return JSONResponse({"error": str(e)}, status_code=500)
