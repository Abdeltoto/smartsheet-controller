"""Unit tests for optional Smartsheet OAuth helper routes (isolated app)."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.oauth_smartsheet import router as oauth_router


def _client(monkeypatch, *, with_creds: bool) -> TestClient:
    if with_creds:
        monkeypatch.setenv("SMARTSHEET_OAUTH_CLIENT_ID", "test_client_id")
        monkeypatch.setenv("SMARTSHEET_OAUTH_CLIENT_SECRET", "test_secret")
    else:
        monkeypatch.delenv("SMARTSHEET_OAUTH_CLIENT_ID", raising=False)
        monkeypatch.delenv("SMARTSHEET_OAUTH_CLIENT_SECRET", raising=False)
    app = FastAPI()
    app.include_router(oauth_router)
    return TestClient(app)


def test_oauth_config_disabled(monkeypatch):
    c = _client(monkeypatch, with_creds=False)
    r = c.get("/api/oauth/smartsheet/config")
    assert r.status_code == 200
    assert r.json() == {"enabled": False}


def test_oauth_config_enabled(monkeypatch):
    c = _client(monkeypatch, with_creds=True)
    r = c.get("/api/oauth/smartsheet/config")
    assert r.status_code == 200
    j = r.json()
    assert j["enabled"] is True
    assert j["client_id"] == "test_client_id"


def test_exchange_not_configured_returns_503(monkeypatch):
    c = _client(monkeypatch, with_creds=False)
    r = c.post(
        "/api/oauth/smartsheet/exchange",
        json={"code": "abc123", "redirect_uri": "https://ext.chromiumapp.org/"},
    )
    assert r.status_code == 503


def test_exchange_success(monkeypatch):
    fake_resp = AsyncMock()
    fake_resp.status_code = 200
    fake_resp.json = lambda: {
        "access_token": "tok_123",
        "token_type": "bearer",
        "expires_in": 3600,
        "refresh_token": "ref_1",
    }
    fake_resp.text = ""

    c = _client(monkeypatch, with_creds=True)
    with patch("backend.oauth_smartsheet.httpx.AsyncClient") as client_cls:
        inst = client_cls.return_value.__aenter__.return_value
        inst.post = AsyncMock(return_value=fake_resp)
        r = c.post(
            "/api/oauth/smartsheet/exchange",
            json={"code": "authcode", "redirect_uri": "https://abc.chromiumapp.org/"},
        )
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"] == "tok_123"
    assert body["refresh_token"] == "ref_1"
