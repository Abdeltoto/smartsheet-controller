"""Session secret validation for in-memory sessions."""
from __future__ import annotations

import pytest
from fastapi import HTTPException


@pytest.fixture
def session_store(monkeypatch):
    from backend.core import state

    store = {
        "abc123": {
            "ws_token": "ws-secret-token",
            "auth_cookie": "cookie-secret",
            "last_activity": 0.0,
        }
    }
    monkeypatch.setattr(state, "sessions", store)
    return store


class TestResolveSession:
    def test_valid_ws_token(self, session_store):
        from backend.app import _resolve_session

        session = _resolve_session("abc123", ws_token="ws-secret-token")
        assert session is session_store["abc123"]

    def test_valid_auth_cookie(self, session_store):
        from backend.app import _resolve_session

        session = _resolve_session("abc123", auth_cookie="cookie-secret")
        assert session is session_store["abc123"]

    def test_missing_session(self, session_store):
        from backend.app import _resolve_session

        with pytest.raises(HTTPException) as exc:
            _resolve_session("missing")
        assert exc.value.status_code == 404

    def test_require_secret_without_credentials(self, session_store):
        from backend.app import _resolve_session

        with pytest.raises(HTTPException) as exc:
            _resolve_session("abc123", require_secret=True)
        assert exc.value.status_code == 403

    def test_wrong_ws_token_rejected(self, session_store):
        from backend.app import _resolve_session

        with pytest.raises(HTTPException) as exc:
            _resolve_session("abc123", ws_token="wrong")
        assert exc.value.status_code == 403

    def test_optional_routes_allow_missing_secret(self, session_store):
        from backend.app import _resolve_session

        session = _resolve_session("abc123")
        assert session is session_store["abc123"]
