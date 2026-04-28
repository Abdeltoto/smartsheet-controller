"""
Optional Smartsheet OAuth 2.0 token exchange (authorization code → access token).

Configure in .env:
  SMARTSHEET_OAUTH_CLIENT_ID
  SMARTSHEET_OAUTH_CLIENT_SECRET

The extension runs the browser redirect; secrets never ship in the extension.
Token exchange: https://developers.smartsheet.com/api/smartsheet/guides/advanced-topics/oauth
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/oauth/smartsheet", tags=["oauth"])

SMARTSHEET_TOKEN_URL = "https://api.smartsheet.com/2.0/token"
# Smartsheet app registration: register the Chrome redirect URL
#   https://<extension-id>.chromiumapp.org/
DEFAULT_OAUTH_SCOPES = "READ_SHEETS WRITE_SHEETS"


@router.get("/config")
async def oauth_config() -> dict[str, Any]:
    """Expose public OAuth parameters for the extension (never the client secret)."""
    cid = os.getenv("SMARTSHEET_OAUTH_CLIENT_ID", "").strip()
    if not cid:
        return {"enabled": False}
    return {
        "enabled": True,
        "client_id": cid,
        "authorize_url": "https://app.smartsheet.com/b/authorize",
        "token_url": SMARTSHEET_TOKEN_URL,
        "scope": os.getenv("SMARTSHEET_OAUTH_SCOPES", DEFAULT_OAUTH_SCOPES),
    }


class ExchangeBody(BaseModel):
    code: str = Field(..., min_length=4)
    redirect_uri: str = Field(..., min_length=8)


@router.post("/exchange")
async def exchange_tokens(body: ExchangeBody):
    """Exchange an authorization code for access + refresh tokens (server-side only)."""
    cid = os.getenv("SMARTSHEET_OAUTH_CLIENT_ID", "").strip()
    secret = os.getenv("SMARTSHEET_OAUTH_CLIENT_SECRET", "").strip()
    if not cid or not secret:
        return JSONResponse(
            {"error": "OAuth is not configured on this server (missing client id/secret)."},
            status_code=503,
        )

    data = {
        "grant_type": "authorization_code",
        "code": body.code.strip(),
        "client_id": cid,
        "client_secret": secret,
        "redirect_uri": body.redirect_uri.strip(),
    }

    try:
        async with httpx.AsyncClient(timeout=45.0) as client:
            r = await client.post(
                SMARTSHEET_TOKEN_URL,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.RequestError as exc:
        return JSONResponse({"error": "Token request failed", "detail": str(exc)}, status_code=502)

    try:
        payload = r.json()
    except Exception:
        return JSONResponse(
            {"error": "Invalid response from Smartsheet", "detail": r.text[:500]},
            status_code=502,
        )

    if r.status_code >= 400:
        err = payload.get("message") or payload.get("error") or payload.get("detail") or r.text
        return JSONResponse(
            {"error": "Smartsheet rejected the token exchange", "detail": err},
            status_code=400,
        )

    # Return only what the client needs; omit refresh_token display in UIs if unused.
    out = {
        "access_token": payload.get("access_token"),
        "token_type": payload.get("token_type", "bearer"),
        "expires_in": payload.get("expires_in"),
        "refresh_token": payload.get("refresh_token"),
    }
    if not out["access_token"]:
        return JSONResponse({"error": "No access_token in Smartsheet response", "detail": payload}, status_code=502)
    return out
