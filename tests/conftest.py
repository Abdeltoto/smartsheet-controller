"""Shared fixtures for the Smartsheet Controller test suite.

Loads `.env` so integration tests have access to the real Smartsheet
token/sheet provided by the user. Unit tests don't need it.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

load_dotenv(REPO_ROOT / ".env", override=False)


# ────────────────────── env helpers / skip markers ──────────────────────

def _has(env_name: str) -> bool:
    return bool(os.getenv(env_name, "").strip())


def _need(env_name: str) -> str:
    val = os.getenv(env_name, "").strip()
    if not val:
        pytest.skip(f"Set {env_name} in .env to run this test.")
    return val


@pytest.fixture(scope="session")
def smartsheet_token() -> str:
    return _need("SMARTSHEET_TOKEN")


@pytest.fixture(scope="session")
def sheet_id() -> str:
    return _need("SHEET_ID")


@pytest.fixture(scope="session")
def openai_api_key() -> str | None:
    return os.getenv("OPENAI_API_KEY", "").strip() or None


# ────────────────────── unit-level fixtures ──────────────────────

@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """Point backend.db at a temp SQLite file and reset its init state.

    Tests that touch the DB must use this fixture so they don't pollute the
    real `data/smartsheet_ctrl.sqlite`.
    """
    db_file = tmp_path / "test.sqlite"
    monkeypatch.setenv("SMARTSHEET_CTRL_DB", str(db_file))
    # Re-import-friendly: mutate module attrs directly so we don't need a reload.
    import backend.db as ssdb
    monkeypatch.setattr(ssdb, "DB_PATH", db_file)
    monkeypatch.setattr(ssdb, "_initialized", False)
    return ssdb


@pytest.fixture
def sample_columns() -> list[dict]:
    return [
        {"id": 1001, "title": "Task", "type": "TEXT_NUMBER", "index": 0},
        {"id": 1002, "title": "Status", "type": "PICKLIST", "index": 1},
        {"id": 1003, "title": "Due", "type": "DATE", "index": 2},
        {"id": 1004, "title": "Owner", "type": "CONTACT_LIST", "index": 3},
    ]


@pytest.fixture
def sample_rows() -> list[dict]:
    rows = []
    for i in range(1, 11):
        rows.append({
            "id": 5000 + i,
            "rowNumber": i,
            "cells": [
                {"columnId": 1001, "value": f"Task {i}", "displayValue": f"Task {i}"},
                {"columnId": 1002, "value": "Done" if i % 2 else "Open"},
                {"columnId": 1003, "value": f"2026-04-{i:02d}"},
            ],
        })
    return rows


# ────────────────────── e2e fixtures ──────────────────────

@pytest.fixture
def stub_llm_provider(monkeypatch):
    """Replace LLMRouter.chat_stream with a deterministic generator that returns
    a single canned response. Lets e2e tests run without an LLM provider."""
    from backend import llm_router

    async def fake_stream(self, messages, tools=None, system=""):
        text = "Hello from the stub LLM.\n[SUGGESTIONS] Do X | Do Y"
        yield {"type": "stream_delta", "content": text}
        yield {"type": "stream_end", "content": text}

    monkeypatch.setattr(llm_router.LLMRouter, "chat_stream", fake_stream)
    return fake_stream
