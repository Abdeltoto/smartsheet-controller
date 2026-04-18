"""Contract tests for tool dispatch.

Goals:
1. Every name in TOOL_DEFINITIONS resolves in `_dispatch` (no typos).
2. Every TOOL_DEFINITIONS entry has a valid OpenAI-style schema:
   - `name` (str), `description` (str), `parameters.type == "object"`
   - All `required` fields appear in `properties`
   - Every `properties[*]` has a `type`
3. There are no silent name collisions.
4. No tool name accidentally collides with the "Unknown tool" sentinel.

These tests catch the most common regression: someone adds a tool to
`TOOL_DEFINITIONS` and forgets to wire it in `_dispatch`, or vice-versa.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from backend.tools import TOOL_DEFINITIONS, execute_tool

pytestmark = [pytest.mark.unit]


# ────────────────────── schema validity ──────────────────────

class TestSchemaValidity:
    def test_all_tools_have_required_fields(self):
        for tool in TOOL_DEFINITIONS:
            assert "name" in tool and isinstance(tool["name"], str) and tool["name"]
            assert "description" in tool and isinstance(tool["description"], str)
            assert tool["description"], f"{tool['name']} has empty description"
            assert "parameters" in tool

    def test_all_parameters_are_object_schemas(self):
        for tool in TOOL_DEFINITIONS:
            params = tool["parameters"]
            assert params.get("type") == "object", f"{tool['name']}: parameters.type must be 'object'"
            assert isinstance(params.get("properties", {}), dict)
            assert isinstance(params.get("required", []), list)

    def test_required_fields_exist_in_properties(self):
        for tool in TOOL_DEFINITIONS:
            props = tool["parameters"].get("properties", {})
            required = tool["parameters"].get("required", [])
            missing = [r for r in required if r not in props]
            assert not missing, f"{tool['name']}: required fields {missing} missing from properties"

    def test_every_property_has_a_type(self):
        for tool in TOOL_DEFINITIONS:
            for pname, pspec in tool["parameters"].get("properties", {}).items():
                assert isinstance(pspec, dict), f"{tool['name']}.{pname} must be dict"
                # Allow oneOf/anyOf/enum-only as alternatives but `type` is the norm
                has_shape = "type" in pspec or "anyOf" in pspec or "oneOf" in pspec or "enum" in pspec
                assert has_shape, f"{tool['name']}.{pname} has no type/anyOf/oneOf/enum"

    def test_no_duplicate_tool_names(self):
        names = [t["name"] for t in TOOL_DEFINITIONS]
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f"Duplicate tool names: {dupes}"


# ────────────────────── dispatch contract ──────────────────────

# Plausible default values per JSON-schema type, used to fabricate args
# that satisfy `required` fields of every tool.
def _sample_for(prop_spec: dict):
    t = prop_spec.get("type")
    if t == "string":
        return "x"
    if t == "integer":
        return 1
    if t == "number":
        return 1.0
    if t == "boolean":
        return False
    if t == "array":
        items = prop_spec.get("items", {}).get("type", "string")
        return [_sample_for({"type": items})]
    if t == "object":
        return {}
    return "x"


def _build_required_args(tool: dict) -> dict:
    props = tool["parameters"].get("properties", {})
    required = tool["parameters"].get("required", [])
    return {r: _sample_for(props.get(r, {})) for r in required}


class _AsyncAutoMock:
    """A stand-in for SmartsheetClient: every method access yields an
    AsyncMock returning {"ok": True, "_method": name}. Used so _dispatch
    can invoke any client method without a real SDK or HTTP."""

    def __init__(self):
        self._cache = {}

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._cache:
            self._cache[name] = AsyncMock(return_value={"ok": True, "_method": name})
        return self._cache[name]


@pytest.fixture
def dummy_client():
    return _AsyncAutoMock()


class TestDispatchContract:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("tool", TOOL_DEFINITIONS, ids=lambda t: t["name"])
    async def test_every_tool_dispatches_without_unknown(self, tool, dummy_client, monkeypatch):
        # The image and chart tools have local synchronous handlers; allow them too.
        # Skip generate_image: it would try to call OpenAI even with a fake client.
        if tool["name"] == "generate_image":
            monkeypatch.delenv("OPENAI_API_KEY", raising=False)
            args = _build_required_args(tool)
            args.setdefault("prompt", "test")
            result = await execute_tool(dummy_client, tool["name"], args)
            # Without API key, execute_tool returns json with an "error" key.
            assert "error" in result.lower() or "OPENAI" in result, \
                f"generate_image without API key should return an error string, got: {result[:200]}"
            return

        if tool["name"] == "generate_chart":
            args = _build_required_args(tool)
            args.update({"chart_type": "bar", "title": "T",
                         "labels": ["a", "b"], "datasets": [{"data": [1, 2]}]})
            result = await execute_tool(dummy_client, tool["name"], args)
            assert "__is_chart__" in result
            return

        args = _build_required_args(tool)
        result = await execute_tool(dummy_client, tool["name"], args)
        # execute_tool returns a JSON string; an unknown tool would yield {"error":"Unknown tool: ..."}
        assert "Unknown tool" not in result, \
            f"Tool '{tool['name']}' is in TOOL_DEFINITIONS but not wired in _dispatch (got: {result[:200]})"

    @pytest.mark.asyncio
    async def test_truly_unknown_tool_returns_error(self, dummy_client):
        result = await execute_tool(dummy_client, "this_does_not_exist_xyz", {})
        assert "Unknown tool" in result


# ────────────────────── intent map sanity ──────────────────────

class TestIntentMapConsistency:
    def test_every_intent_tool_is_a_real_tool(self):
        from backend.tools import _TOOLS_BY_INTENT
        valid_names = {t["name"] for t in TOOL_DEFINITIONS}
        for intent, names in _TOOLS_BY_INTENT.items():
            unknown = [n for n in names if n not in valid_names]
            assert not unknown, f"Intent '{intent}' references unknown tools: {unknown}"
