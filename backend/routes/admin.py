"""Auto-split from backend/app.py — S12 router."""
from __future__ import annotations

import json
import os
import secrets
import time
import traceback
import uuid
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from backend import db as ssdb
from backend.agent import Agent
from backend.core.helpers import detect_available_providers, friendly_error, resolve_api_key
from backend.core.state import (
    APP_START_TIME,
    resolve_session,
    session_secrets,
    sessions,
    touch,
    watchers,
)
from backend.llm_router import LLMRouter, PROVIDERS, get_provider_info
from backend.logging_config import get_logger
from backend.rate_limit import rate_limiter
from backend.services.session import build_sheet_context, build_welcome, create_session
from backend.smartsheet_client import SmartsheetClient

log = get_logger(__name__)
router = APIRouter()

BUG_REPORTS_JSONL = Path(os.getenv(
    "BUG_REPORTS_JSONL_PATH", "data/bug_reports.jsonl"
))
_BUG_DESC_MAX = 8000
_BUG_STEPS_MAX = 4000
_BUG_CTX_MAX = 64000


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


@router.post("/api/bug-reports")
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
            touch(req.session_id)
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


@router.get("/api/bug-reports")
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


@router.post("/api/bug-reports/{report_id}/status")
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
