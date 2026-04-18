"""Unit tests for FastAPI helper functions.

Covers `_friendly_error`, `_detect_available_providers`, and the
process-level constants used by `/health` and `/api/env-status`.
"""
from __future__ import annotations

import pytest

from backend.app import _detect_available_providers, _friendly_error
from backend.smartsheet_client import SmartsheetRateLimitError

pytestmark = [pytest.mark.unit]


class TestFriendlyError:
    def test_smartsheet_rate_limit_passes_through(self):
        exc = SmartsheetRateLimitError("Wait 30 seconds.")
        assert _friendly_error(exc) == "Wait 30 seconds."

    def test_401_maps_to_auth_message(self):
        out = _friendly_error(Exception("HTTP 401 Unauthorized"))
        assert "authentication" in out.lower()

    def test_403_maps_to_permission_message(self):
        out = _friendly_error(Exception("HTTP 403 Forbidden"))
        assert "access denied" in out.lower()

    def test_404_maps_to_not_found_message(self):
        out = _friendly_error(Exception("HTTP 404 Not Found"))
        assert "not found" in out.lower()

    def test_timeout_maps_to_timeout_message(self):
        out = _friendly_error(Exception("Connection timeout after 30s"))
        assert "timed out" in out.lower()

    def test_quota_maps_to_billing_message(self):
        out = _friendly_error(Exception("OpenAI quota exhausted"))
        assert "quota" in out.lower() and "billing" in out.lower()

    def test_rate_limit_text_maps(self):
        out = _friendly_error(Exception("LLM rate limit"))
        assert "rate limit" in out.lower()

    def test_unknown_falls_back_to_generic(self):
        out = _friendly_error(Exception("Some weird thing"))
        assert "unexpected" in out.lower()

    def test_no_stacktrace_or_internal_details_leaked(self):
        # Even if exception text contains a path or token, the helper should
        # never include it in the friendly fallback.
        secret = "Bearer SECRET_TOKEN_LEAKED_xyz"
        out = _friendly_error(Exception(secret))
        assert "SECRET_TOKEN_LEAKED_xyz" not in out, "Friendly errors must not leak secrets"


class TestDetectAvailableProviders:
    def test_no_keys_returns_empty(self, monkeypatch):
        # Wipe every provider key
        from backend.llm_router import PROVIDERS
        for info in PROVIDERS.values():
            monkeypatch.delenv(info["env_key"], raising=False)
        assert _detect_available_providers() == {}

    def test_only_set_keys_appear(self, monkeypatch):
        from backend.llm_router import PROVIDERS
        for info in PROVIDERS.values():
            monkeypatch.delenv(info["env_key"], raising=False)
        # Pick the first provider and set its key
        first_name, first_info = next(iter(PROVIDERS.items()))
        monkeypatch.setenv(first_info["env_key"], "fake-key")
        result = _detect_available_providers()
        assert first_name in result
        assert "default_model" in result[first_name]
        assert "models" in result[first_name]
        # Other providers must NOT appear
        for other in PROVIDERS:
            if other != first_name:
                assert other not in result

    def test_blank_keys_treated_as_unset(self, monkeypatch):
        from backend.llm_router import PROVIDERS
        for info in PROVIDERS.values():
            monkeypatch.setenv(info["env_key"], "   ")  # whitespace only
        assert _detect_available_providers() == {}
