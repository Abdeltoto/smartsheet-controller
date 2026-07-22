"""Static pages: SPA shell, help, prompts catalogue."""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

log = None  # noqa: kept for parity if extended later
router = APIRouter()

PROMPTS_PATH = Path(os.getenv(
    "SMARTSHEET_PROMPTS_PATH", "frontend/data/prompts.json"
))

_FRONTEND_DIR = Path("frontend")
_FRONTEND_DIST = _FRONTEND_DIR / "dist"
_USE_VITE_DIST = (_FRONTEND_DIST / "index.html").is_file()


@router.get("/")
async def index():
    if _USE_VITE_DIST:
        return FileResponse(_FRONTEND_DIST / "index.html")
    return FileResponse(_FRONTEND_DIR / "index.html")


@router.get("/api/prompts")
async def get_prompts_catalogue():
    """Return the full prompt catalogue used by the Help modal/page."""
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


@router.get("/help")
async def help_page():
    """Serve the dedicated full-page prompt library."""
    return FileResponse("frontend/help.html")
