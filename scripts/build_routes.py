#!/usr/bin/env python3
"""Generate FastAPI routers from legacy app.py route sections."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "backend" / "app.py"
LINES = APP.read_text(encoding="utf-8").splitlines()

REPL = [
    ("@app.", "@router."),
    ("_touch(", "touch("),
    ("_resolve_session(", "resolve_session("),
    ("_session_secrets(", "session_secrets("),
    ("_friendly_error(", "friendly_error("),
    ("_detect_available_providers(", "detect_available_providers("),
    ("_resolve_api_key(", "resolve_api_key("),
    ("_build_sheet_context(", "build_sheet_context("),
    ("_create_session(", "create_session("),
    ("_build_welcome(", "build_welcome("),
    ("len(sessions)", "len(sessions)"),
    ("len(watchers)", "len(watchers)"),
]

COMMON = '''"""Auto-split from backend/app.py — S12 router."""
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

'''


def transform(body: str) -> str:
    for old, new in REPL:
        body = body.replace(old, new)
    return body


def slice_lines(start: int, end: int) -> str:
    return "\n".join(LINES[start - 1 : end])


def write_router(name: str, start: int, end: int, extra: str = "") -> None:
    body = transform(slice_lines(start, end))
    path = ROOT / "backend" / "routes" / f"{name}.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(COMMON + extra + body + "\n", encoding="utf-8")
    print(f"wrote routes/{name}.py ({end - start + 1} lines)")


write_router("health", 176, 207)
write_router("sessions", 210, 920)
write_router("persistence", 923, 1100)
write_router("webhooks", 1103, 1151)
write_router("admin", 1172, 1328)
write_router("misc", 1331, 1377)  # generate-title

# pages need special constants
pages_extra = '''
PROMPTS_PATH = Path(os.getenv(
    "SMARTSHEET_PROMPTS_PATH", "frontend/data/prompts.json"
))

_FRONTEND_DIR = Path("frontend")
_FRONTEND_DIST = _FRONTEND_DIR / "dist"
_USE_VITE_DIST = (_FRONTEND_DIST / "index.html").is_file()

'''
write_router("pages", 1629, 1700, extra=pages_extra)

# __init__.py
(ROOT / "backend" / "core" / "__init__.py").write_text("", encoding="utf-8")
(ROOT / "backend" / "services" / "__init__.py").write_text("", encoding="utf-8")
(ROOT / "backend" / "routes" / "__init__.py").write_text("", encoding="utf-8")
(ROOT / "backend" / "ws" / "__init__.py").write_text("", encoding="utf-8")

print("done")
