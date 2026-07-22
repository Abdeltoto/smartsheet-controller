#!/usr/bin/env python3
"""Split backend/app.py into routers + core modules (S12)."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "backend" / "app.py"
TEXT = APP.read_text(encoding="utf-8")
LINES = TEXT.splitlines()


def slice_lines(start: int, end: int) -> str:
    return "\n".join(LINES[start - 1 : end])


def write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.rstrip() + "\n", encoding="utf-8")


# ── core/state.py ──
write(
    ROOT / "backend/core/state.py",
    '''"""In-memory session store and auth helpers."""
from __future__ import annotations

import asyncio
import secrets
import time

from fastapi import Header, HTTPException

from backend.logging_config import get_logger
from backend.rate_limit import rate_limiter
from backend.smartsheet_client import SmartsheetClient

log = get_logger(__name__)

SESSION_IDLE_TIMEOUT = int(__import__("os").getenv("SESSION_IDLE_TIMEOUT", "1800"))
SESSION_CLEANUP_INTERVAL = 300
APP_START_TIME = time.monotonic()

sessions: dict[str, dict] = {}
watchers: dict[str, asyncio.Task] = {}


def touch(session_id: str) -> None:
    s = sessions.get(session_id)
    if s is not None:
        s["last_activity"] = time.monotonic()


def resolve_session(
    session_id: str,
    ws_token: str | None = None,
    auth_cookie: str | None = None,
    *,
    require_secret: bool = False,
) -> dict:
    """Return a live in-memory session after optional ws_token / auth_cookie check."""
    session = sessions.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Invalid session")

    expected_ws = session.get("ws_token") or ""
    expected_cookie = session.get("auth_cookie") or ""

    ws_ok = bool(ws_token and expected_ws and secrets.compare_digest(expected_ws, ws_token))
    cookie_ok = bool(
        auth_cookie and expected_cookie and secrets.compare_digest(expected_cookie, auth_cookie)
    )

    if ws_ok or cookie_ok:
        touch(session_id)
        return session

    if require_secret or ws_token or auth_cookie:
        raise HTTPException(status_code=403, detail="Unauthorized: invalid or missing session secret")

    touch(session_id)
    return session


def session_secrets(
    ws_token: str | None = None,
    auth_cookie: str | None = None,
    x_ws_token: str | None = Header(None, alias="X-WS-Token"),
    x_auth_cookie: str | None = Header(None, alias="X-Auth-Cookie"),
) -> tuple[str | None, str | None]:
    return ws_token or x_ws_token, auth_cookie or x_auth_cookie


async def cleanup_idle_sessions() -> None:
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
                log.info(
                    f"Evicting idle session (idle {int(now - session.get('last_activity', now))}s)",
                    extra={"session_id": sid},
                )
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
''',
)

# ── core/helpers.py ──
helpers_body = slice_lines(144, 174)
helpers_body = helpers_body.replace("def _friendly_error", "def friendly_error")
helpers_body = helpers_body.replace("def _detect_available_providers", "def detect_available_providers")
helpers_body = helpers_body.replace("def _resolve_api_key", "def resolve_api_key")
write(
    ROOT / "backend/core/helpers.py",
    f'''"""Shared HTTP / config helpers."""
from __future__ import annotations

import os

from backend.llm_router import PROVIDERS
from backend.smartsheet_client import SmartsheetRateLimitError


{helpers_body}
''',
)

# ── services/session.py ──
session_body = slice_lines(359, 605)
session_body = session_body.replace("_build_sheet_context", "build_sheet_context")
session_body = session_body.replace("_smart_starter_cards", "smart_starter_cards")
session_body = session_body.replace("_build_welcome", "build_welcome")
session_body = session_body.replace("_create_session", "create_session")
session_body = session_body.replace("_detect_available_providers", "detect_available_providers")
write(
    ROOT / "backend/services/session.py",
    f'''"""Session bootstrap: sheet context, welcome payload, in-memory session creation."""
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


{session_body}
''',
)

# ── ws/chat.py ──
ws_body = slice_lines(1386, 1627)
ws_body = ws_body.replace("_watcher_loop", "watcher_loop")
ws_body = ws_body.replace("_touch(", "touch(")
ws_body = ws_body.replace("_friendly_error", "friendly_error")
ws_body = ws_body.replace("@app.websocket", "@router.websocket")
write(
    ROOT / "backend/ws/chat.py",
    f'''"""WebSocket chat handler and sheet watch loop."""
from __future__ import annotations

import asyncio
import json
import secrets
import traceback

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from backend.agent import Agent
from backend.core.helpers import friendly_error
from backend.core.state import sessions, touch, watchers
from backend import db as ssdb
from backend.logging_config import get_logger
from backend.rate_limit import check_limit
from backend.smartsheet_client import SmartsheetClient

log = get_logger(__name__)
router = APIRouter()


{ws_body}
''',
)

print("Wrote core/state.py, core/helpers.py, services/session.py, ws/chat.py")
