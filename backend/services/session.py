"""Session bootstrap: sheet context, welcome payload, in-memory session creation."""
from __future__ import annotations

import secrets
import time
import uuid

from backend.agent import Agent
from backend.core.helpers import detect_available_providers
from backend.core.state import sessions, touch
from backend import db as ssdb
from backend.llm_router import LLMRouter
from backend.logging_config import get_logger
from backend.smartsheet_client import SmartsheetClient

log = get_logger(__name__)


async def build_sheet_context(ss_client: SmartsheetClient, sheet_id: str) -> dict:
    summary = await ss_client.get_sheet_summary(sheet_id)

    sample_rows = []
    try:
        sheet_data = await ss_client.get_sheet(sheet_id, page_size=5)
        columns_by_id = {c["id"]: c["title"] for c in sheet_data.get("columns", [])}
        for row in sheet_data.get("rows", [])[:5]:
            row_dict = {}
            for cell in row.get("cells", []):
                col_name = columns_by_id.get(cell.get("columnId"), "?")
                val = cell.get("displayValue") or cell.get("value", "")
                if val is not None and str(val).strip():
                    row_dict[col_name] = str(val)
            if row_dict:
                sample_rows.append(row_dict)
    except Exception as e:
        log.warning(f"Could not fetch sample rows: {e}")

    all_sheets = []
    try:
        all_sheets = await ss_client.list_sheets()
    except Exception as e:
        log.warning(f"Could not list sheets: {e}")

    return {
        "summary": summary,
        "sample_rows": sample_rows,
        "all_sheets": all_sheets,
    }


def smart_starter_cards(summary: dict) -> list[dict]:
    """Heuristic, instant 'welcome dynamique': pick relevant starter actions
    based on the column types and names actually present in the sheet."""
    columns = summary.get("columns", [])
    rows = summary.get("totalRowCount", 0) or 0
    cards: list[dict] = []

    # Lowercased lookup helpers
    cols_by_lower = {(c.get("title") or "").strip().lower(): c for c in columns}
    types = {(c.get("title") or "").strip().lower(): (c.get("type") or "") for c in columns}

    def has(*needles: str) -> str | None:
        for needle in needles:
            for title in cols_by_lower:
                if needle in title:
                    return cols_by_lower[title].get("title")
        return None

    # Status / state column → overdue / by status
    status_col = has("status", "statut", "state", "etat", "état")
    if status_col:
        cards.append({
            "icon": "alert",
            "title": f"Group by {status_col}",
            "desc": f"Bucket rows by `{status_col}` and show counts per value.",
            "prompt": f"Group rows by {status_col} and show counts.",
        })

    # Date column → due / upcoming
    date_col = next(
        (c.get("title") for c in columns if (c.get("type") or "").upper() in {"DATE", "ABSTRACT_DATETIME", "DATETIME"}),
        None,
    )
    if date_col:
        cards.append({
            "icon": "alert",
            "title": "What's overdue",
            "desc": f"List rows where `{date_col}` is in the past and not done.",
            "prompt": f"List rows where {date_col} is in the past and the status is not Done or Complete.",
        })

    # Owner / assignee column
    owner_col = has("assigned to", "assignee", "owner", "responsable", "propriétaire", "proprietaire")
    if owner_col:
        cards.append({
            "icon": "share",
            "title": f"Workload by {owner_col}",
            "desc": f"Count rows per assignee in `{owner_col}`.",
            "prompt": f"Show me the workload distribution: count rows by {owner_col}.",
        })

    # Numeric / currency column → totals
    num_col = next(
        (c.get("title") for c in columns if (c.get("type") or "").upper() in {"TEXT_NUMBER", "PICKLIST"} and "amount" in (c.get("title") or "").lower()),
        None,
    ) or next(
        (c.get("title") for c in columns if (c.get("type") or "").upper() == "TEXT_NUMBER" and any(k in (c.get("title") or "").lower() for k in ("price", "cost", "budget", "total", "montant", "prix"))),
        None,
    )
    if num_col:
        cards.append({
            "icon": "chart",
            "title": f"Sum {num_col}",
            "desc": f"Compute the total of `{num_col}` across all rows.",
            "prompt": f"Sum the {num_col} column across all rows and show the total.",
        })

    # Always-on staples (only fill remaining slots so user always has 4 cards)
    staples = [
        {
            "icon": "compass",
            "title": "Tour the sheet",
            "desc": "Get a structured overview of columns, types, and row counts.",
            "prompt": "Show my sheet structure",
        },
        {
            "icon": "alert",
            "title": "Detect issues",
            "desc": "Find duplicates, blanks, broken formulas, or overdue rows.",
            "prompt": "Analyze problems and inconsistencies in this sheet",
        },
        {
            "icon": "rows",
            "title": "Read sample rows",
            "desc": f"Show the first 20 rows of {rows} total in a clean Markdown table." if rows else "Show the first 20 rows in a clean Markdown table.",
            "prompt": "Read the first 20 rows",
        },
        {
            "icon": "share",
            "title": "Permissions audit",
            "desc": "List who has access and at which permission level.",
            "prompt": "Who has access to this sheet?",
        },
    ]
    seen_titles = {c["title"] for c in cards}
    for st in staples:
        if len(cards) >= 4:
            break
        if st["title"] not in seen_titles:
            cards.append(st)

    return cards[:4]


def build_welcome(summary: dict) -> dict:
    name = summary.get("name", "Unknown")
    cols = summary.get("columnCount") or 0
    rows = summary.get("totalRowCount") or 0
    col_names = [c["title"] for c in summary.get("columns", [])[:8]]
    col_str = ", ".join(f"`{c}`" for c in col_names)
    more = f" +{len(summary.get('columns', [])) - 8} more" if len(summary.get("columns", [])) > 8 else ""

    hints = []
    if rows == 0:
        hints.append("\u26A0\uFE0F The sheet is currently empty.")
    elif rows > 1000:
        hints.append(f"\U0001F4CA Large sheet ({rows:,} rows). I'll sample when needed to stay fast.")
    if cols > 25:
        hints.append(f"\U0001F9F1 Wide sheet ({cols} columns). Ask me to focus on the columns that matter.")
    hint_block = ("\n\n" + " \u00B7 ".join(hints)) if hints else ""

    content = (
        f"### Connected to **{name}**\n\n"
        f"| Info | Value |\n|---|---|\n"
        f"| Rows | **{rows:,}** |\n"
        f"| Columns | **{cols}** |\n"
        f"| Structure | {col_str}{more} |"
        f"{hint_block}\n\n"
        f"I'm your Smartsheet expert. Pick a starting point below or ask me anything."
    )

    try_cards = smart_starter_cards(summary)
    suggestions = [c["prompt"] for c in try_cards]

    return {
        "type": "response",
        "content": content,
        "suggestions": suggestions,
        "try_cards": try_cards,
    }


async def create_session(ss_client: SmartsheetClient, sheet_id: str, llm: LLMRouter, smartsheet_token: str = "") -> dict:
    ctx = await build_sheet_context(ss_client, sheet_id)
    summary = ctx["summary"]

    user_info: dict | None = None
    try:
        user_info = await ss_client.get_current_user()
    except Exception:
        user_info = None

    db_user_id: int | None = None
    auth_cookie: str | None = None
    if user_info and smartsheet_token:
        try:
            db_user_id = await ssdb.upsert_user(smartsheet_token, user_info)
            auth_cookie = await ssdb.create_auth_session(db_user_id)
        except Exception as e:
            log.warning(f"User persistence failed: {e}")

    session_id = uuid.uuid4().hex
    ws_token = secrets.token_urlsafe(32)
    now = time.monotonic()

    sessions[session_id] = {
        "smartsheet": ss_client,
        "llm": llm,
        "agent": Agent(llm, ss_client, sheet_id, ctx),
        "messages": [],
        "sheet_id": sheet_id,
        "sheet_name": summary["name"],
        "context": ctx,
        "ws_token": ws_token,
        "created_at": now,
        "last_activity": now,
        "user": user_info,
        "db_user_id": db_user_id,
        "auth_cookie": auth_cookie,
        "active_conversation_id": None,
    }

    all_sheets = [{"id": s.get("id"), "name": s.get("name")} for s in ctx.get("all_sheets", [])]

    log.info(
        f"Session created for sheet '{summary['name']}' ({sheet_id})",
        extra={"session_id": session_id},
    )

    return {
        "session_id": session_id,
        "ws_token": ws_token,
        "auth_cookie": auth_cookie,
        "db_user_id": db_user_id,
        "sheet": summary,
        "all_sheets": all_sheets,
        "welcome": build_welcome(summary),
        "current_model": llm.model,
        "current_provider": llm.provider,
        "available_providers": detect_available_providers(),
        "user": {
            "email": (user_info or {}).get("email"),
            "firstName": (user_info or {}).get("firstName"),
            "lastName": (user_info or {}).get("lastName"),
        } if user_info else None,
    }


def _resolve_api_key(provider: str, user_key: str = "") -> str:
    if user_key:
        return user_key
    info = PROVIDERS.get(provider)
    if info:
        return os.getenv(info["env_key"], "").strip()
    return ""
