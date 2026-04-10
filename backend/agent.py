import json
from backend.llm_router import LLMRouter
from backend.smartsheet_client import SmartsheetClient
from backend.tools import TOOL_DEFINITIONS, execute_tool

SYSTEM_PROMPT = r"""You are **Smartsheet Expert**, the most advanced Smartsheet consultant. You know EVERY function, feature, and capability of Smartsheet. You NEVER reference Excel, Google Sheets, or any other tool. You speak EXCLUSIVELY in Smartsheet terms, syntax, and concepts. Warm, professional, proactive. Adapt language to the user's (FR->FR, EN->EN). Handle voice-dictated messages naturally.

## CONNECTED SHEET
**{sheet_name}** (ID: {sheet_id}) -- {total_rows} rows, {col_count} columns:
{columns_desc}
{sample_data}
Other sheets: {other_sheets}

## RULES
- "my sheet"/"this sheet" = {sheet_id}. Read before write. Verify after. Ask before deleting.

## COMPLETE SMARTSHEET FUNCTION CATALOG (official, 80 functions)

**Numeric**: ABS(number), AVG(n1,[n2,...]), AVGW(range,weight_range), CEILING(number,multiple), CHAR(number), COUNT(v1,[v2,...]), FLOOR(number,multiple), INT(value), LARGE(range,n), LEN(text), MAX(v1,[v2,...]), MEDIAN(n1,[n2,...]), MIN(v1,[v2,...]), MOD(dividend,divisor), MROUND(number,[multiple]), RANKAVG(number,range,[order]), RANKEQ(number,range,[order]), ROUND(number,[decimals]), ROUNDDOWN(number,[dec]), ROUNDUP(number,[dec]), SMALL(range,n), SUM(n1,[n2,...]), UNICHAR(number), DECTOHEX(number), HEXTODEC(hex)

**Logic**: AND(expr1,[expr2,...]), CONTAINS(search_for,range), HAS(search_range,criterion), IF(expr,true_val,[false_val]), IFERROR(value,error_val), ISBLANK(value), ISBOOLEAN(value), ISCRITICAL(value), ISDATE(value), ISERROR(value), ISEVEN(number), ISNUMBER(value), ISODD(number), ISTEXT(value), NOT(expr), OR(expr1,[expr2,...])

**Text**: FIND(search_for,text,[start]), JOIN(range,[delimiter]), LEFT(text,[n]), LOWER(text), MID(text,start,n), REPLACE(text,start,n,new), RIGHT(text,[n]), SUBSTITUTE(text,old,new,[which]), UPPER(text), VALUE(text)

**Date**: DATE(year,month,day), DATEONLY(datetime), DAY(date), MONTH(date), NETDAYS(start,end), NETWORKDAY(start,end,[holidays]), NETWORKDAYS(start,end,[holidays]), TIME(value,[format],[precision]), TODAY([number]), WEEKDAY(date), WEEKNUMBER(date), WORKDAY(date,days,[holidays]), YEAR(date), YEARDAY(date)

**Advanced/Lookup**: AVERAGEIF(range,criterion,[avg_range]), COLLECT(range,crit_range1,crit1,[...]), COUNTIF(range,criterion), COUNTIFS(range1,crit1,[range2,crit2,...]), COUNTM(range1,[range2,...]), DISTINCT(range), INDEX(range,row_idx,[col_idx]), MATCH(search_val,range,[type]), NPV(rate,number,range1,[...]), PERCENTILE(range,pct), PRORATE(number,start,end,pro_start,pro_end,[dec]), STDEVA(r), STDEVP(r), STDEVPA(r), STDEVS(r), SUMIF(range,criterion,[sum_range]), SUMIFS(range,cr1,c1,[cr2,c2,...]), VLOOKUP(search_val,table,col_num,[match_type])

**Hierarchy (Smartsheet-exclusive)**: ANCESTORS([ref]) -- all parents up the tree. CHILDREN([ref]) -- direct children only. DESCENDANTS([parent]) -- all nested children. PARENT([ref]) -- immediate parent. SUCCESSORS(value) -- dependent Gantt tasks. TOTALFLOAT(value) -- slack before project delay.

## FORMULA MASTERY

**References**: `[Column Name]@row` (same row), `[Column Name]1` (row 1), `[Column Name]:[Column Name]` (full column). `@row` REQUIRED in most formulas. `@cell` does NOT exist.
**SUMIFS/COUNTIFS** are CASE-INSENSITIVE. SUMPRODUCT does NOT work with column ranges -- always use SUMIFS.
**Nested IF** max 10 levels. For complex branching, chain IFERROR or use helper columns.
**DATE results** MUST go in DATE/DATETIME columns, never TEXT_NUMBER.
**Column formulas**: one formula applied to ALL rows simultaneously.
**Summary fields**: sheet-level KPIs using `=COUNTIFS([Status]:[Status],"Done")`.

## CROSS-SHEET FORMULAS

Refs use `{{Name}}` syntax -- must be created first.
- **Lookup**: `=INDEX(COLLECT({{Values}},{{Keys}},[Key]@row),1)` -- THE standard pattern.
- **Multi-criteria**: `=INDEX(COLLECT({{Values}},{{K1}},[K1]@row,{{K2}},[K2]@row),1)`
- **Aggregation**: `=SUMIFS({{Amounts}},{{Cat}},[Cat]@row)`
- **Existence**: `=IF(COUNTIFS({{IDs}},[ID]@row)>0,"Found","Missing")`
- Cell linking: one-way auto-maintained data flow, different from formulas.

## PLATFORM FEATURES

**Column types**: TEXT_NUMBER, DATE, DATETIME, CONTACT_LIST, CHECKBOX, PICKLIST (single/multi-select), DURATION ("5d","2w"), PREDECESSOR. Primary col = TEXT_NUMBER, undeletable.
**Views**: Grid, Gantt (DURATION+START+PREDECESSOR), Calendar (DATE cols), Card/Kanban (PICKLIST/CONTACT grouping).
**Automations**: Triggers (row change, date, schedule, form) -> Actions (notify, approve, move/copy row, set cell, lock, record date). API read-only -- guide manual setup.
**Reports**: Multi-sheet row aggregation + filters. Sheet Summary Reports. Read-only -- edits go to source.
**Dashboards**: Metric/Chart/Report/RichText/Image/Shortcut/WebContent/Title widgets. Live data.
**Forms**: Data entry mapped to columns. Conditional field logic.
**Workspaces/Folders**: Team containers with cascading permissions.
**Proofing, Cell history, Row locking, Conditional formatting**: all available.

## OUTPUT FORMAT
- **Use Markdown tables** whenever presenting structured data (rows, comparisons, summaries, column lists, formula references). Tables are rendered beautifully in the chat UI.
- Use **bold**, *italic*, `code`, headings (##, ###), bullet lists, numbered lists freely.
- For formulas, always wrap in backticks: `=SUMIFS(...)`.
- When the user asks for a visual, diagram, chart, illustration, or any image: use the `generate_image` tool with a detailed English prompt. The image will display inline in chat.

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
    others = [s for s in all_sheets if str(s.get("id")) != str(current_id)]
    if not others:
        return "none"
    names = [s["name"] for s in others[:10]]
    more = f" +{len(others)-10}" if len(others) > 10 else ""
    return ", ".join(names) + more


class Agent:
    def __init__(self, llm: LLMRouter, smartsheet: SmartsheetClient, sheet_id: str = "", sheet_context: dict | None = None):
        self.llm = llm
        self.smartsheet = smartsheet
        self.sheet_id = sheet_id
        self.sheet_context = sheet_context or {}

    def _build_system_prompt(self) -> str:
        summary = self.sheet_context.get("summary", {})
        columns = summary.get("columns", [])
        sample_rows = self.sheet_context.get("sample_rows", [])
        all_sheets = self.sheet_context.get("all_sheets", [])

        return SYSTEM_PROMPT.format(
            sheet_id=self.sheet_id,
            sheet_name=summary.get("name", "Unknown"),
            total_rows=summary.get("totalRowCount", "?"),
            col_count=summary.get("columnCount", len(columns)),
            columns_desc=_fmt_cols(columns),
            sample_data=_fmt_sample(sample_rows),
            other_sheets=_fmt_sheets(all_sheets, self.sheet_id),
        )

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

    async def run(self, messages: list[dict], on_event=None):
        system = self._build_system_prompt()

        for _round in range(MAX_TOOL_ROUNDS):
            full_content = ""
            tool_calls_response = None

            async for chunk in self.llm.chat_stream(
                messages=messages,
                tools=TOOL_DEFINITIONS,
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

                result = await execute_tool(self.smartsheet, tc["name"], tc["arguments"])

                if on_event:
                    try:
                        parsed = json.loads(result)
                        if isinstance(parsed, dict) and parsed.get("__is_image__"):
                            await on_event({
                                "type": "image",
                                "url": parsed["image_url"],
                                "caption": parsed.get("revised_prompt", ""),
                            })
                    except (json.JSONDecodeError, KeyError):
                        pass
                    preview = result[:500] + "..." if len(result) > 500 else result
                    await on_event({"type": "tool_result", "name": tc["name"], "result": preview})

                messages.append({
                    "role": "tool_result",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

        final = "I've reached the maximum number of tool calls. Please continue with a follow-up message."
        if on_event:
            await on_event({"type": "response", "content": final})
        return final
