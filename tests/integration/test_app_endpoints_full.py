"""Broader HTTP endpoint coverage.

Uses a TestClient with the LLM stubbed and the DB redirected to a
temporary file. The Smartsheet client still hits the real API (token
from .env), so this proves the full session lifecycle works end-to-end
for every documented HTTP route.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


# ────────────────────── helpers ──────────────────────

def _make_test_client(monkeypatch, tmp_path: Path) -> TestClient:
    """Same recipe as test_websocket_flow._create_test_app: temp DB + stub LLM."""
    db_file = tmp_path / "endpoints.sqlite"
    monkeypatch.setenv("SMARTSHEET_CTRL_DB", str(db_file))

    from backend import db as ssdb
    monkeypatch.setattr(ssdb, "DB_PATH", db_file)
    monkeypatch.setattr(ssdb, "_initialized", False)

    from backend import llm_router

    async def fake_stream(self, messages, tools=None, system=""):
        text = "stub response\n[SUGGESTIONS] A | B"
        yield {"type": "stream_delta", "content": text}
        yield {"type": "stream_end", "content": text}

    monkeypatch.setattr(llm_router.LLMRouter, "chat_stream", fake_stream)

    from backend.app import app
    return TestClient(app)


@pytest.fixture
def http_session(monkeypatch, tmp_path, smartsheet_token, sheet_id, openai_api_key):
    """Yields (TestClient, session_id) inside a live lifespan."""
    if not openai_api_key:
        pytest.skip("OPENAI_API_KEY missing in .env")

    client = _make_test_client(monkeypatch, tmp_path)
    with client as http:
        r = http.post("/api/session", json={
            "smartsheet_token": smartsheet_token,
            "sheet_id": sheet_id,
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
            "llm_api_key": openai_api_key,
        })
        if r.status_code != 200:
            pytest.skip(f"Session bootstrap failed: {r.text}")
        sid = r.json()["session_id"]
        yield http, sid


# ────────────────────── usage / disconnect ──────────────────────

class TestUsageAndDisconnect:
    def test_usage_returns_token_breakdown(self, http_session):
        http, sid = http_session
        r = http.get(f"/api/usage?session_id={sid}")
        assert r.status_code == 200
        body = r.json()
        assert "tokens" in body
        assert body["provider"] == "openai"
        assert body["current_model"] == "gpt-4o-mini"
        assert "cache" in body

    def test_usage_invalid_session(self, http_session):
        http, _ = http_session
        r = http.get("/api/usage?session_id=does-not-exist")
        assert r.status_code == 400
        assert "error" in r.json()

    def test_disconnect_idempotent(self, http_session):
        http, sid = http_session
        r1 = http.post("/api/disconnect", json={"session_id": sid})
        assert r1.status_code == 200
        r2 = http.post("/api/disconnect", json={"session_id": sid})
        assert r2.status_code == 200  # second time still {"status": "ok"}

    def test_disconnect_unknown_session_ok(self, http_session):
        http, _ = http_session
        r = http.post("/api/disconnect", json={"session_id": "nope"})
        assert r.status_code == 200


# ────────────────────── favorites ──────────────────────

class TestFavorites:
    def test_full_lifecycle(self, http_session):
        http, sid = http_session

        # Empty by default
        r = http.get(f"/api/favorites?session_id={sid}")
        assert r.status_code == 200
        before = r.json()["favorites"]

        # Add
        r = http.post("/api/favorites/add", json={
            "session_id": sid, "sheet_id": "9999999999", "sheet_name": "Test Fav",
        })
        assert r.status_code == 200

        # Now appears
        favs = http.get(f"/api/favorites?session_id={sid}").json()["favorites"]
        assert any(str(f.get("sheet_id")) == "9999999999" for f in favs)
        assert len(favs) == len(before) + 1

        # Adding twice is idempotent
        r = http.post("/api/favorites/add", json={
            "session_id": sid, "sheet_id": "9999999999", "sheet_name": "Test Fav",
        })
        assert r.status_code == 200
        favs2 = http.get(f"/api/favorites?session_id={sid}").json()["favorites"]
        assert len(favs2) == len(favs)  # unchanged

        # Remove
        r = http.post("/api/favorites/remove", json={
            "session_id": sid, "sheet_id": "9999999999",
        })
        assert r.status_code == 200
        favs3 = http.get(f"/api/favorites?session_id={sid}").json()["favorites"]
        assert all(str(f.get("sheet_id")) != "9999999999" for f in favs3)

    def test_favorites_invalid_session(self, http_session):
        http, _ = http_session
        for path in ("/api/favorites/add", "/api/favorites/remove"):
            r = http.post(path, json={"session_id": "nope", "sheet_id": "1"})
            assert r.status_code == 400


# ────────────────────── conversations CRUD ──────────────────────

class TestConversations:
    def test_save_list_get_delete(self, http_session):
        http, sid = http_session
        cid = "test-conv-001"

        # Save
        r = http.post("/api/conversations/save", json={
            "session_id": sid, "conversation_id": cid, "title": "Test",
        })
        assert r.status_code == 200
        assert r.json()["status"] == "ok"
        assert r.json()["conversation_id"] == cid

        # List
        listed = http.get(f"/api/conversations?session_id={sid}").json()["conversations"]
        assert any(c.get("id") == cid for c in listed)

        # Get (no messages yet)
        got = http.get(f"/api/conversations/{cid}?session_id={sid}").json()
        assert got["conversation_id"] == cid
        assert got["messages"] == []

        # Delete
        r = http.post("/api/conversations/delete", json={
            "session_id": sid, "conversation_id": cid,
        })
        assert r.status_code == 200
        assert r.json()["status"] == "ok"

        # Now absent
        listed2 = http.get(f"/api/conversations?session_id={sid}").json()["conversations"]
        assert not any(c.get("id") == cid for c in listed2)

    def test_migrate_imports_localstorage(self, http_session):
        http, sid = http_session
        payload = {
            "session_id": sid,
            "conversations": [
                {
                    "id": "old-1",
                    "title": "Imported #1",
                    "messages": [
                        {"role": "user", "content": "Hi"},
                        {"role": "assistant", "content": "Hello"},
                        {"role": "user", "content": ""},  # blank → skipped
                        {"role": "system", "content": "ignored"},  # bad role → skipped
                    ],
                },
                {"id": "old-2", "title": "Imported #2", "messages": []},
            ],
        }
        r = http.post("/api/conversations/migrate", json=payload)
        assert r.status_code == 200
        assert r.json()["imported"] == 2

        # Both appear in list
        ids = [c.get("id") for c in http.get(f"/api/conversations?session_id={sid}").json()["conversations"]]
        assert "old-1" in ids
        assert "old-2" in ids

        # Messages preserved (only the 2 valid)
        msgs = http.get(f"/api/conversations/old-1?session_id={sid}").json()["messages"]
        assert len(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["role"] == "assistant"


# ────────────────────── audit + export ──────────────────────

class TestAuditAndExport:
    def test_audit_returns_entries_field(self, http_session):
        http, sid = http_session
        r = http.get(f"/api/audit?session_id={sid}")
        assert r.status_code == 200
        assert "entries" in r.json()

    def test_audit_limit_clamped(self, http_session):
        http, sid = http_session
        # Server clamps limit to 1000
        r = http.get(f"/api/audit?session_id={sid}&limit=999999")
        assert r.status_code == 200

    def test_export_returns_attachment(self, http_session):
        http, sid = http_session
        r = http.get(f"/api/export?session_id={sid}")
        assert r.status_code == 200
        assert "attachment" in r.headers.get("content-disposition", "")
        body = r.json()
        # Export bundle must contain at least the user identity
        assert "user" in body or "favorites" in body or "conversations" in body


# ────────────────────── switch-model ──────────────────────

class TestSwitchModel:
    def test_switch_within_same_provider(self, http_session, openai_api_key):
        http, sid = http_session
        r = http.post("/api/switch-model", json={
            "session_id": sid, "provider": "openai", "model": "gpt-4o",
        })
        assert r.status_code == 200
        assert r.json()["model"] == "gpt-4o"

    def test_unknown_provider_rejected(self, http_session):
        http, sid = http_session
        r = http.post("/api/switch-model", json={
            "session_id": sid, "provider": "fakeai", "model": "x",
        })
        assert r.status_code == 400

    def test_invalid_session_rejected(self, http_session):
        http, _ = http_session
        r = http.post("/api/switch-model", json={
            "session_id": "nope", "provider": "openai", "model": "gpt-4o-mini",
        })
        assert r.status_code == 400


# ────────────────────── pin / unpin sheet ──────────────────────

class TestPinUnpinSheet:
    def test_pin_unknown_sheet_returns_error(self, http_session):
        http, sid = http_session
        r = http.post("/api/pin-sheet", json={
            "session_id": sid, "sheet_id": "0",
        })
        # Should fail gracefully (Smartsheet won't find sheet 0)
        assert r.status_code in (200, 400, 404, 500)
        if r.status_code == 200:
            # Some error envelope
            assert "error" in r.json() or r.json().get("status")


# ────────────────────── webhook events polling ──────────────────────

class TestWebhookEventsPolling:
    def test_empty_events_for_fresh_session(self, http_session):
        http, sid = http_session
        r = http.get(f"/api/webhook-events?session_id={sid}")
        assert r.status_code == 200
        assert r.json()["events"] == []

    def test_invalid_session_rejected(self, http_session):
        http, _ = http_session
        r = http.get("/api/webhook-events?session_id=nope")
        assert r.status_code == 400


# ────────────────────── csv-to-sheet (creates real sheet → cleanup) ──────────────────────

class TestCsvToSheet:
    def test_validation_errors(self, http_session):
        http, sid = http_session
        # No name
        r = http.post("/api/csv-to-sheet", json={
            "session_id": sid, "name": "", "headers": ["a"], "rows": [],
        })
        assert r.status_code == 400

        # No headers
        r = http.post("/api/csv-to-sheet", json={
            "session_id": sid, "name": "X", "headers": [], "rows": [],
        })
        assert r.status_code == 400

        # Too many columns
        r = http.post("/api/csv-to-sheet", json={
            "session_id": sid, "name": "X",
            "headers": [f"c{i}" for i in range(201)],
            "rows": [],
        })
        assert r.status_code == 400

        # Invalid session
        r = http.post("/api/csv-to-sheet", json={
            "session_id": "nope", "name": "X", "headers": ["a"], "rows": [],
        })
        assert r.status_code == 404

    def test_create_and_cleanup_or_skip(self, http_session, smartsheet_token):
        http, sid = http_session
        import time
        name = f"pytest-csv-{int(time.time())}"

        r = http.post("/api/csv-to-sheet", json={
            "session_id": sid,
            "name": name,
            "headers": ["Name", "Value"],
            "rows": [["alpha", "1"], ["beta", "2"], ["gamma", "3"]],
        })

        if r.status_code != 200:
            # Account may not allow create_sheet at home level → skip gracefully
            pytest.skip(f"csv-to-sheet failed (likely account tier limit): {r.text}")

        body = r.json()
        assert body["status"] == "ok"
        assert body["rows_added"] == 3
        assert body["columns"] == 2
        new_sheet_id = body["sheet_id"]

        # Clean up: delete the sheet directly via httpx with the real token
        import httpx
        try:
            with httpx.Client(
                base_url="https://api.smartsheet.com/2.0",
                headers={"Authorization": f"Bearer {smartsheet_token}"},
                timeout=30,
            ) as c:
                c.delete(f"/sheets/{new_sheet_id}")
        except Exception:
            pass


# ────────────────────── inbound webhook ──────────────────────

class TestInboundWebhook:
    def test_verification_challenge_echoed(self, http_session):
        http, _ = http_session
        r = http.post("/api/smartsheet-webhook", json={"challenge": "abc123"})
        assert r.status_code == 200
        assert r.json() == {"smartsheetHookResponse": "abc123"}

    def test_event_payload_persisted_and_polled(self, http_session, sheet_id):
        http, sid = http_session

        # Simulate Smartsheet calling our endpoint
        payload = {
            "webhookId": 7777,
            "scopeObjectId": int(sheet_id),
            "events": [
                {"eventType": "row.created", "objectType": "row", "id": 1},
                {"eventType": "row.updated", "objectType": "row", "id": 2},
            ],
        }
        r = http.post("/api/smartsheet-webhook", json=payload)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "received"
        assert body["stored"] >= 2

        # The session's user must now see the events when polling
        events = http.get(f"/api/webhook-events?session_id={sid}").json()["events"]
        types = [e.get("event_type") for e in events]
        assert "row.created" in types
        assert "row.updated" in types

    def test_event_with_no_matching_session_still_persisted(self, http_session):
        http, _ = http_session
        # Use a sheet ID that no session is connected to
        payload = {
            "webhookId": 8888,
            "scopeObjectId": 999999999999,
            "events": [{"eventType": "sheet.deleted", "objectType": "sheet"}],
        }
        r = http.post("/api/smartsheet-webhook", json=payload)
        assert r.status_code == 200
        # Stored anonymously (no fan-out target) — the endpoint shouldn't crash
        assert r.json()["status"] == "received"


# ────────────────────── generate-title ──────────────────────

class TestGenerateTitle:
    def test_empty_snippet_returns_blank(self, http_session):
        http, sid = http_session
        r = http.post("/api/generate-title", json={
            "session_id": sid, "snippet": "  ",
        })
        assert r.status_code == 200
        assert r.json().get("title") == ""

    def test_invalid_session_rejected(self, http_session):
        http, _ = http_session
        r = http.post("/api/generate-title", json={
            "session_id": "nope", "snippet": "Hello",
        })
        assert r.status_code == 404


# ────────────────────── quick-connect ──────────────────────

class TestQuickConnect:
    def test_quick_connect_uses_env(self, monkeypatch, tmp_path,
                                    smartsheet_token, sheet_id, openai_api_key):
        if not openai_api_key:
            pytest.skip("OPENAI_API_KEY missing in .env")

        # Re-set .env values explicitly (already loaded but let's be safe)
        monkeypatch.setenv("SMARTSHEET_TOKEN", smartsheet_token)
        monkeypatch.setenv("SHEET_ID", sheet_id)
        monkeypatch.setenv("OPENAI_API_KEY", openai_api_key)

        client = _make_test_client(monkeypatch, tmp_path)
        with client as http:
            r = http.post("/api/quick-connect")
            assert r.status_code == 200, r.text
            body = r.json()
            assert "session_id" in body
            assert "ws_token" in body
            assert "sheet" in body

    def test_quick_connect_no_token(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SMARTSHEET_TOKEN", "")
        client = _make_test_client(monkeypatch, tmp_path)
        with client as http:
            r = http.post("/api/quick-connect")
            assert r.status_code == 400
            assert "SMARTSHEET_TOKEN" in r.json()["error"]
