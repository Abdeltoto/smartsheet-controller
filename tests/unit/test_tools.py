"""Unit tests for backend.tools (intent routing + chart generator)."""
import pytest

from backend.tools import (
    TOOL_DEFINITIONS,
    _generate_chart,
    _INTENT_KEYWORDS,
    _TOOLS_BY_INTENT,
    select_tools_for_message,
)

pytestmark = pytest.mark.unit


def _names(tools: list[dict]) -> set[str]:
    return {t["name"] for t in tools}


class TestToolDefinitions:
    def test_each_tool_has_required_metadata(self):
        for t in TOOL_DEFINITIONS:
            assert "name" in t and t["name"]
            assert "description" in t and t["description"]
            assert "parameters" in t

    def test_unique_tool_names(self):
        names = [t["name"] for t in TOOL_DEFINITIONS]
        assert len(names) == len(set(names)), "Duplicate tool names found"

    def test_intent_map_references_real_tools(self):
        all_names = _names(TOOL_DEFINITIONS)
        for intent, tool_set in _TOOLS_BY_INTENT.items():
            missing = tool_set - all_names
            assert not missing, f"Intent {intent!r} references undefined tools: {missing}"


class TestSelectToolsForMessage:
    def test_empty_message_returns_everything(self):
        assert select_tools_for_message("") == TOOL_DEFINITIONS
        assert select_tools_for_message(None) == TOOL_DEFINITIONS  # type: ignore[arg-type]

    def test_read_only_query_excludes_destructive_writes(self):
        sel = _names(select_tools_for_message("show me the rows in this sheet"))
        # Read tools should always be allowed
        assert "analyze_sheet" in sel
        # Destructive structural writes shouldn't sneak in for a pure read
        # (they're only added when keywords trigger their intent)
        assert "delete_sheet" not in sel
        assert "delete_column" not in sel

    @pytest.mark.parametrize("phrase", [
        "add a new row with task 'demo'",
        "ajoute une ligne",
        "update the row to mark it done",
        "delete row 12",
    ])
    def test_write_row_intent(self, phrase):
        sel = _names(select_tools_for_message(phrase))
        assert "add_rows" in sel or "update_rows" in sel or "delete_rows" in sel

    @pytest.mark.parametrize("phrase", [
        "share this sheet with bob@example.com",
        "donne accès au workspace à alice",
        "list permissions",
    ])
    def test_share_intent(self, phrase):
        sel = _names(select_tools_for_message(phrase))
        assert "list_shares" in sel

    def test_attachment_intent_picks_attachment_tools(self):
        sel = _names(select_tools_for_message("upload a fichier on row 5"))
        assert "attach_url_to_row" in sel or "upload_file_to_row" in sel or "list_attachments" in sel

    def test_chart_intent(self):
        sel = _names(select_tools_for_message("draw a bar chart of statuses"))
        assert "generate_chart" in sel

    def test_webhook_intent(self):
        sel = _names(select_tools_for_message("create a webhook on this sheet"))
        assert "create_webhook" in sel

    def test_safety_net_returns_full_list_when_match_too_small(self):
        # Gibberish: no intent matches; selection should still include
        # core read tools. select_tools_for_message guarantees >=5 tools.
        sel = select_tools_for_message("xyzzy plugh frobnicate")
        assert len(sel) >= 5


class TestGenerateChart:
    def test_marks_chart_payload(self):
        out = _generate_chart({
            "chart_type": "bar",
            "title": "Demo",
            "labels": ["A", "B"],
            "datasets": [{"label": "x", "data": [1, 2]}],
        })
        assert out["__is_chart__"] is True
        assert out["chart_spec"]["type"] == "bar"
        assert out["chart_spec"]["data"]["labels"] == ["A", "B"]

    def test_pie_uses_per_slice_colors(self):
        out = _generate_chart({
            "chart_type": "pie",
            "title": "Pie",
            "labels": ["A", "B", "C"],
            "datasets": [{"label": "d", "data": [1, 2, 3]}],
        })
        ds = out["chart_spec"]["data"]["datasets"][0]
        assert isinstance(ds["backgroundColor"], list)
        assert len(ds["backgroundColor"]) == 3

    def test_default_border_width(self):
        out = _generate_chart({
            "chart_type": "line",
            "title": "x",
            "labels": ["1"],
            "datasets": [{"label": "y", "data": [1]}],
        })
        assert out["chart_spec"]["data"]["datasets"][0]["borderWidth"] == 1

    def test_intent_keywords_are_lowercase(self):
        for intent, kws in _INTENT_KEYWORDS.items():
            for kw in kws:
                assert kw == kw.lower(), f"keyword {kw!r} for {intent!r} not lowercase"
