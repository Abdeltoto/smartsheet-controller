"""Functional smoke runner — exercises every major tool group on a real test sheet.

Run from project root:

    python tests/functional/run_smoke.py

What it does
------------
1. Reads SMARTSHEET_TOKEN from .env (or env var).
2. Creates a sheet named ``FUNCTIONAL_TEST_<timestamp>``.
3. Walks through ~30 steps covering: sheet ops, columns, rows, search,
   discussions, attachments, workspaces, webhooks, reports, dashboards.
4. Each step calls ``backend.tools.execute_tool`` — the SAME entry point the
   agent uses — so the dispatch glue is exercised end-to-end.
5. Logs each step with PASS / FAIL / SKIP, timing, and a short detail line.
6. Prints a final summary table.
7. **Does NOT delete the test sheet.** The sheet ID and Smartsheet URL are
   printed at the end so you can open it and visually verify every artifact
   the runner produced (columns added, rows, discussions, attachments…).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from backend.smartsheet_client import SmartsheetClient
from backend.tools import execute_tool


# ---------------------------------------------------------------------------
# Pretty printing helpers
# ---------------------------------------------------------------------------

class C:
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"
    GREY = "\033[90m"


def banner(text: str) -> None:
    line = "=" * 78
    print(f"\n{C.BOLD}{C.CYAN}{line}\n  {text}\n{line}{C.RESET}")


def section(text: str) -> None:
    print(f"\n{C.BOLD}{C.MAGENTA}--- {text} ---{C.RESET}")


def step_log(idx: int, total: int, name: str, tool: str) -> None:
    print(f"{C.GREY}[{idx:02d}/{total:02d}]{C.RESET} {C.BOLD}{name}{C.RESET} {C.DIM}({tool}){C.RESET}")


def status_pass(detail: str, ms: int) -> None:
    print(f"   {C.GREEN}PASS{C.RESET} {C.DIM}({ms} ms){C.RESET}  {detail}")


def status_fail(detail: str, ms: int) -> None:
    print(f"   {C.RED}FAIL{C.RESET} {C.DIM}({ms} ms){C.RESET}  {detail}")


def status_skip(reason: str) -> None:
    print(f"   {C.YELLOW}SKIP{C.RESET}  {reason}")


# ---------------------------------------------------------------------------
# Step infrastructure
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    idx: int
    name: str
    tool: str
    status: str  # PASS | FAIL | SKIP
    ms: int
    detail: str = ""
    error: str = ""


@dataclass
class State:
    """Shared mutable state across steps (created IDs, etc.)."""
    sheet_id: str = ""
    sheet_permalink: str = ""
    column_ids: dict[str, int] = field(default_factory=dict)  # title -> id
    row_ids: list[int] = field(default_factory=list)
    discussion_id: int | None = None
    attachment_ids: list[int] = field(default_factory=list)
    workspace_id: int | None = None


async def call_tool(client: SmartsheetClient, tool: str, args: dict) -> dict:
    """Call execute_tool and parse the JSON envelope. Raises on dispatch errors."""
    raw = await execute_tool(client, tool, args)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Non-JSON response: {raw[:200]}")
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(data["error"])
    return data


# ---------------------------------------------------------------------------
# Steps (each returns a short detail string on success or raises on failure)
# ---------------------------------------------------------------------------

async def step_create_sheet(client, state):
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    name = f"FUNCTIONAL_TEST_{ts}"
    columns = [
        {"title": "Task", "primary": True, "type": "TEXT_NUMBER"},
        {"title": "Status", "type": "PICKLIST", "options": ["Open", "Done"]},
    ]
    result = await call_tool(client, "create_sheet", {"name": name, "columns": columns})
    payload = result.get("result") or result
    state.sheet_id = str(payload["id"])
    state.sheet_permalink = payload.get("permalink", "")
    for c in payload.get("columns", []):
        state.column_ids[c["title"]] = c["id"]
    return f"sheet_id={state.sheet_id}, name='{name}'"


async def step_get_summary(client, state):
    # Smartsheet has a brief eventual-consistency window after sheet creation
    # where reads can 404. Retry a few times to absorb that delay.
    last_err = None
    for attempt in range(5):
        try:
            result = await call_tool(client, "get_sheet_summary", {"sheet_id": state.sheet_id})
            return f"name='{result['name']}', cols={result['columnCount']}, rows={result['totalRowCount']} (attempt {attempt + 1})"
        except Exception as e:
            last_err = e
            if "404" not in str(e):
                raise
            await asyncio.sleep(0.7)
    raise RuntimeError(f"get_sheet_summary still 404 after 5 retries: {last_err}")


async def step_add_column_text(client, state):
    result = await call_tool(client, "add_column", {
        "sheet_id": state.sheet_id,
        "title": "Notes",
        "col_type": "TEXT_NUMBER",
        "index": 2,
    })
    payload = result.get("result") or result
    cols = payload if isinstance(payload, list) else [payload]
    for c in cols:
        state.column_ids[c.get("title", "Notes")] = c["id"]
    return f"added Notes (TEXT_NUMBER) -> id={cols[0]['id']}"


async def step_add_column_date(client, state):
    result = await call_tool(client, "add_column", {
        "sheet_id": state.sheet_id,
        "title": "Due Date",
        "col_type": "DATE",
        "index": 3,
    })
    payload = result.get("result") or result
    c = payload[0] if isinstance(payload, list) else payload
    state.column_ids["Due Date"] = c["id"]
    return f"added Due Date (DATE) -> id={c['id']}"


async def step_add_column_checkbox(client, state):
    result = await call_tool(client, "add_column", {
        "sheet_id": state.sheet_id,
        "title": "Done?",
        "col_type": "CHECKBOX",
        "index": 4,
    })
    payload = result.get("result") or result
    c = payload[0] if isinstance(payload, list) else payload
    state.column_ids["Done?"] = c["id"]
    return f"added Done? (CHECKBOX) -> id={c['id']}"


async def step_update_column_rename(client, state):
    col_id = state.column_ids["Notes"]
    await call_tool(client, "update_column", {
        "sheet_id": state.sheet_id,
        "column_id": col_id,
        "title": "Comments",
        "description": "Free-text notes about the task",
    })
    state.column_ids["Comments"] = state.column_ids.pop("Notes")
    return f"renamed Notes -> Comments (id={col_id})"


async def step_summary_after_columns(client, state):
    result = await call_tool(client, "get_sheet_summary", {"sheet_id": state.sheet_id})
    titles = [c["title"] for c in result["columns"]]
    expected = {"Task", "Status", "Comments", "Due Date", "Done?"}
    missing = expected - set(titles)
    if missing:
        raise RuntimeError(f"missing columns after adds: {missing}")
    return f"5 columns present: {', '.join(titles)}"


async def step_add_rows_single(client, state):
    result = await call_tool(client, "add_rows", {
        "sheet_id": state.sheet_id,
        "rows": [{"Task": "Write docs", "Status": "Open", "Comments": "First task"}],
    })
    rows = (result.get("result") or [])
    state.row_ids.extend(r["id"] for r in rows)
    return f"added 1 row -> id={rows[0]['id']}"


async def step_add_rows_batch(client, state):
    result = await call_tool(client, "add_rows", {
        "sheet_id": state.sheet_id,
        "rows": [
            {"Task": "Review PR", "Status": "Open", "Comments": "Needs review"},
            {"Task": "Deploy", "Status": "Open", "Comments": "Ship to prod"},
            {"Task": "Test", "Status": "Done", "Comments": "Verified"},
        ],
    })
    rows = (result.get("result") or [])
    state.row_ids.extend(r["id"] for r in rows)
    return f"added 3 rows in batch -> ids={[r['id'] for r in rows]}"


async def step_read_rows(client, state):
    result = await call_tool(client, "read_rows", {"sheet_id": state.sheet_id, "max_rows": 50})
    return f"read {len(result)} rows from sheet"


async def step_get_specific_row(client, state):
    rid = state.row_ids[0]
    result = await call_tool(client, "get_row", {"sheet_id": state.sheet_id, "row_id": rid})
    return f"fetched row id={rid} ({len(result.get('cells', []))} cells)"


async def step_update_rows(client, state):
    rid = state.row_ids[0]
    await call_tool(client, "update_rows", {
        "sheet_id": state.sheet_id,
        "updates": [{"rowId": rid, "cells": {"Status": {"value": "Done"}, "Comments": {"value": "Updated by smoke test"}}}],
    })
    return f"updated row id={rid} (Status=Done)"


async def step_get_cell_history(client, state):
    rid = state.row_ids[0]
    col_id = state.column_ids["Status"]
    result = await call_tool(client, "get_cell_history", {
        "sheet_id": state.sheet_id, "row_id": rid, "column_id": col_id,
    })
    return f"cell history for row {rid}/Status: {len(result)} entries"


async def step_sort_sheet(client, state):
    await call_tool(client, "sort_sheet", {
        "sheet_id": state.sheet_id,
        "sort_criteria": [{"columnId": state.column_ids["Task"], "direction": "ASCENDING"}],
    })
    return "sorted sheet by Task ASC"


async def step_delete_one_row(client, state):
    rid = state.row_ids.pop()  # last row
    await call_tool(client, "delete_rows", {"sheet_id": state.sheet_id, "row_ids": [rid]})
    return f"deleted row id={rid}"


async def step_delete_one_column(client, state):
    col_id = state.column_ids.pop("Done?")
    await call_tool(client, "delete_column", {"sheet_id": state.sheet_id, "column_id": col_id})
    return f"deleted column Done? (id={col_id})"


async def step_rename_sheet(client, state):
    new_name = f"FUNCTIONAL_TEST_RENAMED_{int(time.time())}"
    await call_tool(client, "rename_sheet", {"sheet_id": state.sheet_id, "new_name": new_name})
    return f"renamed sheet -> '{new_name}'"


async def step_list_sheets(client, state):
    result = await call_tool(client, "list_sheets", {})
    found = any(str(s.get("id")) == state.sheet_id for s in result)
    if not found:
        raise RuntimeError(f"test sheet {state.sheet_id} not found in list_sheets")
    return f"list_sheets returned {len(result)} sheets, our test sheet is present"


async def step_search_sheet(client, state):
    result = await call_tool(client, "search_sheet", {"sheet_id": state.sheet_id, "query": "Updated"})
    hits = result.get("results", []) if isinstance(result, dict) else result
    return f"search_sheet('Updated') -> {len(hits)} hits"


async def step_create_discussion(client, state):
    rid = state.row_ids[0]
    result = await call_tool(client, "create_row_discussion", {
        "sheet_id": state.sheet_id, "row_id": rid, "text": "Smoke test discussion thread",
    })
    payload = result.get("result") or result
    state.discussion_id = payload["id"]
    return f"created discussion on row {rid} -> id={state.discussion_id}"


async def step_list_row_discussions(client, state):
    rid = state.row_ids[0]
    result = await call_tool(client, "list_row_discussions", {"sheet_id": state.sheet_id, "row_id": rid})
    discs = result if isinstance(result, list) else result.get("data", [])
    return f"row {rid} has {len(discs)} discussion(s)"


async def step_add_comment(client, state):
    if not state.discussion_id:
        raise RuntimeError("no discussion_id from previous step")
    await call_tool(client, "add_comment", {
        "sheet_id": state.sheet_id,
        "discussion_id": state.discussion_id,
        "text": "Reply from smoke test runner",
    })
    return f"added comment to discussion {state.discussion_id}"


async def step_attach_url_sheet(client, state):
    result = await call_tool(client, "attach_url_to_sheet", {
        "sheet_id": state.sheet_id,
        "name": "Smoke test link",
        "url": "https://example.com/smoke-test",
    })
    payload = result.get("result") or result
    state.attachment_ids.append(payload["id"])
    return f"attached URL to sheet -> id={payload['id']}"


async def step_attach_url_row(client, state):
    rid = state.row_ids[0]
    result = await call_tool(client, "attach_url_to_row", {
        "sheet_id": state.sheet_id, "row_id": rid,
        "name": "Row link",
        "url": "https://example.com/row-attachment",
    })
    payload = result.get("result") or result
    state.attachment_ids.append(payload["id"])
    return f"attached URL to row {rid} -> id={payload['id']}"


async def step_list_attachments(client, state):
    result = await call_tool(client, "list_attachments", {"sheet_id": state.sheet_id})
    items = result if isinstance(result, list) else result.get("data", [])
    return f"sheet has {len(items)} attachment(s)"


async def step_list_row_attachments(client, state):
    rid = state.row_ids[0]
    result = await call_tool(client, "list_row_attachments", {"sheet_id": state.sheet_id, "row_id": rid})
    items = result if isinstance(result, list) else result.get("data", [])
    return f"row {rid} has {len(items)} attachment(s)"


async def step_list_workspaces(client, state):
    result = await call_tool(client, "list_workspaces", {})
    if result:
        state.workspace_id = result[0].get("id")
    return f"{len(result)} workspace(s) accessible"


async def step_list_webhooks(client, state):
    result = await call_tool(client, "list_webhooks", {})
    return f"{len(result)} webhook(s) registered"


async def step_list_reports(client, state):
    result = await call_tool(client, "list_reports", {})
    return f"{len(result)} report(s) accessible"


async def step_list_dashboards(client, state):
    result = await call_tool(client, "list_dashboards", {})
    return f"{len(result)} dashboard(s) accessible"


async def step_list_automations(client, state):
    result = await call_tool(client, "list_automations", {"sheet_id": state.sheet_id})
    return f"{len(result)} automation rule(s) on test sheet"


async def step_list_shares(client, state):
    result = await call_tool(client, "list_shares", {"sheet_id": state.sheet_id})
    items = result if isinstance(result, list) else result.get("data", [])
    return f"{len(items)} share(s) on test sheet"


async def step_detect_issues(client, state):
    result = await call_tool(client, "detect_issues", {"sheet_id": state.sheet_id})
    n = result.get("total_issues", 0)
    return f"detect_issues -> {n} issue(s) found"


async def step_get_current_user(client, state):
    result = await call_tool(client, "get_current_user", {})
    return f"authenticated as {result.get('email', '?')} ({result.get('id', '?')})"


# ---------------------------------------------------------------------------
# The ordered list of all steps (section, name, tool used, fn)
# ---------------------------------------------------------------------------

STEPS: list[tuple[str, str, str, "asyncio.coroutines"]] = [
    ("Account",     "Get current user",                 "get_current_user",       step_get_current_user),
    ("Setup",       "Create FUNCTIONAL_TEST sheet",     "create_sheet",           step_create_sheet),
    ("Setup",       "Get sheet summary (initial)",      "get_sheet_summary",      step_get_summary),

    ("Columns",     "Add column TEXT_NUMBER",           "add_column",             step_add_column_text),
    ("Columns",     "Add column DATE",                  "add_column",             step_add_column_date),
    ("Columns",     "Add column CHECKBOX",              "add_column",             step_add_column_checkbox),
    ("Columns",     "Update column (rename)",           "update_column",          step_update_column_rename),
    ("Columns",     "Verify columns after adds",        "get_sheet_summary",      step_summary_after_columns),

    ("Rows",        "Add 1 row",                        "add_rows",               step_add_rows_single),
    ("Rows",        "Add 3 rows (batch)",               "add_rows",               step_add_rows_batch),
    ("Rows",        "Read all rows",                    "read_rows",              step_read_rows),
    ("Rows",        "Get a specific row",               "get_row",                step_get_specific_row),
    ("Rows",        "Update first row",                 "update_rows",            step_update_rows),
    ("Rows",        "Get cell history",                 "get_cell_history",       step_get_cell_history),
    ("Rows",        "Sort by Task ASC",                 "sort_sheet",             step_sort_sheet),

    ("Search",      "Search inside sheet",              "search_sheet",           step_search_sheet),
    ("Search",      "List all sheets (find ours)",      "list_sheets",            step_list_sheets),

    ("Discussions", "Create row discussion",            "create_row_discussion",  step_create_discussion),
    ("Discussions", "List row discussions",             "list_row_discussions",   step_list_row_discussions),
    ("Discussions", "Add comment to discussion",        "add_comment",            step_add_comment),

    ("Attachments", "Attach URL to sheet",              "attach_url_to_sheet",    step_attach_url_sheet),
    ("Attachments", "Attach URL to row",                "attach_url_to_row",      step_attach_url_row),
    ("Attachments", "List sheet attachments",           "list_attachments",       step_list_attachments),
    ("Attachments", "List row attachments",             "list_row_attachments",   step_list_row_attachments),

    ("Misc",        "List workspaces",                  "list_workspaces",        step_list_workspaces),
    ("Misc",        "List webhooks",                    "list_webhooks",          step_list_webhooks),
    ("Misc",        "List reports",                     "list_reports",           step_list_reports),
    ("Misc",        "List dashboards",                  "list_dashboards",        step_list_dashboards),
    ("Misc",        "List automations on sheet",        "list_automations",       step_list_automations),
    ("Misc",        "List shares on sheet",             "list_shares",            step_list_shares),
    ("Misc",        "Detect issues on sheet",           "detect_issues",          step_detect_issues),

    ("Cleanup-ish", "Delete one row",                   "delete_rows",            step_delete_one_row),
    ("Cleanup-ish", "Delete one column",                "delete_column",          step_delete_one_column),
    ("Cleanup-ish", "Rename test sheet",                "rename_sheet",           step_rename_sheet),
]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run() -> int:
    token = (os.getenv("SMARTSHEET_TOKEN") or "").strip()
    if not token:
        print(f"{C.RED}SMARTSHEET_TOKEN not found in .env or environment.{C.RESET}")
        return 2

    banner("Smartsheet Controller — Functional Smoke Runner")
    print(f"  Started at: {datetime.now().isoformat(timespec='seconds')}")
    print(f"  Token:      {token[:6]}…{token[-4:]} ({len(token)} chars)")
    print(f"  Steps:      {len(STEPS)} planned")

    state = State()
    results: list[StepResult] = []

    client = SmartsheetClient(token)
    current_section = ""
    t0 = time.monotonic()

    try:
        for idx, (sec, name, tool, fn) in enumerate(STEPS, start=1):
            if sec != current_section:
                section(sec)
                current_section = sec

            step_log(idx, len(STEPS), name, tool)

            # Sheet-dependent steps must be skipped if the sheet was never created
            if tool != "create_sheet" and tool != "list_sheets" and tool != "list_workspaces" \
                    and tool != "list_webhooks" and tool != "list_reports" \
                    and tool != "list_dashboards" and tool != "get_current_user" \
                    and not state.sheet_id:
                status_skip("test sheet was not created — skipping")
                results.append(StepResult(idx, name, tool, "SKIP", 0, "no sheet"))
                continue

            t_start = time.monotonic()
            try:
                detail = await fn(client, state)
                ms = int((time.monotonic() - t_start) * 1000)
                status_pass(detail, ms)
                results.append(StepResult(idx, name, tool, "PASS", ms, detail))
            except Exception as exc:
                ms = int((time.monotonic() - t_start) * 1000)
                err = str(exc)
                status_fail(err, ms)
                results.append(StepResult(idx, name, tool, "FAIL", ms, "", err))
    finally:
        try:
            await client.close()
        except Exception:
            pass

    total_ms = int((time.monotonic() - t0) * 1000)

    # ---- Summary ----------------------------------------------------------
    banner("SUMMARY")
    n_pass = sum(1 for r in results if r.status == "PASS")
    n_fail = sum(1 for r in results if r.status == "FAIL")
    n_skip = sum(1 for r in results if r.status == "SKIP")
    print(f"  {C.GREEN}PASS: {n_pass}{C.RESET}   {C.RED}FAIL: {n_fail}{C.RESET}   {C.YELLOW}SKIP: {n_skip}{C.RESET}   Total time: {total_ms} ms")

    if n_fail:
        print(f"\n{C.RED}{C.BOLD}Failures:{C.RESET}")
        for r in results:
            if r.status == "FAIL":
                print(f"  - [{r.idx:02d}] {r.name} ({r.tool}): {r.error}")

    # ---- Where to inspect -------------------------------------------------
    if state.sheet_id:
        banner("TEST SHEET KEPT — open it to inspect every artifact")
        print(f"  Sheet ID:  {C.BOLD}{state.sheet_id}{C.RESET}")
        if state.sheet_permalink:
            print(f"  URL:       {C.CYAN}{state.sheet_permalink}{C.RESET}")
        print(f"  Columns:   {', '.join(state.column_ids.keys())}")
        print(f"  Rows kept: {len(state.row_ids)}")
        print(f"  Discussion id: {state.discussion_id}")
        print(f"  Attachments:   {state.attachment_ids}")
        print()
        print(f"  {C.DIM}To delete it later:{C.RESET}")
        print(f"  {C.DIM}  curl -X DELETE -H \"Authorization: Bearer $SMARTSHEET_TOKEN\" \\{C.RESET}")
        print(f"  {C.DIM}    https://api.smartsheet.com/2.0/sheets/{state.sheet_id}{C.RESET}")

    # ---- Persist a JSON report next to the script -------------------------
    report_path = Path(__file__).parent / f"smoke_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "total_ms": total_ms,
        "totals": {"pass": n_pass, "fail": n_fail, "skip": n_skip},
        "sheet_id": state.sheet_id,
        "sheet_url": state.sheet_permalink,
        "steps": [
            {"idx": r.idx, "name": r.name, "tool": r.tool, "status": r.status,
             "ms": r.ms, "detail": r.detail, "error": r.error}
            for r in results
        ],
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  Full JSON report saved: {C.DIM}{report_path}{C.RESET}\n")

    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    # Enable ANSI colors on Windows terminals that opt in
    if sys.platform == "win32":
        try:
            import colorama
            colorama.just_fix_windows_console()
        except Exception:
            pass
    rc = asyncio.run(run())
    sys.exit(rc)
