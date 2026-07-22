"""Smartsheet Controller — FastAPI application entry point."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from backend import db as ssdb
from backend.core.helpers import detect_available_providers as _detect_available_providers
from backend.core.helpers import friendly_error as _friendly_error
from backend.core.state import (
    SESSION_IDLE_TIMEOUT,
    cleanup_idle_sessions,
    resolve_session as _resolve_session,
    sessions,
    watchers,
)
from backend.logging_config import get_logger, setup_logging
from backend.routes import admin, health, misc, pages, persistence, sessions, webhooks
from backend.services.session import build_welcome as _build_welcome
from backend.ws.chat import router as ws_router
from backend.ws.chat import watcher_loop as _watcher_loop

import os

setup_logging(os.getenv("LOG_LEVEL", "INFO"))
log = get_logger(__name__)
load_dotenv(override=True)

_cleanup_task: asyncio.Task | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _cleanup_task
    await ssdb.init_db()
    _cleanup_task = asyncio.create_task(cleanup_idle_sessions())
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

app.include_router(health.router)
app.include_router(sessions.router)
app.include_router(persistence.router)
app.include_router(webhooks.router)
app.include_router(admin.router)
app.include_router(misc.router)
app.include_router(ws_router)
app.include_router(pages.router)

app.mount("/static", StaticFiles(directory="frontend"), name="static")

__all__ = [
    "app",
    "sessions",
    "watchers",
    "_friendly_error",
    "_detect_available_providers",
    "_build_welcome",
    "_resolve_session",
    "_watcher_loop",
]
