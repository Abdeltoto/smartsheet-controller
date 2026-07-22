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

@router.get("/health")
async def health():
    uptime = int(time.monotonic() - APP_START_TIME)
    return {
        "status": "ok",
        "sessions": len(sessions),
        "watchers": len(watchers),
        "uptime_seconds": uptime,
    }


@router.get("/api/env-status")
async def env_status():
    ss_token = os.getenv("SMARTSHEET_TOKEN", "").strip()
    sheet_id = os.getenv("SHEET_ID", "").strip()
    available = detect_available_providers()
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


@router.get("/api/providers")
async def list_providers():
    return get_provider_info()
