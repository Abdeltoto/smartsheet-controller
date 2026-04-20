"""Unit tests for the agent reliability metrics (P3.3).

Each safety-net activation must increment its counter so /api/usage can
expose the harness behaviour to the user.
"""
from __future__ import annotations

from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.agent import LOOP_REPEAT_THRESHOLD, Agent, _new_metrics

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _Stub:
    def __init__(self, rounds: list[list[dict]]):
        self.rounds, self.idx = rounds, 0
        self.model, self.provider = "stub", "test"

    async def chat_stream(self, messages, tools=None, system="") -> AsyncIterator[dict]:
        if self.idx >= len(self.rounds):
            yield {"type": "stream_end", "content": "OK"}
            return
        chunks = self.rounds[self.idx]
        self.idx += 1
        for c in chunks:
            yield c


def _round(name: str, args: dict, cid: str = "c") -> list[dict]:
    return [{
        "type": "tool_calls",
        "tool_calls": [{"id": cid, "name": name, "arguments": args}],
        "raw_message": {"role": "assistant", "tool_calls": [{"id": cid}]},
    }]


def _agent() -> Agent:
    a = Agent.__new__(Agent)
    a.smartsheet = MagicMock()
    a.smartsheet.get_sheet = AsyncMock(return_value={
        "id": 1, "name": "S",
        "columns": [{"id": 10, "title": "Task", "type": "TEXT_NUMBER"}],
    })
    a.smartsheet.add_rows = AsyncMock(return_value={"message": "SUCCESS"})
    a.sheet_id = "1"
    a.sheet_context = {"summary": {
        "name": "S", "totalRowCount": 0, "columnCount": 1,
        "columns": [{"title": "Task", "type": "TEXT_NUMBER"}],
    }}
    a.pinned_sheets = []
    a.metrics = _new_metrics()
    return a


async def _noop(_):
    return None


class TestMetricsInitialState:
    async def test_new_metrics_has_all_expected_counters(self):
        m = _new_metrics()
        assert set(m.keys()) == {
            "tool_calls", "tool_errors", "loop_blocked",
            "schema_guard_triggered", "parse_errors", "user_rejections",
            "rounds_exhausted", "turns",
        }
        assert all(v == 0 for v in m.values())


class TestMetricsCount:
    async def test_turn_counter_increments_per_run(self):
        a = _agent()
        a.llm = _Stub([[{"type": "stream_end", "content": "hi"}]])
        await a.run([{"role": "user", "content": "ping"}], on_event=_noop)
        assert a.metrics["turns"] == 1
        a.llm = _Stub([[{"type": "stream_end", "content": "hi"}]])
        await a.run([{"role": "user", "content": "ping again"}], on_event=_noop)
        assert a.metrics["turns"] == 2

    async def test_successful_tool_call_increments_tool_calls_only(self, monkeypatch):
        a = _agent()
        a.llm = _Stub([
            _round("list_sheets", {}),
            [{"type": "stream_end", "content": "done"}],
        ])
        async def fake_exec(c, n, ar):
            return "[]"
        monkeypatch.setattr("backend.agent.execute_tool", fake_exec)
        await a.run([{"role": "user", "content": "list"}], on_event=_noop)
        assert a.metrics["tool_calls"] == 1
        assert a.metrics["tool_errors"] == 0
        assert a.metrics["schema_guard_triggered"] == 0

    async def test_unknown_columns_increments_schema_guard_and_tool_errors(self):
        # Real execute_tool path: add_rows with bogus column → schema-guard
        # returns UNKNOWN_COLUMNS and the agent must count both flags.
        a = _agent()
        a.llm = _Stub([
            _round("add_rows", {
                "sheet_id": "1",
                "rows": [{"NotAColumn": "x"}],
            }),
            [{"type": "stream_end", "content": "done"}],
        ])
        await a.run([{"role": "user", "content": "add"}], on_event=_noop)
        assert a.metrics["schema_guard_triggered"] == 1
        assert a.metrics["tool_errors"] == 1
        assert a.metrics["tool_calls"] == 1

    async def test_loop_block_increments_loop_blocked(self, monkeypatch):
        a = _agent()
        rounds = [_round("list_sheets", {}, cid=f"c{i}")
                  for i in range(LOOP_REPEAT_THRESHOLD + 1)]
        rounds.append([{"type": "stream_end", "content": "done"}])
        a.llm = _Stub(rounds)
        async def fake_exec(c, n, ar):
            return "[]"
        monkeypatch.setattr("backend.agent.execute_tool", fake_exec)
        await a.run([{"role": "user", "content": "loop"}], on_event=_noop)
        assert a.metrics["loop_blocked"] == 1
        # Only THRESHOLD calls actually executed
        assert a.metrics["tool_calls"] == LOOP_REPEAT_THRESHOLD

    async def test_parse_error_increments_parse_errors(self):
        a = _agent()
        a.llm = _Stub([
            [{
                "type": "tool_calls",
                "tool_calls": [{
                    "id": "c1",
                    "name": "list_sheets",
                    "arguments": {"__parse_error__": "bad json", "__raw__": "{oops"},
                }],
                "raw_message": {"role": "assistant", "tool_calls": [{"id": "c1"}]},
            }],
            [{"type": "stream_end", "content": "done"}],
        ])
        await a.run([{"role": "user", "content": "x"}], on_event=_noop)
        assert a.metrics["parse_errors"] == 1
        assert a.metrics["tool_calls"] == 0  # never executed

    async def test_user_rejection_increments_user_rejections(self):
        a = _agent()
        a.llm = _Stub([
            _round("delete_rows", {"sheet_id": "1", "row_ids": [1]}),
            [{"type": "stream_end", "content": "done"}],
        ])
        async def reject(*_):
            return False
        await a.run([{"role": "user", "content": "del"}], on_event=_noop, confirm_callback=reject)
        assert a.metrics["user_rejections"] == 1
        assert a.metrics["tool_calls"] == 0
