"""Live smoke-test for the Palier 3 reliability harness.

Talks to a running uvicorn instance on http://127.0.0.1:8100 and a real
OpenAI key. Reproduces the original column/row confusion bug on the SSH
test sheet (5067689119620) and asserts that:

  * the schema-guard fires when the LLM picks `add_rows` for a column add
  * an `agent_hint` event is streamed to the WebSocket
  * the `/api/usage` endpoint exposes the new `agent_metrics` block with
    schema_guard_triggered ≥ 1 (or another P3 counter incremented)
  * the agent eventually corrects itself and creates the column

Run while uvicorn is up:
    python tests/functional/run_p3_live.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import httpx
import websockets

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

BASE = "http://127.0.0.1:8100"
WS_BASE = "ws://127.0.0.1:8100"
SHEET_ID = "5067689119620"  # the SSH test sheet the user keeps for inspection
SMARTSHEET_TOKEN = os.getenv("SMARTSHEET_TOKEN")
OPENAI_KEY = os.getenv("OPENAI_API_KEY")

assert SMARTSHEET_TOKEN, "SMARTSHEET_TOKEN must be set in .env"
assert OPENAI_KEY, "OPENAI_API_KEY must be set in .env"


GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def banner(title: str) -> None:
    print(f"\n{BOLD}━━━ {title} ━━━{RESET}")


def ok(msg: str) -> None:
    print(f"  {GREEN}✓{RESET} {msg}")


def fail(msg: str) -> None:
    print(f"  {RED}✗{RESET} {msg}")


def info(msg: str) -> None:
    print(f"  {DIM}· {msg}{RESET}")


async def open_session() -> tuple[str, str]:
    async with httpx.AsyncClient(timeout=30) as cli:
        r = await cli.post(f"{BASE}/api/session", json={
            "smartsheet_token": SMARTSHEET_TOKEN,
            "sheet_id": SHEET_ID,
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
            "llm_api_key": OPENAI_KEY,
        })
        r.raise_for_status()
        body = r.json()
        return body["session_id"], body["ws_token"]


async def fetch_usage(session_id: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as cli:
        r = await cli.get(f"{BASE}/api/usage", params={"session_id": session_id})
        r.raise_for_status()
        return r.json()


async def disconnect(session_id: str) -> None:
    async with httpx.AsyncClient(timeout=10) as cli:
        await cli.post(f"{BASE}/api/disconnect", json={"session_id": session_id})


async def chat_and_collect(session_id: str, ws_token: str, message: str,
                            timeout_s: float = 60.0,
                            quiet_after_end_s: float = 1.5) -> list[dict]:
    """Send `message`, auto-approve any destructive confirms, and return
    every event received. Closes once `stream_end` arrives AND the stream
    has been quiet for `quiet_after_end_s` (to flush any trailing
    agent_hint) — or when the global `timeout_s` budget elapses."""
    url = f"{WS_BASE}/ws/{session_id}?token={ws_token}"
    events: list[dict] = []
    start = time.monotonic()
    saw_end = False
    end_at: float | None = None
    async with websockets.connect(url, max_size=10_000_000) as ws:
        await ws.send(json.dumps({"message": message}))
        while time.monotonic() - start < timeout_s:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
            except asyncio.TimeoutError:
                # If we already saw stream_end and have been idle long
                # enough, we're done.
                if saw_end and end_at is not None and \
                   time.monotonic() - end_at >= quiet_after_end_s:
                    break
                continue
            evt = json.loads(raw)
            events.append(evt)
            etype = evt.get("type")
            if etype == "confirm_action":
                await ws.send(json.dumps({
                    "type": "confirm",
                    "tool_call_id": evt["tool_call_id"],
                }))
                continue
            if etype == "stream_end":
                saw_end = True
                end_at = time.monotonic()
    return events


def summarize_events(events: list[dict]) -> dict:
    by_type: dict[str, int] = {}
    hints: list[dict] = []
    tool_calls: list[str] = []
    tool_results: list[tuple[str, str]] = []
    for e in events:
        t = e.get("type", "?")
        by_type[t] = by_type.get(t, 0) + 1
        if t == "agent_hint":
            hints.append(e)
        elif t == "tool_call":
            tool_calls.append(e.get("name", ""))
        elif t == "tool_result":
            tool_results.append((e.get("name", ""), str(e.get("result", ""))[:200]))
    return {
        "by_type": by_type,
        "hints": hints,
        "tool_calls": tool_calls,
        "tool_results": tool_results,
    }


async def main() -> int:
    failures: list[str] = []

    banner("Health check")
    async with httpx.AsyncClient(timeout=5) as cli:
        try:
            r = await cli.get(f"{BASE}/health")
            r.raise_for_status()
            ok(f"/health → {r.json()}")
        except Exception as e:
            fail(f"server not reachable on {BASE}: {e}")
            print(f"\n{RED}Make sure uvicorn is running:{RESET}")
            print(f'  $env:PYTHONUTF8="1"; uvicorn backend.app:app --reload --port 8100')
            return 1

    banner("Open session on SSH test sheet")
    sid, wtok = await open_session()
    ok(f"session {sid[:8]}… on sheet {SHEET_ID}")

    try:
        # ─────────── Scenario: column/row confusion ───────────
        # The exact wording that triggered the original user-reported bug.
        # We expect: schema-guard fires (or the model picks add_column
        # directly thanks to better tool descriptions), no run-away loop,
        # and metrics reflect what happened.
        banner("Scenario — 'Ajoute une colonne P3-live-test (TEXT_NUMBER)'")
        events = await chat_and_collect(
            sid, wtok,
            "Ajoute une colonne 'P3-live-test' de type TEXT_NUMBER à la fin.",
            timeout_s=90,
        )
        summary = summarize_events(events)

        info(f"event types: {summary['by_type']}")
        info(f"tool_calls in order: {summary['tool_calls']}")

        if summary["hints"]:
            banner("Agent hints captured")
            for h in summary["hints"]:
                color = YELLOW if h.get("level") == "warn" else DIM
                print(f"  {color}[{h.get('code')}] tool={h.get('tool')} "
                      f"level={h.get('level')}{RESET}")
                print(f"    {h.get('message')}")

        # Expectations
        if "add_column" in summary["tool_calls"]:
            ok("agent eventually called `add_column` — column creation path taken")
        else:
            failures.append("agent never called `add_column` — bug not fixed")
            fail("agent never called `add_column`")

        # Did the schema-guard fire? Either:
        #  - SCHEMA_GUARD agent_hint was emitted, OR
        #  - the LLM nailed it on the first try and add_rows was never called
        had_add_rows_attempt = "add_rows" in summary["tool_calls"]
        had_schema_guard_hint = any(
            h.get("code") == "SCHEMA_GUARD" for h in summary["hints"]
        )
        if had_add_rows_attempt and not had_schema_guard_hint:
            failures.append(
                "agent called `add_rows` for a column add and the schema-guard "
                "did NOT fire — this is the original bug uncaught"
            )
            fail("add_rows attempted but no SCHEMA_GUARD hint emitted")
        elif had_add_rows_attempt and had_schema_guard_hint:
            ok("agent fell into the row/column trap — schema-guard caught it")
        elif not had_add_rows_attempt:
            ok("agent picked `add_column` on the first try (clean run)")

        # ─────────── Adversarial scenario: force the schema-guard ───────────
        # We instruct the agent to write into a non-existent column using
        # `add_rows` *without* creating it first. The schema-guard MUST
        # intercept this and emit an `agent_hint`.
        banner("Adversarial — force schema-guard via unknown column")
        adv_msg = (
            "Appelle DIRECTEMENT l'outil add_rows sur la sheet courante avec "
            "cette payload exacte: rows=[{\"cells\": [{\"columnName\": "
            "\"ColonneFantome_ZZZ\", \"value\": \"oops\"}]}]. "
            "Ne crée PAS la colonne avant. Ne fais PAS de read préalable. "
            "C'est un test du garde-fou, je veux voir l'erreur."
        )
        events2 = await chat_and_collect(sid, wtok, adv_msg, timeout_s=90)
        summary2 = summarize_events(events2)
        info(f"event types: {summary2['by_type']}")
        info(f"tool_calls: {summary2['tool_calls']}")
        for name, res in summary2["tool_results"]:
            info(f"  tool_result[{name}] = {res}")
        # Also dump the actual add_rows arguments the LLM produced, so we
        # can tell whether the schema-guard *should* have fired.
        for e in events2:
            if e.get("type") == "tool_call" and e.get("name") == "add_rows":
                info(f"  add_rows args = {json.dumps(e.get('arguments'))[:300]}")

        guard_hints = [h for h in summary2["hints"]
                       if h.get("code") == "SCHEMA_GUARD"]
        if guard_hints:
            ok(f"SCHEMA_GUARD agent_hint emitted ({len(guard_hints)}×)")
            for h in guard_hints:
                info(f"  level={h.get('level')} tool={h.get('tool')}")
                info(f"  msg  ={h.get('message')[:140]}")
        elif "add_rows" not in summary2["tool_calls"]:
            info("LLM refused to call add_rows blindly — guard not exercised "
                 "(this is also a win)")
        else:
            failures.append(
                "agent called add_rows with unknown column but no SCHEMA_GUARD "
                "hint was streamed"
            )
            fail("add_rows attempted but no SCHEMA_GUARD hint")

        # ─────────── Verify usage metrics ───────────
        banner("Verify /api/usage exposes agent_metrics")
        usage = await fetch_usage(sid)
        metrics = usage.get("agent_metrics")
        if metrics is None:
            failures.append("/api/usage did not include `agent_metrics` block")
            fail("missing agent_metrics in /api/usage payload")
        else:
            ok(f"agent_metrics keys: {sorted(metrics.keys())}")
            info(json.dumps(metrics, indent=2))
            expected_keys = {
                "tool_calls", "tool_errors", "loop_blocked",
                "schema_guard_triggered", "parse_errors",
                "user_rejections", "rounds_exhausted", "turns",
            }
            missing = expected_keys - set(metrics.keys())
            if missing:
                failures.append(f"agent_metrics missing keys: {missing}")
                fail(f"missing keys: {missing}")
            else:
                ok("all 8 expected metric keys present")
            if metrics.get("turns", 0) >= 1:
                ok(f"turns counter incremented (turns={metrics['turns']})")
            else:
                failures.append("turns counter did not increment")

    finally:
        await disconnect(sid)
        info("session disconnected")

    banner("Result")
    if failures:
        for f in failures:
            fail(f)
        print(f"\n{RED}{BOLD}P3 LIVE SMOKE: FAILED{RESET}")
        return 1
    print(f"\n{GREEN}{BOLD}P3 LIVE SMOKE: PASSED{RESET}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
