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

@router.get("/api/webhook-events")
async def get_webhook_events(session_id: str, since: float = 0.0, limit: int = 50):
    """Polled by the frontend for real-time toasts when a webhook fires."""
    session = sessions.get(session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)
    user_id = session.get("db_user_id")
    if not user_id:
        return {"events": []}
    touch(session_id)
    return {"events": await ssdb.list_webhook_events(user_id, limit=min(limit, 200), since=since)}


@router.post("/api/smartsheet-webhook")
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
