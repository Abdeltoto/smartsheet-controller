"""Unit tests for the `agent_hint` event stream (P3.4).

When a safety net activates, the harness must emit a structured
`agent_hint` event so the frontend can display a human-readable banner.
"""
from __future__ import annotations

from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.agent import LOOP_REPEAT_THRESHOLD, Agent, _new_metrics

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _Stub:
    def __init__(self, rounds): self.rounds, self.idx = rounds, 0; self.model="s"; self.provider="t"
    async def chat_stream(self, messages, tools=None, system="") -> AsyncIterator[dict]:
        if self.idx >= len(self.rounds):
            yield {"type": "stream_end", "content": "OK"}; return
        chunks = self.rounds[self.idx]; self.idx += 1
        for c in chunks: yield c


def _round(name, args, cid="c"):
    return [{"type": "tool_calls",
             "tool_calls": [{"id": cid, "name": name, "arguments": args}],
             "raw_message": {"role": "assistant", "tool_calls": [{"id": cid}]}}]


def _agent():
    a = Agent.__new__(Agent)
    a.smartsheet = MagicMock()
    a.smartsheet.get_sheet = AsyncMock(return_value={
        "id": 1, "name": "S",
        "columns": [{"id": 10, "title": "Task", "type": "TEXT_NUMBER"}],
    })
    a.smartsheet.add_rows = AsyncMock(return_value={"message": "SUCCESS"})
    a.sheet_id = "1"
    a.sheet_context = {"summary": {"name": "S", "totalRowCount": 0, "columnCount": 1,
                                    "columns": [{"title": "Task", "type": "TEXT_NUMBER"}]}}
    a.pinned_sheets = []
    a.metrics = _new_metrics()
    return a


def _hints(events): return [e for e in events if e.get("type") == "agent_hint"]


class TestSchemaGuardEmitsHint:
    async def test_unknown_columns_emits_warn_hint(self):
        a = _agent()
        a.llm = _Stub([
            _round("add_rows", {"sheet_id": "1", "rows": [{"Bogus": "x"}]}),
            [{"type": "stream_end", "content": "done"}],
        ])
        events = []
        async def cap(e): events.append(e)
        await a.run([{"role": "user", "content": "go"}], on_event=cap)

        hints = _hints(events)
        assert len(hints) == 1
        h = hints[0]
        assert h["level"] == "warn"
        assert h["code"] == "SCHEMA_GUARD"
        assert h["tool"] == "add_rows"
        assert "Bogus" in h["message"]


class TestLoopKillerEmitsHint:
    async def test_loop_emits_warn_hint(self, monkeypatch):
        a = _agent()
        rounds = [_round("list_sheets", {}, cid=f"c{i}") for i in range(LOOP_REPEAT_THRESHOLD + 1)]
        rounds.append([{"type": "stream_end", "content": "done"}])
        a.llm = _Stub(rounds)

        async def fake_exec(c, n, ar): return "[]"
        monkeypatch.setattr("backend.agent.execute_tool", fake_exec)

        events = []
        async def cap(e): events.append(e)
        await a.run([{"role": "user", "content": "x"}], on_event=cap)

        hints = _hints(events)
        loop_hints = [h for h in hints if h["code"] == "LOOP_BLOCKED"]
        assert len(loop_hints) == 1
        assert loop_hints[0]["level"] == "warn"
        assert loop_hints[0]["tool"] == "list_sheets"


class TestParseErrorEmitsHint:
    async def test_invalid_json_emits_warn_hint(self):
        a = _agent()
        a.llm = _Stub([
            [{
                "type": "tool_calls",
                "tool_calls": [{"id": "c1", "name": "list_sheets",
                               "arguments": {"__parse_error__": "bad", "__raw__": "{"}}],
                "raw_message": {"role": "assistant", "tool_calls": [{"id": "c1"}]},
            }],
            [{"type": "stream_end", "content": "done"}],
        ])
        events = []
        async def cap(e): events.append(e)
        await a.run([{"role": "user", "content": "x"}], on_event=cap)

        hints = _hints(events)
        parse_hints = [h for h in hints if h["code"] == "PARSE_ERROR"]
        assert len(parse_hints) == 1


class TestUserRejectionEmitsHint:
    async def test_destructive_rejection_emits_info_hint(self):
        a = _agent()
        a.llm = _Stub([
            _round("delete_rows", {"sheet_id": "1", "row_ids": [1]}),
            [{"type": "stream_end", "content": "done"}],
        ])
        async def reject(*_): return False
        events = []
        async def cap(e): events.append(e)
        await a.run([{"role": "user", "content": "del"}], on_event=cap, confirm_callback=reject)

        hints = _hints(events)
        rj = [h for h in hints if h["code"] == "USER_REJECTION"]
        assert len(rj) == 1
        assert rj[0]["level"] == "info"


class TestNoSpuriousHintsOnSuccess:
    async def test_clean_run_emits_zero_hints(self, monkeypatch):
        a = _agent()
        a.llm = _Stub([
            _round("list_sheets", {}),
            [{"type": "stream_end", "content": "done"}],
        ])
        async def fake_exec(c, n, ar): return "[]"
        monkeypatch.setattr("backend.agent.execute_tool", fake_exec)
        events = []
        async def cap(e): events.append(e)
        await a.run([{"role": "user", "content": "list"}], on_event=cap)
        assert _hints(events) == []
