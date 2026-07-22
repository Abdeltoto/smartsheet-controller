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

# ──────────────── Sprint 5 — Server-side persistence ────────────────

class ConvSaveRequest(BaseModel):
    session_id: str
    conversation_id: str
    title: str | None = None


@router.post("/api/conversations/save")
async def save_conv(req: ConvSaveRequest):
    """Create/update conversation metadata (title, sheet)."""
    session = sessions.get(req.session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)
    user_id = session.get("db_user_id")
    if not user_id:
        return JSONResponse({"error": "User not persisted"}, status_code=400)
    touch(req.session_id)
    await ssdb.save_conversation(req.conversation_id, user_id, session.get("sheet_id"), req.title)
    session["active_conversation_id"] = req.conversation_id
    return {"status": "ok", "conversation_id": req.conversation_id}


@router.get("/api/conversations")
async def list_conv(session_id: str):
    session = sessions.get(session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)
    user_id = session.get("db_user_id")
    if not user_id:
        return {"conversations": []}
    touch(session_id)
    return {"conversations": await ssdb.list_conversations(user_id)}


@router.get("/api/conversations/{conv_id}")
async def get_conv(conv_id: str, session_id: str):
    session = sessions.get(session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)
    user_id = session.get("db_user_id")
    if not user_id:
        return {"messages": []}
    touch(session_id)
    msgs = await ssdb.get_conversation_messages(conv_id, user_id)
    return {"conversation_id": conv_id, "messages": msgs}


class ConvDeleteRequest(BaseModel):
    session_id: str
    conversation_id: str


@router.post("/api/conversations/delete")
async def delete_conv(
    req: ConvDeleteRequest,
    x_ws_token: str | None = Header(None, alias="X-WS-Token"),
    x_auth_cookie: str | None = Header(None, alias="X-Auth-Cookie"),
):
    session = resolve_session(req.session_id, x_ws_token, x_auth_cookie, require_secret=True)
    user_id = session.get("db_user_id")
    if not user_id:
        return JSONResponse({"error": "User not persisted"}, status_code=400)
    ok = await ssdb.delete_conversation(req.conversation_id, user_id)
    return {"status": "ok" if ok else "not_found"}


class MigrationRequest(BaseModel):
    session_id: str
    conversations: list[dict]  # [{id, title, sheet_id?, messages:[{role, content}]}]


@router.post("/api/conversations/migrate")
async def migrate_conv(
    req: MigrationRequest,
    x_ws_token: str | None = Header(None, alias="X-WS-Token"),
    x_auth_cookie: str | None = Header(None, alias="X-Auth-Cookie"),
):
    """Bulk import localStorage conversations into the DB on first login."""
    session = resolve_session(req.session_id, x_ws_token, x_auth_cookie, require_secret=True)
    user_id = session.get("db_user_id")
    if not user_id:
        return JSONResponse({"error": "User not persisted"}, status_code=400)
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


@router.get("/api/audit")
async def get_audit(
    session_id: str,
    sheet_id: str | None = None,
    limit: int = 200,
    ws_token: str | None = None,
    auth_cookie: str | None = None,
    x_ws_token: str | None = Header(None, alias="X-WS-Token"),
    x_auth_cookie: str | None = Header(None, alias="X-Auth-Cookie"),
):
    token, cookie = session_secrets(ws_token, auth_cookie, x_ws_token, x_auth_cookie)
    session = resolve_session(session_id, token, cookie, require_secret=True)
    user_id = session.get("db_user_id")
    if not user_id:
        return {"entries": []}
    return {"entries": await ssdb.list_audit(user_id, limit=min(limit, 1000), sheet_id=sheet_id)}


@router.get("/api/favorites")
async def get_favs(session_id: str):
    session = sessions.get(session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)
    user_id = session.get("db_user_id")
    if not user_id:
        return {"favorites": []}
    touch(session_id)
    return {"favorites": await ssdb.list_favorites(user_id)}


class FavRequest(BaseModel):
    session_id: str
    sheet_id: str
    sheet_name: str | None = None


@router.post("/api/favorites/add")
async def add_fav(req: FavRequest):
    session = sessions.get(req.session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)
    user_id = session.get("db_user_id")
    if not user_id:
        return JSONResponse({"error": "User not persisted"}, status_code=400)
    touch(req.session_id)
    await ssdb.add_favorite(user_id, req.sheet_id, req.sheet_name)
    return {"status": "ok"}


@router.post("/api/favorites/remove")
async def remove_fav(req: FavRequest):
    session = sessions.get(req.session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=400)
    user_id = session.get("db_user_id")
    if not user_id:
        return JSONResponse({"error": "User not persisted"}, status_code=400)
    touch(req.session_id)
    await ssdb.remove_favorite(user_id, req.sheet_id)
    return {"status": "ok"}


@router.get("/api/export")
async def export_account(
    session_id: str,
    ws_token: str | None = None,
    auth_cookie: str | None = None,
    x_ws_token: str | None = Header(None, alias="X-WS-Token"),
    x_auth_cookie: str | None = Header(None, alias="X-Auth-Cookie"),
):
    """RGPD-style: download a JSON dump of everything the server knows about the user."""
    token, cookie = session_secrets(ws_token, auth_cookie, x_ws_token, x_auth_cookie)
    session = resolve_session(session_id, token, cookie, require_secret=True)
    user_id = session.get("db_user_id")
    if not user_id:
        return JSONResponse({"error": "User not persisted"}, status_code=400)
    data = await ssdb.export_user_data(user_id)
    headers = {"Content-Disposition": f'attachment; filename="smartsheet-controller-export-{int(time.time())}.json"'}
    return JSONResponse(data, headers=headers)
