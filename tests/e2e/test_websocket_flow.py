"""End-to-end WebSocket flow:

1. POST /api/session  → get a session_id + ws_token
2. Connect to /ws/{session_id}?token=...
3. Send a chat message
4. Receive stream_delta + stream_end events from the stubbed LLM
5. Confirm the FastAPI lifespan + DB init worked end-to-end

We stub `LLMRouter.chat_stream` to avoid real OpenAI calls and keep the test
fast and deterministic. The Smartsheet client still talks to the real API
(uses the token from .env) so this exercises the genuine session bootstrap.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.e2e


def _create_test_app(monkeypatch, tmp_path: Path):
    """Build a fresh TestClient with the DB redirected to tmp and the LLM
    stubbed. The lifespan event runs init_db() the moment we enter the
    `with` block."""
    db_file = tmp_path / "e2e.sqlite"
    monkeypatch.setenv("SMARTSHEET_CTRL_DB", str(db_file))

    from backend import db as ssdb
    monkeypatch.setattr(ssdb, "DB_PATH", db_file)
    monkeypatch.setattr(ssdb, "_initialized", False)

    # Stub the LLM streaming so no OpenAI/Anthropic call is made.
    from backend import llm_router

    async def fake_stream(self, messages, tools=None, system=""):
        text = "Hello from the stubbed LLM.\n[SUGGESTIONS] Show rows | Detect issues"
        yield {"type": "stream_delta", "content": text}
        yield {"type": "stream_end", "content": text}

    monkeypatch.setattr(llm_router.LLMRouter, "chat_stream", fake_stream)

    from backend.app import app
    return TestClient(app)


class TestWebSocketHappyPath:
    def test_full_flow(self, monkeypatch, tmp_path, smartsheet_token, sheet_id, openai_api_key):
        if not openai_api_key:
            pytest.skip("OPENAI_API_KEY missing in .env (used to satisfy provider check, not for real calls)")

        with _create_test_app(monkeypatch, tmp_path) as http:
            # 1) Open a session
            r = http.post("/api/session", json={
                "smartsheet_token": smartsheet_token,
                "sheet_id": sheet_id,
                "llm_provider": "openai",
                "llm_model": "gpt-4o-mini",
                "llm_api_key": openai_api_key,
            })
            assert r.status_code == 200, r.text
            body = r.json()
            session_id = body["session_id"]
            ws_token = body["ws_token"]
            assert session_id and ws_token
            assert body["sheet"]["name"]
            assert "welcome" in body

            # 2) Connect over WebSocket
            url = f"/ws/{session_id}?token={ws_token}"
            with http.websocket_connect(url) as ws:
                # 3) Send a benign user message
                ws.send_text(json.dumps({"message": "hello agent"}))

                # 4) Collect events until we see stream_end (or timeout)
                seen: list[dict] = []
                for _ in range(20):
                    evt = ws.receive_json()
                    seen.append(evt)
                    if evt.get("type") == "stream_end":
                        break

                types = [e.get("type") for e in seen]
                assert "stream_delta" in types
                assert "stream_end" in types

                final = next(e for e in seen if e["type"] == "stream_end")
                assert "stubbed LLM" in final["content"]
                # The agent must have stripped the [SUGGESTIONS] line and
                # promoted them to a structured field
                assert "[SUGGESTIONS]" not in final["content"]
                assert final.get("suggestions") == ["Show rows", "Detect issues"]


class TestWebSocketAuth:
    def test_bad_ws_token_rejected(self, monkeypatch, tmp_path, smartsheet_token, sheet_id, openai_api_key):
        if not openai_api_key:
            pytest.skip("OPENAI_API_KEY missing in .env")

        with _create_test_app(monkeypatch, tmp_path) as http:
            r = http.post("/api/session", json={
                "smartsheet_token": smartsheet_token,
                "sheet_id": sheet_id,
                "llm_provider": "openai",
                "llm_model": "gpt-4o-mini",
                "llm_api_key": openai_api_key,
            })
            assert r.status_code == 200
            session_id = r.json()["session_id"]

            # Connect with the wrong token: server should send an error
            # event and close the connection.
            with http.websocket_connect(f"/ws/{session_id}?token=WRONG") as ws:
                first = ws.receive_json()
                assert first.get("type") == "error"
                assert "Unauthorized" in first.get("content", "")

    def test_unknown_session_rejected(self, monkeypatch, tmp_path):
        with _create_test_app(monkeypatch, tmp_path) as http:
            with http.websocket_connect("/ws/this-session-does-not-exist?token=x") as ws:
                first = ws.receive_json()
                assert first.get("type") == "error"
                assert "Invalid session" in first.get("content", "")


class TestPublicEndpointsUnderLifespan:
    """Sanity check that the lifespan boots cleanly with the test DB and
    exposes the expected health/env routes."""

    def test_health_and_env(self, monkeypatch, tmp_path):
        with _create_test_app(monkeypatch, tmp_path) as http:
            assert http.get("/health").status_code == 200
            env = http.get("/api/env-status").json()
            assert env["has_smartsheet_token"] is True
            assert env["has_sheet_id"] is True
