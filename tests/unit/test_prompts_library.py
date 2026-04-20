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
    @pytest.mark.parametrize("idx", range(8))
    def test_each_category_has_required_fields(self, catalogue, idx):
        if idx >= len(catalogue["categories"]):
            pytest.skip("fewer categories than expected slots — extra slots are ok")
        cat = catalogue["categories"][idx]
        assert isinstance(cat.get("title"), str) and cat["title"].strip()
        assert isinstance(cat.get("description"), str)
        assert cat.get("icon") in ALLOWED_ICONS, (
            f"category '{cat.get('id')}' uses unknown icon '{cat.get('icon')}'. "
            f"Add it to the icon map in help.html and index.html if you really "
            f"need a new one."
        )
        assert isinstance(cat.get("prompts"), list) and len(cat["prompts"]) >= 1


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
