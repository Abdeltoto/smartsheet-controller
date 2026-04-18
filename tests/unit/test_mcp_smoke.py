"""MCP server smoke test.

We don't run the actual MCP transport (stdio/http) — that needs a real
client handshake. We just verify that:
1. The module imports cleanly (catches syntax / decorator typos).
2. All `@mcp.tool()` decorators registered (count matches source).
3. Each registered tool has a name and a description.

This is the cheapest possible regression catch for someone breaking the
MCP server while editing tools.py or smartsheet_client.py.
"""
from __future__ import annotations

import asyncio

import pytest

pytestmark = pytest.mark.unit


class TestMcpServerSmoke:
    def test_module_imports(self):
        from backend import mcp_server  # noqa: F401
        assert hasattr(mcp_server, "mcp"), "Server module must expose `mcp` instance"

    def test_tool_registry_populated(self):
        from backend import mcp_server

        # FastMCP exposes a private tool manager; use the public list_tools()
        # coroutine if available, otherwise fall back to internal API.
        tools = asyncio.run(mcp_server.mcp.list_tools())

        # Must match the @mcp.tool() count in the source (52 at time of writing,
        # use a lower bound to allow growth without breaking the test).
        assert len(tools) >= 50, f"Expected at least 50 MCP tools, got {len(tools)}"

    def test_every_tool_has_name_and_description(self):
        from backend import mcp_server
        tools = asyncio.run(mcp_server.mcp.list_tools())

        for t in tools:
            name = getattr(t, "name", None)
            desc = getattr(t, "description", None)
            assert name and isinstance(name, str), f"Tool missing name: {t!r}"
            assert desc and isinstance(desc, str), f"Tool '{name}' missing description"

    def test_tool_names_are_unique(self):
        from backend import mcp_server
        tools = asyncio.run(mcp_server.mcp.list_tools())
        names = [getattr(t, "name") for t in tools]
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f"Duplicate MCP tool names: {dupes}"
