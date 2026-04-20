"""Unit tests for the agent loop killer (P1.1).

The loop killer is the safety net that stops the model from burning every
remaining round on the same failing tool call. It tracks (tool_name,
canonical_args) signatures within a single turn and, after
`LOOP_REPEAT_THRESHOLD` identical executions, refuses to run the call again
and instead injects a structured `REPEATED_CALL` tool_result so the model
breaks out of the dead end.

These tests make the contract explicit and regression-proof.
"""
from __future__ import annotations

import json
from typing import AsyncIterator

import pytest

from backend.agent import LOOP_REPEAT_THRESHOLD, MAX_TOOL_ROUNDS, Agent, _new_metrics

pytestmark = [pytest.mark.unit]


# ────────────────────── helpers ──────────────────────


def _make_agent() -> Agent:
    a = Agent.__new__(Agent)
    a.llm = None
    a.smartsheet = object()
    a.sheet_id = "1"
    a.sheet_context = {"summary": {"name": "S", "totalRowCount": 0, "columnCount": 0, "columns": []}}
    a.pinned_sheets = []
    a.metrics = _new_metrics()
    return a


class _StubLLM:
    """Scripts a sequence of chat_stream responses, one per agent round."""

    def __init__(self, scripted_rounds: list[list[dict]]):
        self._rounds = scripted_rounds
        self._idx = 0
        self.model = "test-model"
        self.provider = "test"

    async def chat_stream(self, messages, tools=None, system="") -> AsyncIterator[dict]:
        if self._idx >= len(self._rounds):
            yield {"type": "stream_end", "content": "done"}
            return
        chunks = self._rounds[self._idx]
        self._idx += 1
        for c in chunks:
            yield c


def _round_calling(name: str, arguments: dict, call_id: str = "c1") -> list[dict]:
    return [{
        "type": "tool_calls",
        "tool_calls": [{"id": call_id, "name": name, "arguments": arguments}],
        "raw_message": {"role": "assistant", "tool_calls": [{"id": call_id}]},
    }]


def _events_collector():
    events: list[dict] = []

    async def on_event(e):
        events.append(e)

    return events, on_event


# ────────────────────── signature stability ──────────────────────


@pytest.mark.asyncio
class TestCallSignature:
    async def test_same_args_different_key_order_have_same_signature(self):
        s1 = Agent._call_signature("foo", {"a": 1, "b": 2})
        s2 = Agent._call_signature("foo", {"b": 2, "a": 1})
        assert s1 == s2, "signature must be order-independent (uses sort_keys)"

    async def test_different_tool_names_have_different_signatures(self):
        s1 = Agent._call_signature("foo", {"a": 1})
        s2 = Agent._call_signature("bar", {"a": 1})
        assert s1 != s2

    async def test_different_args_have_different_signatures(self):
        s1 = Agent._call_signature("foo", {"a": 1})
        s2 = Agent._call_signature("foo", {"a": 2})
        assert s1 != s2

    async def test_handles_unserialisable_args_without_crashing(self):
        class Weird:
            def __repr__(self) -> str:
                return "<weird>"

        sig = Agent._call_signature("foo", {"x": Weird()})
        assert isinstance(sig, str) and "foo::" in sig


# ────────────────────── threshold semantics ──────────────────────


@pytest.mark.asyncio
class TestLoopThreshold:
    """First THRESHOLD calls execute; the (THRESHOLD+1)-th and later are
    blocked with a REPEATED_CALL tool_result."""

    async def test_calls_below_threshold_all_execute(self, monkeypatch):
        # The model wants to call list_sheets exactly LOOP_REPEAT_THRESHOLD
        # times then close. Each call must execute.
        rounds = [_round_calling("list_sheets", {}, call_id=f"c{i}")
                  for i in range(LOOP_REPEAT_THRESHOLD)]
        rounds.append([{"type": "stream_end", "content": "done"}])

        a = _make_agent()
        a.llm = _StubLLM(rounds)

        executed = {"n": 0}

        async def fake_execute(client, name, args):
            executed["n"] += 1
            return "[]"

        monkeypatch.setattr("backend.agent.execute_tool", fake_execute)

        events, on_event = _events_collector()
        await a.run([{"role": "user", "content": "go"}], on_event=on_event)

        assert executed["n"] == LOOP_REPEAT_THRESHOLD, (
            f"first {LOOP_REPEAT_THRESHOLD} identical calls must all run"
        )

    async def test_call_above_threshold_is_blocked(self, monkeypatch):
        # Model spams the same call THRESHOLD+1 times. Only the first
        # THRESHOLD execute; the next is blocked by the loop killer.
        n_attempts = LOOP_REPEAT_THRESHOLD + 1
        rounds = [_round_calling("list_sheets", {}, call_id=f"c{i}")
                  for i in range(n_attempts)]
        rounds.append([{"type": "stream_end", "content": "done"}])

        a = _make_agent()
        a.llm = _StubLLM(rounds)

        executed = {"n": 0}

        async def fake_execute(client, name, args):
            executed["n"] += 1
            return "[]"

        monkeypatch.setattr("backend.agent.execute_tool", fake_execute)

        events, on_event = _events_collector()
        msgs: list[dict] = [{"role": "user", "content": "loop"}]
        await a.run(msgs, on_event=on_event)

        assert executed["n"] == LOOP_REPEAT_THRESHOLD, (
            "the (THRESHOLD+1)-th identical call must NOT execute — loop killer blocks it"
        )

        # A REPEATED_CALL tool_result must be injected into the conversation
        repeats = [m for m in msgs
                   if m.get("role") == "tool_result"
                   and "REPEATED_CALL" in m.get("content", "")]
        assert repeats, "loop killer must inject a REPEATED_CALL tool_result"

        # And surface the loop event to the on_event listener
        loop_evts = [e for e in events
                     if e.get("type") == "tool_result"
                     and "Loop detected" in str(e.get("result", ""))]
        assert loop_evts, "loop killer must emit a 'Loop detected' tool_result event"

    async def test_block_message_is_actionable(self, monkeypatch):
        # The injected payload must tell the model what to do (change tool / args / ask user).
        n_attempts = LOOP_REPEAT_THRESHOLD + 1
        rounds = [_round_calling("delete_rows", {"sheet_id": "1", "row_ids": [99]}, call_id=f"c{i}")
                  for i in range(n_attempts)]
        rounds.append([{"type": "stream_end", "content": "done"}])

        a = _make_agent()
        a.llm = _StubLLM(rounds)

        async def fake_execute(client, name, args):
            return "[]"

        monkeypatch.setattr("backend.agent.execute_tool", fake_execute)
        # delete_rows is destructive — auto-approve so we hit the loop path.
        async def confirm(_n, _a, _i):
            return True

        async def noop_event(_e):
            return None

        msgs: list[dict] = [{"role": "user", "content": "del"}]
        await a.run(msgs, on_event=noop_event, confirm_callback=confirm)

        repeats = [m for m in msgs
                   if m.get("role") == "tool_result"
                   and "REPEATED_CALL" in m.get("content", "")]
        assert repeats
        payload = json.loads(repeats[0]["content"])
        assert payload["error"] == "REPEATED_CALL"
        assert payload["tool"] == "delete_rows"
        assert payload["repeat_count"] == LOOP_REPEAT_THRESHOLD + 1
        # Must list at least the three escape hatches (different tool / different args / ask user)
        msg = payload["message"].lower()
        assert "different tool" in msg
        assert "different arguments" in msg
        assert "clarifying question" in msg


# ────────────────────── isolation ──────────────────────


@pytest.mark.asyncio
class TestLoopIsolation:
    """Different signatures must not cross-contaminate the counter."""

    async def test_different_args_count_separately(self, monkeypatch):
        # 3 calls with row_ids=[1], then 3 calls with row_ids=[2].
        # Both signatures stay AT the threshold but never exceed it, so all 6 execute.
        async def fake_confirm(_n, _a, _i):
            return True

        rounds = []
        for rid in [1, 1, 1, 2, 2, 2]:
            rounds.append(_round_calling(
                "delete_rows", {"sheet_id": "1", "row_ids": [rid]},
                call_id=f"c{rid}"
            ))
        rounds.append([{"type": "stream_end", "content": "done"}])

        a = _make_agent()
        a.llm = _StubLLM(rounds)

        executed = {"n": 0}

        async def fake_execute(client, name, args):
            executed["n"] += 1
            return "[]"

        monkeypatch.setattr("backend.agent.execute_tool", fake_execute)

        async def noop_event(_e):
            return None

        await a.run(
            [{"role": "user", "content": "x"}],
            on_event=noop_event,
            confirm_callback=fake_confirm,
        )
        assert executed["n"] == 6, "different signatures must not share a counter"
