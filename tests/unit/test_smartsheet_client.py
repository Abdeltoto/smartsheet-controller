"""Unit tests for SmartsheetClient — uses a fake httpx transport so no
network calls are made."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from backend.smartsheet_client import (
    BASE_URL,
    SCHEMA_CACHE_TTL,
    SmartsheetClient,
    SmartsheetRateLimitError,
    _parse_range,
)

pytestmark = pytest.mark.unit


# ────────────────────── helpers ──────────────────────

def _make_client_with_handler(handler) -> SmartsheetClient:
    """Build a SmartsheetClient whose underlying httpx.AsyncClient uses a
    MockTransport instead of a real network stack."""
    client = SmartsheetClient("fake-token")
    transport = httpx.MockTransport(handler)
    # Replace the AsyncClient with one bound to the mock transport, keep headers.
    client.client = httpx.AsyncClient(transport=transport, headers=client.headers, timeout=5.0)
    return client


# ────────────────────── _parse_range ──────────────────────

class TestParseRange:
    @pytest.mark.parametrize("inp,expected", [
        ("1-5", (1, 5)),
        ("10-20", (10, 20)),
        ("7", (7, 7)),
        (" 3 - 9 ", (3, 9)),
    ])
    def test_valid(self, inp, expected):
        assert _parse_range(inp) == expected

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            _parse_range("abc")


# ────────────────────── cache_stats / invalidate ──────────────────────

class TestCacheStats:
    def test_starts_empty(self):
        c = SmartsheetClient("t")
        s = c.cache_stats()
        assert s == {"hits": 0, "misses": 0, "hit_rate": 0.0, "size": 0}

    def test_invalidate_clears(self):
        c = SmartsheetClient("t")
        c._schema_cache["123"] = (9999999.0, {"a": 1})
        assert c.cache_stats()["size"] == 1
        c.invalidate_schema_cache("123")
        assert c.cache_stats()["size"] == 0

    def test_invalidate_all(self):
        c = SmartsheetClient("t")
        c._schema_cache["1"] = (9999999.0, {})
        c._schema_cache["2"] = (9999999.0, {})
        c.invalidate_schema_cache(None)
        assert c.cache_stats()["size"] == 0


# ────────────────────── _request retry & error mapping ──────────────────────

class TestRequestRetry:
    @pytest.mark.asyncio
    async def test_429_then_success_retries(self):
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(429, headers={"Retry-After": "0"}, json={"errorCode": 4003})
            return httpx.Response(200, json={"id": 1, "email": "a@b", "firstName": "A", "lastName": "B"})

        client = _make_client_with_handler(handler)
        try:
            user = await client.get_current_user()
            assert user["email"] == "a@b"
            assert calls["n"] == 2  # one 429, one success
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_persistent_429_raises_rate_limit_error(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"errorCode": 4003})

        client = _make_client_with_handler(handler)
        try:
            with pytest.raises(SmartsheetRateLimitError):
                await client.get_current_user()
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_500_then_success_retries(self):
        calls = {"n": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(500, json={"error": "boom"})
            return httpx.Response(200, json={"id": 99, "email": "x@y"})

        client = _make_client_with_handler(handler)
        try:
            user = await client.get_current_user()
            assert user["id"] == 99
            assert calls["n"] == 2
        finally:
            await client.close()


# ────────────────────── schema cache hit/miss ──────────────────────

class TestSchemaCache:
    @pytest.mark.asyncio
    async def test_schema_cache_hit_skips_network(self, monkeypatch):
        calls = {"n": 0}
        body = {"id": 1, "name": "Sheet", "columns": [{"id": 11, "title": "A", "type": "TEXT_NUMBER", "index": 0}], "totalRowCount": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            return httpx.Response(200, json=body)

        client = _make_client_with_handler(handler)
        try:
            await client.get_sheet("42", page_size=0)
            await client.get_sheet("42", page_size=0)  # served from cache
            stats = client.cache_stats()
            assert stats["hits"] == 1
            assert stats["misses"] == 1
            assert calls["n"] == 1
        finally:
            await client.close()

    @pytest.mark.asyncio
    async def test_schema_cache_expires(self, monkeypatch):
        body = {"id": 1, "name": "S", "columns": [], "totalRowCount": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=body)

        # Force the cache TTL to 0 so the second call must re-fetch.
        from backend import smartsheet_client as sc
        monkeypatch.setattr(sc, "SCHEMA_CACHE_TTL", 0.0)

        client = _make_client_with_handler(handler)
        try:
            await client.get_sheet("99", page_size=0)
            await client.get_sheet("99", page_size=0)
            stats = client.cache_stats()
            assert stats["misses"] == 2
        finally:
            await client.close()


# ────────────────────── high-level helpers ──────────────────────

class TestGetCurrentUser:
    @pytest.mark.asyncio
    async def test_normalizes_response(self):
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.path.endswith("/users/me")
            assert req.headers["Authorization"] == "Bearer fake-token"
            return httpx.Response(200, json={
                "id": 7, "email": "u@e.com",
                "firstName": "Foo", "lastName": "Bar",
                "locale": "fr_FR", "timeZone": "Europe/Paris",
                "account": {"name": "AcmeCo"},
                "extra": "ignored",
            })

        client = _make_client_with_handler(handler)
        try:
            u = await client.get_current_user()
        finally:
            await client.close()
        assert u == {
            "id": 7, "email": "u@e.com",
            "firstName": "Foo", "lastName": "Bar",
            "locale": "fr_FR", "timeZone": "Europe/Paris",
            "account": {"name": "AcmeCo"},
        }


class TestGetSheetSummary:
    @pytest.mark.asyncio
    async def test_projects_columns_only(self):
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "name": "MySheet",
                "totalRowCount": 42,
                "columns": [
                    {"id": 1, "title": "Task", "type": "TEXT_NUMBER", "index": 0, "extra": True},
                    {"id": 2, "title": "Status", "type": "PICKLIST", "index": 1},
                ],
            })

        client = _make_client_with_handler(handler)
        try:
            s = await client.get_sheet_summary("123")
        finally:
            await client.close()
        assert s["name"] == "MySheet"
        assert s["totalRowCount"] == 42
        assert s["columnCount"] == 2
        assert {c["title"] for c in s["columns"]} == {"Task", "Status"}
        # extra fields stripped
        assert "extra" not in s["columns"][0]


class TestUpdateRowsColumnMapping:
    @pytest.mark.asyncio
    async def test_unknown_column_silently_dropped(self):
        captured = {"body": None}

        def handler(req: httpx.Request) -> httpx.Response:
            if req.method == "GET":
                return httpx.Response(200, json={
                    "id": 1, "name": "S",
                    "columns": [{"id": 100, "title": "Task", "type": "TEXT_NUMBER", "index": 0}],
                    "totalRowCount": 0,
                })
            captured["body"] = json.loads(req.content.decode())
            return httpx.Response(200, json={"message": "SUCCESS"})

        client = _make_client_with_handler(handler)
        try:
            await client.update_rows("1", [{
                "rowId": 555,
                "cells": {
                    "Task": {"value": "go"},
                    "GhostColumn": {"value": "ignored"},
                },
            }])
        finally:
            await client.close()
        sent = captured["body"]
        assert sent[0]["id"] == 555
        # Only the real column survived
        assert {c["columnId"] for c in sent[0]["cells"]} == {100}


class TestDeleteRowsBuildsCsvIds:
    @pytest.mark.asyncio
    async def test_csv_ids_in_query(self):
        captured = {"url": None}

        def handler(req: httpx.Request) -> httpx.Response:
            captured["url"] = str(req.url)
            return httpx.Response(200, json={"message": "SUCCESS"})

        client = _make_client_with_handler(handler)
        try:
            await client.delete_rows("9", [1, 2, 3])
        finally:
            await client.close()
        assert "ids=1,2,3" in captured["url"]
