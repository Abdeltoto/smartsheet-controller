"""In-memory session store and auth helpers."""
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
