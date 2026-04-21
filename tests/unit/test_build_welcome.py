"""Unit tests for the `_build_welcome` helper used on connect / sheet-switch.

These guard against a class of TypeError seen in production where Smartsheet
sometimes returns `None` for `totalRowCount` / `columnCount` on freshly created
or partially loaded sheets, causing comparisons like `rows > 1000` to crash.
"""

from __future__ import annotations

import pytest

from backend.app import _build_welcome


def _summary(**overrides) -> dict:
    base = {
        "name": "Demo sheet",
        "columnCount": 3,
        "totalRowCount": 42,
        "columns": [
            {"title": "Task"},
            {"title": "Status"},
            {"title": "Owner"},
        ],
    }
    base.update(overrides)
    return base


class TestBuildWelcomeShape:
    def test_returns_response_envelope(self):
        out = _build_welcome(_summary())
        assert out["type"] == "response"
        assert isinstance(out["content"], str)
        assert isinstance(out["suggestions"], list)
        assert isinstance(out["try_cards"], list)

    def test_includes_sheet_name_and_metrics(self):
        out = _build_welcome(_summary(name="Q1 Budget", totalRowCount=12, columnCount=5))
        assert "Q1 Budget" in out["content"]
        assert "12" in out["content"]
        assert "5" in out["content"]


class TestBuildWelcomeHandlesMissingMetrics:
    """The crash we fixed: Smartsheet may return None for these keys."""

    @pytest.mark.parametrize(
        "rows_value",
        [None, 0],
        ids=["rows-None", "rows-zero"],
    )
    def test_empty_sheet_emits_warning_hint(self, rows_value):
        summary = _summary(totalRowCount=rows_value)
        out = _build_welcome(summary)
        assert "currently empty" in out["content"]

    @pytest.mark.parametrize(
        "cols_value",
        [None, 0],
        ids=["cols-None", "cols-zero"],
    )
    def test_none_columncount_does_not_crash(self, cols_value):
        summary = _summary(columnCount=cols_value)
        out = _build_welcome(summary)
        assert out["type"] == "response"

    def test_both_none_does_not_crash(self):
        summary = _summary(totalRowCount=None, columnCount=None)
        out = _build_welcome(summary)
        assert out["type"] == "response"
        assert "currently empty" in out["content"]


class TestBuildWelcomeHints:
    def test_large_sheet_emits_size_hint(self):
        out = _build_welcome(_summary(totalRowCount=5_000))
        assert "Large sheet" in out["content"]
        assert "5,000" in out["content"]

    def test_wide_sheet_emits_columns_hint(self):
        cols = [{"title": f"Col{i}"} for i in range(40)]
        out = _build_welcome(_summary(columnCount=40, columns=cols))
        assert "Wide sheet" in out["content"]
        assert "40" in out["content"]

    def test_truncates_column_list_with_more_marker(self):
        cols = [{"title": f"Col{i}"} for i in range(15)]
        out = _build_welcome(_summary(columns=cols, columnCount=15))
        assert "+7 more" in out["content"]

    def test_threshold_boundary_no_large_hint_at_1000(self):
        out = _build_welcome(_summary(totalRowCount=1000))
        assert "Large sheet" not in out["content"]

    def test_threshold_boundary_large_hint_at_1001(self):
        out = _build_welcome(_summary(totalRowCount=1001))
        assert "Large sheet" in out["content"]
