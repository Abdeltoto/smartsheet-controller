"""Unit tests for the Help / Prompts library.

Covers:

- The shipped `frontend/data/prompts.json` is well-formed and respects the
  contract consumed by both the in-app modal and the dedicated `/help`
  page (every category has an id/title/icon, every prompt has the
  required fields, difficulty/risk values are within the legend).
- The `/api/prompts` endpoint returns the catalogue as JSON, gracefully
  handles a missing or malformed file, and the `/help` route serves the
  dedicated page.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


REPO_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_FILE = REPO_ROOT / "frontend" / "data" / "prompts.json"
HELP_PAGE = REPO_ROOT / "frontend" / "help.html"


# ────────────────────────── Static contract tests ──────────────────────────


VALID_DIFFICULTIES = {"easy", "medium", "advanced"}
VALID_RISKS = {"safe", "caution", "destructive"}
ALLOWED_ICONS = {
    "search", "rows", "columns", "link", "sigma",
    "users", "bolt", "wrench",
    # New in v2 (April 2026): hierarchy, discussions, attachments,
    # reports/dashboards, workspace categories.
    "tree", "chat", "paperclip", "chart", "folder",
}
REQUIRED_PROMPT_KEYS = {"id", "title", "prompt"}


@pytest.fixture(scope="module")
def catalogue() -> dict:
    assert PROMPTS_FILE.exists(), (
        f"Prompts catalogue is missing at {PROMPTS_FILE}. The Help feature "
        f"depends on this file being shipped with the app."
    )
    with PROMPTS_FILE.open("r", encoding="utf-8") as fh:
        return json.load(fh)


class TestCatalogueShape:
    def test_top_level_keys(self, catalogue):
        assert isinstance(catalogue, dict)
        assert "version" in catalogue and isinstance(catalogue["version"], int)
        assert "categories" in catalogue
        assert isinstance(catalogue["categories"], list)
        assert len(catalogue["categories"]) >= 1

    def test_intro_and_legend_present(self, catalogue):
        # The Help modal/page show these — keep them in the catalogue so
        # operators don't have to redocument the difficulty/risk axes.
        assert isinstance(catalogue.get("intro"), str) and catalogue["intro"].strip()
        legend = catalogue.get("legend") or {}
        assert "difficulty" in legend and "risk" in legend
        assert set(legend["difficulty"].keys()) == VALID_DIFFICULTIES
        assert set(legend["risk"].keys()) == VALID_RISKS

    def test_category_ids_unique(self, catalogue):
        ids = [c.get("id") for c in catalogue["categories"]]
        assert all(isinstance(i, str) and i for i in ids), "category ids must be non-empty strings"
        assert len(ids) == len(set(ids)), f"duplicate category id: {ids}"


class TestCategoryShape:
    def test_each_category_has_required_fields(self, catalogue):
        # Validate every category dynamically — the catalogue grows
        # over time and we never want a new category to silently bypass
        # the contract checks.
        problems = []
        for cat in catalogue["categories"]:
            cid = cat.get("id", "?")
            if not (isinstance(cat.get("title"), str) and cat["title"].strip()):
                problems.append(f"[{cid}] empty/missing title")
            if not isinstance(cat.get("description"), str):
                problems.append(f"[{cid}] description must be a string")
            if cat.get("icon") not in ALLOWED_ICONS:
                problems.append(
                    f"[{cid}] unknown icon '{cat.get('icon')}'. "
                    f"Add it to the icon map in help.html and index.html "
                    f"and to ALLOWED_ICONS in this test."
                )
            if not (isinstance(cat.get("prompts"), list) and len(cat["prompts"]) >= 1):
                problems.append(f"[{cid}] must contain at least one prompt")
        assert not problems, "Category contract violations:\n" + "\n".join(
            f"  - {msg}" for msg in problems
        )


class TestPromptShape:
    def test_every_prompt_has_required_fields(self, catalogue):
        problems = []
        seen_ids = set()
        for cat in catalogue["categories"]:
            for p in cat["prompts"]:
                missing = REQUIRED_PROMPT_KEYS - set(p.keys())
                if missing:
                    problems.append((cat["id"], p.get("id", "?"), f"missing keys: {missing}"))
                pid = p.get("id")
                if pid in seen_ids:
                    problems.append((cat["id"], pid, "duplicate prompt id across catalogue"))
                seen_ids.add(pid)
                if not isinstance(p.get("prompt"), str) or not p["prompt"].strip():
                    problems.append((cat["id"], pid, "prompt body is empty"))
                if "difficulty" in p and p["difficulty"] not in VALID_DIFFICULTIES:
                    problems.append((cat["id"], pid, f"bad difficulty: {p['difficulty']}"))
                if "risk" in p and p["risk"] not in VALID_RISKS:
                    problems.append((cat["id"], pid, f"bad risk: {p['risk']}"))
                if "tags" in p and not isinstance(p["tags"], list):
                    problems.append((cat["id"], pid, "tags must be a list"))
        assert not problems, "Catalogue contract violations:\n" + "\n".join(
            f"  - [{c}/{pid}] {msg}" for c, pid, msg in problems
        )

    def test_destructive_prompts_mention_confirmation(self, catalogue):
        # A destructive prompt SHOULD instruct the agent to confirm before
        # acting — otherwise it will surprise users in production. We accept
        # any of the canonical confirmation cues.
        cues = ("confirm", "preview", "list", "ask", "wait", "before applying", "before doing")
        offenders = []
        for cat in catalogue["categories"]:
            for p in cat["prompts"]:
                if p.get("risk") != "destructive":
                    continue
                body = (p.get("prompt") or "").lower()
                if not any(c in body for c in cues):
                    offenders.append(f"{cat['id']}/{p['id']}")
        assert not offenders, (
            "Destructive prompts must instruct the agent to preview/confirm "
            f"before acting. Offenders: {offenders}"
        )


# ────────────────────────── HTTP layer ──────────────────────────


@pytest.fixture
def http_client(monkeypatch, tmp_path):
    """Spin up the FastAPI app with isolated DB + JSONL paths."""
    db_file = tmp_path / "test.sqlite"
    monkeypatch.setenv("SMARTSHEET_CTRL_DB", str(db_file))
    monkeypatch.setenv("BUG_REPORTS_JSONL_PATH", str(tmp_path / "bugs.jsonl"))

    import backend.db as ssdb
    importlib.reload(ssdb)
    import backend.app as bapp
    importlib.reload(bapp)
    ssdb.DB_PATH = db_file
    ssdb._initialized = False

    with TestClient(bapp.app) as client:
        yield client, bapp


class TestPromptsEndpoint:
    def test_returns_catalogue(self, http_client):
        client, _ = http_client
        r = client.get("/api/prompts")
        assert r.status_code == 200
        body = r.json()
        assert "categories" in body
        assert isinstance(body["categories"], list)
        # The shipped catalogue must include at least one cross-sheet
        # prompt — that's the headline use case our agent had to be
        # taught and we never want this to regress.
        ids = {c["id"] for c in body["categories"]}
        assert "crosssheet" in ids

    def test_handles_missing_file(self, http_client, monkeypatch, tmp_path):
        client, bapp = http_client
        bogus = tmp_path / "nope.json"
        monkeypatch.setattr(bapp, "PROMPTS_PATH", bogus)
        r = client.get("/api/prompts")
        assert r.status_code == 404
        assert "not found" in (r.json().get("error") or "").lower()

    def test_handles_malformed_json(self, http_client, monkeypatch, tmp_path):
        client, bapp = http_client
        bad = tmp_path / "bad.json"
        bad.write_text("not json", encoding="utf-8")
        monkeypatch.setattr(bapp, "PROMPTS_PATH", bad)
        r = client.get("/api/prompts")
        assert r.status_code == 500
        assert "valid json" in (r.json().get("error") or "").lower()

    def test_handles_missing_categories_key(self, http_client, monkeypatch, tmp_path):
        client, bapp = http_client
        bad = tmp_path / "wrong.json"
        bad.write_text(json.dumps({"version": 1}), encoding="utf-8")
        monkeypatch.setattr(bapp, "PROMPTS_PATH", bad)
        r = client.get("/api/prompts")
        assert r.status_code == 500
        assert "categories" in (r.json().get("error") or "")

    def test_help_page_served(self, http_client):
        client, _ = http_client
        r = client.get("/help")
        assert r.status_code == 200
        assert "text/html" in r.headers.get("content-type", "")
        # Sanity: the page should reference the API it consumes.
        assert b"/api/prompts" in r.content
        # And carry the prompts library title so we know it's the right file.
        assert b"Prompts library" in r.content


class TestHelpPageStaticAssets:
    def test_help_page_file_present(self):
        assert HELP_PAGE.exists(), (
            f"Missing {HELP_PAGE}. The /help route depends on this file."
        )

    def test_help_page_back_link_to_root(self):
        body = HELP_PAGE.read_text(encoding="utf-8")
        # The full-page version must offer a way back to the app.
        assert 'href="/"' in body or "href='/'" in body


# ────────────────────────── Prompts sidebar (right margin) ──────────────────────────


INDEX_PAGE = REPO_ROOT / "frontend" / "index.html"


class TestPromptSidebarStaticAssets:
    """The prompts sidebar lives in `index.html` and surfaces the same
    `/api/prompts` catalogue as the Help modal, but in a denser, always-
    at-hand form. These tests pin down the structural contract so a
    refactor of the chat layout can't silently delete it.
    """

    @pytest.fixture(scope="class")
    def index_html(self) -> str:
        assert INDEX_PAGE.exists(), f"Missing {INDEX_PAGE}"
        return INDEX_PAGE.read_text(encoding="utf-8")

    def test_sidebar_container_present(self, index_html):
        # The aside lives inside `#chat`, after `.chat-main`.
        assert 'id="prompt-sidebar"' in index_html
        assert 'aria-label="Prompts library"' in index_html

    def test_header_toggle_button_present(self, index_html):
        # A toggle in the top header (with a sensible default-hidden state
        # since it should only appear once a session is open).
        assert 'id="btn-prompt-sidebar"' in index_html
        assert 'onclick="togglePromptSidebar()"' in index_html

    def test_reopen_rail_present(self, index_html):
        # When the user collapses the sidebar, a vertical "Prompts" rail
        # on the right edge brings it back — without it, collapse becomes
        # a one-way trap unless the user knows the keyboard shortcut.
        assert 'id="prompt-sidebar-rail"' in index_html

    def test_search_input_present(self, index_html):
        assert 'id="psb-search"' in index_html
        assert 'oninput="filterPromptSidebar(' in index_html

    def test_body_container_present(self, index_html):
        assert 'id="psb-body"' in index_html

    def test_initialised_when_chat_opens(self, index_html):
        # `openChat` must call `initPromptSidebar()` so the catalogue is
        # rendered as soon as the user lands in the chat — otherwise the
        # sidebar stays empty until they manually open it.
        assert "initPromptSidebar();" in index_html

    def test_keyboard_shortcut_wired(self, index_html):
        # Ctrl+Shift+K toggles the sidebar.
        assert "case 'K': e.preventDefault(); togglePromptSidebar();" in index_html
        # And the shortcuts modal must document it so users can discover it.
        assert "Toggle prompts sidebar" in index_html

    def test_disconnect_clears_sidebar(self, index_html):
        # On disconnect we hide the toggle and collapse the sidebar so the
        # next account that connects starts in a clean state.
        assert "if (psbSb) psbSb.classList.add('collapsed')" in index_html
        assert "if (psbRail) psbRail.style.display = 'none'" in index_html

    def test_core_functions_defined(self, index_html):
        # Defensive: pin down the public function names that other parts
        # of the file (and the tests) rely on.
        for name in (
            "function togglePromptSidebar",
            "async function initPromptSidebar",
            "function renderPromptSidebar",
            "function filterPromptSidebar",
            "function insertPromptSidebarPrompt",
            "async function copyPromptSidebarPrompt",
            "function togglePromptSidebarCategory",
        ):
            assert name in index_html, f"Missing JS function: {name}"

    def test_persistence_keys_defined(self, index_html):
        # The open/closed state and the open categories are persisted in
        # localStorage. Hard-coding these keys here keeps us honest about
        # backwards-compatibility if we ever rename them.
        assert "ss_ctrl_psb_open" in index_html
        assert "ss_ctrl_psb_open_cats" in index_html

    def test_default_open_category_is_exploration(self, index_html):
        # Per the UX brief, the "Exploration" category is the only one
        # that should be expanded by default — that's the lowest-risk,
        # most-discoverable starting point for new users.
        assert "PSB_DEFAULT_OPEN_CATS = ['exploration']" in index_html

    def test_exploration_category_exists(self, catalogue):
        # The default-open category must actually exist in the catalogue,
        # otherwise the sidebar opens with nothing visible.
        ids = {c["id"] for c in catalogue["categories"]}
        assert "exploration" in ids, (
            "The sidebar defaults to the 'exploration' category being open. "
            "If you renamed it, also update PSB_DEFAULT_OPEN_CATS in "
            "index.html and this test."
        )
