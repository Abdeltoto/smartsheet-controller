"""Unit tests for the Agent.run() tool-calling loop.

Stubs `LLMRouter.chat_stream` to script exact model behaviour, and stubs
`backend.tools.execute_tool` so we don't talk to Smartsheet. This is the
single most important code path in the project — it's where confirmation,
parse-error recovery, and round limits live.
"""
from __future__ import annotations

import json
from typing import AsyncIterator

import pytest

from backend.agent import MAX_TOOL_ROUNDS, Agent, _new_metrics

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


# ────────────────────── helpers ──────────────────────

def _make_agent() -> Agent:
    """Bypass __init__ — we don't need real LLM/Smartsheet objects, the
    chat_stream and execute_tool functions will be monkey-patched per test."""
    a = Agent.__new__(Agent)
    a.llm = None  # filled by tests via _StubLLM
    a.smartsheet = object()  # placeholder; execute_tool is patched
    a.sheet_id = "1"
    a.sheet_context = {"summary": {"name": "S", "totalRowCount": 0, "columnCount": 0, "columns": []}}
    a.pinned_sheets = []
    a.metrics = _new_metrics()
    return a


class _StubLLM:
    """Scripts a sequence of chat_stream responses, one per agent round."""

    def __init__(self, scripted_rounds: list[list[dict]]):
        # Each "round" is the list of chunks chat_stream yields for that round.
        self._rounds = scripted_rounds
        self._idx = 0
        self.model = "test-model"
        self.provider = "test"

    async def chat_stream(self, messages, tools=None, system="") -> AsyncIterator[dict]:
        if self._idx >= len(self._rounds):
            # Default tail: respond with a closing message.
            yield {"type": "stream_end", "content": "done"}
            return
        chunks = self._rounds[self._idx]
        self._idx += 1
        for c in chunks:
            yield c


def _events_collector():
    events: list[dict] = []

    async def on_event(e):
        events.append(e)

    return events, on_event


# ────────────────────── plain text response (no tools) ──────────────────────

class TestNoToolCall:
    async def test_streams_then_emits_stream_end(self):
        a = _make_agent()
        a.llm = _StubLLM([[
            {"type": "stream_delta", "content": "Hello"},
            {"type": "stream_end", "content": "Hello\n[SUGGESTIONS] X | Y"},
        ]])
        events, on_event = _events_collector()
        result = await a.run([{"role": "user", "content": "hi"}], on_event=on_event)
        assert result == "Hello"
        # Suggestions extracted
        end = next(e for e in events if e["type"] == "stream_end")
        assert end["suggestions"] == ["X", "Y"]
        assert "[SUGGESTIONS]" not in end["content"]


# ────────────────────── single tool call → second round closes ──────────────────────

class TestSingleToolCall:
    async def test_dispatches_then_continues(self, monkeypatch):
        # Round 1: model calls a non-destructive tool. Round 2: closes.
        round1 = [{
            "type": "tool_calls",
            "tool_calls": [{
                "id": "call-1",
                "name": "list_sheets",
                "arguments": {},
            }],
            "raw_message": {"role": "assistant", "tool_calls": [{"id": "call-1"}]},
        }]
        round2 = [{"type": "stream_end", "content": "I found 3 sheets."}]
        a = _make_agent()
        a.llm = _StubLLM([round1, round2])

        async def fake_execute(client, name, args):
            assert name == "list_sheets"
            return json.dumps([{"id": 1, "name": "Demo"}])

        monkeypatch.setattr("backend.agent.execute_tool", fake_execute)

        events, on_event = _events_collector()
        msgs = [{"role": "user", "content": "list my sheets"}]
        result = await a.run(msgs, on_event=on_event)

        assert result == "I found 3 sheets."

        # Agent must emit tool_call + tool_result events
        types = [e["type"] for e in events]
        assert "tool_call" in types
        assert "tool_result" in types

        # Conversation messages now include the assistant tool_call + tool_result
        roles = [m["role"] for m in msgs]
        assert "tool_result" in roles


# ────────────────────── destructive tool requires confirmation ──────────────────────

class TestDestructiveConfirmation:
    async def test_approval_executes_tool(self, monkeypatch):
        round1 = [{
            "type": "tool_calls",
            "tool_calls": [{"id": "c1", "name": "delete_rows",
                             "arguments": {"sheet_id": "1", "row_ids": [99]}}],
            "raw_message": {"role": "assistant", "tool_calls": [{"id": "c1"}]},
        }]
        round2 = [{"type": "stream_end", "content": "Deleted."}]
        a = _make_agent()
        a.llm = _StubLLM([round1, round2])

        executed = {"called": False}

        async def fake_execute(client, name, args):
            executed["called"] = True
            return json.dumps({"message": "SUCCESS"})

        monkeypatch.setattr("backend.agent.execute_tool", fake_execute)

        async def confirm(name, args, tcid):
            assert name == "delete_rows"
            assert tcid == "c1"
            return True

        events, on_event = _events_collector()
        await a.run([{"role": "user", "content": "delete row 99"}],
                    on_event=on_event, confirm_callback=confirm)
        assert executed["called"] is True

    async def test_rejection_skips_tool_and_records_rejection(self, monkeypatch):
        round1 = [{
            "type": "tool_calls",
            "tool_calls": [{"id": "c1", "name": "delete_rows",
                             "arguments": {"sheet_id": "1", "row_ids": [99]}}],
            "raw_message": {"role": "assistant", "tool_calls": [{"id": "c1"}]},
        }]
        round2 = [{"type": "stream_end", "content": "Cancelled."}]
        a = _make_agent()
        a.llm = _StubLLM([round1, round2])

        called = {"n": 0}

        async def fake_execute(client, name, args):
            called["n"] += 1
            return "should not happen"

        monkeypatch.setattr("backend.agent.execute_tool", fake_execute)

        async def reject(name, args, tcid):
            return False

        events, on_event = _events_collector()
        msgs = [{"role": "user", "content": "delete row 99"}]
        await a.run(msgs, on_event=on_event, confirm_callback=reject)

        assert called["n"] == 0  # tool never executed
        # The tool_result message should record the rejection so the model
        # has feedback for the next round.
        rejected_results = [m for m in msgs
                            if m.get("role") == "tool_result"
                            and "rejected" in m.get("content", "").lower()]
        assert rejected_results, "Rejection must be persisted as tool_result"


# ────────────────────── invalid JSON arguments recovery ──────────────────────

class TestInvalidJsonArgs:
    async def test_parse_error_creates_correction_tool_result(self, monkeypatch):
        round1 = [{
            "type": "tool_calls",
            "tool_calls": [{
                "id": "c1", "name": "add_rows",
                "arguments": {"__parse_error__": "Unexpected token", "__raw__": "{not json"},
            }],
            "raw_message": {"role": "assistant", "tool_calls": [{"id": "c1"}]},
        }]
        round2 = [{"type": "stream_end", "content": "Sorry, retried."}]
        a = _make_agent()
        a.llm = _StubLLM([round1, round2])

        called = {"n": 0}

        async def fake_execute(client, name, args):
            called["n"] += 1
            return ""

        monkeypatch.setattr("backend.agent.execute_tool", fake_execute)

        events, on_event = _events_collector()
        msgs = [{"role": "user", "content": "add a row"}]
        await a.run(msgs, on_event=on_event)

        # Tool was NOT executed (parse error short-circuits)
        assert called["n"] == 0
        # An INVALID_JSON tool_result was injected
        recovery = [m for m in msgs
                    if m.get("role") == "tool_result"
                    and "INVALID_JSON" in m.get("content", "")]
        assert recovery, "Parse error must be surfaced back to the model as tool_result"


# ────────────────────── image / chart event surfacing ──────────────────────

class TestSpecialResultEvents:
    async def test_image_result_emits_image_event(self, monkeypatch):
        round1 = [{
            "type": "tool_calls",
            "tool_calls": [{"id": "c1", "name": "generate_image",
                             "arguments": {"prompt": "logo"}}],
            "raw_message": {"role": "assistant", "tool_calls": [{"id": "c1"}]},
        }]
        round2 = [{"type": "stream_end", "content": "Here's the logo."}]
        a = _make_agent()
        a.llm = _StubLLM([round1, round2])

        async def fake_execute(client, name, args):
            return json.dumps({
                "__is_image__": True,
                "image_url": "https://example.com/x.png",
                "revised_prompt": "a beautiful logo",
            })

        monkeypatch.setattr("backend.agent.execute_tool", fake_execute)

        events, on_event = _events_collector()
        await a.run([{"role": "user", "content": "make a logo"}], on_event=on_event)

        img = next((e for e in events if e["type"] == "image"), None)
        assert img is not None
        assert img["url"] == "https://example.com/x.png"
        assert "logo" in img["caption"]

    async def test_chart_result_emits_chart_event(self, monkeypatch):
        round1 = [{
            "type": "tool_calls",
            "tool_calls": [{"id": "c1", "name": "generate_chart",
                             "arguments": {"chart_type": "bar"}}],
            "raw_message": {"role": "assistant", "tool_calls": [{"id": "c1"}]},
        }]
        round2 = [{"type": "stream_end", "content": "Here's the chart."}]
        a = _make_agent()
        a.llm = _StubLLM([round1, round2])

        async def fake_execute(client, name, args):
            return json.dumps({
                "__is_chart__": True,
                "chart_spec": {"type": "bar", "data": {"labels": ["A"], "datasets": []}},
            })

        monkeypatch.setattr("backend.agent.execute_tool", fake_execute)

        events, on_event = _events_collector()
        await a.run([{"role": "user", "content": "chart"}], on_event=on_event)

        ch = next((e for e in events if e["type"] == "chart"), None)
        assert ch is not None
        assert ch["spec"]["type"] == "bar"


# ────────────────────── max rounds guardrail ──────────────────────

class TestMaxRoundsGuardrail:
    async def test_emits_terminal_response_when_loop_exhausted(self, monkeypatch):
        # Always-respond-with-tool-call: agent must give up after MAX_TOOL_ROUNDS.
        infinite_round = [{
            "type": "tool_calls",
            "tool_calls": [{"id": "c", "name": "list_sheets", "arguments": {}}],
            "raw_message": {"role": "assistant", "tool_calls": [{"id": "c"}]},
        }]
        a = _make_agent()
        a.llm = _StubLLM([infinite_round] * (MAX_TOOL_ROUNDS + 5))

        async def fake_execute(client, name, args):
            return "[]"

        monkeypatch.setattr("backend.agent.execute_tool", fake_execute)

        events, on_event = _events_collector()
        result = await a.run([{"role": "user", "content": "loop forever"}], on_event=on_event)

        # The terminal "max rounds" message must surface
        assert "maximum number of tool calls" in result
        finals = [e for e in events if e["type"] == "response"
                  and "maximum number of tool calls" in e.get("content", "")]
        assert finals, "Agent must emit a final response when loop exhausted"
