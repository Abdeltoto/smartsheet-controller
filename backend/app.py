import os
import json
import asyncio
import secrets
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from backend.logging_config import setup_logging, get_logger
from backend.smartsheet_client import SmartsheetClient, SmartsheetRateLimitError
from backend.llm_router import LLMRouter, PROVIDERS, get_provider_info
from backend.agent import Agent
from backend.rate_limit import check_limit, rate_limiter
from backend import db as ssdb

setup_logging(os.getenv("LOG_LEVEL", "INFO"))
log = get_logger(__name__)

load_dotenv(override=True)


SESSION_IDLE_TIMEOUT = int(os.getenv("SESSION_IDLE_TIMEOUT", "1800"))
SESSION_CLEANUP_INTERVAL = 300
APP_START_TIME = time.monotonic()

sessions: dict[str, dict] = {}
watchers: dict[str, asyncio.Task] = {}
_cleanup_task: asyncio.Task | None = None


async def _cleanup_idle_sessions() -> None:
    """Background task: evict sessions idle longer than SESSION_IDLE_TIMEOUT."""
    while True:
        try:
            await asyncio.sleep(SESSION_CLEANUP_INTERVAL)
            now = time.monotonic()
            expired = [
                sid for sid, s in sessions.items()
                if now - s.get("last_activity", now) > SESSION_IDLE_TIMEOUT
            ]
            for sid in expired:
                session = sessions.pop(sid, None)
                if not session:
                    continue
                log.info(f"Evicting idle session (idle {int(now - session.get('last_activity', now))}s)", extra={"session_id": sid})
                ss_client: SmartsheetClient | None = session.get("smartsheet")
                if ss_client:
                    try:
                        await ss_client.close()
                    except Exception:
                        pass
                task = watchers.pop(sid, None)
                if task and not task.done():
                    task.cancel()
                rate_limiter.clear(sid)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning(f"Cleanup loop error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cleanup_task
    await ssdb.init_db()
    _cleanup_task = asyncio.create_task(_cleanup_idle_sessions())
    log.info(f"App started (idle timeout {SESSION_IDLE_TIMEOUT}s)")
    try:
        yield
    finally:
        if _cleanup_task and not _cleanup_task.done():
            _cleanup_task.cancel()
        for task in list(watchers.values()):
            if not task.done():
                task.cancel()
        for session in list(sessions.values()):
            ss_client = session.get("smartsheet")
            if ss_client:
                try:
                    await ss_client.close()
                except Exception:
                    pass
        sessions.clear()
        watchers.clear()


app = FastAPI(title="Smartsheet Controller", lifespan=lifespan)


def _touch(session_id: str) -> None:
    s = sessions.get(session_id)
    if s is not None:
        s["last_activity"] = time.monotonic()


def _friendly_error(exc: Exception) -> str:
    """Map exception to user-safe message. Full trace is logged separately."""
    if isinstance(exc, SmartsheetRateLimitError):
        return str(exc)
    msg = str(exc)
    if "401" in msg or "Unauthorized" in msg:
        return "Smartsheet authentication failed. Check your API token."
    if "403" in msg or "Forbidden" in msg:
        return "Access denied. Your token may not have permission for this resource."
    if "404" in msg or "Not Found" in msg:
        return "Resource not found. Check the sheet ID."
    if "timeout" in msg.lower():
        return "Request timed out. Please try again."
    if "quota" in msg.lower():
        return "LLM API quota exceeded. Check your provider billing or switch models."
    if "rate" in msg.lower() and "limit" in msg.lower():
        return "Rate limit reached. Please wait a moment and retry."
    return "An unexpected error occurred. Please retry."


def _detect_available_providers() -> dict:
    available = {}
    for name, info in PROVIDERS.items():
        key = os.getenv(info["env_key"], "").strip()
        if key:
            available[name] = {
                "default_model": info["default_model"],
                "models": info["models"],
            }
    return available


@app.get("/health")
async def health():
    uptime = int(time.monotonic() - APP_START_TIME)
    return {
        "status": "ok",
        "sessions": len(sessions),
        "watchers": len(watchers),
        "uptime_seconds": uptime,
    }


@app.get("/api/env-status")
async def env_status():
    ss_token = os.getenv("SMARTSHEET_TOKEN", "").strip()
    sheet_id = os.getenv("SHEET_ID", "").strip()
    available = _detect_available_providers()
    first_provider = next(iter(available), "")

    return {
        "ready": bool(ss_token and sheet_id and available),
        "has_smartsheet_token": bool(ss_token),
        "has_sheet_id": bool(sheet_id),
        "sheet_id": sheet_id,
        "provider": first_provider,
        "has_llm_key": bool(available),
        "available_providers": available,
    }


@app.get("/api/providers")
async def list_providers():
    return get_provider_info()


class ValidateTokenRequest(BaseModel):
    smartsheet_token: str


@app.post("/api/validate-token")
async def validate_token(req: ValidateTokenRequest):
    """Validate a Smartsheet token and return user info + sheet list in one call.

    Used by the BYOT connect form: user pastes their token, we confirm who
    they are and populate the sheet browser in step 2.
    """
    token = req.smartsheet_token.strip()
    if not token or len(token) < 16:
        return JSONResponse({"error": "Token looks too short. Please paste your full Smartsheet API token."}, status_code=400)

    ss_client = SmartsheetClient(token)
    try:
        user = await ss_client.get_current_user()
        sheets = await ss_client.list_sheets()
    except Exception as e:
        await ss_client.close()
        log.warning(f"validate-token failed: {traceback.format_exc().splitlines()[-1]}")
        return JSONResponse({"error": _friendly_error(e)}, status_code=400)

    try:
        await ss_client.close()
    except Exception:
        pass

    available = _detect_available_providers()
    return {
        "user": {
            "id": user.get("id"),
            "email": user.get("email"),
            "firstName": user.get("firstName"),
            "lastName": user.get("lastName"),
            "account": (user.get("account") or {}).get("name"),
        },
        "sheets": sheets,
        "available_providers": available,
    }


class LookupSheetRequest(BaseModel):
    smartsheet_token: str
    sheet_id: str


@app.post("/api/lookup-sheet")
async def lookup_sheet(req: LookupSheetRequest):
    """Resolve a sheet ID against the given token.

    Used by the connect wizard "By ID" tab : user pastes a sheet ID and we
    confirm the sheet exists and is accessible before letting them start a
    session on it. Returns the sheet name and a quick summary on success.
    """
    token = req.smartsheet_token.strip()
    sheet_id = req.sheet_id.strip()
    if not token or len(token) < 16:
        return JSONResponse({"error": "Smartsheet token missing or too short."}, status_code=400)
    if not sheet_id.isdigit():
        return JSONResponse({"error": "Sheet ID must be numeric (e.g. 7340597274509188)."}, status_code=400)

    ss_client = SmartsheetClient(token)
    try:
        summary = await ss_client.get_sheet_summary(sheet_id)
    except Exception as e:
        await ss_client.close()
        log.warning(f"lookup-sheet failed for {sheet_id}: {traceback.format_exc().splitlines()[-1]}")
        return JSONResponse({"error": _friendly_error(e)}, status_code=400)

    try:
        await ss_client.close()
    except Exception:
        pass

    return {
        "id": sheet_id,
        "name": summary.get("name", ""),
        "row_count": summary.get("totalRowCount", 0),
        "column_count": summary.get("columnCount", 0),
    }


class CreateBlankSheetRequest(BaseModel):
    smartsheet_token: str
    name: str
    columns: list[dict] | None = None


@app.post("/api/create-sheet")
async def create_blank_sheet(req: CreateBlankSheetRequest):
    """Create a brand-new sheet with sensible starter columns.

    Used by the connect wizard "Create new" tab. The user gives a name, the
    server creates a blank sheet (Task / Status / Due Date / Notes by default
    or caller-provided columns), and the new ID flows back to the wizard so
    the session opens directly on it.
    """
    token = req.smartsheet_token.strip()
    name = (req.name or "").strip()
    if not token or len(token) < 16:
        return JSONResponse({"error": "Smartsheet token missing or too short."}, status_code=400)
    if not name:
        return JSONResponse({"error": "Please give the new sheet a name."}, status_code=400)
    if len(name) > 50:
        return JSONResponse({"error": "Sheet name must be 50 characters or fewer."}, status_code=400)

    columns = req.columns or [
        {"title": "Task", "primary": True, "type": "TEXT_NUMBER"},
        {"title": "Status", "type": "PICKLIST", "options": ["Not Started", "In Progress", "Done"]},
        {"title": "Due Date", "type": "DATE"},
        {"title": "Notes", "type": "TEXT_NUMBER"},
    ]

    ss_client = SmartsheetClient(token)
    try:
        created = await ss_client.create_sheet(name, columns)
    except Exception as e:
        await ss_client.close()
        log.warning(f"create-sheet failed for '{name}': {traceback.format_exc().splitlines()[-1]}")
        return JSONResponse({"error": _friendly_error(e)}, status_code=400)

    try:
        await ss_client.close()
    except Exception:
        pass

    payload = created.get("result") or created.get("data") or created
    new_id = payload.get("id") or created.get("id")
    if not new_id:
        return JSONResponse({"error": "Sheet created but Smartsheet did not return an ID."}, status_code=500)

    log.info(f"Blank sheet '{name}' created (id={new_id})")
    return {
        "id": str(new_id),
        "name": payload.get("name") or name,
        "permalink": payload.get("permalink", ""),
    }


class SessionConfig(BaseModel):
    smartsheet_token: str
    sheet_id: str
    llm_provider: str = "openai"
    llm_model: str = ""
    llm_api_key: str = ""


async def _build_sheet_context(ss_client: SmartsheetClient, sheet_id: str) -> dict:
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


def _smart_starter_cards(summary: dict) -> list[dict]:
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


def _build_welcome(summary: dict) -> dict:
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

    try_cards = _smart_starter_cards(summary)
    suggestions = [c["prompt"] for c in try_cards]

    return {
        "type": "response",
        "content": content,
        "suggestions": suggestions,
        "try_cards": try_cards,
    }


async def _create_session(ss_client: SmartsheetClient, sheet_id: str, llm: LLMRouter, smartsheet_token: str = "") -> dict:
    ctx = await _build_sheet_context(ss_client, sheet_id)
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
        "welcome": _build_welcome(summary),
        "current_model": llm.model,
        "current_provider": llm.provider,
        "available_providers": _detect_available_providers(),
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


@app.post("/api/session")
async def create_session(config: SessionConfig):
    provider = config.llm_provider.lower()
    model = config.llm_model.strip()
    if not model:
        info = PROVIDERS.get(provider, {})
        model = info.get("default_model", "gpt-4o-mini") if info else "gpt-4o-mini"

    api_key = _resolve_api_key(provider, config.llm_api_key)
    if not api_key:
        return JSONResponse(
            {"error": f"No API key for {provider}. Provide one or set {PROVIDERS.get(provider, {}).get('env_key', '???')} in .env"},
            status_code=400,
        )

    token = config.smartsheet_token.strip()
    if not token:
        return JSONResponse({"error": "Smartsheet token is required."}, status_code=400)

    sheet_id = config.sheet_id.strip()
    if not sheet_id.isdigit():
        return JSONResponse({"error": "Sheet ID must be a numeric Smartsheet sheet ID."}, status_code=400)

    ss_client = SmartsheetClient(token)
    try:
        result = await _create_session(ss_client, sheet_id, LLMRouter(provider, model, api_key), smartsheet_token=token)
    except Exception as e:
        await ss_client.close()
        log.warning(f"Session creation failed: {traceback.format_exc().splitlines()[-1]}")
        return JSONResponse({"error": _friendly_error(e)}, status_code=400)

    return result


@app.post("/api/quick-connect")
async def quick_connect():
    ss_token = os.getenv("SMARTSHEET_TOKEN", "").strip()
    sheet_id = os.getenv("SHEET_ID", "").strip()

    if not ss_token:
        return JSONResponse({"error": "SMARTSHEET_TOKEN not set in .env"}, status_code=400)
    if not sheet_id:
        return JSONResponse({"error": "SHEET_ID not set in .env"}, status_code=400)

    available = _detect_available_providers()
    if not available:
        return JSONResponse({"error": "No LLM API key in .env"}, status_code=400)

    provider = next(iter(available))
    info = PROVIDERS[provider]
    api_key = os.getenv(info["env_key"], "").strip()
    model = info["default_model"]

    ss_client = SmartsheetClient(ss_token)
    try:
        result = await _create_session(ss_client, sheet_id, LLMRouter(provider, model, api_key), smartsheet_token=ss_token)
    except Exception as e:
        await ss_client.close()
        log.warning(f"Quick-connect failed: {traceback.format_exc().splitlines()[-1]}")
        return JSONResponse({"error": _friendly_error(e)}, status_code=400)

    return result


class SwitchSheetRequest(BaseModel):
    session_id: str
    sheet_id: str


@app.post("/api/switch-sheet")
async def switch_sheet(req: SwitchSheetRequest):
    session = sessions.get(req.session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)

    _touch(req.session_id)

    ss_client: SmartsheetClient = session["smartsheet"]
    try:
        ctx = await _build_sheet_context(ss_client, req.sheet_id)
    except Exception as e:
        log.warning(f"switch-sheet failed: {e}")
        return JSONResponse({"error": _friendly_error(e)}, status_code=400)

    summary = ctx["summary"]
    session["agent"] = Agent(session["llm"], ss_client, req.sheet_id, ctx)
    session["messages"] = []
    session["sheet_id"] = req.sheet_id
    session["sheet_name"] = summary["name"]
    session["context"] = ctx

    return {"sheet": summary, "welcome": _build_welcome(summary)}


class PinSheetRequest(BaseModel):
    session_id: str
    sheet_id: str


@app.post("/api/pin-sheet")
async def pin_sheet(req: PinSheetRequest):
    session = sessions.get(req.session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)

    _touch(req.session_id)
    pinned: list = session.setdefault("pinned_sheets", [])
    if any(str(p["id"]) == str(req.sheet_id) for p in pinned):
        return JSONResponse({"error": "Sheet already pinned"}, status_code=400)
    if len(pinned) >= 3:
        return JSONResponse({"error": "Max 3 pinned sheets"}, status_code=400)

    ss_client: SmartsheetClient = session["smartsheet"]
    try:
        summary = await ss_client.get_sheet_summary(req.sheet_id)
    except Exception as e:
        return JSONResponse({"error": _friendly_error(e)}, status_code=400)

    pinned.append({"id": req.sheet_id, "name": summary["name"], "summary": summary})
    session["agent"].pinned_sheets = pinned

    return {"pinned": [{"id": p["id"], "name": p["name"]} for p in pinned]}


@app.post("/api/unpin-sheet")
async def unpin_sheet(req: PinSheetRequest):
    session = sessions.get(req.session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)

    _touch(req.session_id)
    pinned: list = session.get("pinned_sheets", [])
    pinned[:] = [p for p in pinned if str(p["id"]) != str(req.sheet_id)]
    session["agent"].pinned_sheets = pinned

    return {"pinned": [{"id": p["id"], "name": p["name"]} for p in pinned]}


class SwitchModelRequest(BaseModel):
    session_id: str
    provider: str
    model: str
    api_key: str | None = None


@app.post("/api/switch-model")
async def switch_model(req: SwitchModelRequest):
    session = sessions.get(req.session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)

    _touch(req.session_id)
    provider = req.provider.lower()
    model = req.model.strip()

    if provider not in PROVIDERS:
        return JSONResponse({"error": f"Unknown provider: {provider}"}, status_code=400)

    # Priority: explicit api_key from request > env var
    api_key = (req.api_key or "").strip() or _resolve_api_key(provider)
    if not api_key:
        return JSONResponse(
            {
                "error": f"No API key for {provider}. Provide one in the request body "
                f"or set {PROVIDERS[provider]['env_key']} in .env",
                "needs_key": True,
                "provider": provider,
            },
            status_code=400,
        )

    old_llm = session["llm"]
    if old_llm.provider == provider:
        old_llm.switch_model(model)
    else:
        new_llm = LLMRouter(provider, model, api_key)
        session["llm"] = new_llm
        session["agent"].llm = new_llm

    return {"provider": provider, "model": model}


class DisconnectRequest(BaseModel):
    session_id: str


@app.get("/api/usage")
async def get_usage(session_id: str):
    """Token usage and cache stats for a session — exposed in Settings."""
    session = sessions.get(session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)
    _touch(session_id)
    llm = session.get("llm")
    ss_client = session.get("smartsheet")
    agent = session.get("agent")
    return {
        "tokens": llm.usage if llm else None,
        "provider": llm.provider if llm else None,
        "current_model": llm.model if llm else None,
        "cache": ss_client.cache_stats() if ss_client else None,
        # Agent reliability metrics: each counter ticks every time a safety
        # net (loop killer, schema-guard, parse recovery) catches a model
        # mistake, so the user can see in Settings how often the harness
        # actually saved the day.
        "agent_metrics": agent.metrics if agent and hasattr(agent, "metrics") else None,
    }


@app.post("/api/disconnect")
async def disconnect(req: DisconnectRequest):
    session = sessions.pop(req.session_id, None)
    if not session:
        return {"status": "ok"}

    task = watchers.pop(req.session_id, None)
    if task and not task.done():
        task.cancel()

    ss_client = session.get("smartsheet")
    if ss_client:
        try:
            await ss_client.close()
        except Exception:
            pass

    rate_limiter.clear(req.session_id)
    log.info("Session disconnected", extra={"session_id": req.session_id})
    return {"status": "ok"}


class CsvImportRequest(BaseModel):
    session_id: str
    name: str
    headers: list[str]
    rows: list[list[str]]


@app.post("/api/csv-to-sheet")
async def csv_to_sheet(req: CsvImportRequest):
    session = sessions.get(req.session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=404)
    _touch(req.session_id)

    name = (req.name or "").strip()
    if not name:
        return JSONResponse({"error": "Sheet name is required"}, status_code=400)
    if not req.headers:
        return JSONResponse({"error": "At least one column is required"}, status_code=400)
    if len(req.headers) > 200:
        return JSONResponse({"error": "Smartsheet supports max 200 columns"}, status_code=400)

    ss_client = session["smartsheet"]
    columns = []
    for i, h in enumerate(req.headers):
        title = (h or f"Column {i + 1}").strip()[:50] or f"Column {i + 1}"
        col = {"title": title, "type": "TEXT_NUMBER"}
        if i == 0:
            col["primary"] = True
        columns.append(col)

    try:
        created = await ss_client.create_sheet(name, columns)
        sheet_payload = created.get("result") or created.get("data") or created
        new_sheet_id = sheet_payload.get("id") or created.get("id")
        if not new_sheet_id:
            return JSONResponse({"error": "Sheet created but ID not returned"}, status_code=500)

        rows_to_add = []
        for row in req.rows[:5000]:
            mapped = {}
            for i, val in enumerate(row[: len(req.headers)]):
                col_title = columns[i]["title"]
                mapped[col_title] = val
            if mapped:
                rows_to_add.append(mapped)

        added = 0
        if rows_to_add:
            BATCH = 250
            for i in range(0, len(rows_to_add), BATCH):
                chunk = rows_to_add[i : i + BATCH]
                await ss_client.add_rows(str(new_sheet_id), chunk, to_bottom=True)
                added += len(chunk)

        log.info(
            f"CSV imported as sheet '{name}' ({added} rows, {len(req.headers)} cols)",
            extra={"session_id": req.session_id},
        )
        return {
            "status": "ok",
            "sheet_id": str(new_sheet_id),
            "name": name,
            "rows_added": added,
            "columns": len(req.headers),
        }
    except Exception as e:
        log.error(f"CSV import failed: {e}", extra={"session_id": req.session_id})
        return JSONResponse({"error": str(e)}, status_code=500)


# ──────────────── Sprint 5 — Server-side persistence ────────────────

class ConvSaveRequest(BaseModel):
    session_id: str
    conversation_id: str
    title: str | None = None


@app.post("/api/conversations/save")
async def save_conv(req: ConvSaveRequest):
    """Create/update conversation metadata (title, sheet)."""
    session = sessions.get(req.session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)
    user_id = session.get("db_user_id")
    if not user_id:
        return JSONResponse({"error": "User not persisted"}, status_code=400)
    _touch(req.session_id)
    await ssdb.save_conversation(req.conversation_id, user_id, session.get("sheet_id"), req.title)
    session["active_conversation_id"] = req.conversation_id
    return {"status": "ok", "conversation_id": req.conversation_id}


@app.get("/api/conversations")
async def list_conv(session_id: str):
    session = sessions.get(session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)
    user_id = session.get("db_user_id")
    if not user_id:
        return {"conversations": []}
    _touch(session_id)
    return {"conversations": await ssdb.list_conversations(user_id)}


@app.get("/api/conversations/{conv_id}")
async def get_conv(conv_id: str, session_id: str):
    session = sessions.get(session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)
    user_id = session.get("db_user_id")
    if not user_id:
        return {"messages": []}
    _touch(session_id)
    msgs = await ssdb.get_conversation_messages(conv_id, user_id)
    return {"conversation_id": conv_id, "messages": msgs}


class ConvDeleteRequest(BaseModel):
    session_id: str
    conversation_id: str


@app.post("/api/conversations/delete")
async def delete_conv(req: ConvDeleteRequest):
    session = sessions.get(req.session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)
    user_id = session.get("db_user_id")
    if not user_id:
        return JSONResponse({"error": "User not persisted"}, status_code=400)
    _touch(req.session_id)
    ok = await ssdb.delete_conversation(req.conversation_id, user_id)
    return {"status": "ok" if ok else "not_found"}


class MigrationRequest(BaseModel):
    session_id: str
    conversations: list[dict]  # [{id, title, sheet_id?, messages:[{role, content}]}]


@app.post("/api/conversations/migrate")
async def migrate_conv(req: MigrationRequest):
    """Bulk import localStorage conversations into the DB on first login."""
    session = sessions.get(req.session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)
    user_id = session.get("db_user_id")
    if not user_id:
        return JSONResponse({"error": "User not persisted"}, status_code=400)
    _touch(req.session_id)
    imported = 0
    for c in req.conversations:
        cid = c.get("id") or uuid.uuid4().hex
        await ssdb.save_conversation(cid, user_id, c.get("sheet_id"), c.get("title"))
        for m in c.get("messages") or []:
            role = m.get("role")
            if role not in ("user", "assistant", "tool"):
                continue
            content = m.get("content") or ""
            if not content.strip():
                continue
            await ssdb.append_message(cid, role, content)
        imported += 1
    return {"status": "ok", "imported": imported}


@app.get("/api/audit")
async def get_audit(session_id: str, sheet_id: str | None = None, limit: int = 200):
    session = sessions.get(session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)
    user_id = session.get("db_user_id")
    if not user_id:
        return {"entries": []}
    _touch(session_id)
    return {"entries": await ssdb.list_audit(user_id, limit=min(limit, 1000), sheet_id=sheet_id)}


@app.get("/api/favorites")
async def get_favs(session_id: str):
    session = sessions.get(session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)
    user_id = session.get("db_user_id")
    if not user_id:
        return {"favorites": []}
    _touch(session_id)
    return {"favorites": await ssdb.list_favorites(user_id)}


class FavRequest(BaseModel):
    session_id: str
    sheet_id: str
    sheet_name: str | None = None


@app.post("/api/favorites/add")
async def add_fav(req: FavRequest):
    session = sessions.get(req.session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)
    user_id = session.get("db_user_id")
    if not user_id:
        return JSONResponse({"error": "User not persisted"}, status_code=400)
    _touch(req.session_id)
    await ssdb.add_favorite(user_id, req.sheet_id, req.sheet_name)
    return {"status": "ok"}


@app.post("/api/favorites/remove")
async def remove_fav(req: FavRequest):
    session = sessions.get(req.session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)
    user_id = session.get("db_user_id")
    if not user_id:
        return JSONResponse({"error": "User not persisted"}, status_code=400)
    _touch(req.session_id)
    await ssdb.remove_favorite(user_id, req.sheet_id)
    return {"status": "ok"}


@app.get("/api/export")
async def export_account(session_id: str):
    """RGPD-style: download a JSON dump of everything the server knows about the user."""
    session = sessions.get(session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)
    user_id = session.get("db_user_id")
    if not user_id:
        return JSONResponse({"error": "User not persisted"}, status_code=400)
    _touch(session_id)
    data = await ssdb.export_user_data(user_id)
    headers = {"Content-Disposition": f'attachment; filename="smartsheet-controller-export-{int(time.time())}.json"'}
    return JSONResponse(data, headers=headers)


@app.get("/api/webhook-events")
async def get_webhook_events(session_id: str, since: float = 0.0, limit: int = 50):
    """Polled by the frontend for real-time toasts when a webhook fires."""
    session = sessions.get(session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)
    user_id = session.get("db_user_id")
    if not user_id:
        return {"events": []}
    _touch(session_id)
    return {"events": await ssdb.list_webhook_events(user_id, limit=min(limit, 200), since=since)}


@app.post("/api/smartsheet-webhook")
async def smartsheet_webhook(payload: dict):
    """Inbound endpoint for Smartsheet webhook callbacks.

    Smartsheet sends a verification challenge first (no events). After that,
    every event is persisted and the user's sessions can pick it up by polling
    /api/webhook-events?since=<ts>.
    """
    # 1) Verification handshake
    challenge = payload.get("challenge")
    if challenge:
        return {"smartsheetHookResponse": challenge}

    # 2) Real events
    webhook_id = payload.get("webhookId")
    sheet_id = str(payload.get("scopeObjectId") or "") or None
    events = payload.get("events", []) or []

    # Map sheet_id → user_id by checking sessions in memory.
    user_ids: set[int] = set()
    for s in sessions.values():
        if s.get("db_user_id") and (str(s.get("sheet_id")) == sheet_id or sheet_id is None):
            user_ids.add(s["db_user_id"])

    if not user_ids:
        # Persist anonymously so the UI shows it next time the user logs in
        user_ids = {None}  # type: ignore

    saved = 0
    for uid in user_ids:
        for ev in events:
            await ssdb.record_webhook_event(uid, sheet_id, webhook_id, ev.get("eventType"), ev)
            saved += 1

    log.info(f"Webhook event received (webhook={webhook_id} sheet={sheet_id} events={len(events)} fanout={saved})")
    return {"status": "received", "stored": saved}


# ───────────────────────── Bug reports ─────────────────────────
#
# Lightweight in-app bug box: every user can send a report with a
# free-text description plus an optional auto-collected client
# context bundle (last messages, agent metrics, browser, …). Reports
# are persisted in SQLite (table bug_reports) AND mirrored to a
# JSONL append-only file under data/ so they survive even if the DB
# gets re-initialised. The admin GET endpoint is gated by the
# BUG_REPORTS_ADMIN_TOKEN env var (no env → endpoint disabled).

BUG_REPORTS_JSONL = Path(os.getenv(
    "BUG_REPORTS_JSONL_PATH", "data/bug_reports.jsonl"
))
_BUG_DESC_MAX = 8000
_BUG_STEPS_MAX = 4000
_BUG_CTX_MAX = 64000  # serialised JSON length budget


def _admin_token_ok(provided: str | None) -> bool:
    expected = os.getenv("BUG_REPORTS_ADMIN_TOKEN", "")
    if not expected:
        return False  # endpoint disabled
    if not provided:
        return False
    return secrets.compare_digest(expected, provided)


def _append_bug_jsonl(record: dict) -> None:
    """Append a single record to the bug-report JSONL mirror.

    Best-effort — failure to write must NEVER break the API response.
    """
    try:
        BUG_REPORTS_JSONL.parent.mkdir(parents=True, exist_ok=True)
        with BUG_REPORTS_JSONL.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning(f"bug-report jsonl mirror failed: {e}")


class BugReportRequest(BaseModel):
    description: str
    session_id: str | None = None
    steps: str | None = None
    severity: str | None = "normal"   # low / normal / high / blocker
    reporter_email: str | None = None
    reporter_name: str | None = None
    context: dict | None = None       # client-collected bundle


@app.post("/api/bug-reports")
async def submit_bug_report(req: BugReportRequest, request: Request):
    """Public endpoint — anyone using the app can file a bug.

    We do NOT require a session: a bug can occur at the login screen.
    If `session_id` is provided we attach the user_id and sheet_id
    server-side so the report is fully contextualised.
    """
    desc = (req.description or "").strip()
    if not desc:
        return JSONResponse(
            {"error": "description is required"}, status_code=400,
        )
    if len(desc) > _BUG_DESC_MAX:
        desc = desc[:_BUG_DESC_MAX]
    steps = (req.steps or "").strip()[:_BUG_STEPS_MAX] or None
    severity = (req.severity or "normal").strip().lower()
    reporter_email = (req.reporter_email or "").strip()[:320] or None
    reporter_name = (req.reporter_name or "").strip()[:120] or None

    user_id: int | None = None
    sheet_id: str | None = None
    if req.session_id:
        session = sessions.get(req.session_id)
        if session:
            _touch(req.session_id)
            user_id = session.get("db_user_id")
            sheet_id = str(session.get("sheet_id") or "") or None

    # Enrich the context with server-side facts the client cannot fake.
    ctx = dict(req.context or {})
    ctx.setdefault("server_time", time.time())
    ctx.setdefault("client_ip", request.client.host if request.client else None)
    ua = request.headers.get("user-agent")
    if ua:
        ctx.setdefault("user_agent", ua)
    # Attach a lightweight snapshot of agent metrics if we have a session.
    if req.session_id:
        sess = sessions.get(req.session_id) or {}
        agent = sess.get("agent")
        if agent is not None and hasattr(agent, "metrics"):
            ctx.setdefault("agent_metrics_snapshot", dict(agent.metrics))
        llm = sess.get("llm")
        if llm is not None:
            ctx.setdefault("llm_provider", getattr(llm, "provider", None))
            ctx.setdefault("llm_model", getattr(llm, "model", None))

    # Cap serialised context size.
    try:
        ctx_serialised = json.dumps(ctx, default=str)
    except (TypeError, ValueError):
        ctx_serialised = "{}"
    if len(ctx_serialised) > _BUG_CTX_MAX:
        ctx = {"_truncated": True, "_original_size": len(ctx_serialised)}

    report_id = await ssdb.create_bug_report(
        user_id=user_id,
        session_id=req.session_id,
        sheet_id=sheet_id,
        reporter_email=reporter_email,
        reporter_name=reporter_name,
        description=desc,
        steps=steps,
        severity=severity,
        context=ctx,
    )

    _append_bug_jsonl({
        "id": report_id,
        "created_at": time.time(),
        "user_id": user_id,
        "session_id": req.session_id,
        "sheet_id": sheet_id,
        "reporter_email": reporter_email,
        "reporter_name": reporter_name,
        "severity": severity,
        "description": desc,
        "steps": steps,
        "context": ctx,
    })

    log.info(
        f"bug-report #{report_id} filed "
        f"(severity={severity} sheet={sheet_id} user={user_id})"
    )
    return {"status": "ok", "id": report_id}


@app.get("/api/bug-reports")
async def list_bug_reports(
    request: Request,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
):
    """Admin endpoint. Disabled unless BUG_REPORTS_ADMIN_TOKEN is set
    in the environment AND the request carries a matching
    `X-Admin-Token` header."""
    if not _admin_token_ok(request.headers.get("X-Admin-Token")):
        return JSONResponse(
            {"error": "forbidden"}, status_code=403,
        )
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    items = await ssdb.list_bug_reports(status=status, limit=limit, offset=offset)
    total = await ssdb.count_bug_reports(status=status)
    return {"total": total, "limit": limit, "offset": offset, "items": items}


class BugReportStatusUpdate(BaseModel):
    status: str   # open / triaged / fixed / wontfix


@app.post("/api/bug-reports/{report_id}/status")
async def update_bug_report_status_route(
    report_id: int, req: BugReportStatusUpdate, request: Request,
):
    if not _admin_token_ok(request.headers.get("X-Admin-Token")):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    ok = await ssdb.update_bug_report_status(report_id, req.status)
    if not ok:
        return JSONResponse(
            {"error": "not_found_or_invalid_status"}, status_code=404,
        )
    return {"status": "ok"}


class GenerateTitleRequest(BaseModel):
    session_id: str
    snippet: str


@app.post("/api/generate-title")
async def generate_title(req: GenerateTitleRequest):
    session = sessions.get(req.session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=404)

    _touch(req.session_id)
    llm = session.get("llm")
    if llm is None:
        return {"title": ""}

    snippet = (req.snippet or "").strip()
    if not snippet:
        return {"title": ""}
    snippet = snippet[:2400]

    system = (
        "You generate short, descriptive titles for chat conversations. "
        "Respond with ONLY the title (no quotes, no punctuation beyond normal words), "
        "max 6 words, title case. No emojis, no preamble."
    )
    user = f"Write a short title (max 6 words) for this conversation:\n\n{snippet}"

    try:
        result = await llm.chat(
            messages=[{"role": "user", "content": user}],
            tools=None,
            system=system,
        )
    except Exception as e:
        log.warning(f"generate-title failed: {e}")
        return {"title": ""}

    if result.get("type") != "text":
        return {"title": ""}
    title = (result.get("content") or "").strip()
    # Strip surrounding quotes and truncate
    title = title.strip('"\'')
    if "\n" in title:
        title = title.split("\n", 1)[0].strip()
    title = title[:80]
    return {"title": title}


class WatchRequest(BaseModel):
    session_id: str
    enabled: bool = True
    interval_seconds: int = 60


async def _watcher_loop(session_id: str, interval: int, ws_ref: list):
    session = sessions.get(session_id)
    if not session:
        return

    ss_client: SmartsheetClient = session["smartsheet"]
    sheet_id = session["sheet_id"]
    last_snapshot: dict | None = None

    while True:
        await asyncio.sleep(interval)
        try:
            sheet = await ss_client.get_sheet(sheet_id, page_size=100, max_rows=100)
            rows = sheet.get("rows", [])
            snapshot = {str(r["id"]): len(r.get("cells", [])) for r in rows}

            if last_snapshot is not None and snapshot != last_snapshot:
                added = set(snapshot.keys()) - set(last_snapshot.keys())
                removed = set(last_snapshot.keys()) - set(snapshot.keys())
                changes = []
                if added:
                    changes.append(f"{len(added)} new row(s)")
                if removed:
                    changes.append(f"{len(removed)} removed row(s)")
                if not changes:
                    changes.append("cell data changed")

                for w in ws_ref:
                    try:
                        await w.send_json({
                            "type": "notification",
                            "changes": changes,
                        })
                    except Exception:
                        pass

            last_snapshot = snapshot
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.debug(f"Watcher error: {e}", extra={"session_id": session_id})


@app.websocket("/ws/{session_id}")
async def websocket_chat(ws: WebSocket, session_id: str):
    await ws.accept()
    session = sessions.get(session_id)
    if not session:
        await ws.send_json({"type": "error", "content": "Invalid session"})
        await ws.close()
        return

    expected_token = session.get("ws_token")
    provided_token = ws.query_params.get("token", "")
    if expected_token and not secrets.compare_digest(expected_token, provided_token):
        log.warning("Rejected WS with bad ws_token", extra={"session_id": session_id})
        await ws.send_json({"type": "error", "content": "Unauthorized: missing or invalid ws_token."})
        await ws.close(code=1008)
        return

    _touch(session_id)
    agent: Agent = session["agent"]
    messages: list = session["messages"]
    agent_task: asyncio.Task | None = None
    pending_confirmations: dict[str, asyncio.Future] = {}

    ws_list: list = session.setdefault("_ws_list", [])
    ws_list.append(ws)

    log.info("WS connected", extra={"session_id": session_id})

    async def _persist_assistant(content: str):
        cid = session.get("active_conversation_id")
        uid = session.get("db_user_id")
        if cid and uid and content:
            try:
                await ssdb.append_message(cid, "assistant", content)
            except Exception as e:
                log.debug(f"persist assistant failed: {e}")

    async def send_event(event):
        await ws.send_json(event)
        # Persist final assistant text into the conversation log
        if event.get("type") in ("response", "stream_end") and event.get("content"):
            await _persist_assistant(event["content"])

    async def confirm_callback(tool_name: str, arguments: dict, tool_call_id: str) -> bool:
        future = asyncio.get_event_loop().create_future()
        pending_confirmations[tool_call_id] = future
        await ws.send_json({
            "type": "confirm_action",
            "tool_call_id": tool_call_id,
            "tool": tool_name,
            "arguments": arguments,
        })
        try:
            approved = await future
            uid = session.get("db_user_id")
            if uid:
                try:
                    await ssdb.log_audit(
                        user_id=uid,
                        sheet_id=str(arguments.get("sheet_id") or session.get("sheet_id") or ""),
                        tool_name=tool_name,
                        arguments=arguments,
                        before=None,
                        after=None,
                        status="approved" if approved else "rejected",
                    )
                except Exception as e:
                    log.debug(f"Audit log write failed: {e}")
            return approved
        finally:
            pending_confirmations.pop(tool_call_id, None)

    async def run_agent_safe(user_msg: str):
        messages.append({"role": "user", "content": user_msg})
        # Persist user message
        cid = session.get("active_conversation_id")
        uid = session.get("db_user_id")
        if cid and uid:
            try:
                await ssdb.append_message(cid, "user", user_msg)
            except Exception as e:
                log.debug(f"persist user failed: {e}")
        try:
            await agent.run(messages, on_event=send_event, confirm_callback=confirm_callback)
        except asyncio.CancelledError:
            await ws.send_json({"type": "cancelled", "content": "Request interrupted."})
        except Exception as e:
            log.error(f"Agent error: {traceback.format_exc()}", extra={"session_id": session_id})
            await ws.send_json({"type": "response", "content": _friendly_error(e)})

    recv_task: asyncio.Task | None = None

    def _handle_control(payload: dict) -> bool:
        nonlocal agent_task
        msg_type = payload.get("type", "")
        if msg_type == "cancel":
            for fut in pending_confirmations.values():
                if not fut.done():
                    fut.set_result(False)
            if agent_task and not agent_task.done():
                agent_task.cancel()
            return True
        if msg_type == "confirm":
            tcid = payload.get("tool_call_id")
            if tcid in pending_confirmations and not pending_confirmations[tcid].done():
                pending_confirmations[tcid].set_result(True)
            return True
        if msg_type == "reject":
            tcid = payload.get("tool_call_id")
            if tcid in pending_confirmations and not pending_confirmations[tcid].done():
                pending_confirmations[tcid].set_result(False)
            return True
        return False

    async def _handle_watch(payload: dict):
        enabled = payload.get("enabled", True)
        if enabled:
            interval = max(15, int(payload.get("interval", 60)))
            if session_id in watchers:
                watchers[session_id].cancel()
            watchers[session_id] = asyncio.create_task(
                _watcher_loop(session_id, interval, ws_list)
            )
            await ws.send_json({"type": "response", "content": f"Watch mode enabled (every {interval}s)."})
        else:
            task = watchers.pop(session_id, None)
            if task:
                task.cancel()
            await ws.send_json({"type": "response", "content": "Watch mode disabled."})

    try:
        while True:
            if recv_task is None or recv_task.done():
                recv_task = asyncio.ensure_future(ws.receive_text())

            wait_set = {recv_task}
            if agent_task and not agent_task.done():
                wait_set.add(agent_task)

            done, _ = await asyncio.wait(wait_set, return_when=asyncio.FIRST_COMPLETED)

            if recv_task in done:
                try:
                    payload = json.loads(recv_task.result())
                except (json.JSONDecodeError, Exception):
                    recv_task = None
                    continue
                recv_task = None
                _touch(session_id)

                ok, retry_after = check_limit(session_id, "ws")
                if not ok:
                    await ws.send_json({
                        "type": "response",
                        "content": f"Slow down! Please wait {retry_after:.1f}s before sending more messages.",
                    })
                    continue

                if _handle_control(payload):
                    continue
                if payload.get("type") == "watch":
                    await _handle_watch(payload)
                    continue

                user_msg = payload.get("message", "")
                if user_msg:
                    llm_ok, llm_retry = check_limit(session_id, "llm")
                    if not llm_ok:
                        await ws.send_json({
                            "type": "response",
                            "content": f"LLM rate limit reached. Retry in {llm_retry:.1f}s.",
                        })
                        continue
                    if agent_task and not agent_task.done():
                        agent_task.cancel()
                        try:
                            await agent_task
                        except asyncio.CancelledError:
                            pass
                    agent_task = asyncio.create_task(run_agent_safe(user_msg))

            if agent_task and agent_task in done:
                try:
                    agent_task.result()
                except (asyncio.CancelledError, Exception):
                    pass
                agent_task = None

    except WebSocketDisconnect:
        log.info("WS disconnected", extra={"session_id": session_id})
        if agent_task and not agent_task.done():
            agent_task.cancel()
        for fut in pending_confirmations.values():
            if not fut.done():
                fut.cancel()
        if ws in ws_list:
            ws_list.remove(ws)
        if recv_task and not recv_task.done():
            recv_task.cancel()


app.mount("/static", StaticFiles(directory="frontend"), name="static")


@app.get("/")
async def index():
    return FileResponse("frontend/index.html")


# ───────────────────────── Prompts library ─────────────────────────
#
# A curated catalogue of copy-paste prompts shipped with the app so
# operators don't have to re-invent canonical phrasings every time.
# The catalogue lives in `frontend/data/prompts.json` (one source of
# truth, easy to edit without rebuilding) and is exposed both as raw
# JSON via `/api/prompts` (consumed by the in-app modal and the
# dedicated `/help` page) and indirectly via `/static/data/prompts.json`.
# We deliberately read the file on every request: the catalogue is
# small (~30 entries / a few KB) and operators may edit it live in
# production without restarting uvicorn.

PROMPTS_PATH = Path(os.getenv(
    "SMARTSHEET_PROMPTS_PATH", "frontend/data/prompts.json"
))


@app.get("/api/prompts")
async def get_prompts_catalogue():
    """Return the full prompt catalogue used by the Help modal/page.

    The endpoint is public on purpose — it's static documentation,
    not user data — and re-reads the JSON file on every request so
    edits to `frontend/data/prompts.json` go live without restart.
    """
    try:
        with PROMPTS_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return JSONResponse(
            {"error": "prompts catalogue not found", "path": str(PROMPTS_PATH)},
            status_code=404,
        )
    except json.JSONDecodeError as exc:
        return JSONResponse(
            {"error": "prompts catalogue is not valid JSON", "detail": str(exc)},
            status_code=500,
        )

    if not isinstance(data, dict) or "categories" not in data:
        return JSONResponse(
            {"error": "prompts catalogue malformed: missing 'categories' key"},
            status_code=500,
        )
    return data


@app.get("/help")
async def help_page():
    """Serve the dedicated full-page prompt library."""
    return FileResponse("frontend/help.html")
