"""Cross-sheet formula coverage runner.

Run from project root:

    python tests/functional/run_cross_sheet.py

What it does
------------
1. Creates TWO disposable sheets in your default Smartsheet "Sheets" home:
   - XSHEET_SOURCE_<ts>  : a deterministic 10-row catalogue of fake products
                           (SKU, Name, Category, Price, Stock, Active, AddedOn).
   - XSHEET_TARGET_<ts>  : the test sheet where formula rows live.
2. Creates 8 cross-sheet references on TARGET that point to SOURCE columns
   (one per source column + one multi-column range "xref_All" used by
   VLOOKUP / INDEX 2D).
3. Inserts ~35 formula rows on TARGET, each calling a single formula that
   pulls data from SOURCE through the named refs. Coverage:
     - Aggregation (SUM, AVG, MIN, MAX, COUNT, COUNTA, MEDIAN, LARGE, SMALL)
     - Conditional aggregation (SUMIF, SUMIFS, COUNTIF, COUNTIFS, AVERAGEIF,
       AVERAGEIFS, MAXIFS, MINIFS) including @cell criteria
     - Lookups (VLOOKUP, INDEX/MATCH, INDEX/COLLECT mono and multi-criteria)
     - Existence (IF + COUNTIFS)
     - Distinct / set ops
     - Date math (MAX/MIN/YEAR/MONTH on cross-sheet date column)
     - JOIN concat
     - Multi-column range slicing (INDEX 2D)
4. Waits, then reads each row back, compares to the expected value, and
   prints PASS / FAIL per category.

BOTH SHEETS ARE KEPT after the run so you can open them in Smartsheet and
visually inspect every formula.

Override sheet creation by passing existing IDs:

    python tests/functional/run_cross_sheet.py \\
        --source-id 123456 --target-id 654321
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from backend.smartsheet_client import SmartsheetClient


# ────────────────────────────── Pretty printing ──────────────────────────────

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


# ────────────────────────────── Source data ──────────────────────────────────
#
# Deterministic fake product catalogue. Computed expectations below depend
# on these exact values — DO NOT change without updating the TESTS table.

SOURCE_COLUMNS = [
    {"title": "SKU",      "type": "TEXT_NUMBER", "primary": True},
    {"title": "Name",     "type": "TEXT_NUMBER"},
    {"title": "Category", "type": "TEXT_NUMBER"},
    {"title": "Price",    "type": "TEXT_NUMBER"},
    {"title": "Stock",    "type": "TEXT_NUMBER"},
    {"title": "Active",   "type": "CHECKBOX"},
    {"title": "AddedOn",  "type": "DATE"},
]

SOURCE_ROWS: list[dict] = [
    {"SKU": "A001", "Name": "Widget A",    "Category": "Tools",   "Price": 10,  "Stock": 50,  "Active": True,  "AddedOn": "2026-01-01"},
    {"SKU": "A002", "Name": "Widget B",    "Category": "Tools",   "Price": 20,  "Stock": 30,  "Active": True,  "AddedOn": "2026-01-15"},
    {"SKU": "A003", "Name": "Widget C",    "Category": "Tools",   "Price": 15,  "Stock": 0,   "Active": False, "AddedOn": "2026-02-01"},
    {"SKU": "B001", "Name": "Gizmo X",     "Category": "Gadgets", "Price": 100, "Stock": 5,   "Active": True,  "AddedOn": "2026-02-10"},
    {"SKU": "B002", "Name": "Gizmo Y",     "Category": "Gadgets", "Price": 200, "Stock": 10,  "Active": False, "AddedOn": "2026-03-01"},
    {"SKU": "C001", "Name": "Doohickey 1", "Category": "Misc",    "Price": 5,   "Stock": 100, "Active": True,  "AddedOn": "2026-03-15"},
    {"SKU": "C002", "Name": "Doohickey 2", "Category": "Misc",    "Price": 7,   "Stock": 80,  "Active": True,  "AddedOn": "2026-04-01"},
    {"SKU": "C003", "Name": "Doohickey 3", "Category": "Misc",    "Price": 3,   "Stock": 0,   "Active": False, "AddedOn": "2026-04-15"},
    {"SKU": "D001", "Name": "Premium 1",   "Category": "Premium", "Price": 500, "Stock": 2,   "Active": True,  "AddedOn": "2026-04-20"},
    {"SKU": "D002", "Name": "Premium 2",   "Category": "Premium", "Price": 750, "Stock": 1,   "Active": True,  "AddedOn": "2026-04-22"},
]


# ──────────────────────────── Target sheet schema ────────────────────────────

TARGET_COLUMNS = [
    {"title": "Function",      "type": "TEXT_NUMBER", "primary": True},
    {"title": "Category",      "type": "TEXT_NUMBER"},
    {"title": "Description",   "type": "TEXT_NUMBER"},
    {"title": "Formula",       "type": "TEXT_NUMBER"},  # text/number results
    {"title": "Formula_check", "type": "CHECKBOX"},     # boolean results
    {"title": "Formula_date",  "type": "DATE"},         # date results
    {"title": "Expected",      "type": "TEXT_NUMBER"},
]


# ─────────────────────────── Cross-sheet refs plan ───────────────────────────
#
# (ref_name, source_column_title_start, source_column_title_end)
# For single-column refs, start == end. For the "all columns" range, we span
# from the first to the last source column.

REFS_PLAN = [
    ("xref_SKU",      "SKU",      "SKU"),
    ("xref_Name",     "Name",     "Name"),
    ("xref_Category", "Category", "Category"),
    ("xref_Price",    "Price",    "Price"),
    ("xref_Stock",    "Stock",    "Stock"),
    ("xref_Active",   "Active",   "Active"),
    ("xref_AddedOn",  "AddedOn",  "AddedOn"),
    ("xref_All",      "SKU",      "AddedOn"),  # 7-column horizontal range
]


# ────────────────────────────── Test cases ───────────────────────────────────
#
# result_type drives the column the formula writes into:
#   "text" -> Formula        (TEXT_NUMBER)
#   "bool" -> Formula_check  (CHECKBOX)
#   "date" -> Formula_date   (DATE)
# `expected = None` means "any non-error result counts as PASS".

@dataclass
class FormulaTest:
    func: str
    category: str
    description: str
    formula: str
    expected: str | None = None
    result_type: str = "text"


TESTS: list[FormulaTest] = [
    # ── 1. Plain aggregation on a cross-sheet column ────────────────────────
    FormulaTest("SUM",          "agg-basic", "Sum of all prices",                     "=SUM({xref_Price})",                          "1610"),
    FormulaTest("AVG",          "agg-basic", "Average of all prices",                 "=AVG({xref_Price})",                          "161"),
    FormulaTest("MIN",          "agg-basic", "Min price",                             "=MIN({xref_Price})",                          "3"),
    FormulaTest("MAX",          "agg-basic", "Max price",                             "=MAX({xref_Price})",                          "750"),
    FormulaTest("COUNT",        "agg-basic", "Count of numeric prices",               "=COUNT({xref_Price})",                        "10"),
    FormulaTest("COUNT_NEMPTY", "agg-basic", "Count of non-empty names (COUNTIF<>)",  "=COUNTIF({xref_Name}, <>\"\")",               "10"),
    FormulaTest("MEDIAN",       "agg-basic", "Median of prices",                      "=MEDIAN({xref_Price})",                       "17.5"),
    FormulaTest("LARGE",        "agg-basic", "2nd-largest price",                     "=LARGE({xref_Price}, 2)",                     "500"),
    FormulaTest("SMALL",        "agg-basic", "2nd-smallest price",                    "=SMALL({xref_Price}, 2)",                     "5"),

    # ── 2. Conditional aggregation (the bread-and-butter use case) ──────────
    FormulaTest("SUMIF",        "agg-cond", "SUMIF Tools price",                                "=SUMIF({xref_Category}, \"Tools\", {xref_Price})",                                       "45"),
    FormulaTest("SUMIFS",       "agg-cond", "SUMIFS Gadgets active price",                      "=SUMIFS({xref_Price}, {xref_Category}, \"Gadgets\", {xref_Active}, true)",               "100"),
    FormulaTest("SUMIFS_op",    "agg-cond", "SUMIFS with > comparator on Stock when Price>100", "=SUMIFS({xref_Stock}, {xref_Price}, >100)",                                              "13"),
    FormulaTest("SUMIFS_range", "agg-cond", "SUMIFS Stock where 100 <= Price <= 500",           "=SUMIFS({xref_Stock}, {xref_Price}, >=100, {xref_Price}, <=500)",                        "17"),
    FormulaTest("COUNTIF",      "agg-cond", "COUNTIF Tools",                                    "=COUNTIF({xref_Category}, \"Tools\")",                                                   "3"),
    FormulaTest("COUNTIFS",     "agg-cond", "COUNTIFS active Tools",                            "=COUNTIFS({xref_Active}, true, {xref_Category}, \"Tools\")",                             "2"),
    FormulaTest("COUNTIFS_cell","agg-cond", "COUNTIFS @cell CONTAINS Doohickey on Name",        "=COUNTIFS({xref_Name}, CONTAINS(\"Doohickey\", @cell))",                                 "3"),
    FormulaTest("AVERAGEIF",    "agg-cond", "AVERAGEIF Tools price",                            "=AVERAGEIF({xref_Category}, \"Tools\", {xref_Price})",                                   "15"),
    FormulaTest("AVG_via_SUMIFS","agg-cond","Multi-criterion AVG (SUMIFS / COUNTIFS) Tools+Active",
                "=SUMIFS({xref_Price}, {xref_Category}, \"Tools\", {xref_Active}, true) / COUNTIFS({xref_Category}, \"Tools\", {xref_Active}, true)",
                "15"),
    FormulaTest("MAX_COLLECT",  "agg-cond", "Conditional MAX via MAX(COLLECT(...)) Misc price",
                "=MAX(COLLECT({xref_Price}, {xref_Category}, \"Misc\"))",                                  "7"),
    FormulaTest("MIN_COLLECT",  "agg-cond", "Conditional MIN via MIN(COLLECT(...)) Misc price",
                "=MIN(COLLECT({xref_Price}, {xref_Category}, \"Misc\"))",                                  "3"),
    FormulaTest("Active_count", "agg-cond", "Active items only",                                "=COUNTIF({xref_Active}, true)",                                                          "7"),

    # ── 3. Lookup patterns ──────────────────────────────────────────────────
    FormulaTest("VLOOKUP",       "lookup", "VLOOKUP A002 -> Name (col 2)",                                "=VLOOKUP(\"A002\", {xref_All}, 2, false)",                                         "Widget B"),
    FormulaTest("VLOOKUP_price", "lookup", "VLOOKUP D001 -> Price (col 4)",                               "=VLOOKUP(\"D001\", {xref_All}, 4, false)",                                         "500"),
    FormulaTest("INDEX_MATCH",   "lookup", "INDEX/MATCH A003 -> Name",                                    "=INDEX({xref_Name}, MATCH(\"A003\", {xref_SKU}, 0))",                              "Widget C"),
    FormulaTest("INDEX_COLLECT_1","lookup","INDEX(COLLECT(...)) lookup B001 name (single-criterion)",     "=INDEX(COLLECT({xref_Name}, {xref_SKU}, \"B001\"), 1)",                            "Gizmo X"),
    FormulaTest("INDEX_COLLECT_2","lookup","INDEX(COLLECT(...)) Premium + Active price (multi-criterion)","=INDEX(COLLECT({xref_Price}, {xref_Category}, \"Premium\", {xref_Active}, true), 1)","500"),
    FormulaTest("INDEX_2D",      "lookup", "INDEX 2D on multi-col range row 2 col 4 (price A002)",        "=INDEX({xref_All}, 2, 4)",                                                         "20"),

    # ── 4. Existence / membership ───────────────────────────────────────────
    FormulaTest("EXISTS_yes", "exists", "IF SKU A001 found",       "=IF(COUNTIF({xref_SKU}, \"A001\") > 0, \"Found\", \"Missing\")",   "Found"),
    FormulaTest("EXISTS_no",  "exists", "IF SKU ZZZ not found",    "=IF(COUNTIF({xref_SKU}, \"ZZZ\")  > 0, \"Found\", \"Missing\")",   "Missing"),

    # ── 5. Distinct / set ───────────────────────────────────────────────────
    FormulaTest("DISTINCT_count", "set", "Number of distinct categories",
                "=COUNT(DISTINCT({xref_Category}))", "4"),

    # ── 6. Dates ────────────────────────────────────────────────────────────
    FormulaTest("MAX_date",  "date", "Latest AddedOn",   "=MAX({xref_AddedOn})", "2026-04-22", result_type="date"),
    FormulaTest("MIN_date",  "date", "Earliest AddedOn", "=MIN({xref_AddedOn})", "2026-01-01", result_type="date"),
    FormulaTest("YEAR_MIN",  "date", "Year of earliest", "=YEAR(MIN({xref_AddedOn}))", "2026"),
    FormulaTest("MONTH_MAX", "date", "Month of latest",  "=MONTH(MAX({xref_AddedOn}))", "4"),
    FormulaTest("COUNT_date","date", "Items added on or after 2026-03-01",
                "=COUNTIFS({xref_AddedOn}, @cell >= DATE(2026, 3, 1))", "6"),

    # ── 7. JOIN concat ──────────────────────────────────────────────────────
    # NOTE: a raw JOIN of a cross-sheet column includes empty trailing cells in
    # the range, which produces dangling separators. The supported pattern is
    # to wrap with COLLECT(<col>, <col>, <>"") to drop the empties first.
    FormulaTest("JOIN", "concat", "Pipe-join all SKUs (COLLECT removes empties)",
                "=JOIN(COLLECT({xref_SKU}, {xref_SKU}, <>\"\"), \"|\")",
                "A001|A002|A003|B001|B002|C001|C002|C003|D001|D002"),

    # ── 8. Composition (nested cross-sheet calls) ───────────────────────────
    FormulaTest("ADD_SUMIF", "compose",
                "Sum(Tools.Price) + Sum(Gadgets.Price) chained from two SUMIF calls",
                "=SUMIF({xref_Category}, \"Tools\", {xref_Price}) + SUMIF({xref_Category}, \"Gadgets\", {xref_Price})",
                "345"),
    FormulaTest("IF_COUNTIF", "compose",
                "IF on cross-sheet inactive count",
                "=IF(COUNTIF({xref_Active}, false) > 2, \"WARN\", \"OK\")",
                "WARN"),
]


# ──────────────────────────────── Helpers ────────────────────────────────────

def _result_col_for(t: FormulaTest) -> str:
    return {"text": "Formula", "bool": "Formula_check", "date": "Formula_date"}[t.result_type]


def is_error_value(v: str) -> bool:
    if not isinstance(v, str):
        return False
    return v.strip().startswith("#") or "error" in v.lower() and "value" in v.lower()


def values_match(actual: str, expected: str) -> bool:
    a = (actual or "").strip()
    e = (expected or "").strip()
    if a == e:
        return True
    # numeric tolerance for floats coming back as "17.50" / "17.5"
    try:
        return abs(float(a) - float(e)) < 1e-6
    except (ValueError, TypeError):
        return False


# ────────────────────────────── Main runner ──────────────────────────────────

async def create_source_sheet(client: SmartsheetClient, name: str) -> tuple[str, dict]:
    print(f"   creating SOURCE sheet  {C.BOLD}{name}{C.RESET}")
    res = await client.create_sheet(name, SOURCE_COLUMNS)
    sheet = res.get("result") or res
    sid = str(sheet["id"])
    cols_by_title = {c["title"]: c for c in sheet["columns"]}
    print(f"   -> sheet_id = {C.CYAN}{sid}{C.RESET} "
          f"({len(cols_by_title)} columns)")
    return sid, cols_by_title


async def populate_source(client: SmartsheetClient, sid: str) -> int:
    print(f"   populating SOURCE with {len(SOURCE_ROWS)} fake rows")
    res = await client.add_rows(sid, SOURCE_ROWS, to_bottom=True)
    inserted = len(res.get("result", []))
    print(f"   -> {inserted} rows inserted")
    return inserted


async def create_target_sheet(client: SmartsheetClient, name: str) -> tuple[str, dict]:
    print(f"   creating TARGET sheet  {C.BOLD}{name}{C.RESET}")
    res = await client.create_sheet(name, TARGET_COLUMNS)
    sheet = res.get("result") or res
    tid = str(sheet["id"])
    cols = {c["title"]: c["id"] for c in sheet["columns"]}
    print(f"   -> sheet_id = {C.CYAN}{tid}{C.RESET}")
    return tid, cols


async def create_refs(
    client: SmartsheetClient,
    target_id: str,
    source_id: str,
    source_cols: dict[str, dict],
) -> dict[str, int]:
    """Create the cross-sheet references and return {ref_name: ref_id}.

    Smartsheet's cross-sheet endpoint can return 404 right after the target
    sheet is created (eventual consistency), so each ref gets up to 4 retries
    with exponential back-off.
    """
    out: dict[str, int] = {}
    for name, start_title, end_title in REFS_PLAN:
        start_col = source_cols[start_title]["id"]
        end_col = source_cols[end_title]["id"]
        span = start_title if start_title == end_title else f"{start_title}..{end_title}"
        last_exc: Exception | None = None
        for attempt in range(1, 5):
            try:
                res = await client.create_cross_sheet_ref(
                    target_id, name,
                    source_sheet_id=int(source_id),
                    start_col_id=int(start_col),
                    end_col_id=int(end_col),
                )
                ref = res.get("result") or res
                ref_id = ref.get("id") or 0
                out[name] = ref_id
                tag = f"id={ref_id}" + (f" ({C.YELLOW}retry {attempt}{C.RESET})" if attempt > 1 else "")
                print(f"   {C.GREEN}+{C.RESET} {name:<14} ({span})  {tag}")
                break
            except Exception as exc:
                last_exc = exc
                msg = str(exc).split("\n", 1)[0]
                if attempt < 4:
                    delay = 0.5 * (2 ** (attempt - 1))
                    print(f"   {C.YELLOW}~{C.RESET} {name:<14} attempt {attempt} failed ({msg!s:.80}); retrying in {delay:.1f}s")
                    await asyncio.sleep(delay)
                else:
                    print(f"   {C.RED}!{C.RESET} {name}: gave up after 4 attempts -> {msg}")
        if name not in out and last_exc is not None:
            # leave it absent; downstream formulas will surface as #INVALID REF
            pass
    return out


async def insert_formula_rows(client: SmartsheetClient, target_id: str) -> list[int]:
    rows: list[dict] = []
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
        res = await client.add_rows(target_id, chunk, to_bottom=True)
        for r in res.get("result", []):
            new_ids.append(r["id"])
        print(f"   inserted batch {i // BATCH + 1}: {len(chunk)} rows "
              f"({len(new_ids)}/{len(rows)} total)")
    return new_ids


async def read_back(client: SmartsheetClient, target_id: str,
                    row_ids: list[int]) -> dict[int, dict[str, str]]:
    sheet = await client.get_sheet(target_id, page_size=500)
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


async def run(source_id: str | None, target_id: str | None) -> int:
    token = (os.getenv("SMARTSHEET_TOKEN") or "").strip()
    if not token:
        print(f"{C.RED}SMARTSHEET_TOKEN not found in .env or environment.{C.RESET}")
        return 2

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    banner("Smartsheet Controller — Cross-Sheet Formula Coverage Runner")
    print(f"  Tests planned:  {len(TESTS)} across "
          f"{len({t.category for t in TESTS})} categories")
    print(f"  Cross-refs:     {len(REFS_PLAN)} (incl. one multi-column)")
    print(f"  Source sheet:   {source_id or '(will be created)'}")
    print(f"  Target sheet:   {target_id or '(will be created)'}")
    print(f"  Started:        {datetime.now().isoformat(timespec='seconds')}")

    client = SmartsheetClient(token)
    t0 = time.monotonic()
    report_rows: list[dict] = []
    per_cat_pass: dict[str, int] = defaultdict(int)
    per_cat_fail: dict[str, int] = defaultdict(int)

    src_sheet_dict: dict | None = None
    refs: dict[str, int] = {}

    try:
        # ── 1. Source sheet ────────────────────────────────────────────────
        section("1. Source sheet")
        if source_id:
            src = await client.get_sheet(source_id, page_size=0)
            src_sheet_dict = {c["title"]: c for c in src.get("columns", [])}
            print(f"   reusing existing source sheet {source_id}")
        else:
            source_id, src_sheet_dict = await create_source_sheet(
                client, f"XSHEET_SOURCE_{ts}",
            )
            await populate_source(client, source_id)

        # Eventual-consistency buffer for the freshly created sheet
        await asyncio.sleep(1.0)

        # ── 2. Target sheet ────────────────────────────────────────────────
        section("2. Target sheet")
        if target_id:
            print(f"   reusing existing target sheet {target_id}")
        else:
            target_id, _ = await create_target_sheet(
                client, f"XSHEET_TARGET_{ts}",
            )
        # Smartsheet can 404 on the cross-sheet endpoint right after a sheet is
        # created; give the back-end a moment to surface it before we hammer it.
        await asyncio.sleep(2.0)

        # ── 3. Cross-sheet references ──────────────────────────────────────
        section("3. Create cross-sheet references on TARGET -> SOURCE")
        refs = await create_refs(client, target_id, source_id, src_sheet_dict or {})
        print(f"   total refs created: {len(refs)} / {len(REFS_PLAN)}")
        if len(refs) < len(REFS_PLAN):
            print(f"{C.RED}   ! Some refs failed to create — formulas using them will fail.{C.RESET}")

        # ── 4. Insert formula rows ─────────────────────────────────────────
        section("4. Insert formula rows on TARGET")
        row_ids = await insert_formula_rows(client, target_id)
        print(f"   {len(row_ids)} formula rows inserted")

        # ── 5. Wait for evaluation ─────────────────────────────────────────
        section("5. Wait for Smartsheet to evaluate cross-sheet formulas")
        for sec in range(5, 0, -1):
            print(f"   waiting {sec}s...")
            await asyncio.sleep(1)

        # ── 6. Read back & compare ─────────────────────────────────────────
        section("6. Read back and compare to expected")
        results = await read_back(client, target_id, row_ids)
        print(f"   read {len(results)} rows back\n")

        for test, rid in zip(TESTS, row_ids):
            cells = results.get(rid, {})
            actual = cells.get(_result_col_for(test), "")
            expected = test.expected
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

            color = C.GREEN if status == "PASS" else C.RED
            label = f"[{test.category:<9}] {test.func:<16}"
            print(f"   {color}{status}{C.RESET}  {label} {C.DIM}{note}{C.RESET}")

            if status == "PASS":
                per_cat_pass[test.category] += 1
            else:
                per_cat_fail[test.category] += 1

            report_rows.append({
                "function":    test.func,
                "category":    test.category,
                "description": test.description,
                "formula":     test.formula,
                "result_type": test.result_type,
                "expected":    expected,
                "actual":      actual,
                "status":      status,
                "note":        note,
                "row_id":      rid,
            })

    finally:
        try:
            await client.close()
        except Exception:
            pass

    total_ms = int((time.monotonic() - t0) * 1000)

    # ── 7. Summary ─────────────────────────────────────────────────────────
    banner("SUMMARY")
    cats = sorted({t.category for t in TESTS})
    print(f"  {'Category':<10} {'PASS':>5}  {'FAIL':>5}")
    print(f"  {'-' * 10} {'-' * 5}  {'-' * 5}")
    total_pass = total_fail = 0
    for cat in cats:
        p = per_cat_pass[cat]
        f = per_cat_fail[cat]
        total_pass += p
        total_fail += f
        line_color = C.RED if f else C.GREEN
        print(f"  {line_color}{cat:<10}{C.RESET} {p:>5}  {f:>5}")
    print(f"  {'-' * 10} {'-' * 5}  {'-' * 5}")
    print(f"  {C.BOLD}{'TOTAL':<10}{C.RESET} {C.GREEN}{total_pass:>5}{C.RESET}  "
          f"{C.RED}{total_fail:>5}{C.RESET}    ({total_ms} ms)")

    if total_fail:
        print(f"\n{C.RED}{C.BOLD}Failed formulas (open the target sheet to inspect):{C.RESET}")
        for r in report_rows:
            if r["status"] == "FAIL":
                print(f"  - [{r['category']}] {r['function']}: {r['note']}")

    # ── 8. Where to inspect ────────────────────────────────────────────────
    banner("BOTH SHEETS KEPT — open them to inspect every cross-sheet formula")
    print(f"  Source sheet:  {C.BOLD}{source_id}{C.RESET}")
    print(f"  Source URL:    {C.CYAN}https://app.smartsheet.com/sheets/{source_id}{C.RESET}")
    print(f"  Target sheet:  {C.BOLD}{target_id}{C.RESET}")
    print(f"  Target URL:    {C.CYAN}https://app.smartsheet.com/sheets/{target_id}{C.RESET}")
    print(f"  {C.DIM}(Smartsheet may take a few seconds to refresh in your browser){C.RESET}")

    # ── 9. JSON report ─────────────────────────────────────────────────────
    report_path = Path(__file__).parent / f"cross_sheet_report_{ts}.json"
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "source_sheet_id": source_id,
        "target_sheet_id": target_id,
        "refs_planned": [{"name": n, "start": s, "end": e} for n, s, e in REFS_PLAN],
        "refs_created": refs,
        "total_ms": total_ms,
        "totals": {"pass": total_pass, "fail": total_fail},
        "per_category": {
            cat: {"pass": per_cat_pass[cat], "fail": per_cat_fail[cat]}
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

    parser = argparse.ArgumentParser(description="Run Smartsheet cross-sheet formula coverage tests.")
    parser.add_argument("--source-id", help="Existing source sheet ID. If omitted, a new sheet is created.")
    parser.add_argument("--target-id", help="Existing target sheet ID. If omitted, a new sheet is created.")
    args = parser.parse_args()

    rc = asyncio.run(run(args.source_id, args.target_id))
    sys.exit(rc)
