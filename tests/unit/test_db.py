"""Unit tests for backend.db (SQLite) using a temp DB.

Each test gets its own database file via the `tmp_db` fixture (in conftest)
so we never touch `data/smartsheet_ctrl.sqlite`.
"""
import time

import pytest

pytestmark = pytest.mark.unit


# ────────────────────── core lifecycle ──────────────────────

class TestInitDb:
    @pytest.mark.asyncio
    async def test_init_creates_tables(self, tmp_db):
        await tmp_db.init_db()
        # File created
        assert tmp_db.DB_PATH.exists()
        # Schema is queryable: list users (empty)
        users = tmp_db._list_audit_sync(user_id=999, limit=10)
        assert users == []


class TestUpsertUser:
    @pytest.mark.asyncio
    async def test_inserts_then_returns_same_id(self, tmp_db):
        await tmp_db.init_db()
        profile = {"id": "ss-1", "email": "x@y", "firstName": "X", "lastName": "Y"}
        uid1 = await tmp_db.upsert_user("token-1", profile)
        uid2 = await tmp_db.upsert_user("token-1", profile)
        assert uid1 == uid2

    @pytest.mark.asyncio
    async def test_token_hash_changes_no_id_collision(self, tmp_db):
        await tmp_db.init_db()
        a = await tmp_db.upsert_user("tok-A", {"id": "u1", "email": "a@a"})
        b = await tmp_db.upsert_user("tok-B", {"id": "u2", "email": "b@b"})
        assert a != b


class TestAuthSession:
    @pytest.mark.asyncio
    async def test_create_then_lookup(self, tmp_db):
        await tmp_db.init_db()
        uid = await tmp_db.upsert_user("tok", {"id": "u1", "email": "a@a"})
        cookie = await tmp_db.create_auth_session(uid, ttl_days=7)
        assert isinstance(cookie, str) and len(cookie) > 16

        u = await tmp_db.get_user_by_cookie(cookie)
        assert u is not None
        assert u["id"] == uid
        assert u["email"] == "a@a"

    @pytest.mark.asyncio
    async def test_unknown_cookie_returns_none(self, tmp_db):
        await tmp_db.init_db()
        u = await tmp_db.get_user_by_cookie("not-a-real-cookie")
        assert u is None

    @pytest.mark.asyncio
    async def test_delete_invalidates(self, tmp_db):
        await tmp_db.init_db()
        uid = await tmp_db.upsert_user("tok", {"id": "u", "email": "e@e"})
        cookie = await tmp_db.create_auth_session(uid)
        await tmp_db.delete_auth_session(cookie)
        assert await tmp_db.get_user_by_cookie(cookie) is None


# ────────────────────── conversations ──────────────────────

class TestConversations:
    @pytest.mark.asyncio
    async def test_save_list_get_delete_roundtrip(self, tmp_db):
        await tmp_db.init_db()
        uid = await tmp_db.upsert_user("tok", {"id": "u", "email": "e@e"})
        await tmp_db.save_conversation("conv-1", uid, "sheet-42", "My chat")

        await tmp_db.append_message("conv-1", "user", "Hello!")
        await tmp_db.append_message("conv-1", "assistant", "Hi there.")

        convs = await tmp_db.list_conversations(uid)
        assert any(c.get("id") == "conv-1" for c in convs)

        msgs = await tmp_db.get_conversation_messages("conv-1", uid)
        roles = [m["role"] for m in msgs]
        assert "user" in roles and "assistant" in roles

        ok = await tmp_db.delete_conversation("conv-1", uid)
        assert ok is True
        assert await tmp_db.get_conversation_messages("conv-1", uid) == []

    @pytest.mark.asyncio
    async def test_other_user_cannot_read_conversation(self, tmp_db):
        await tmp_db.init_db()
        uid_a = await tmp_db.upsert_user("a", {"id": "ua", "email": "a@a"})
        uid_b = await tmp_db.upsert_user("b", {"id": "ub", "email": "b@b"})
        await tmp_db.save_conversation("c", uid_a, None, "secret")
        await tmp_db.append_message("c", "user", "private")
        msgs = await tmp_db.get_conversation_messages("c", uid_b)
        assert msgs == []


# ────────────────────── favorites ──────────────────────

class TestFavorites:
    @pytest.mark.asyncio
    async def test_add_list_remove(self, tmp_db):
        await tmp_db.init_db()
        uid = await tmp_db.upsert_user("t", {"id": "u", "email": "e"})
        await tmp_db.add_favorite(uid, "sheet-1", "Q1 plan")
        await tmp_db.add_favorite(uid, "sheet-2", "Backlog")

        favs = await tmp_db.list_favorites(uid)
        ids = {f.get("sheet_id") for f in favs}
        assert ids == {"sheet-1", "sheet-2"}

        await tmp_db.remove_favorite(uid, "sheet-1")
        favs = await tmp_db.list_favorites(uid)
        assert {f.get("sheet_id") for f in favs} == {"sheet-2"}

    @pytest.mark.asyncio
    async def test_add_is_idempotent(self, tmp_db):
        await tmp_db.init_db()
        uid = await tmp_db.upsert_user("t", {"id": "u", "email": "e"})
        await tmp_db.add_favorite(uid, "sheet-x", "X")
        await tmp_db.add_favorite(uid, "sheet-x", "X-renamed")
        favs = await tmp_db.list_favorites(uid)
        assert sum(1 for f in favs if f.get("sheet_id") == "sheet-x") == 1


# ────────────────────── audit log ──────────────────────

class TestAudit:
    @pytest.mark.asyncio
    async def test_log_then_list(self, tmp_db):
        await tmp_db.init_db()
        uid = await tmp_db.upsert_user("t", {"id": "u", "email": "e"})
        await tmp_db.log_audit(
            user_id=uid, sheet_id="sheet-1",
            tool_name="add_rows", arguments={"rows": [{"x": 1}]},
            before=None, after={"id": 99}, status="approved",
        )
        await tmp_db.log_audit(
            user_id=uid, sheet_id="sheet-2",
            tool_name="delete_rows", arguments={"row_ids": [1]},
            before={"id": 1}, after=None, status="approved",
        )

        all_entries = await tmp_db.list_audit(uid, limit=10)
        assert len(all_entries) >= 2

        only_sheet1 = await tmp_db.list_audit(uid, sheet_id="sheet-1", limit=10)
        assert all(e["sheet_id"] == "sheet-1" for e in only_sheet1)


# ────────────────────── webhook events ──────────────────────

class TestWebhookEvents:
    @pytest.mark.asyncio
    async def test_record_then_list_since(self, tmp_db):
        await tmp_db.init_db()
        uid = await tmp_db.upsert_user("t", {"id": "u", "email": "e"})
        t0 = time.time()
        await tmp_db.record_webhook_event(
            user_id=uid, sheet_id="sheet-1",
            webhook_id=42, event_type="row.created",
            payload={"foo": "bar"},
        )
        # Advance the "since" cutoff to filter older events
        events_recent = await tmp_db.list_webhook_events(uid, since=t0 - 1, limit=10)
        assert len(events_recent) >= 1

        events_future = await tmp_db.list_webhook_events(uid, since=time.time() + 60, limit=10)
        assert events_future == []


# ────────────────────── export ──────────────────────

class TestExport:
    @pytest.mark.asyncio
    async def test_export_contains_user_block(self, tmp_db):
        await tmp_db.init_db()
        uid = await tmp_db.upsert_user("t", {"id": "u-exp", "email": "z@z"})
        await tmp_db.add_favorite(uid, "s-1", "fav")
        await tmp_db.save_conversation("c-exp", uid, "s-1", "title")
        await tmp_db.append_message("c-exp", "user", "hi")

        dump = await tmp_db.export_user_data(uid)
        assert isinstance(dump, dict)
        # The export must at least include enough to re-identify the user
        # without requiring an exact key naming. We assert the user email
        # appears somewhere in the JSON-able payload.
        import json
        text = json.dumps(dump, default=str)
        assert "z@z" in text
        assert "c-exp" in text
