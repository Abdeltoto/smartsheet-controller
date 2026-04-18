"""Integration tests for the FastAPI HTTP layer (no WebSocket here).

Hits the real /health, /api/env-status, /api/providers and /api/validate-token
endpoints. Uses the real Smartsheet token from .env for the validation step.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.app import app

pytestmark = pytest.mark.integration


@pytest.fixture
def http():
    """TestClient drives FastAPI lifespan (startup + shutdown) automatically
    when used as a context manager."""
    with TestClient(app) as client:
        yield client


# ────────────────────── public endpoints ──────────────────────

class TestHealth:
    def test_health_ok(self, http):
        r = http.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert "uptime_seconds" in body
        assert "sessions" in body and "watchers" in body


class TestProviders:
    def test_provider_catalog(self, http):
        r = http.get("/api/providers")
        assert r.status_code == 200
        body = r.json()
        # All known providers should be advertised, each with a default model
        for p in ("openai", "anthropic", "google", "groq", "mistral", "deepseek", "openrouter"):
            assert p in body
            assert body[p]["default_model"]
            assert body[p]["env_key"]


class TestEnvStatus:
    def test_reflects_dot_env(self, http):
        r = http.get("/api/env-status")
        assert r.status_code == 200
        body = r.json()
        assert body["has_smartsheet_token"] is True
        assert body["has_sheet_id"] is True
        assert body["sheet_id"]  # non-empty
        # OpenAI key is set in .env, so we should detect at least one provider
        assert body["has_llm_key"] is True
        assert "openai" in body["available_providers"]


# ────────────────────── token validation ──────────────────────

class TestValidateToken:
    def test_too_short_rejected(self, http):
        r = http.post("/api/validate-token", json={"smartsheet_token": "short"})
        assert r.status_code == 400
        assert "error" in r.json()

    def test_real_token_succeeds(self, http, smartsheet_token):
        r = http.post("/api/validate-token", json={"smartsheet_token": smartsheet_token})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["user"]["email"]
        assert isinstance(body["sheets"], list)
        assert isinstance(body["available_providers"], dict)


# ────────────────────── session lifecycle ──────────────────────

class TestSessionEndpoints:
    def test_session_validation_errors(self, http):
        # Missing token
        r = http.post("/api/session", json={
            "smartsheet_token": "",
            "sheet_id": "1234567890",
            "llm_provider": "openai",
        })
        assert r.status_code == 400

        # Non-numeric sheet_id
        r = http.post("/api/session", json={
            "smartsheet_token": "x" * 32,
            "sheet_id": "not-a-number",
            "llm_provider": "openai",
            "llm_api_key": "fake-key",
        })
        assert r.status_code == 400

        # Unknown provider, no api_key in env
        r = http.post("/api/session", json={
            "smartsheet_token": "x" * 32,
            "sheet_id": "1234567890",
            "llm_provider": "this-provider-does-not-exist",
            "llm_api_key": "",
        })
        assert r.status_code == 400
