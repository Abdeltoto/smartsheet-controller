"""Unit tests for backend.llm_router (no real API calls)."""
import pytest

from backend.llm_router import (
    PROVIDERS,
    LLMRouter,
    _safe_parse_args,
    get_provider_info,
)

pytestmark = pytest.mark.unit


class TestSafeParseArgs:
    def test_empty_returns_empty_dict(self):
        assert _safe_parse_args(None) == {}
        assert _safe_parse_args("") == {}

    def test_valid_json(self):
        assert _safe_parse_args('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}

    def test_invalid_json_returns_error_marker(self):
        out = _safe_parse_args("not json {{")
        assert "__parse_error__" in out
        assert "__raw__" in out
        assert out["__raw__"].startswith("not json")

    def test_truncates_huge_invalid_payload(self):
        huge = "x" * 5000
        out = _safe_parse_args(huge)
        assert len(out["__raw__"]) <= 500


class TestProviderInfo:
    def test_all_providers_have_required_fields(self):
        info = get_provider_info()
        assert set(info.keys()) == set(PROVIDERS.keys())
        for name, p in info.items():
            assert "default_model" in p
            assert "models" in p and isinstance(p["models"], list) and p["models"]
            assert "env_key" in p
            # Default model should be in the listed models
            assert p["default_model"] in p["models"], f"{name} default not in models"


class TestLLMRouter:
    def test_unsupported_provider_raises(self):
        with pytest.raises(ValueError, match="Unsupported provider"):
            LLMRouter("not-a-provider", "x", "fake-key")

    def test_default_model_when_blank(self):
        r = LLMRouter("openai", "", "fake-key")
        assert r.model == PROVIDERS["openai"]["default_model"]

    def test_switch_model(self):
        r = LLMRouter("openai", "gpt-4o", "fake-key")
        r.switch_model("gpt-4o-mini")
        assert r.model == "gpt-4o-mini"

    def test_record_usage_accumulates(self):
        r = LLMRouter("openai", "gpt-4o-mini", "fake-key")
        r._record_usage(100, 50)
        r._record_usage(200, 75)
        assert r.usage["input_tokens"] == 300
        assert r.usage["output_tokens"] == 125
        assert r.usage["calls"] == 2
        assert r.usage["by_model"]["gpt-4o-mini"]["input"] == 300
        assert r.usage["by_model"]["gpt-4o-mini"]["calls"] == 2
        assert r.usage["last_call"]["model"] == "gpt-4o-mini"

    def test_record_usage_per_model(self):
        r = LLMRouter("openai", "gpt-4o-mini", "fake-key")
        r._record_usage(10, 5)
        r.switch_model("gpt-4o")
        r._record_usage(20, 10)
        assert r.usage["by_model"]["gpt-4o-mini"]["calls"] == 1
        assert r.usage["by_model"]["gpt-4o"]["calls"] == 1
        assert r.usage["calls"] == 2

    def test_anthropic_uses_anthropic_client(self):
        r = LLMRouter("anthropic", "claude-sonnet-4-20250514", "fake-key")
        assert hasattr(r, "anthropic_client")
        assert not hasattr(r, "openai_client")

    def test_openai_compat_uses_openai_client(self):
        r = LLMRouter("groq", "llama-3.3-70b-versatile", "fake-key")
        assert hasattr(r, "openai_client")
        assert not hasattr(r, "anthropic_client")
