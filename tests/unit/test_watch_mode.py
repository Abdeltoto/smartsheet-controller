"""Watch mode polling loop notifies connected WebSocket clients."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_watcher_loop_emits_notification_on_new_row(monkeypatch):
    from backend.core import state
    import backend.ws.chat as ws_mod

    sheet_v1 = {"rows": [{"id": 1, "cells": [{"columnId": 1}]}]}
    sheet_v2 = {
        "rows": [
            {"id": 1, "cells": [{"columnId": 1}]},
            {"id": 2, "cells": [{"columnId": 1}]},
        ]
    }
    calls = {"n": 0}

    async def fake_get_sheet(sheet_id, page_size=100, max_rows=100):
        calls["n"] += 1
        return sheet_v1 if calls["n"] == 1 else sheet_v2

    ss_client = AsyncMock()
    ss_client.get_sheet = fake_get_sheet

    session_id = "watch-test"
    state.sessions[session_id] = {
        "smartsheet": ss_client,
        "sheet_id": "1234567890",
    }

    ws = AsyncMock()
    ws_list = [ws]

    sleep_calls = {"n": 0}
    orig_sleep = asyncio.sleep

    async def fake_sleep(_seconds):
        sleep_calls["n"] += 1
        await orig_sleep(0)
        if sleep_calls["n"] >= 3:
            raise asyncio.CancelledError()

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await ws_mod.watcher_loop(session_id, 15, ws_list)

    state.sessions.pop(session_id, None)

    assert ws.send_json.await_count >= 1
    payload = ws.send_json.await_args_list[-1].args[0]
    assert payload["type"] == "notification"
    assert any("new row" in c.lower() for c in payload.get("changes", []))
