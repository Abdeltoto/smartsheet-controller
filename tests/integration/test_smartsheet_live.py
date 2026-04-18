"""Integration tests against the real Smartsheet API.

Skipped automatically when SMARTSHEET_TOKEN / SHEET_ID are not in `.env`.
These tests are read-only against the user's existing sheet, plus one
ephemeral sheet that we create + delete in the same test to exercise the
write path safely (nothing in the production sheet is touched).
"""
from __future__ import annotations

import time
from typing import AsyncIterator

import pytest
import pytest_asyncio

from backend.smartsheet_client import SmartsheetClient

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def client(smartsheet_token) -> AsyncIterator[SmartsheetClient]:
    c = SmartsheetClient(smartsheet_token)
    try:
        yield c
    finally:
        await c.close()


# ────────────────────── read-only on the user's sheet ──────────────────────

class TestUserAndSheets:
    async def test_get_current_user(self, client: SmartsheetClient):
        u = await client.get_current_user()
        assert isinstance(u.get("id"), int)
        assert u.get("email"), "User should have an email"

    async def test_list_sheets(self, client: SmartsheetClient):
        sheets = await client.list_sheets()
        assert isinstance(sheets, list)
        # We don't assert non-empty: a brand-new account is allowed.
        for s in sheets[:5]:
            assert "id" in s and "name" in s

    async def test_get_sheet_summary(self, client: SmartsheetClient, sheet_id: str):
        s = await client.get_sheet_summary(sheet_id)
        assert s["name"]
        assert isinstance(s["columnCount"], int)
        assert isinstance(s["columns"], list)

    async def test_get_sheet_paginated(self, client: SmartsheetClient, sheet_id: str):
        sheet = await client.get_sheet(sheet_id, page_size=10, max_rows=10)
        assert "columns" in sheet
        assert sheet.get("_loadedRows", 0) <= 10

    async def test_schema_cache_active_on_repeat(self, client: SmartsheetClient, sheet_id: str):
        await client.get_sheet(sheet_id, page_size=0)
        await client.get_sheet(sheet_id, page_size=0)
        stats = client.cache_stats()
        assert stats["hits"] >= 1


class TestSearch:
    async def test_search_everything_runs(self, client: SmartsheetClient):
        # Empty result is fine, we just need a 200 response shape.
        r = await client.search_everything("zzz_unlikely_term_zzz")
        assert isinstance(r, dict)
        assert "results" in r or "totalCount" in r or r == {}


class TestWorkspaces:
    async def test_list_workspaces(self, client: SmartsheetClient):
        ws = await client.list_workspaces()
        assert isinstance(ws, list)


class TestShares:
    async def test_list_shares_on_user_sheet(self, client: SmartsheetClient, sheet_id: str):
        shares = await client.list_shares(sheet_id)
        assert isinstance(shares, list)
        for s in shares[:3]:
            assert "accessLevel" in s


# ────────────────────── safe write loop on a throwaway sheet ──────────────────────

class TestEphemeralSheetLifecycle:
    """Create → add rows → update rows → delete rows → delete sheet, all
    inside the user's first workspace (when available). Falls back to
    creating at the home "Sheets" folder otherwise. Skips cleanly if the
    account tier doesn't permit programmatic sheet creation, so the test
    suite stays green for read-only Smartsheet plans."""

    async def test_create_modify_delete(self, client: SmartsheetClient, smartsheet_token: str):
        import httpx
        from backend.smartsheet_client import BASE_URL

        sheet_name = f"_ctrl_test_{int(time.time())}"
        columns = [
            {"title": "Task", "type": "TEXT_NUMBER", "primary": True},
            {"title": "Status", "type": "PICKLIST",
             "options": ["Open", "Done"]},
        ]

        # Prefer a workspace if any — sheets created there are guaranteed
        # to be addressable immediately.
        workspaces = await client.list_workspaces()
        new_sheet_id: str | None = None
        try:
            if workspaces:
                ws_id = workspaces[0]["id"]
                r = await client._request(
                    "POST",
                    f"{BASE_URL}/workspaces/{ws_id}/sheets",
                    json={"name": sheet_name, "columns": columns},
                )
                r.raise_for_status()
                created = r.json()
            else:
                created = await client.create_sheet(sheet_name, columns)
            new_sheet_id = str((created.get("result") or {}).get("id") or created.get("id"))
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (403, 404):
                pytest.skip(f"Account does not allow sheet creation here: {e.response.status_code}")
            raise

        assert new_sheet_id and new_sheet_id != "None"

        try:
            # Verify the sheet is addressable; if not, the account is
            # restricted and the rest of the lifecycle isn't meaningful.
            try:
                await client.get_sheet(new_sheet_id, page_size=0)
            except httpx.HTTPStatusError as e:
                pytest.skip(f"Created sheet not readable ({e.response.status_code}); skipping write loop.")

            add_resp = await client.add_rows(new_sheet_id, [
                {"Task": "First", "Status": "Open"},
                {"Task": "Second", "Status": "Open"},
            ])
            added = add_resp.get("result") or []
            assert len(added) == 2
            row_ids = [r["id"] for r in added]

            await client.update_rows(new_sheet_id, [{
                "rowId": row_ids[0],
                "cells": {"Status": {"value": "Done"}},
            }])

            sheet = await client.get_sheet(new_sheet_id, page_size=50)
            assert sheet.get("totalRowCount", 0) >= 2

            await client.delete_rows(new_sheet_id, row_ids)
        finally:
            if new_sheet_id:
                try:
                    await client.delete_sheet(new_sheet_id)
                except Exception:
                    pass
