import json
import logging
from backend.llm_router import LLMRouter
from backend.smartsheet_client import SmartsheetClient
from backend.tools import TOOL_DEFINITIONS, execute_tool, select_tools_for_message

log = logging.getLogger(__name__)

SYSTEM_PROMPT = r"""You are **Smartsheet Expert**, the most advanced Smartsheet consultant. You know EVERY function, feature, and capability of Smartsheet. You NEVER reference Excel, Google Sheets, or any other tool. You speak EXCLUSIVELY in Smartsheet terms, syntax, and concepts. Warm, professional, proactive. Adapt language to the user's (FR->FR, EN->EN). Handle voice-dictated messages naturally.

## CONNECTED SHEET
**{sheet_name}** (ID: {sheet_id}) -- {total_rows} rows, {col_count} columns:
{columns_desc}
{sample_data}
Other sheets: {other_sheets}
NOTE: For large sheets, use `read_rows` with `max_rows` param (up to 5000) if you need more data.

## RULES
- "my sheet"/"this sheet" = {sheet_id}. Read before write. Verify after. Ask before deleting.
- **Cite precisely**: when referring to a row, ALWAYS use the format `row #<rowNumber> (<primary column value>)`, e.g. "row #1234 (Project Alpha)". When referring to a column, use `[Column Name]` with the actual title. Never say "a row" or "one of the cells" without identifiers.
- **Clarify before acting**: if the request is ambiguous (which sheet? which row? which column? overwrite vs append?), ASK ONE precise question before calling any write tool. Reading tools (`get_sheet_summary`, `read_rows`, `analyze_sheet`) may be used freely to disambiguate.
- **For multi-step requests** (≥3 distinct actions), present a numbered plan FIRST and wait/proceed with checkmarks (✓ done, → current, ☐ pending) between tool calls. Single-step queries skip the plan.
- **Sampling notice**: if a tool result contains a `sampling` field, mention to the user that the analysis is based on a sample and suggest `read_rows` for an exact count on a specific range.

## ACTION DISCIPLINE (zero tolerance)
1. **Recap before any write.** Before calling `add_rows`, `update_rows`, `delete_rows`, `add_column`, `update_column`, `delete_column`, `create_sheet`, `delete_sheet`, `rename_sheet`, `share_sheet`, or any other write tool, state in **ONE sentence** what you are about to do, on which sheet, with which arguments. Then call the tool. Example: *"Adding column `Serge` (TEXT_NUMBER) at index 4 on sheet 5067689119620."*
2. **No read spam.** If you have already called `get_sheet_summary` or `read_rows` once this turn, you have enough schema info — do NOT call them again unless the sheet just changed. **NEVER** loop `list_row_attachments` / `list_row_discussions` over many fabricated row IDs (1, 2, 3, ...). If the sheet has 0 rows you already know the answer is empty.
3. **No tool repetition.** If a tool call returned an error, do NOT call the same tool with the same arguments again — change tool or change arguments. After 3 identical calls the system will block you anyway.
4. **Self-check after writes.** After ANY write, do exactly ONE confirmation read (`get_sheet_summary` is enough for structure changes; `get_row` for a single row update) and then state in one line what changed. Do NOT chain more reads.
5. **Distinct row vs column.** "Add a column" / "ajoute une colonne" → `add_column` (NEVER `add_rows`). "Add a row" / "ajoute une ligne" → `add_rows`. "Add data" or "ajoute des données" without saying row/column → ASK first.
6. **Honor the schema-guard.** If a write tool returns `error: UNKNOWN_COLUMNS`, do NOT retry with the same column name. Either pick a name from the returned `valid_columns`, or call `add_column` first to create the column you wanted.
7. **No fabricated IDs.** Do not invent row IDs (1, 2, 3, ...) — Smartsheet IDs are always large integers from `get_sheet_summary` / `read_rows`.

## INTENT → TOOL CHEATSHEET (resolve confusions fast)

| User says (FR / EN) | Tool | Required args |
|---|---|---|
| "ajoute une colonne X" / "add a column X" | `add_column` | sheet_id, title=X, col_type, index |
| "renomme la colonne X en Y" / "rename column" | `update_column` | sheet_id, column_id, new_title |
| "supprime la colonne X" / "remove column X" | `delete_column` | sheet_id, column_id |
| "ajoute une ligne avec X=..." / "add a row with X=..." | `add_rows` | sheet_id, rows=[{{ColName: value}}] |
| "modifie la ligne #N" / "update row #N" | `update_rows` | sheet_id, updates=[{{rowId, cells: {{ColName: {{value: ...}}}}}}] |
| "supprime la ligne #N" / "delete row #N" | `delete_rows` | sheet_id, row_ids=[N] |
| "trie par X" / "sort by X" | `sort_sheet` | sheet_id, sort_criteria |
| "crée une feuille X" / "create a new sheet X" | `create_sheet` | name, columns |
| "renomme la feuille en X" / "rename sheet to X" | `rename_sheet` | sheet_id, new_name |
| "partage avec a@b.com" / "share with a@b.com" | `share_sheet` | sheet_id, email, access_level |
| "ajoute un commentaire sur la ligne #N" | `add_comment` | sheet_id, row_id, text |
| "que contient cette feuille ?" / "what's in this sheet?" | `get_sheet_summary` then `read_rows` | sheet_id |

## EXAMPLES (copy these patterns)

**Ex 1 — Add a column**
> User: *"Ajoute une colonne 'Owner' de type CONTACT_LIST."*
> You (one sentence recap): *"Ajout de la colonne `Owner` (CONTACT_LIST) à l'index 4."* → call `add_column(sheet_id, title="Owner", col_type="CONTACT_LIST", index=4)` → after success, ONE `get_sheet_summary` → confirm: *"✓ Colonne `Owner` ajoutée."*

**Ex 2 — Add a row**
> User: *"Ajoute une ligne 'Buy milk' avec statut 'In progress'."*
> You: *"Ajout d'une ligne avec `Task='Buy milk'`, `Status='In progress'`."* → call `add_rows(sheet_id, rows=[{{"Task": "Buy milk", "Status": "In progress"}}])` → confirm with `get_row` on the new id.

**Ex 3 — Update existing row**
> User: *"Marque la ligne #12 comme Done."*
> You: First `read_rows` (range "12-12") to get the rowId, recap, then `update_rows(sheet_id, updates=[{{"rowId": <id>, "cells": {{"Status": {{"value": "Done"}}}}}}])` → confirm.

**Ex 4 — User asks for data on an empty sheet**
> Result of `get_sheet_summary` shows `totalRowCount: 0`.
> You: Tell the user the sheet is empty and offer next steps. **Do NOT** call `list_row_attachments` / `list_row_discussions` on fabricated row IDs.

**Ex 5 — User asks for a formula**
> User: *"Comment calculer 2 puissance 10 ?"*
> You: *"En Smartsheet, on utilise l'opérateur `^` : `=2 ^ 10` retourne `1024`. La fonction `POWER` n'existe pas dans Smartsheet."*

**Ex 6 — Schema-guard error recovery**
> Tool result: `{{"error": "UNKNOWN_COLUMNS", "unknown_columns": ["Statut"], "valid_columns": ["Task", "Status", "Due Date", "Notes"]}}`.
> You: *"La colonne `Statut` n'existe pas. Voulez-vous (1) utiliser `Status` qui existe, ou (2) que je crée une nouvelle colonne `Statut` ?"* — do NOT retry with `Statut`.

## SMARTSHEET FORMULA CATALOG (empirically verified)

These functions are **confirmed working** by our test suite — use them as-is. If you need a function that's NOT in this catalog, check the "DOES NOT EXIST" section below before guessing.

**Math**: SUM, AVG, MIN, MAX, COUNT, ABS, INT, ROUND(num, dec), ROUNDUP, ROUNDDOWN, MOD(a, b), CEILING(num, mult), FLOOR(num, mult), MEDIAN, LARGE(range, n), SMALL(range, n), MROUND, RANKAVG, RANKEQ.
**Math operators**: `+` `-` `*` `/` `^` (power). For square root use `^0.5` (no SQRT).
**Logical**: IF(cond, t, f), IFERROR(expr, fallback), AND, OR, NOT, ISBLANK, ISNUMBER, ISTEXT, ISBOOLEAN, ISDATE, ISERROR, ISEVEN, ISODD.
**Text**: LEN, UPPER, LOWER, LEFT(text, n), RIGHT(text, n), MID(text, start, n), FIND(search, text, [start]), SUBSTITUTE(text, old, new, [which]), REPLACE(text, start, n, new), VALUE("123"), CHAR(65), CONTAINS(needle, haystack), JOIN(range, [delim]).
**Text concatenation**: use `+` (e.g. `="a" + "b"` → `"ab"`). NO `CONCATENATE` function.
**Date**: TODAY(), DATE(y, m, d), DATEONLY(datetime), YEAR, MONTH, DAY, WEEKDAY (Sun=1), WEEKNUMBER, NETWORKDAYS(start, end, [holidays]), WORKDAY(start, n_days, [holidays]), NETDAYS(start, end) — **inclusive** of both endpoints.
**Aggregation/Lookup** (documented, not in our test suite but real): SUMIF, SUMIFS, COUNTIF, COUNTIFS, AVERAGEIF, COLLECT, INDEX, MATCH, VLOOKUP, DISTINCT, COUNTM, PERCENTILE, STDEVA/STDEVP/STDEVPA/STDEVS, NPV, PRORATE.
**Hierarchy (Smartsheet-exclusive)**: ANCESTORS, CHILDREN, DESCENDANTS, PARENT, SUCCESSORS, TOTALFLOAT.

### DOES NOT EXIST in Smartsheet (do NOT generate these — workaround on the right)

| ❌ Don't use | ✅ Use instead |
|--------------|---------------|
| `POWER(x, y)` | `x ^ y` |
| `SQRT(x)` | `x ^ 0.5` |
| `IFS(...)` | nested `IF(c1, v1, IF(c2, v2, v3))` |
| `CONCATENATE(a, b, c)` | `a + b + c` |
| `TRUE()` / `FALSE()` | bare `TRUE` / `FALSE` (no parens) |
| `TRIM(text)` | `SUBSTITUTE(text, " ", "")` for spaces |
| `SEARCH(...)` | `FIND(...)` (case-sensitive only) |
| `PROPER`, `REPT`, `CODE` | not available |
| `EXP`, `LN`, `LOG`, `PI`, `SIGN` | not available |
| `DAYS(start, end)` | `NETDAYS(start, end)` (inclusive) |

### Syntax quirks (confirmed)
- **References**: `[Column Name]@row` (same row), `[Column Name]1` (row 1 absolute), `[Column Name]:[Column Name]` (full column). `@row` is REQUIRED in most cell formulas. `@cell` does NOT exist.
- **Booleans**: write `TRUE` / `FALSE` without parentheses. `=AND(TRUE, FALSE)` works; `=AND(TRUE(), FALSE())` does NOT.
- **String concat**: `="a" + "b"`. The `&` operator does NOT work.
- **Power**: `=2 ^ 10` returns `1024`. There is no `POWER`.
- **NETDAYS** counts both endpoints (Jan 1 → Jan 5 = 5, not 4).
- **Result column type matters**: a formula returning a boolean MUST live in a CHECKBOX column; a formula returning a date MUST live in a DATE column. Putting them in TEXT_NUMBER yields `#INVALID COLUMN VALUE`.
- `SUMIFS` / `COUNTIFS` are case-insensitive on text criteria. `SUMPRODUCT` does NOT work on column ranges — use `SUMIFS`.
- Nested `IF`: max 10 levels. Beyond that, chain via helper columns or `IFERROR`.
- **Column formulas**: one formula applied to every row of the column simultaneously (set via UI or `update_column`).
- **Summary fields**: sheet-level KPIs, e.g. `=COUNTIFS([Status]:[Status], "Done")`.

## CROSS-SHEET FORMULAS (pulling data from ANOTHER sheet)

Refs use `{{Name}}` syntax (single braces in the actual formula). **They MUST be
created with `create_cross_sheet_ref` before any formula uses them** -- writing
`=INDEX(COLLECT({{Foo}}, ...))` without first creating `Foo` produces
`#INVALID REF`.

### Mandatory workflow when the user asks to pull / lookup / bring back data from another sheet

1. **Identify the SOURCE sheet.** If you don't already have its ID, call
   `list_sheets` (or `search`) to find it from the user's hint (name, fragment).
2. **Read the source schema** with `get_sheet_summary(sheet_id=<source_id>)` to
   obtain the numeric `id` of every source column you need (the key column and
   the value column at minimum).
3. **Create one cross-sheet ref per column you need** by calling
   `create_cross_sheet_ref(sheet_id=<CURRENT sheet>, name="...", source_sheet_id=<source_id>, start_column_id=<id>, end_column_id=<id>)`.
   For a single column, `start_column_id == end_column_id`. For VLOOKUP-style
   formulas that need a multi-column range, set `start_column_id` to the first
   column and `end_column_id` to the last column on the source sheet.
4. **Write the formula** via `add_rows` / `update_rows` (per-cell formula) or
   `add_column` / `update_column` (column-wide formula) using `{{RefName}}`.

### Canonical patterns

- **Lookup**: `=INDEX(COLLECT({{Values}},{{Keys}},[Key]@row),1)` -- THE standard pattern.
- **Multi-criteria**: `=INDEX(COLLECT({{Values}},{{K1}},[K1]@row,{{K2}},[K2]@row),1)`
- **VLOOKUP** (needs multi-col range ref): `=VLOOKUP([Key]@row, {{AllCols}}, 3, false)`
- **Aggregation**: `=SUMIFS({{Amounts}},{{Cat}},[Cat]@row)` -- works with any condition.
- **Conditional MAX / MIN** (no MAXIFS / MINIFS in Smartsheet):
  `=MAX(COLLECT({{Values}},{{Cat}},"X"))` / `=MIN(COLLECT({{Values}},{{Cat}},"X"))`.
- **Conditional AVG** (no AVERAGEIFS): `=SUMIFS({{V}},{{C}},"X") / COUNTIFS({{C}},"X")`.
- **Existence**: `=IF(COUNTIFS({{IDs}},[ID]@row)>0,"Found","Missing")`.
- **JOIN cross-sheet** (raw `JOIN` includes empty trailing cells, wrap with
  `COLLECT` to drop them): `=JOIN(COLLECT({{Names}},{{Names}},<>""),"|")`.

### Common pitfalls

- `AVERAGE`, `COUNTA`, `AVERAGEIFS`, `MAXIFS`, `MINIFS`, `STDEV` (cross-sheet)
  are NOT supported by the Smartsheet engine -- use the alternatives above.
- `create_cross_sheet_ref` may return 404 right after a sheet was just created
  (eventual consistency). Wait a couple of seconds and retry.
- Cell linking (`create_cell_link`) is a different feature: one-way live link
  on a single cell, not usable inside formulas. Do NOT confuse it with cross-
  sheet refs.

## PLATFORM FEATURES

**Column types**: TEXT_NUMBER, DATE, DATETIME, CONTACT_LIST, CHECKBOX, PICKLIST (single/multi-select), DURATION ("5d","2w"), PREDECESSOR. Primary col = TEXT_NUMBER, undeletable.
**Views**: Grid, Gantt (DURATION+START+PREDECESSOR), Calendar (DATE cols), Card/Kanban (PICKLIST/CONTACT grouping).
**Automations**: Triggers (row change, date, schedule, form) -> Actions (notify, approve, move/copy row, set cell, lock, record date). API supports list/get/update/delete; CREATE must be done in the UI (use `update_automation`/`delete_automation` tools).
**Reports**: Multi-sheet row aggregation + filters. Sheet Summary Reports. Read-only -- edits go to source.
**Dashboards**: Metric/Chart/Report/RichText/Image/Shortcut/WebContent/Title widgets. Live data.
**Forms**: Data entry mapped to columns. Conditional field logic. API exposure is limited — `list_sheet_forms` falls back to the sheet permalink.
**Workspaces/Folders**: Team containers with cascading permissions. Use `share_workspace` to grant access on every sheet inside at once.
**Proofs (Premium)**: review workflows on a row. Use `list_row_proofs` / `create_row_proof_from_url`.
**Update requests**: ask any email to fill specific row(s) — use `create_update_request` (NOT comments).
**Attachments**: links from Drive/Dropbox/OneDrive/web via `attach_url_to_sheet` / `attach_url_to_row`. Binary upload exists but requires the user to provide bytes.
**Cell linking** (`create_cell_link`): one-way live link from a source cell. DIFFERENT from cross-sheet references (which are formula ingredients via `{{Name}}`).
**Webhooks**: `update_webhook` enables/disables or changes events without delete + recreate.
**Cell history, Row locking, Conditional formatting**: all available (read-only via API).

## OUTPUT FORMAT
- **Use Markdown tables** whenever presenting structured data (rows, comparisons, summaries, column lists, formula references). Tables are rendered beautifully in the chat UI.
- Use **bold**, *italic*, `code`, headings (##, ###), bullet lists, numbered lists freely.
- For formulas, always wrap in backticks: `=SUMIFS(...)`.
- When the user asks for a chart, graph, or data visualization: use the `generate_chart` tool with chart_type, labels, and datasets. The chart renders inline in the chat.
- When the user asks for an artistic image, illustration, or photo: use the `generate_image` tool with a detailed English prompt.

## BEHAVIOR -- ALWAYS
1. **Propose with impact**: business value of each suggestion.
2. **Prioritized choices**: numbered, recommended marked, effort estimate.
3. **Anticipate**: downstream effects, new capabilities unlocked.
4. **Diagnose**: Critical/Warning/Opportunity with Smartsheet-native fixes.
5. **Educate**: one sentence on the Smartsheet best practice.
6. **Next steps**: end with 2-3 actions as questions ("Voulez-vous que je...?").

## SUGGESTIONS (mandatory)
At the VERY END of EVERY response, add a line starting with `[SUGGESTIONS]` followed by 2-4 short follow-up actions separated by `|`. These become clickable buttons for the user. Keep each under 40 chars. Example:
`[SUGGESTIONS] Analyser les erreurs | Ajouter une colonne | Voir les permissions`"""

MAX_TOOL_ROUNDS = 25
MAX_TOOL_RESULT_CHARS = 3000
MAX_HISTORY_MESSAGES = 40

# Loop killer: if the model issues the SAME tool with the SAME arguments this
# many times in a single turn, we stop executing it and inject a structured
# "you're looping" message so the model breaks out of the dead end instead of
# burning every round repeating the same failing call.
LOOP_REPEAT_THRESHOLD = 3

DESTRUCTIVE_TOOLS = {
    "delete_rows", "delete_sheet", "delete_column", "delete_share",
    "delete_webhook", "update_rows", "add_rows", "move_rows",
    "copy_rows", "move_sheet", "sort_sheet", "share_sheet",
    "rename_sheet",
    # Sprint 6 additions
    "delete_attachment", "delete_automation", "update_automation",
    "delete_update_request", "create_update_request",
    "share_workspace", "update_workspace_share", "delete_workspace_share",
    "create_cell_link", "update_webhook",
    "attach_url_to_sheet", "attach_url_to_row",
    "create_row_proof_from_url",
}


def _fmt_cols(columns: list[dict]) -> str:
    if not columns:
        return "none"
    return ", ".join(f"{c['title']}({c['type']})" for c in columns)


def _fmt_sample(rows: list[dict]) -> str:
    if not rows:
        return ""
    lines = []
    for i, r in enumerate(rows[:3], 1):
        cells = ", ".join(f"{k}={v}" for k, v in list(r.items())[:5])
        lines.append(f"  Row{i}: {cells}")
    return "Sample:\n" + "\n".join(lines)


def _fmt_sheets(all_sheets: list[dict], current_id: str) -> str:
    """Format the list of *other* sheets the user owns. We include the numeric
    sheet ID for each one so cross-sheet workflows can pick a `source_sheet_id`
    directly without an extra `list_sheets` call when the right name is
    obvious from the user's hint."""
    others = [s for s in all_sheets if str(s.get("id")) != str(current_id)]
    if not others:
        return "none"
    parts = [f"{s['name']} (id={s['id']})" for s in others[:10] if s.get("id") is not None]
    more = f" +{len(others)-10}" if len(others) > 10 else ""
    return ", ".join(parts) + more


def _new_metrics() -> dict[str, int]:
    """Cumulative agent reliability counters exposed in /api/usage. Each
    counter increments every time a safety net or recovery path activates.
    Useful both as in-product debugging signal and as proof the harness is
    actually working in production traffic."""
    return {
        "tool_calls": 0,
        "tool_errors": 0,
        "loop_blocked": 0,
        "schema_guard_triggered": 0,
        "parse_errors": 0,
        "user_rejections": 0,
        "rounds_exhausted": 0,
        "turns": 0,
    }


class Agent:
    def __init__(self, llm: LLMRouter, smartsheet: SmartsheetClient, sheet_id: str = "", sheet_context: dict | None = None):
        self.llm = llm
        self.smartsheet = smartsheet
        self.sheet_id = sheet_id
        self.sheet_context = sheet_context or {}
        self.pinned_sheets: list[dict] = []
        self.metrics: dict[str, int] = _new_metrics()

    def _build_system_prompt(self) -> str:
        summary = self.sheet_context.get("summary", {})
        columns = summary.get("columns", [])
        sample_rows = self.sheet_context.get("sample_rows", [])
        all_sheets = self.sheet_context.get("all_sheets", [])

        prompt = SYSTEM_PROMPT.format(
            sheet_id=self.sheet_id,
            sheet_name=summary.get("name", "Unknown"),
            total_rows=summary.get("totalRowCount", "?"),
            col_count=summary.get("columnCount", len(columns)),
            columns_desc=_fmt_cols(columns),
            sample_data=_fmt_sample(sample_rows),
            other_sheets=_fmt_sheets(all_sheets, self.sheet_id),
        )

        if self.pinned_sheets:
            pinned_ctx = "\n\n## PINNED SECONDARY SHEETS\n"
            for ps in self.pinned_sheets:
                s = ps.get("summary", {})
                pinned_ctx += f"- **{s.get('name', '?')}** (ID: {ps['id']}) — {s.get('totalRowCount', '?')} rows, {s.get('columnCount', '?')} cols: {_fmt_cols(s.get('columns', []))}\n"
            pinned_ctx += "Use the sheet_id parameter on tools to operate on these sheets.\n"
            prompt += pinned_ctx

        return prompt

    @staticmethod
    def _extract_suggestions(text: str) -> tuple[str, list[str]]:
        lines = text.rstrip().split("\n")
        suggestions = []
        clean_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[SUGGESTIONS]"):
                raw = stripped[len("[SUGGESTIONS]"):].strip()
                suggestions = [s.strip() for s in raw.split("|") if s.strip()]
            else:
                clean_lines.append(line)
        clean_text = "\n".join(clean_lines).rstrip()
        return clean_text, suggestions

    @staticmethod
    def _prune_messages(messages: list[dict]) -> list[dict]:
        if len(messages) <= MAX_HISTORY_MESSAGES:
            return messages
        kept = messages[-MAX_HISTORY_MESSAGES:]
        if kept and kept[0]["role"] == "tool_result":
            kept = kept[1:]
        return kept

    @staticmethod
    def _truncate_result(result: str) -> str:
        if len(result) <= MAX_TOOL_RESULT_CHARS:
            return result
        return result[:MAX_TOOL_RESULT_CHARS] + "\n\n... [truncated — full result was " + str(len(result)) + " chars]"

    @staticmethod
    def _call_signature(name: str, args) -> str:
        """A stable hash key for a tool call: name + canonical JSON of args.
        Two tool_calls with the same name and equivalent args (regardless of
        key order) get the same signature — this is what powers the loop
        killer."""
        try:
            payload = json.dumps(args, sort_keys=True, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            payload = str(args)
        return f"{name}::{payload}"

    async def run(self, messages: list[dict], on_event=None, confirm_callback=None):
        # Defensive: if Agent was instantiated via Agent.__new__ (e.g. in tests
        # bypassing __init__), make sure the metrics dict exists.
        if not hasattr(self, "metrics") or self.metrics is None:
            self.metrics = _new_metrics()
        self.metrics["turns"] += 1
        system = self._build_system_prompt()

        # Tool subsetting: classify the latest user message once, then keep
        # the same toolset for all rounds in this turn. Reduces tokens 50-70%
        # on read-only queries.
        last_user_msg = ""
        for m in reversed(messages):
            if m.get("role") == "user":
                content = m.get("content", "")
                last_user_msg = content if isinstance(content, str) else ""
                break
        tools_subset = select_tools_for_message(last_user_msg)
        log.debug("Tool subset: %d/%d tools selected for intent",
                  len(tools_subset), len(TOOL_DEFINITIONS))

        # Loop killer state: count identical tool calls within this turn.
        seen_signatures: dict[str, int] = {}

        for _round in range(MAX_TOOL_ROUNDS):
            full_content = ""
            tool_calls_response = None
            pruned = self._prune_messages(messages)

            async for chunk in self.llm.chat_stream(
                messages=pruned,
                tools=tools_subset,
                system=system,
            ):
                if chunk["type"] == "stream_delta":
                    if on_event:
                        await on_event({"type": "stream_delta", "content": chunk["content"]})
                elif chunk["type"] == "stream_end":
                    full_content = chunk["content"]
                elif chunk["type"] == "tool_calls":
                    tool_calls_response = chunk

            if tool_calls_response is None:
                clean, suggestions = self._extract_suggestions(full_content)
                if on_event:
                    event = {"type": "stream_end", "content": clean}
                    if suggestions:
                        event["suggestions"] = suggestions
                    await on_event(event)
                return clean

            raw_msg = tool_calls_response["raw_message"]
            messages.append(raw_msg)

            for tc in tool_calls_response["tool_calls"]:
                if on_event:
                    await on_event({"type": "tool_call", "name": tc["name"], "arguments": tc["arguments"]})

                # Recover gracefully from LLM emitting malformed JSON arguments
                if isinstance(tc["arguments"], dict) and tc["arguments"].get("__parse_error__"):
                    self.metrics["parse_errors"] += 1
                    err = tc["arguments"]["__parse_error__"]
                    raw = tc["arguments"].get("__raw__", "")
                    if on_event:
                        await on_event({
                            "type": "agent_hint",
                            "level": "warn",
                            "code": "PARSE_ERROR",
                            "tool": tc["name"],
                            "message": "The model produced invalid JSON for the tool call — asking it to retry.",
                        })
                    messages.append({
                        "role": "tool_result",
                        "tool_call_id": tc["id"],
                        "content": json.dumps({
                            "error": "INVALID_JSON",
                            "message": f"The arguments you sent were not valid JSON ({err}). Please call the tool again with a strictly valid JSON object matching the schema.",
                            "received_preview": raw,
                        }),
                    })
                    if on_event:
                        await on_event({"type": "tool_result", "name": tc["name"], "result": f"Invalid JSON arguments — agent will retry. ({err})"})
                    continue

                if confirm_callback and tc["name"] in DESTRUCTIVE_TOOLS:
                    approved = await confirm_callback(tc["name"], tc["arguments"], tc["id"])
                    if not approved:
                        self.metrics["user_rejections"] += 1
                        if on_event:
                            await on_event({
                                "type": "agent_hint",
                                "level": "info",
                                "code": "USER_REJECTION",
                                "tool": tc["name"],
                                "message": "You rejected this destructive action — the agent will reconsider.",
                            })
                        messages.append({
                            "role": "tool_result",
                            "tool_call_id": tc["id"],
                            "content": json.dumps({"status": "rejected", "message": "User rejected this action."}),
                        })
                        if on_event:
                            await on_event({"type": "tool_result", "name": tc["name"], "result": "Action rejected by user."})
                        continue

                # Loop killer: detect identical-call repetition within this turn.
                signature = self._call_signature(tc["name"], tc["arguments"])
                seen_signatures[signature] = seen_signatures.get(signature, 0) + 1
                if seen_signatures[signature] > LOOP_REPEAT_THRESHOLD:
                    self.metrics["loop_blocked"] += 1
                    repeat_count = seen_signatures[signature]
                    if on_event:
                        await on_event({
                            "type": "agent_hint",
                            "level": "warn",
                            "code": "LOOP_BLOCKED",
                            "tool": tc["name"],
                            "message": (
                                f"The agent tried '{tc['name']}' with the same arguments {repeat_count}× — "
                                "the loop killer stopped it and asked the model to change strategy."
                            ),
                        })
                    messages.append({
                        "role": "tool_result",
                        "tool_call_id": tc["id"],
                        "content": json.dumps({
                            "error": "REPEATED_CALL",
                            "tool": tc["name"],
                            "repeat_count": repeat_count,
                            "message": (
                                f"You have already called '{tc['name']}' with these exact arguments "
                                f"{repeat_count} times this turn — and it keeps producing the same result. "
                                "STOP repeating it. Choose ONE of: "
                                "(a) call a DIFFERENT tool, "
                                "(b) call the same tool with DIFFERENT arguments (fix what was wrong), "
                                "(c) ask the user a clarifying question. "
                                "Do NOT issue this exact call again."
                            ),
                        }),
                    })
                    if on_event:
                        await on_event({
                            "type": "tool_result",
                            "name": tc["name"],
                            "result": f"⚠ Loop detected — same call attempted {repeat_count}× this turn. Asking model to change approach.",
                        })
                    continue

                result = await execute_tool(self.smartsheet, tc["name"], tc["arguments"])
                self.metrics["tool_calls"] += 1

                # Inspect the result to update reliability counters: every
                # safety-net activation surfaces a structured `error` field
                # that we can recognise.
                try:
                    _parsed = json.loads(result)
                except (json.JSONDecodeError, TypeError):
                    _parsed = None
                if isinstance(_parsed, dict):
                    err_code = _parsed.get("error")
                    if err_code:
                        self.metrics["tool_errors"] += 1
                        if err_code == "UNKNOWN_COLUMNS":
                            self.metrics["schema_guard_triggered"] += 1
                            if on_event:
                                unknown = _parsed.get("unknown_columns", [])
                                await on_event({
                                    "type": "agent_hint",
                                    "level": "warn",
                                    "code": "SCHEMA_GUARD",
                                    "tool": tc["name"],
                                    "message": (
                                        f"Schema-guard blocked '{tc['name']}' — the model referenced "
                                        f"columns that don't exist on the sheet ({', '.join(unknown) or 'unknown'}). "
                                        "It will retry with the valid column names."
                                    ),
                                })

                if on_event:
                    try:
                        parsed = json.loads(result)
                        if isinstance(parsed, dict) and parsed.get("__is_image__"):
                            await on_event({
                                "type": "image",
                                "url": parsed["image_url"],
                                "caption": parsed.get("revised_prompt", ""),
                            })
                        elif isinstance(parsed, dict) and parsed.get("__is_chart__"):
                            await on_event({
                                "type": "chart",
                                "spec": parsed["chart_spec"],
                            })
                    except (json.JSONDecodeError, KeyError):
                        pass
                    preview = result[:500] + "..." if len(result) > 500 else result
                    await on_event({"type": "tool_result", "name": tc["name"], "result": preview})

                messages.append({
                    "role": "tool_result",
                    "tool_call_id": tc["id"],
                    "content": self._truncate_result(result),
                })

        self.metrics["rounds_exhausted"] += 1
        final = "I've reached the maximum number of tool calls. Please continue with a follow-up message."
        if on_event:
            await on_event({"type": "response", "content": final})
        return final
