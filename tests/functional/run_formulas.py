"""Formula coverage runner — tests Smartsheet's formula engine on the test sheet.

Run from project root:

    python tests/functional/run_formulas.py
    python tests/functional/run_formulas.py --sheet-id 5555264947179396
    python tests/functional/run_formulas.py --no-clean   # keep previous test rows

What it does
------------
1. Picks the test sheet (defaults to the most recent FUNCTIONAL_TEST sheet
   found in ``tests/functional/smoke_report_*.json``; you can override with
   ``--sheet-id``).
2. Adds 7 dedicated columns the first time it runs:
   Function, Category, Description, Formula, Formula_check, Formula_date,
   Expected. The latter two have CHECKBOX and DATE types so Smartsheet can
   accept boolean and date results without coercion errors.
3. Cleans up any previous formula-test rows (rows whose Function column has
   a value) so re-runs stay tidy.
4. Inserts ~55 rows, each row testing ONE Smartsheet function or operator
   with a self-contained formula (literal arguments — no cross-row refs).
5. Waits for Smartsheet to evaluate the formulas server-side.
6. Reads the rows back, compares each computed cell to the expected value,
   and prints PASS / FAIL per function with a per-category summary.
7. Saves a JSON report alongside the script.

The test sheet is **kept** so you can open it in Smartsheet and visually
review every formula and its computed value.
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from backend.smartsheet_client import SmartsheetClient
from backend.tools import execute_tool


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------

class C:
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    GREEN = "\033[32m"
    RED = "\033[31m"
    YELLOW = "\033[33m"
    CYAN = "\033[36m"
    MAGENTA = "\033[35m"
    GREY = "\033[90m"


def banner(text: str) -> None:
    line = "=" * 78
    print(f"\n{C.BOLD}{C.CYAN}{line}\n  {text}\n{line}{C.RESET}")


def section(text: str) -> None:
    print(f"\n{C.BOLD}{C.MAGENTA}--- {text} ---{C.RESET}")


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

# result_type tells us which column the formula writes into:
#   "text" -> Formula        (TEXT_NUMBER)
#   "bool" -> Formula_check  (CHECKBOX)
#   "date" -> Formula_date   (DATE)


@dataclass
class FormulaTest:
    func: str
    category: str
    description: str
    formula: str
    expected: str | None       # None = "any non-error result counts"
    result_type: str = "text"  # "text" | "bool" | "date"


TESTS: list[FormulaTest] = [
    # =================================================================== MATH
    # Built-in functions confirmed available in Smartsheet's formula engine
    FormulaTest("SUM",       "Math", "Sum of three numbers",         "=SUM(1, 2, 3)",         "6"),
    FormulaTest("AVG",       "Math", "Average of three numbers",     "=AVG(2, 4, 6)",         "4"),
    FormulaTest("MIN",       "Math", "Smallest of three",            "=MIN(5, 2, 8)",         "2"),
    FormulaTest("MAX",       "Math", "Largest of three",             "=MAX(5, 2, 8)",         "8"),
    FormulaTest("COUNT",     "Math", "Count of four numbers",        "=COUNT(1, 2, 3, 4)",    "4"),
    FormulaTest("ABS",       "Math", "Absolute value of -7",         "=ABS(-7)",              "7"),
    FormulaTest("INT",       "Math", "Integer part of 3.7",          "=INT(3.7)",             "3"),
    FormulaTest("ROUND",     "Math", "Round 3.456 to 2 decimals",    "=ROUND(3.456, 2)",      "3.46"),
    FormulaTest("ROUNDUP",   "Math", "Round up 3.1",                 "=ROUNDUP(3.1, 0)",      "4"),
    FormulaTest("ROUNDDOWN", "Math", "Round down 3.9",               "=ROUNDDOWN(3.9, 0)",    "3"),
    FormulaTest("MOD",       "Math", "Remainder of 10 / 3",          "=MOD(10, 3)",           "1"),
    FormulaTest("CEILING",   "Math", "Round 3.1 up to nearest 1",    "=CEILING(3.1, 1)",      "4"),
    FormulaTest("FLOOR",     "Math", "Round 3.9 down to nearest 1",  "=FLOOR(3.9, 1)",        "3"),
    FormulaTest("MEDIAN",    "Math", "Middle value of 1..5",         "=MEDIAN(1, 2, 3, 4, 5)", "3"),

    # ---- Operators (Smartsheet has no POWER/SQRT/PI; use ^ and arithmetic)
    FormulaTest("op_plus",   "Math", "Operator: 2 + 3",              "=2 + 3",                "5"),
    FormulaTest("op_minus",  "Math", "Operator: 5 - 2",              "=5 - 2",                "3"),
    FormulaTest("op_times",  "Math", "Operator: 4 * 3",              "=4 * 3",                "12"),
    FormulaTest("op_divide", "Math", "Operator: 10 / 2",             "=10 / 2",               "5"),
    FormulaTest("op_power",  "Math", "Power via ^: 2 ^ 10",          "=2 ^ 10",               "1024"),
    FormulaTest("op_sqrt",   "Math", "Sqrt via ^0.5: 16 ^ 0.5",      "=16 ^ 0.5",             "4"),

    # ================================================================ LOGICAL
    # Functions returning text -> Formula column
    FormulaTest("IF_true",     "Logical", "IF true branch",           '=IF(1 = 1, "yes", "no")',  "yes"),
    FormulaTest("IF_false",    "Logical", "IF false branch",          '=IF(1 > 2, "yes", "no")',  "no"),
    FormulaTest("IF_nested",   "Logical", "Nested IF (Smartsheet's IFS equivalent)", '=IF(1 = 2, "a", IF(1 = 1, "b", "c"))', "b"),
    FormulaTest("IFERROR_div", "Logical", "IFERROR catches divide-by-zero", '=IFERROR(1/0, "err")', "err"),
    FormulaTest("IFERROR_ok",  "Logical", "IFERROR passthrough",      '=IFERROR(10/2, "err")',    "5"),

    # Functions returning boolean -> Formula_check (CHECKBOX) column
    FormulaTest("AND_TT",      "Logical", "AND(TRUE, TRUE)",          "=AND(TRUE, TRUE)",   "true",  "bool"),
    FormulaTest("AND_TF",      "Logical", "AND(TRUE, FALSE)",         "=AND(TRUE, FALSE)",  "false", "bool"),
    FormulaTest("OR_FT",       "Logical", "OR(FALSE, TRUE)",          "=OR(FALSE, TRUE)",   "true",  "bool"),
    FormulaTest("OR_FF",       "Logical", "OR(FALSE, FALSE)",         "=OR(FALSE, FALSE)",  "false", "bool"),
    FormulaTest("NOT_T",       "Logical", "NOT(TRUE)",                "=NOT(TRUE)",         "false", "bool"),
    FormulaTest("NOT_F",       "Logical", "NOT(FALSE)",               "=NOT(FALSE)",        "true",  "bool"),
    FormulaTest("ISNUMBER_T",  "Logical", "ISNUMBER on a number",     "=ISNUMBER(42)",      "true",  "bool"),
    FormulaTest("ISNUMBER_F",  "Logical", "ISNUMBER on a string",     '=ISNUMBER("hi")',    "false", "bool"),
    FormulaTest("ISTEXT",      "Logical", "ISTEXT on a string",       '=ISTEXT("hello")',   "true",  "bool"),
    FormulaTest("ISBOOLEAN",   "Logical", "ISBOOLEAN on TRUE",        "=ISBOOLEAN(TRUE)",   "true",  "bool"),
    FormulaTest("ISDATE",      "Logical", "ISDATE on TODAY()",        "=ISDATE(TODAY())",   "true",  "bool"),
    FormulaTest("ISBLANK",     "Logical", "ISBLANK on empty string",  '=ISBLANK("")',       None,    "bool"),
    FormulaTest("ISERROR",     "Logical", "ISERROR on 1/0",           "=ISERROR(1/0)",      "true",  "bool"),

    # =================================================================== TEXT
    FormulaTest("LEN",         "Text", "Length of 'hello'",            '=LEN("hello")',                  "5"),
    FormulaTest("UPPER",       "Text", "UPPER('hi')",                  '=UPPER("hi")',                   "HI"),
    FormulaTest("LOWER",       "Text", "LOWER('HI')",                  '=LOWER("HI")',                   "hi"),
    FormulaTest("LEFT",        "Text", "LEFT('hello', 3)",             '=LEFT("hello", 3)',              "hel"),
    FormulaTest("RIGHT",       "Text", "RIGHT('hello', 3)",            '=RIGHT("hello", 3)',             "llo"),
    FormulaTest("MID",         "Text", "MID('hello', 2, 3)",           '=MID("hello", 2, 3)',            "ell"),
    FormulaTest("FIND",        "Text", "FIND 'l' in 'hello'",          '=FIND("l", "hello")',            "3"),
    FormulaTest("SUBSTITUTE",  "Text", "SUBSTITUTE l -> L in 'hello'", '=SUBSTITUTE("hello", "l", "L")', "heLLo"),
    FormulaTest("REPLACE",     "Text", "REPLACE 3 chars at pos 2",     '=REPLACE("hello", 2, 3, "x")',   "hxo"),
    FormulaTest("VALUE",       "Text", "VALUE('123')",                 '=VALUE("123")',                  "123"),
    FormulaTest("CHAR",        "Text", "CHAR(65)",                     "=CHAR(65)",                      "A"),
    FormulaTest("op_concat",   "Text", "Concat via +: 'a' + 'b' + 'c'", '="a" + "b" + "c"',              "abc"),
    FormulaTest("CONTAINS",    "Text", "CONTAINS('ell', 'hello')",     '=CONTAINS("ell", "hello")',     "true", "bool"),

    # =================================================================== DATE
    # Numeric extractions (text result)
    FormulaTest("YEAR",        "Date", "YEAR of 2024-06-15",        "=YEAR(DATE(2024, 6, 15))",        "2024"),
    FormulaTest("MONTH",       "Date", "MONTH of 2024-06-15",       "=MONTH(DATE(2024, 6, 15))",       "6"),
    FormulaTest("DAY",         "Date", "DAY of 2024-06-15",         "=DAY(DATE(2024, 6, 15))",         "15"),
    FormulaTest("WEEKDAY",     "Date", "WEEKDAY 2024-01-01 (Mon)",  "=WEEKDAY(DATE(2024, 1, 1))",      "2"),  # Sun=1
    FormulaTest("WEEKNUMBER",  "Date", "WEEKNUMBER 2024-01-01",     "=WEEKNUMBER(DATE(2024, 1, 1))",   "1"),
    FormulaTest("NETWORKDAYS", "Date", "Workdays Jan 1..5 2024",    "=NETWORKDAYS(DATE(2024,1,1), DATE(2024,1,5))", "5"),
    FormulaTest("NETDAYS",     "Date", "Total calendar days (inclusive)", "=NETDAYS(DATE(2024,1,1), DATE(2024,1,5))", "5"),

    # Functions returning a date object -> Formula_date (DATE) column
    FormulaTest("TODAY",       "Date", "TODAY() returns today",     "=TODAY()",                        None,                "date"),
    FormulaTest("DATE",        "Date", "DATE(2024, 1, 1)",          "=DATE(2024, 1, 1)",               "2024-01-01",        "date"),
    FormulaTest("WORKDAY",     "Date", "5 workdays after 2024-01-01", "=WORKDAY(DATE(2024,1,1), 5)",   "2024-01-08",        "date"),
]


# ---------------------------------------------------------------------------
# Comparison logic
# ---------------------------------------------------------------------------

ERROR_MARKERS = ("#INVALID", "#UNPARSEABLE", "#DIVIDEBYZERO", "#NAME?",
                 "#REF!", "#NO MATCH", "#CIRCULAR", "#INCORRECT")


def is_error_value(s: str) -> bool:
    if not s:
        return False
    up = s.upper().strip()
    return any(up.startswith(m) for m in ERROR_MARKERS)


def normalize(s: str) -> str:
    return (s or "").strip().lower()


def values_match(actual: str, expected: str) -> bool:
    a = normalize(actual)
    e = normalize(expected)
    if a == e:
        return True
    # Numeric comparison with tolerance
    try:
        af = float(a.replace(",", ".").replace(" ", ""))
        ef = float(e.replace(",", ".").replace(" ", ""))
        return abs(af - ef) < 0.001 or abs(af - ef) / max(abs(ef), 1e-9) < 0.001
    except (ValueError, ZeroDivisionError):
        return False


# ---------------------------------------------------------------------------
# Sheet ID resolution
# ---------------------------------------------------------------------------

def latest_smoke_sheet_id() -> str | None:
    reports = sorted(glob.glob(str(Path(__file__).parent / "smoke_report_*.json")))
    if not reports:
        return None
    try:
        data = json.loads(Path(reports[-1]).read_text(encoding="utf-8"))
        sid = data.get("sheet_id")
        return str(sid) if sid else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Helpers calling our existing tool dispatch
# ---------------------------------------------------------------------------

async def call(client: SmartsheetClient, tool: str, args: dict) -> dict:
    raw = await execute_tool(client, tool, args)
    data = json.loads(raw)
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(data["error"])
    return data


COLUMN_SPECS = [
    ("Function",      "TEXT_NUMBER"),
    ("Category",      "TEXT_NUMBER"),
    ("Description",   "TEXT_NUMBER"),
    ("Formula",       "TEXT_NUMBER"),
    ("Formula_check", "CHECKBOX"),
    ("Formula_date",  "DATE"),
    ("Expected",      "TEXT_NUMBER"),
]


async def ensure_columns(client: SmartsheetClient, sheet_id: str) -> dict[str, int]:
    """Make sure all formula-test columns exist. Idempotent."""
    summary = await call(client, "get_sheet_summary", {"sheet_id": sheet_id})
    existing = {c["title"]: c["id"] for c in summary["columns"]}
    next_index = summary["columnCount"]

    for title, col_type in COLUMN_SPECS:
        if title in existing:
            continue
        result = await call(client, "add_column", {
            "sheet_id": sheet_id,
            "title": title,
            "col_type": col_type,
            "index": next_index,
        })
        payload = result.get("result") or result
        cols = payload if isinstance(payload, list) else [payload]
        existing[title] = cols[0]["id"]
        next_index += 1
        print(f"   {C.GREEN}+{C.RESET} added column {C.BOLD}{title}{C.RESET} "
              f"({col_type}, id={cols[0]['id']})")
    return existing


async def cleanup_previous_test_rows(client: SmartsheetClient, sheet_id: str) -> int:
    """Delete rows whose Function column has a value (i.e. previous formula tests)."""
    sheet = await client.get_sheet(sheet_id, page_size=500)
    cols_by_id = {c["id"]: c["title"] for c in sheet.get("columns", [])}
    func_col = next((cid for cid, t in cols_by_id.items() if t == "Function"), None)
    if not func_col:
        return 0
    to_delete = []
    for row in sheet.get("rows", []):
        for cell in row.get("cells", []):
            if cell.get("columnId") == func_col and cell.get("value"):
                to_delete.append(row["id"])
                break
    if not to_delete:
        return 0
    # delete_rows accepts up to ~500 IDs per call; one batch is plenty here
    await call(client, "delete_rows", {"sheet_id": sheet_id, "row_ids": to_delete})
    return len(to_delete)


def _result_col_for(test: FormulaTest) -> str:
    return {"text": "Formula", "bool": "Formula_check", "date": "Formula_date"}[test.result_type]


async def insert_formula_rows(client: SmartsheetClient, sheet_id: str) -> list[int]:
    rows = []
    for t in TESTS:
        rows.append({
            "Function":      t.func,
            "Category":      t.category,
            "Description":   t.description,
            _result_col_for(t): {"formula": t.formula},
            "Expected":      t.expected if t.expected is not None else "",
        })

    BATCH = 100
    new_ids: list[int] = []
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        result = await call(client, "add_rows", {"sheet_id": sheet_id, "rows": chunk})
        for r in result.get("result", []):
            new_ids.append(r["id"])
        print(f"   inserted batch {i // BATCH + 1}: {len(chunk)} rows "
              f"({len(new_ids)}/{len(rows)} total)")
    return new_ids


async def read_back_results(client: SmartsheetClient, sheet_id: str,
                            row_ids: list[int]) -> dict[int, dict[str, str]]:
    sheet = await client.get_sheet(sheet_id, page_size=500)
    cols_by_id = {c["id"]: c["title"] for c in sheet.get("columns", [])}
    wanted = set(row_ids)

    out: dict[int, dict[str, str]] = {}
    for row in sheet.get("rows", []):
        if row["id"] not in wanted:
            continue
        cells: dict[str, str] = {}
        for cell in row.get("cells", []):
            title = cols_by_id.get(cell.get("columnId"), "?")
            val = cell.get("displayValue")
            if val is None:
                v = cell.get("value")
                if isinstance(v, bool):
                    val = "true" if v else "false"
                elif v is None:
                    val = ""
                else:
                    val = str(v)
            cells[title] = "" if val is None else str(val)
        out[row["id"]] = cells
    return out


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run(sheet_id: str | None, do_clean: bool) -> int:
    token = (os.getenv("SMARTSHEET_TOKEN") or "").strip()
    if not token:
        print(f"{C.RED}SMARTSHEET_TOKEN not found in .env or environment.{C.RESET}")
        return 2

    if not sheet_id:
        sheet_id = latest_smoke_sheet_id()
        if not sheet_id:
            print(f"{C.RED}No --sheet-id given and no recent smoke_report_*.json found.{C.RESET}")
            print(f"Run {C.BOLD}python tests/functional/run_smoke.py{C.RESET} first, or pass --sheet-id.")
            return 2

    banner("Smartsheet Controller — Formula Coverage Runner")
    print(f"  Sheet ID:  {C.BOLD}{sheet_id}{C.RESET}")
    print(f"  Tests:     {len(TESTS)} planned across "
          f"{len({t.category for t in TESTS})} categories")
    print(f"  Started:   {datetime.now().isoformat(timespec='seconds')}")

    client = SmartsheetClient(token)
    t0 = time.monotonic()
    report_rows: list[dict] = []
    per_cat_pass: dict[str, int] = defaultdict(int)
    per_cat_fail: dict[str, int] = defaultdict(int)
    per_cat_skip: dict[str, int] = defaultdict(int)

    try:
        # ---------- 1. Ensure columns exist
        section("1. Ensure formula-test columns exist")
        col_ids = await ensure_columns(client, sheet_id)
        print(f"   columns ready: {', '.join(col_ids)}")

        # ---------- 2. Cleanup previous test rows
        if do_clean:
            section("2. Clean up previous formula-test rows")
            removed = await cleanup_previous_test_rows(client, sheet_id)
            print(f"   removed {removed} previous test row(s)")
        else:
            section("2. Skipping cleanup (--no-clean)")

        # ---------- 3. Insert formula rows
        section("3. Insert formula rows")
        row_ids = await insert_formula_rows(client, sheet_id)

        # ---------- 4. Wait for Smartsheet to evaluate
        section("4. Wait for server-side evaluation")
        for sec in range(3, 0, -1):
            print(f"   waiting {sec}s for formula evaluation...")
            await asyncio.sleep(1)

        # ---------- 5. Read back and compare
        section("5. Read back and compare to expected")
        results = await read_back_results(client, sheet_id, row_ids)
        print(f"   read {len(results)} rows back from Smartsheet\n")

        for test, rid in zip(TESTS, row_ids):
            cells = results.get(rid, {})
            actual = cells.get(_result_col_for(test), "")
            expected = test.expected
            status: str
            note: str

            if is_error_value(actual):
                status = "FAIL"
                note = f"engine error: {actual}"
            elif expected is None:
                if actual.strip() == "":
                    status = "FAIL"
                    note = "empty result"
                else:
                    status = "PASS"
                    note = f"non-error value: {actual!r}"
            elif values_match(actual, expected):
                status = "PASS"
                note = f"actual={actual!r}"
            else:
                status = "FAIL"
                note = f"expected={expected!r} got={actual!r}"

            color = C.GREEN if status == "PASS" else (C.YELLOW if status == "SKIP" else C.RED)
            label = f"[{test.category:<7}] {test.func:<14}"
            print(f"   {color}{status}{C.RESET}  {label} {C.DIM}{note}{C.RESET}")

            if status == "PASS":
                per_cat_pass[test.category] += 1
            elif status == "FAIL":
                per_cat_fail[test.category] += 1
            else:
                per_cat_skip[test.category] += 1

            report_rows.append({
                "function": test.func,
                "category": test.category,
                "description": test.description,
                "formula": test.formula,
                "result_type": test.result_type,
                "expected": expected,
                "actual": actual,
                "status": status,
                "note": note,
                "row_id": rid,
            })

    finally:
        try:
            await client.close()
        except Exception:
            pass

    total_ms = int((time.monotonic() - t0) * 1000)

    # ---------- 6. Summary
    banner("SUMMARY")
    cats = sorted({t.category for t in TESTS})
    print(f"  {'Category':<10} {'PASS':>5}  {'FAIL':>5}  {'SKIP':>5}")
    print(f"  {'-' * 10} {'-' * 5}  {'-' * 5}  {'-' * 5}")
    total_pass = total_fail = total_skip = 0
    for cat in cats:
        p, f, s = per_cat_pass[cat], per_cat_fail[cat], per_cat_skip[cat]
        total_pass += p; total_fail += f; total_skip += s
        line_color = C.RED if f else C.GREEN
        print(f"  {line_color}{cat:<10}{C.RESET} {p:>5}  {f:>5}  {s:>5}")
    print(f"  {'-' * 10} {'-' * 5}  {'-' * 5}  {'-' * 5}")
    print(f"  {C.BOLD}{'TOTAL':<10}{C.RESET} {C.GREEN}{total_pass:>5}{C.RESET}  "
          f"{C.RED}{total_fail:>5}{C.RESET}  {C.YELLOW}{total_skip:>5}{C.RESET}    "
          f"({total_ms} ms)")

    if total_fail:
        print(f"\n{C.RED}{C.BOLD}Failed functions (open the sheet to inspect):{C.RESET}")
        for r in report_rows:
            if r["status"] == "FAIL":
                print(f"  - [{r['category']}] {r['function']}: {r['note']}")

    # ---------- 7. Where to inspect
    banner("TEST SHEET KEPT — open it to inspect every formula")
    print(f"  Sheet ID: {C.BOLD}{sheet_id}{C.RESET}")
    print(f"  Direct:   {C.CYAN}https://app.smartsheet.com/sheets/{sheet_id}{C.RESET}")
    print(f"  {C.DIM}(Smartsheet may take a few seconds to refresh in your browser){C.RESET}")

    # ---------- 8. JSON report
    report_path = Path(__file__).parent / f"formula_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "sheet_id": sheet_id,
        "total_ms": total_ms,
        "totals": {"pass": total_pass, "fail": total_fail, "skip": total_skip},
        "per_category": {
            cat: {"pass": per_cat_pass[cat], "fail": per_cat_fail[cat], "skip": per_cat_skip[cat]}
            for cat in cats
        },
        "tests": report_rows,
    }
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  Full JSON report saved: {C.DIM}{report_path}{C.RESET}\n")

    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    if sys.platform == "win32":
        try:
            import colorama
            colorama.just_fix_windows_console()
        except Exception:
            pass

    parser = argparse.ArgumentParser(description="Run Smartsheet formula coverage tests.")
    parser.add_argument("--sheet-id", help="Sheet ID to test on. If omitted, uses the most recent smoke_report_*.json.")
    parser.add_argument("--no-clean", action="store_true",
                        help="Don't delete previous formula-test rows before inserting.")
    args = parser.parse_args()

    rc = asyncio.run(run(args.sheet_id, do_clean=not args.no_clean))
    sys.exit(rc)
