"""Unit tests for backend.agent (no LLM, no Smartsheet)."""
import pytest

from backend.agent import (
    DESTRUCTIVE_TOOLS,
    MAX_HISTORY_MESSAGES,
    MAX_TOOL_RESULT_CHARS,
    Agent,
    _fmt_cols,
    _fmt_sample,
    _fmt_sheets,
)

pytestmark = pytest.mark.unit


# ────────────────────── DESTRUCTIVE_TOOLS coverage ──────────────────────

class TestDestructiveTools:
    def test_includes_classic_writes(self):
        for n in ("delete_rows", "delete_sheet", "delete_column",
                  "update_rows", "add_rows", "share_sheet"):
            assert n in DESTRUCTIVE_TOOLS, f"{n} should be confirmed"

    def test_includes_sprint6_writes(self):
        for n in ("delete_attachment", "delete_automation", "update_automation",
                  "create_update_request", "share_workspace",
                  "create_cell_link", "update_webhook",
                  "attach_url_to_sheet", "attach_url_to_row"):
            assert n in DESTRUCTIVE_TOOLS, f"{n} should be confirmed"

    def test_excludes_pure_reads(self):
        for n in ("analyze_sheet", "list_shares", "get_sheet_summary",
                  "list_attachments", "list_webhooks"):
            assert n not in DESTRUCTIVE_TOOLS


# ────────────────────── formatters ──────────────────────

class TestFormatters:
    def test_fmt_cols(self):
        out = _fmt_cols([
            {"title": "A", "type": "TEXT_NUMBER"},
            {"title": "B", "type": "DATE"},
        ])
        assert "A(TEXT_NUMBER)" in out and "B(DATE)" in out

    def test_fmt_cols_empty(self):
        assert _fmt_cols([]) == "none"

    def test_fmt_sample_caps_at_three_rows(self):
        rows = [{"col": f"v{i}"} for i in range(10)]
        out = _fmt_sample(rows)
        assert out.count("Row") == 3

    def test_fmt_sample_empty(self):
        assert _fmt_sample([]) == ""

    def test_fmt_sheets_filters_current(self):
        sheets = [{"id": 1, "name": "Main"}, {"id": 2, "name": "Other"}]
        out = _fmt_sheets(sheets, "1")
        assert "Other" in out
        assert "Main" not in out

    def test_fmt_sheets_overflow_marker(self):
        sheets = [{"id": i, "name": f"S{i}"} for i in range(20)]
        out = _fmt_sheets(sheets, "999")
        assert "+" in out  # "+10" overflow indicator


# ────────────────────── _extract_suggestions ──────────────────────

class TestExtractSuggestions:
    def test_no_marker_returns_empty(self):
        text = "Just a plain answer."
        clean, sugg = Agent._extract_suggestions(text)
        assert clean == text
        assert sugg == []

    def test_pipe_separated(self):
        text = "Here are insights.\n\n[SUGGESTIONS] Show overdue rows | Sum amounts | Group by status"
        clean, sugg = Agent._extract_suggestions(text)
        assert "[SUGGESTIONS]" not in clean
        assert sugg == ["Show overdue rows", "Sum amounts", "Group by status"]

    def test_strips_empty_segments(self):
        text = "Body\n[SUGGESTIONS] A | | B |"
        _, sugg = Agent._extract_suggestions(text)
        assert sugg == ["A", "B"]


# ────────────────────── _prune_messages ──────────────────────

class TestPruneMessages:
    def test_under_limit_unchanged(self):
        msgs = [{"role": "user", "content": str(i)} for i in range(5)]
        assert Agent._prune_messages(msgs) == msgs

    def test_over_limit_keeps_tail(self):
        msgs = [{"role": "user", "content": str(i)} for i in range(MAX_HISTORY_MESSAGES + 10)]
        out = Agent._prune_messages(msgs)
        assert len(out) <= MAX_HISTORY_MESSAGES
        # Newest content preserved
        assert out[-1]["content"] == str(MAX_HISTORY_MESSAGES + 9)

    def test_drops_orphan_tool_result_at_head(self):
        msgs = [{"role": "user", "content": str(i)} for i in range(MAX_HISTORY_MESSAGES + 5)]
        # Force the message at the kept-window head to be a tool_result
        head_idx = len(msgs) - MAX_HISTORY_MESSAGES
        msgs[head_idx] = {"role": "tool_result", "tool_call_id": "x", "content": "ok"}
        out = Agent._prune_messages(msgs)
        assert out and out[0]["role"] != "tool_result"


# ────────────────────── _truncate_result ──────────────────────

class TestTruncateResult:
    def test_short_unchanged(self):
        s = "small payload"
        assert Agent._truncate_result(s) == s

    def test_long_is_truncated_with_marker(self):
        s = "x" * (MAX_TOOL_RESULT_CHARS * 2)
        out = Agent._truncate_result(s)
        assert len(out) > MAX_TOOL_RESULT_CHARS
        assert "[truncated" in out
        assert str(len(s)) in out


# ────────────────────── _build_system_prompt ──────────────────────

class TestBuildSystemPrompt:
    def test_substitutes_sheet_metadata(self):
        # Build a fake context — we don't need a real LLM/SmartsheetClient
        # because the prompt builder only reads from self.sheet_context.
        ctx = {
            "summary": {
                "name": "Demo Sheet",
                "totalRowCount": 123,
                "columnCount": 4,
                "columns": [
                    {"title": "Task", "type": "TEXT_NUMBER"},
                    {"title": "Status", "type": "PICKLIST"},
                ],
            },
            "sample_rows": [{"Task": "Do thing", "Status": "Open"}],
            "all_sheets": [{"id": 9, "name": "Other Sheet"}],
        }
        agent = Agent.__new__(Agent)  # bypass __init__ (no real deps needed)
        agent.sheet_context = ctx
        agent.sheet_id = "1"
        agent.pinned_sheets = []

        prompt = agent._build_system_prompt()
        assert "Demo Sheet" in prompt
        assert "Task(TEXT_NUMBER)" in prompt
        assert "123" in prompt
        assert "Other Sheet" in prompt

    def test_appends_pinned_sheets_block(self):
        agent = Agent.__new__(Agent)
        agent.sheet_context = {"summary": {"name": "Main", "totalRowCount": 1, "columnCount": 0, "columns": []}}
        agent.sheet_id = "1"
        agent.pinned_sheets = [{
            "id": "42",
            "summary": {"name": "Pinned", "totalRowCount": 5, "columnCount": 1,
                        "columns": [{"title": "X", "type": "TEXT_NUMBER"}]},
        }]
        prompt = agent._build_system_prompt()
        assert "PINNED SECONDARY SHEETS" in prompt
        assert "Pinned" in prompt
        assert "ID: 42" in prompt
