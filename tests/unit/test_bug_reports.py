"""Unit + integration tests for the in-app bug-report feature.

Covers:
- DB layer (`backend.db` helpers): create / list / count / status update.
- HTTP layer (`/api/bug-reports`): public POST, admin-gated GET and status
  update, validation, context enrichment, JSONL mirror.

The DB tests use the `tmp_db` fixture so we never touch the real SQLite file.
The HTTP tests spin up the FastAPI app with `TestClient`, monkeypatch
`SMARTSHEET_CTRL_DB` and the JSONL path to a temp directory, and provide an
admin token via `BUG_REPORTS_ADMIN_TOKEN`.
"""
from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.unit


# ─────────────────────────── DB layer ───────────────────────────

class TestCreateBugReport:
    @pytest.mark.asyncio
    async def test_minimal_required_fields(self, tmp_db):
        await tmp_db.init_db()
        rid = await tmp_db.create_bug_report(
            user_id=None,
            session_id=None,
            sheet_id=None,
            reporter_email=None,
            reporter_name=None,
            description="Boom: clicking submit crashes",
            steps=None,
        )
        assert isinstance(rid, int) and rid >= 1

        rows = await tmp_db.list_bug_reports()
        assert len(rows) == 1
        r = rows[0]
        assert r["description"] == "Boom: clicking submit crashes"
        assert r["status"] == "open"
        assert r["severity"] == "normal"
        assert r["context"] is None

    @pytest.mark.asyncio
    async def test_invalid_severity_falls_back_to_normal(self, tmp_db):
        await tmp_db.init_db()
        await tmp_db.create_bug_report(
            user_id=None, session_id=None, sheet_id=None,
            reporter_email=None, reporter_name=None,
            description="x", steps=None, severity="catastrophic",
        )
        rows = await tmp_db.list_bug_reports()
        assert rows[0]["severity"] == "normal"

    @pytest.mark.asyncio
    async def test_persists_context_as_json(self, tmp_db):
        await tmp_db.init_db()
        ctx = {"user_agent": "pytest", "metrics": {"loop_blocked": 2}}
        await tmp_db.create_bug_report(
            user_id=42, session_id="abc", sheet_id="999",
            reporter_email="x@y.z", reporter_name="X Y",
            description="ctx test", steps="1. do thing",
            severity="high", context=ctx,
        )
        rows = await tmp_db.list_bug_reports()
        assert rows[0]["context"] == ctx
        assert rows[0]["user_id"] == 42
        assert rows[0]["session_id"] == "abc"
        assert rows[0]["sheet_id"] == "999"
        assert rows[0]["reporter_email"] == "x@y.z"
        assert rows[0]["steps"] == "1. do thing"
        assert rows[0]["severity"] == "high"


class TestListAndCount:
    @pytest.mark.asyncio
    async def test_list_orders_by_recent_first(self, tmp_db):
        await tmp_db.init_db()
        for i in range(3):
            await tmp_db.create_bug_report(
                user_id=None, session_id=None, sheet_id=None,
                reporter_email=None, reporter_name=None,
                description=f"bug {i}", steps=None,
            )
        rows = await tmp_db.list_bug_reports()
        assert [r["description"] for r in rows] == ["bug 2", "bug 1", "bug 0"]

    @pytest.mark.asyncio
    async def test_filter_by_status(self, tmp_db):
        await tmp_db.init_db()
        ids = []
        for i in range(3):
            ids.append(await tmp_db.create_bug_report(
                user_id=None, session_id=None, sheet_id=None,
                reporter_email=None, reporter_name=None,
                description=f"bug {i}", steps=None,
            ))
        await tmp_db.update_bug_report_status(ids[0], "fixed")
        await tmp_db.update_bug_report_status(ids[1], "triaged")

        opens = await tmp_db.list_bug_reports(status="open")
        fixed = await tmp_db.list_bug_reports(status="fixed")
        triaged = await tmp_db.list_bug_reports(status="triaged")
        assert len(opens) == 1 and opens[0]["description"] == "bug 2"
        assert len(fixed) == 1 and fixed[0]["id"] == ids[0]
        assert len(triaged) == 1 and triaged[0]["id"] == ids[1]

        assert await tmp_db.count_bug_reports() == 3
        assert await tmp_db.count_bug_reports("open") == 1
        assert await tmp_db.count_bug_reports("fixed") == 1

    @pytest.mark.asyncio
    async def test_unknown_status_filter_is_ignored(self, tmp_db):
        await tmp_db.init_db()
        await tmp_db.create_bug_report(
            user_id=None, session_id=None, sheet_id=None,
            reporter_email=None, reporter_name=None,
            description="x", steps=None,
        )
        # Garbage status → returns all reports (defensive)
        rows = await tmp_db.list_bug_reports(status="banana")
        assert len(rows) == 1
        # And count_bug_reports too
        assert await tmp_db.count_bug_reports("banana") == 1

    @pytest.mark.asyncio
    async def test_pagination(self, tmp_db):
        await tmp_db.init_db()
        for i in range(5):
            await tmp_db.create_bug_report(
                user_id=None, session_id=None, sheet_id=None,
                reporter_email=None, reporter_name=None,
                description=f"bug {i}", steps=None,
            )
        first = await tmp_db.list_bug_reports(limit=2, offset=0)
        page2 = await tmp_db.list_bug_reports(limit=2, offset=2)
        assert len(first) == 2 and len(page2) == 2
        assert first[0]["description"] == "bug 4"
        assert page2[0]["description"] == "bug 2"


class TestStatusUpdate:
    @pytest.mark.asyncio
    async def test_valid_transitions(self, tmp_db):
        await tmp_db.init_db()
        rid = await tmp_db.create_bug_report(
            user_id=None, session_id=None, sheet_id=None,
            reporter_email=None, reporter_name=None,
            description="x", steps=None,
        )
        for new_status in ("triaged", "fixed", "wontfix", "open"):
            ok = await tmp_db.update_bug_report_status(rid, new_status)
            assert ok is True
            rows = await tmp_db.list_bug_reports()
            assert rows[0]["status"] == new_status

    @pytest.mark.asyncio
    async def test_invalid_status_rejected(self, tmp_db):
        await tmp_db.init_db()
        rid = await tmp_db.create_bug_report(
            user_id=None, session_id=None, sheet_id=None,
            reporter_email=None, reporter_name=None,
            description="x", steps=None,
        )
        ok = await tmp_db.update_bug_report_status(rid, "deleted")
        assert ok is False
        rows = await tmp_db.list_bug_reports()
        assert rows[0]["status"] == "open"

    @pytest.mark.asyncio
    async def test_unknown_id_returns_false(self, tmp_db):
        await tmp_db.init_db()
        ok = await tmp_db.update_bug_report_status(99999, "fixed")
        assert ok is False


# ─────────────────────────── HTTP layer ───────────────────────────

@pytest.fixture
def http_bug(monkeypatch, tmp_path):
    """FastAPI TestClient with the DB and JSONL mirror redirected to tmp_path,
    and the admin token set to a deterministic value."""
    db_file = tmp_path / "bugs.sqlite"
    jsonl_file = tmp_path / "bug_reports.jsonl"

    monkeypatch.setenv("SMARTSHEET_CTRL_DB", str(db_file))
    monkeypatch.setenv("BUG_REPORTS_JSONL_PATH", str(jsonl_file))
    monkeypatch.setenv("BUG_REPORTS_ADMIN_TOKEN", "test-admin-token")

    # Re-import db & app so our env vars are picked up at module load.
    import backend.db as ssdb
    importlib.reload(ssdb)
    import backend.app as bapp
    importlib.reload(bapp)

    # Force the redirected paths even though reload should have done it.
    bapp.BUG_REPORTS_JSONL = Path(str(jsonl_file))
    ssdb.DB_PATH = db_file
    ssdb._initialized = False

    with TestClient(bapp.app) as client:
        yield client, jsonl_file


class TestBugReportEndpointPublic:
    def test_post_minimal_creates_report(self, http_bug):
        client, _ = http_bug
        r = client.post("/api/bug-reports", json={"description": "boom"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert isinstance(body["id"], int) and body["id"] >= 1

    def test_post_empty_description_rejected(self, http_bug):
        client, _ = http_bug
        r = client.post("/api/bug-reports", json={"description": "   "})
        assert r.status_code == 400
        assert "description" in r.json()["error"]

    def test_post_missing_description_rejected(self, http_bug):
        client, _ = http_bug
        r = client.post("/api/bug-reports", json={})
        # Pydantic validation → 422
        assert r.status_code in (400, 422)

    def test_post_attaches_server_side_facts(self, http_bug):
        client, jsonl_file = http_bug
        r = client.post(
            "/api/bug-reports",
            json={
                "description": "checking server enrichment",
                "context": {"user_agent": "client-claim"},
            },
            headers={"User-Agent": "pytest-real-ua/1.0"},
        )
        assert r.status_code == 200
        # Inspect the JSONL mirror to see the persisted context
        assert jsonl_file.exists()
        records = [json.loads(l) for l in jsonl_file.read_text("utf-8").splitlines() if l]
        assert len(records) == 1
        ctx = records[0]["context"]
        # server-side enrichment
        assert "server_time" in ctx
        # user_agent: client value wins via setdefault, but server records its own when absent;
        # here the client explicitly claimed one so it should be preserved.
        assert ctx["user_agent"] == "client-claim"

    def test_post_long_description_is_truncated(self, http_bug):
        client, jsonl_file = http_bug
        big = "x" * 10_000
        r = client.post("/api/bug-reports", json={"description": big})
        assert r.status_code == 200
        records = [json.loads(l) for l in jsonl_file.read_text("utf-8").splitlines() if l]
        assert len(records[0]["description"]) == 8000


class TestBugReportEndpointAdmin:
    def test_get_requires_admin_token(self, http_bug):
        client, _ = http_bug
        client.post("/api/bug-reports", json={"description": "one"})
        r = client.get("/api/bug-reports")
        assert r.status_code == 403

    def test_get_with_wrong_token_rejected(self, http_bug):
        client, _ = http_bug
        client.post("/api/bug-reports", json={"description": "one"})
        r = client.get("/api/bug-reports", headers={"X-Admin-Token": "nope"})
        assert r.status_code == 403

    def test_get_with_correct_token_returns_items(self, http_bug):
        client, _ = http_bug
        client.post("/api/bug-reports", json={"description": "first"})
        client.post("/api/bug-reports", json={"description": "second"})
        r = client.get(
            "/api/bug-reports",
            headers={"X-Admin-Token": "test-admin-token"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2
        assert len(body["items"]) == 2
        # Most recent first
        assert body["items"][0]["description"] == "second"

    def test_get_filter_by_status(self, http_bug):
        client, _ = http_bug
        r1 = client.post("/api/bug-reports", json={"description": "to-fix"})
        client.post("/api/bug-reports", json={"description": "still-open"})
        rid = r1.json()["id"]
        # Mark as fixed
        client.post(
            f"/api/bug-reports/{rid}/status",
            json={"status": "fixed"},
            headers={"X-Admin-Token": "test-admin-token"},
        )
        opens = client.get(
            "/api/bug-reports?status=open",
            headers={"X-Admin-Token": "test-admin-token"},
        ).json()
        fixed = client.get(
            "/api/bug-reports?status=fixed",
            headers={"X-Admin-Token": "test-admin-token"},
        ).json()
        assert opens["total"] == 1
        assert fixed["total"] == 1

    def test_status_update_requires_admin_token(self, http_bug):
        client, _ = http_bug
        rid = client.post("/api/bug-reports", json={"description": "x"}).json()["id"]
        r = client.post(f"/api/bug-reports/{rid}/status", json={"status": "fixed"})
        assert r.status_code == 403

    def test_status_update_invalid_status(self, http_bug):
        client, _ = http_bug
        rid = client.post("/api/bug-reports", json={"description": "x"}).json()["id"]
        r = client.post(
            f"/api/bug-reports/{rid}/status",
            json={"status": "banana"},
            headers={"X-Admin-Token": "test-admin-token"},
        )
        assert r.status_code == 404

    def test_admin_endpoint_disabled_when_token_unset(self, monkeypatch, tmp_path):
        # Unlike other tests, we do NOT set BUG_REPORTS_ADMIN_TOKEN.
        db_file = tmp_path / "bugs2.sqlite"
        jsonl_file = tmp_path / "bugs2.jsonl"
        monkeypatch.setenv("SMARTSHEET_CTRL_DB", str(db_file))
        monkeypatch.setenv("BUG_REPORTS_JSONL_PATH", str(jsonl_file))
        monkeypatch.delenv("BUG_REPORTS_ADMIN_TOKEN", raising=False)

        import backend.db as ssdb
        importlib.reload(ssdb)
        import backend.app as bapp
        importlib.reload(bapp)
        bapp.BUG_REPORTS_JSONL = Path(str(jsonl_file))
        ssdb.DB_PATH = db_file
        ssdb._initialized = False

        with TestClient(bapp.app) as client:
            client.post("/api/bug-reports", json={"description": "x"})
            # No header at all
            assert client.get("/api/bug-reports").status_code == 403
            # And even WITH a header value: env is unset → endpoint disabled
            r = client.get(
                "/api/bug-reports",
                headers={"X-Admin-Token": "anything"},
            )
            assert r.status_code == 403
