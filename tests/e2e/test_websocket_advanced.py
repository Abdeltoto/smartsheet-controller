"""Advanced WebSocket scenarios:

- Cancel mid-stream → server replies with {"type":"cancelled"}
- Tool-call confirm flow over WS (approve + reject)
- Rate-limit response when client floods messages
- Multi-message conversation in a single WS connection

All scenarios use a stubbed LLM so they're deterministic and fast.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.e2e


# ────────────────────── shared bootstrap ──────────────────────

def _bootstrap(monkeypatch, tmp_path: Path, fake_stream, fake_execute=None):
    db_file = tmp_path / "advws.sqlite"
    monkeypatch.setenv("SMARTSHEET_CTRL_DB", str(db_file))

    from backend import db as ssdb
    monkeypatch.setattr(ssdb, "DB_PATH", db_file)
    monkeypatch.setattr(ssdb, "_initialized", False)

    from backend import llm_router
    monkeypatch.setattr(llm_router.LLMRouter, "chat_stream", fake_stream)

    if fake_execute is not None:
        from backend import agent as agent_mod
        monkeypatch.setattr(agent_mod, "execute_tool", fake_execute)

    from backend.app import app
    return TestClient(app)


def _open_session(http: TestClient, smartsheet_token: str, sheet_id: str,
                  openai_api_key: str) -> tuple[str, str]:
    r = http.post("/api/session", json={
        "smartsheet_token": smartsheet_token,
        "sheet_id": sheet_id,
        "llm_provider": "openai",
        "llm_model": "gpt-4o-mini",
        "llm_api_key": openai_api_key,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    return body["session_id"], body["ws_token"]


# ────────────────────── cancel mid-stream ──────────────────────

class TestCancelMidStream:
    def test_cancel_during_long_response(self, monkeypatch, tmp_path,
                                          smartsheet_token, sheet_id, openai_api_key):
        if not openai_api_key:
            pytest.skip("OPENAI_API_KEY missing in .env")

        async def slow_stream(self, messages, tools=None, system=""):
            # Trickle deltas so the test has time to send a cancel
            for i in range(10):
                yield {"type": "stream_delta", "content": f"chunk {i} "}
                await asyncio.sleep(0.2)
            yield {"type": "stream_end", "content": "should never finish"}

        client = _bootstrap(monkeypatch, tmp_path, slow_stream)
        with client as http:
            sid, tok = _open_session(http, smartsheet_token, sheet_id, openai_api_key)

            with http.websocket_connect(f"/ws/{sid}?token={tok}") as ws:
                ws.send_text(json.dumps({"message": "tell me a long story"}))

                # Receive at least one delta, then cancel
                first = ws.receive_json()
                assert first["type"] == "stream_delta"

                ws.send_text(json.dumps({"type": "cancel"}))

                # Drain until we see the cancelled event (or run out of patience)
                cancelled = None
                for _ in range(30):
                    try:
                        evt = ws.receive_json()
                    except Exception:
                        break
                    if evt.get("type") == "cancelled":
                        cancelled = evt
                        break
                assert cancelled is not None, "Server must emit a 'cancelled' event after a cancel control message"
                assert "interrupted" in cancelled["content"].lower()


# ────────────────────── tool-call confirm flow ──────────────────────

class TestToolCallConfirmFlow:
    """Scripts a 2-round agent loop: round 1 emits a destructive tool_call,
    round 2 closes after the tool result. We control whether the user
    approves or rejects via the WebSocket."""

    @staticmethod
    def _scripted_two_round_stream():
        """Returns a chat_stream that emits tool_calls on round 1, plain text on round 2."""
        state = {"round": 0}

        async def stream(self, messages, tools=None, system=""):
            state["round"] += 1
            if state["round"] == 1:
                yield {
                    "type": "tool_calls",
                    "tool_calls": [{
                        "id": "tc-ws-1",
                        "name": "delete_rows",  # destructive → triggers confirm_action
                        "arguments": {"sheet_id": "1", "row_ids": [42]},
                    }],
                    "raw_message": {"role": "assistant", "tool_calls": [{"id": "tc-ws-1"}]},
                }
            else:
                yield {"type": "stream_end", "content": "Done.\n[SUGGESTIONS] Refresh"}

        return stream

    def test_approve_executes_tool(self, monkeypatch, tmp_path,
                                    smartsheet_token, sheet_id, openai_api_key):
        if not openai_api_key:
            pytest.skip("OPENAI_API_KEY missing in .env")

        executed = {"called": False}

        async def fake_execute(client, name, args):
            executed["called"] = True
            return json.dumps({"message": "row deleted"})

        client = _bootstrap(monkeypatch, tmp_path,
                            self._scripted_two_round_stream(),
                            fake_execute=fake_execute)
        with client as http:
            sid, tok = _open_session(http, smartsheet_token, sheet_id, openai_api_key)
            with http.websocket_connect(f"/ws/{sid}?token={tok}") as ws:
                ws.send_text(json.dumps({"message": "delete row 42"}))

                # First we should get a confirm_action
                evt = ws.receive_json()
                while evt.get("type") not in ("confirm_action", "stream_end"):
                    evt = ws.receive_json()
                assert evt["type"] == "confirm_action"
                assert evt["tool"] == "delete_rows"
                tcid = evt["tool_call_id"]

                # Approve
                ws.send_text(json.dumps({"type": "confirm", "tool_call_id": tcid}))

                # Drain until stream_end
                end = None
                for _ in range(15):
                    e = ws.receive_json()
                    if e.get("type") == "stream_end":
                        end = e
                        break
                assert end is not None
                assert "Done" in end["content"]
                assert executed["called"], "Tool must execute when user approves"

    def test_reject_skips_tool(self, monkeypatch, tmp_path,
                                smartsheet_token, sheet_id, openai_api_key):
        if not openai_api_key:
            pytest.skip("OPENAI_API_KEY missing in .env")

        executed = {"called": False}

        async def fake_execute(client, name, args):
            executed["called"] = True
            return json.dumps({"message": "should not happen"})

        client = _bootstrap(monkeypatch, tmp_path,
                            self._scripted_two_round_stream(),
                            fake_execute=fake_execute)
        with client as http:
            sid, tok = _open_session(http, smartsheet_token, sheet_id, openai_api_key)
            with http.websocket_connect(f"/ws/{sid}?token={tok}") as ws:
                ws.send_text(json.dumps({"message": "delete row 42"}))

                # Wait for confirm_action
                evt = ws.receive_json()
                while evt.get("type") not in ("confirm_action", "stream_end"):
                    evt = ws.receive_json()
                assert evt["type"] == "confirm_action"
                tcid = evt["tool_call_id"]

                # Reject
                ws.send_text(json.dumps({"type": "reject", "tool_call_id": tcid}))

                # Should still wrap up with stream_end (round 2 of the agent loop)
                end = None
                for _ in range(15):
                    e = ws.receive_json()
                    if e.get("type") == "stream_end":
                        end = e
                        break
                assert end is not None
                assert not executed["called"], "Tool must NOT run when user rejects"


# ────────────────────── rate limit ──────────────────────

class TestWebsocketRateLimit:
    def test_floods_get_throttled(self, monkeypatch, tmp_path,
                                   smartsheet_token, sheet_id, openai_api_key):
        if not openai_api_key:
            pytest.skip("OPENAI_API_KEY missing in .env")

        async def fast_stream(self, messages, tools=None, system=""):
            text = "ok\n[SUGGESTIONS] More"
            yield {"type": "stream_delta", "content": text}
            yield {"type": "stream_end", "content": text}

        # Tighten the WS rate limit so we can hit it easily
        from backend import rate_limit as rl_mod
        original_capacity = rl_mod.WS_BUCKET_CAPACITY if hasattr(rl_mod, "WS_BUCKET_CAPACITY") else None

        client = _bootstrap(monkeypatch, tmp_path, fast_stream)
        with client as http:
            sid, tok = _open_session(http, smartsheet_token, sheet_id, openai_api_key)

            # Replace the rate limiter on the running app with a very tight one
            from backend import app as app_mod
            from backend.rate_limit import SessionRateLimiter
            tight = SessionRateLimiter()

            def _tight_check(session_id, channel):
                # Hard cap: only first 2 messages allowed; everything else throttled
                state = tight.__dict__.setdefault("_count", {})
                key = (session_id, channel)
                state[key] = state.get(key, 0) + 1
                if state[key] > 2:
                    return False, 5.0
                return True, 0.0

            monkeypatch.setattr(app_mod, "check_limit", _tight_check)

            with http.websocket_connect(f"/ws/{sid}?token={tok}") as ws:
                throttle_seen = False
                for i in range(8):
                    ws.send_text(json.dumps({"message": f"msg {i}"}))
                    # Drain a few events for each message
                    for _ in range(6):
                        try:
                            evt = ws.receive_json()
                        except Exception:
                            break
                        content = evt.get("content", "")
                        if "Slow down" in content or "rate limit" in content.lower():
                            throttle_seen = True
                            break
                        if evt.get("type") == "stream_end":
                            break
                    if throttle_seen:
                        break

                assert throttle_seen, "Server must throttle when the WS rate limit is exceeded"


# ────────────────────── multi-turn conversation in one connection ──────────────────────

class TestMultiTurnInOneConnection:
    def test_two_consecutive_messages(self, monkeypatch, tmp_path,
                                       smartsheet_token, sheet_id, openai_api_key):
        if not openai_api_key:
            pytest.skip("OPENAI_API_KEY missing in .env")

        async def echo_stream(self, messages, tools=None, system=""):
            # Echo last user message so we can verify both turns landed
            last = next((m["content"] for m in reversed(messages)
                          if m.get("role") == "user"), "?")
            text = f"echo:{last}\n[SUGGESTIONS] Continue"
            yield {"type": "stream_delta", "content": text}
            yield {"type": "stream_end", "content": text}

        client = _bootstrap(monkeypatch, tmp_path, echo_stream)
        with client as http:
            sid, tok = _open_session(http, smartsheet_token, sheet_id, openai_api_key)
            with http.websocket_connect(f"/ws/{sid}?token={tok}") as ws:
                for turn in ("first", "second"):
                    ws.send_text(json.dumps({"message": turn}))
                    end = None
                    for _ in range(10):
                        evt = ws.receive_json()
                        if evt.get("type") == "stream_end":
                            end = evt
                            break
                    assert end is not None, f"No stream_end for turn '{turn}'"
                    assert f"echo:{turn}" in end["content"]
