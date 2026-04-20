"""End-to-end safety-net tests for the agent harness.

These tests reproduce the failure modes that motivated Palier 1 / Palier 2 of
the agent rework. Each scenario scripts an LLM that intentionally walks into
a trap; we then assert that the harness catches the mistake and that the
follow-up correction succeeds.

Scenarios covered:
1. **Column/row confusion** (the original user-reported bug):
   the LLM tries to `add_rows({"NewCol": ""})` to create a column.
   → schema-guard returns UNKNOWN_COLUMNS with the recovery hint.
   → on the next round, the LLM corrects to `add_column(...)` and succeeds.
2. **Loop killer**: the LLM spams the same dead-end call.
   → after LOOP_REPEAT_THRESHOLD identical attempts the harness blocks the
     repeat and surfaces a REPEATED_CALL tool_result.
3. **Schema-guard with valid update**: a multi-step write where the first
   payload references a typo'd column; the harness blocks, the LLM uses the
   `valid_columns` field from the error to rewrite, and the second attempt
   goes through.

These tests run **without a real Smartsheet token**: the `SmartsheetClient`
is a `MagicMock` whose responses we script. The point is to verify the
agent-harness behaviour, not the API integration.
"""
from __future__ import annotations

import json
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.agent import LOOP_REPEAT_THRESHOLD, Agent, _new_metrics

pytestmark = [pytest.mark.e2e, pytest.mark.asyncio]


# ────────────────────── infra ──────────────────────


class ScriptedLLM:
    """A deterministic LLM that yields a pre-scripted tool_calls sequence,
    one round per conversation turn."""

    def __init__(self, rounds: list[list[dict]]):
        self.rounds = rounds
        self.idx = 0
        self.model = "scripted"
        self.provider = "test"
        # Every chat_stream invocation receives the live message history; we
        # capture it so tests can introspect what the harness fed back.
        self.received_histories: list[list[dict]] = []

    async def chat_stream(self, messages, tools=None, system="") -> AsyncIterator[dict]:
        # Snapshot the conversation as the harness sees it on this turn
        self.received_histories.append([dict(m) for m in messages])
        if self.idx >= len(self.rounds):
            yield {"type": "stream_end", "content": "OK"}
            return
        chunks = self.rounds[self.idx]
        self.idx += 1
        for c in chunks:
            yield c


def _tool_round(name: str, args: dict, call_id: str = "c") -> list[dict]:
    return [{
        "type": "tool_calls",
        "tool_calls": [{"id": call_id, "name": name, "arguments": args}],
        "raw_message": {"role": "assistant", "tool_calls": [{"id": call_id}]},
    }]


def _final_round(text: str = "Done.") -> list[dict]:
    return [{"type": "stream_end", "content": text}]


def _make_client(columns: list[dict] | None = None) -> MagicMock:
    """A mocked SmartsheetClient with the methods the agent might touch."""
    client = MagicMock()
    sheet = {
        "id": 1,
        "name": "Demo sheet",
        "columns": columns or [
            {"id": 10, "title": "Task", "type": "TEXT_NUMBER"},
            {"id": 20, "title": "Status", "type": "PICKLIST"},
        ],
    }
    client.get_sheet = AsyncMock(return_value=sheet)
    client.add_rows = AsyncMock(return_value={"message": "SUCCESS", "result": []})
    client.add_column = AsyncMock(return_value={
        "message": "SUCCESS",
        "result": {"id": 99, "title": "Owner", "index": 2},
    })
    client.update_rows = AsyncMock(return_value={"message": "SUCCESS"})
    client.delete_rows = AsyncMock(return_value={"message": "SUCCESS"})
    client.list_sheets = AsyncMock(return_value=[])
    return client


def _make_agent(client) -> Agent:
    a = Agent.__new__(Agent)
    a.smartsheet = client
    a.sheet_id = "1"
    a.sheet_context = {"summary": {
        "name": "Demo sheet", "totalRowCount": 0, "columnCount": 2,
        "columns": [
            {"title": "Task", "type": "TEXT_NUMBER"},
            {"title": "Status", "type": "PICKLIST"},
        ],
    }}
    a.pinned_sheets = []
    a.metrics = _new_metrics()
    return a


async def _noop(_e):
    return None


# ────────────────────── scenario 1: column/row confusion ──────────────────────


class TestColumnRowConfusionRecovery:
    """The original bug: 'ajoute une colonne test-abdel' → LLM picked
    add_rows({"test-abdel": ""}). With the schema-guard, that call is
    short-circuited and the LLM is told (a) which columns exist and (b) to
    use add_column for new columns."""

    async def test_first_attempt_is_blocked_then_correction_succeeds(self):
        client = _make_client()

        # Round 1: bug — add_rows with a non-existent column
        # Round 2: the LLM reads the UNKNOWN_COLUMNS error and corrects to add_column
        # Round 3: final answer
        llm = ScriptedLLM([
            _tool_round("add_rows", {
                "sheet_id": "1",
                "rows": [{"test-abdel": ""}],
            }, call_id="c1"),
            _tool_round("add_column", {
                "sheet_id": "1",
                "title": "test-abdel",
                "col_type": "TEXT_NUMBER",
                "index": 2,
            }, call_id="c2"),
            _final_round("Column 'test-abdel' added."),
        ])

        a = _make_agent(client)
        a.llm = llm

        msgs = [{"role": "user", "content": "Ajoute une colonne 'test-abdel'"}]
        await a.run(msgs, on_event=_noop)

        # Schema-guard blocked the first call → client.add_rows was never invoked
        client.add_rows.assert_not_called()
        # The correction round actually called add_column
        client.add_column.assert_awaited_once()
        # The conversation history must contain the structured UNKNOWN_COLUMNS payload
        # so the LLM has the hint to recover.
        tool_results = [m for m in msgs if m.get("role") == "tool_result"]
        assert any("UNKNOWN_COLUMNS" in m["content"] for m in tool_results)

    async def test_unknown_columns_payload_carries_recovery_metadata(self):
        client = _make_client()

        llm = ScriptedLLM([
            _tool_round("add_rows", {
                "sheet_id": "1",
                "rows": [{"GhostColumn": "x", "Task": "ok"}],
            }, call_id="c1"),
            _final_round(),
        ])

        a = _make_agent(client)
        a.llm = llm

        msgs = [{"role": "user", "content": "Add a row"}]
        await a.run(msgs, on_event=_noop)

        # Pull back the UNKNOWN_COLUMNS tool_result and inspect its shape
        tr = next(m for m in msgs
                  if m.get("role") == "tool_result"
                  and "UNKNOWN_COLUMNS" in m.get("content", ""))
        payload = json.loads(tr["content"])
        assert payload["error"] == "UNKNOWN_COLUMNS"
        assert "GhostColumn" in payload["unknown_columns"]
        assert "Task" in payload["valid_columns"]
        assert "Status" in payload["valid_columns"]
        # The hint must teach BOTH escape hatches
        hint = payload["hint"].lower()
        assert "add_column" in hint
        assert "get_sheet_summary" in hint


# ────────────────────── scenario 2: loop killer ──────────────────────


class TestLoopKillerInRealRun:
    async def test_dead_end_loop_is_broken_after_threshold(self):
        client = _make_client()
        # Make every add_column call fail at the API level so the LLM keeps retrying.
        client.add_column = AsyncMock(side_effect=Exception("transient API failure"))

        attempts = LOOP_REPEAT_THRESHOLD + 2
        rounds = []
        for i in range(attempts):
            rounds.append(_tool_round("add_column", {
                "sheet_id": "1",
                "title": "Owner",
                "col_type": "TEXT_NUMBER",
                "index": 2,
            }, call_id=f"c{i}"))
        rounds.append(_final_round("giving up"))

        llm = ScriptedLLM(rounds)
        a = _make_agent(client)
        a.llm = llm

        msgs = [{"role": "user", "content": "Add column Owner"}]
        await a.run(msgs, on_event=_noop)

        # The harness must have stopped invoking client.add_column once the
        # loop killer triggered (i.e. NOT all `attempts` calls go through).
        actual_calls = client.add_column.await_count
        assert actual_calls <= LOOP_REPEAT_THRESHOLD, (
            f"loop killer must cap repeats at {LOOP_REPEAT_THRESHOLD}, got {actual_calls}"
        )
        # And the conversation must contain a REPEATED_CALL signal so the LLM
        # knows to change strategy.
        repeat_msgs = [m for m in msgs
                       if m.get("role") == "tool_result"
                       and "REPEATED_CALL" in m.get("content", "")]
        assert repeat_msgs, "loop killer must inject a REPEATED_CALL tool_result"


# ────────────────────── scenario 3: schema-guard does NOT block legitimate writes ──────────────────────


class TestSchemaGuardDoesNotOverreach:
    """Critical regression check: the guard must let valid writes through
    without ceremony. If we turn it into a brick wall we lose the agent."""

    async def test_valid_column_names_pass_through(self):
        client = _make_client()

        llm = ScriptedLLM([
            _tool_round("add_rows", {
                "sheet_id": "1",
                "rows": [{"Task": "Buy milk", "Status": "Open"}],
            }, call_id="c1"),
            _final_round("Row added."),
        ])

        a = _make_agent(client)
        a.llm = llm
        msgs = [{"role": "user", "content": "Add a row Buy milk"}]
        await a.run(msgs, on_event=_noop)

        client.add_rows.assert_awaited_once()
        # No UNKNOWN_COLUMNS error should have surfaced
        unknown = [m for m in msgs
                   if m.get("role") == "tool_result"
                   and "UNKNOWN_COLUMNS" in m.get("content", "")]
        assert not unknown

    async def test_api_shape_payload_passes_through_without_validation(self):
        """If the LLM emits the API-shape rows ({cells: [{columnId, value}]})
        the guard cannot validate column names and must let the API call go."""
        client = _make_client()

        llm = ScriptedLLM([
            _tool_round("add_rows", {
                "sheet_id": "1",
                "rows": [{"cells": [{"columnId": 10, "value": "X"}]}],
            }, call_id="c1"),
            _final_round("Done."),
        ])

        a = _make_agent(client)
        a.llm = llm
        msgs = [{"role": "user", "content": "Add a raw row"}]
        await a.run(msgs, on_event=_noop)

        client.add_rows.assert_awaited_once()


# ────────────────────── scenario 4: the harness feeds the corrected schema back ──────────────────────


class TestSchemaGuardEducationLoop:
    """When the guard fires, the very next chat_stream call must include the
    UNKNOWN_COLUMNS error in the message history — that's how the LLM
    'sees' the recovery instructions."""

    async def test_second_round_sees_the_error_in_its_history(self):
        client = _make_client()

        llm = ScriptedLLM([
            _tool_round("add_rows", {
                "sheet_id": "1",
                "rows": [{"Bogus": "x"}],
            }, call_id="c1"),
            _tool_round("get_sheet_summary", {"sheet_id": "1"}, call_id="c2"),
            _final_round(),
        ])

        a = _make_agent(client)
        a.llm = llm
        msgs = [{"role": "user", "content": "Add a row"}]
        await a.run(msgs, on_event=_noop)

        # Inspect the message history fed into the LLM on round 2
        assert len(llm.received_histories) >= 2
        round2 = llm.received_histories[1]
        # The UNKNOWN_COLUMNS tool_result must be visible to the LLM here
        round2_blob = json.dumps(round2)
        assert "UNKNOWN_COLUMNS" in round2_blob
        assert "Bogus" in round2_blob
        assert "Task" in round2_blob  # one of the valid columns
