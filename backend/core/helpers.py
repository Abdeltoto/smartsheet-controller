"""Shared HTTP / config helpers."""
from __future__ import annotations

import os

from backend.llm_router import PROVIDERS
from backend.smartsheet_client import SmartsheetRateLimitError


def friendly_error(exc: Exception) -> str:
    """Map exception to user-safe message. Full trace is logged separately."""
    if isinstance(exc, SmartsheetRateLimitError):
        return str(exc)
    msg = str(exc)
    if "401" in msg or "Unauthorized" in msg:
        return "Smartsheet authentication failed. Check your API token."
    if "403" in msg or "Forbidden" in msg:
        return "Access denied. Your token may not have permission for this resource."
    if "404" in msg or "Not Found" in msg:
        return "Resource not found. Check the sheet ID."
    if "timeout" in msg.lower():
        return "Request timed out. Please try again."
    if "quota" in msg.lower():
        return "LLM API quota exceeded. Check your provider billing or switch models."
    if "rate" in msg.lower() and "limit" in msg.lower():
        return "Rate limit reached. Please wait a moment and retry."
    return "An unexpected error occurred. Please retry."


def detect_available_providers() -> dict:
    available = {}
    for name, info in PROVIDERS.items():
        key = os.getenv(info["env_key"], "").strip()
        if key:
            available[name] = {
                "default_model": info["default_model"],
                "models": info["models"],
            }
    return available


def resolve_api_key(provider: str, user_key: str = "") -> str:
    if user_key:
        return user_key
    info = PROVIDERS.get(provider)
    if info:
        return os.getenv(info["env_key"], "").strip()
    return ""
