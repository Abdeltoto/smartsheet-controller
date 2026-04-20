"""Unit tests for the schema-guard on add_rows / update_rows (P1.2).

The schema-guard catches the silent "row created but empty" bug: when the LLM
references a column name that doesn't exist on the sheet, the upstream
SmartsheetClient used to drop the cell silently (`if not col_id: continue`).
The guard now intercepts this BEFORE the API call and returns a structured
`UNKNOWN_COLUMNS` error with the list of valid columns and an actionable hint.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.tools import (
    _extract_referenced_columns,
    _validate_columns_for_write,
    execute_tool,
)

pytestmark = [pytest.mark.unit]


# ────────────────────── pure helper: column extraction ──────────────────────


class TestExtractReferencedColumns:
    def test_add_rows_pulls_keys_as_column_names(self):
        rows = [{"Task": "X", "Status": "Y"}, {"Task": "Z"}]
        refs = _extract_referenced_columns(rows, "add_rows")
        assert refs == {"Task", "Status"}

    def test_add_rows_skips_api_metadata_keys(self):
        # If the model emits API-shape rows ({cells: [...], toBottom, parentId}),
        # the metadata keys must NOT be treated as column names.
        rows = [{"cells": [{"columnId": 1, "value": "X"}], "toBottom": True}]
        refs = _extract_referenced_columns(rows, "add_rows")
        assert refs == set(), "API-shape rows must not produce column refs"

    def test_update_rows_reads_inside_cells_dict(self):
        updates = [{"rowId": 42, "cells": {"Status": {"value": "Done"}}}]
        refs = _extract_referenced_columns(updates, "update_rows")
        assert refs == {"Status"}

    def test_update_rows_with_api_shape_cells_list_yields_no_refs(self):
        # API-shape: cells is a list, not a dict. We can't extract names → skip.
        updates = [{"rowId": 1, "cells": [{"columnId": 5, "value": "x"}]}]
        refs = _extract_referenced_columns(updates, "update_rows")
        assert refs == set()

    def test_empty_payload_returns_empty_set(self):
        assert _extract_referenced_columns([], "add_rows") == set()
        assert _extract_referenced_columns(None, "add_rows") == set()

    def test_non_dict_rows_are_tolerated(self):
        # Defensive: junk in the list should not crash the helper.
        refs = _extract_referenced_columns([42, None, "oops"], "add_rows")
        assert refs == set()

    # ─── regression: live-smoke discovered the LLM produces this shape ───
    # add_rows({rows:[{cells:[{columnName:"Foo", value:"Bar"}]}]})
    # The schema-guard MUST extract "Foo" so it can be validated against
    # the sheet's columns. Previously it was silently ignored because
    # `cells` was in the reserved-keys list and `columnName` was never
    # looked at.
    def test_add_rows_api_shape_with_columnName_extracts_the_name(self):
        rows = [{"cells": [
            {"columnName": "Task", "value": "X"},
            {"columnName": "Status", "value": "Done"},
        ]}]
        refs = _extract_referenced_columns(rows, "add_rows")
        assert refs == {"Task", "Status"}

    def test_add_rows_api_shape_with_columnId_yields_no_refs(self):
        # Numeric IDs are pre-validated by Smartsheet — leave them alone.
        rows = [{"cells": [{"columnId": 12345, "value": "X"}]}]
        refs = _extract_referenced_columns(rows, "add_rows")
        assert refs == set()

    def test_add_rows_api_shape_mixed_columnName_and_columnId(self):
        rows = [{"cells": [
            {"columnId": 12345, "value": "A"},
            {"columnName": "Notes", "value": "B"},
        ]}]
        refs = _extract_referenced_columns(rows, "add_rows")
        assert refs == {"Notes"}

    def test_update_rows_api_shape_with_columnName_extracts_the_name(self):
        updates = [{"rowId": 99, "cells": [
            {"columnName": "Status", "value": "Done"},
        ]}]
        refs = _extract_referenced_columns(updates, "update_rows")
        assert refs == {"Status"}


# ────────────────────── validator behaviour ──────────────────────


def _client_with_columns(columns: list[dict]) -> MagicMock:
    """A mock SmartsheetClient whose get_sheet returns a sheet with given columns."""
    client = MagicMock()
    client.get_sheet = AsyncMock(return_value={
        "id": 1,
        "name": "Demo",
        "columns": columns,
    })
    return client


class TestValidateColumnsForWrite:
    @pytest.mark.asyncio
    async def test_returns_none_when_all_columns_exist(self):
        client = _client_with_columns([
            {"id": 1, "title": "Task"},
            {"id": 2, "title": "Status"},
        ])
        result = await _validate_columns_for_write(
            client, "1", [{"Task": "X", "Status": "Y"}], kind="add_rows"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_blocks_unknown_columns_with_structured_payload(self):
        client = _client_with_columns([
            {"id": 1, "title": "Task"},
            {"id": 2, "title": "Status"},
        ])
        result = await _validate_columns_for_write(
            client, "1", [{"Task": "X", "Statut": "Y"}], kind="add_rows"
        )
        assert result is not None
        assert result["error"] == "UNKNOWN_COLUMNS"
        assert result["unknown_columns"] == ["Statut"]
        assert "Status" in result["valid_columns"]
        assert "case-sensitive" in result["hint"].lower()
        # Must mention the recovery actions
        assert "add_column" in result["hint"]
        assert "get_sheet_summary" in result["hint"]

    @pytest.mark.asyncio
    async def test_case_sensitivity_is_enforced(self):
        # "task" (lowercase) must not match "Task".
        client = _client_with_columns([{"id": 1, "title": "Task"}])
        result = await _validate_columns_for_write(
            client, "1", [{"task": "X"}], kind="add_rows"
        )
        assert result is not None and result["error"] == "UNKNOWN_COLUMNS"
        assert "task" in result["unknown_columns"]

    @pytest.mark.asyncio
    async def test_skips_validation_when_payload_is_api_shaped(self):
        # API-shape rows: model emitted {cells: [{columnId, value}]}. We can't
        # validate column names from columnIds, so we let it through.
        client = _client_with_columns([{"id": 1, "title": "Task"}])
        result = await _validate_columns_for_write(
            client, "1",
            [{"cells": [{"columnId": 1, "value": "X"}]}],
            kind="add_rows",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_skips_when_payload_empty(self):
        client = _client_with_columns([{"id": 1, "title": "Task"}])
        assert await _validate_columns_for_write(client, "1", [], "add_rows") is None
        assert await _validate_columns_for_write(client, "1", None, "add_rows") is None

    @pytest.mark.asyncio
    async def test_falls_through_silently_when_get_sheet_fails(self):
        # If we can't pre-fetch the schema (network blip, mock client), we let
        # the actual API call surface its own error rather than block the call.
        client = MagicMock()
        client.get_sheet = AsyncMock(side_effect=RuntimeError("network down"))
        result = await _validate_columns_for_write(
            client, "1", [{"Task": "X"}], kind="add_rows"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_falls_through_when_sheet_has_no_columns(self):
        # Empty schema (or mock returning a stub) → skip validation.
        client = _client_with_columns([])
        result = await _validate_columns_for_write(
            client, "1", [{"Task": "X"}], kind="add_rows"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_update_rows_payload_validates_inside_cells_dict(self):
        client = _client_with_columns([{"id": 1, "title": "Status"}])
        # Wrong inner column name → blocked.
        result = await _validate_columns_for_write(
            client, "1",
            [{"rowId": 99, "cells": {"Statut": {"value": "Done"}}}],
            kind="update_rows",
        )
        assert result is not None and result["error"] == "UNKNOWN_COLUMNS"
        assert "Statut" in result["unknown_columns"]


# ────────────────────── end-to-end via execute_tool ──────────────────────


class TestExecuteToolGuardIntegration:
    """Verify the guard short-circuits inside the real dispatch entry point —
    no add_rows / update_rows call ever reaches the SmartsheetClient when the
    column doesn't exist."""

    @pytest.mark.asyncio
    async def test_add_rows_with_unknown_column_does_not_call_client_add_rows(self):
        client = MagicMock()
        client.get_sheet = AsyncMock(return_value={
            "id": 1, "name": "S",
            "columns": [{"id": 10, "title": "Task"}],
        })
        client.add_rows = AsyncMock(return_value={"message": "SUCCESS"})

        result_str = await execute_tool(client, "add_rows", {
            "sheet_id": "1",
            "rows": [{"Statut": "Done"}],
        })

        client.add_rows.assert_not_called(), \
            "schema-guard must block the API call when columns are unknown"
        result = json.loads(result_str)
        assert result.get("error") == "UNKNOWN_COLUMNS"

    @pytest.mark.asyncio
    async def test_add_rows_with_valid_column_calls_client(self):
        client = MagicMock()
        client.get_sheet = AsyncMock(return_value={
            "id": 1, "name": "S",
            "columns": [{"id": 10, "title": "Task"}],
        })
        client.add_rows = AsyncMock(return_value={"message": "SUCCESS"})

        await execute_tool(client, "add_rows", {
            "sheet_id": "1",
            "rows": [{"Task": "Buy milk"}],
        })

        client.add_rows.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_add_rows_api_shape_with_unknown_columnName_is_blocked(self):
        # Live-smoke regression: the LLM produced
        #   add_rows(rows=[{"cells": [{"columnName": "GhostCol", "value": "x"}]}])
        # and Smartsheet silently created an empty row instead of erroring.
        # The schema-guard must intercept and return UNKNOWN_COLUMNS.
        client = MagicMock()
        client.get_sheet = AsyncMock(return_value={
            "id": 1, "name": "S",
            "columns": [{"id": 10, "title": "Task"}],
        })
        client.add_rows = AsyncMock(return_value={"message": "SUCCESS"})

        result_str = await execute_tool(client, "add_rows", {
            "sheet_id": "1",
            "rows": [{"cells": [
                {"columnName": "GhostCol", "value": "oops"},
            ]}],
        })

        client.add_rows.assert_not_called(), \
            "API-shape add_rows with unknown columnName must also be blocked"
        result = json.loads(result_str)
        assert result.get("error") == "UNKNOWN_COLUMNS"
        assert "GhostCol" in result.get("unknown_columns", [])

    @pytest.mark.asyncio
    async def test_update_rows_with_unknown_column_blocks_api_call(self):
        client = MagicMock()
        client.get_sheet = AsyncMock(return_value={
            "id": 1, "name": "S",
            "columns": [{"id": 10, "title": "Status"}],
        })
        client.update_rows = AsyncMock(return_value={"message": "SUCCESS"})

        result_str = await execute_tool(client, "update_rows", {
            "sheet_id": "1",
            "updates": [{"rowId": 42, "cells": {"Statut": {"value": "Done"}}}],
        })

        client.update_rows.assert_not_called()
        result = json.loads(result_str)
        assert result.get("error") == "UNKNOWN_COLUMNS"
