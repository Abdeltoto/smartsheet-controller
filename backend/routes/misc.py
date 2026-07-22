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

class GenerateTitleRequest(BaseModel):
    session_id: str
    snippet: str


@router.post("/api/generate-title")
async def generate_title(req: GenerateTitleRequest):
    session = sessions.get(req.session_id)
    if not session:
        return JSONResponse({"error": "Invalid session"}, status_code=404)

    touch(req.session_id)
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
