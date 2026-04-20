"""SQLite persistence layer (Sprint 5).

Zero-config single-file DB. Uses stdlib sqlite3 + asyncio.to_thread for async safety.
Schema covers: users, sessions, conversations, messages, pins, favorites,
audit_log, webhook_events.

Source of truth for cross-device data; the frontend's localStorage becomes a
write-through cache.
"""
import asyncio
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DB_PATH = Path(os.getenv("SMARTSHEET_CTRL_DB", "data/smartsheet_ctrl.sqlite"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    smartsheet_user_id TEXT UNIQUE NOT NULL,
    email TEXT,
    first_name TEXT,
    last_name TEXT,
    token_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_login REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);

CREATE TABLE IF NOT EXISTS auth_sessions (
    cookie_token TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL,
    expires_at REAL NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    sheet_id TEXT,
    title TEXT,
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_conversations_user ON conversations(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    payload_json TEXT,
    created_at REAL NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS favorites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    sheet_id TEXT NOT NULL,
    sheet_name TEXT,
    created_at REAL NOT NULL,
    UNIQUE(user_id, sheet_id),
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    sheet_id TEXT,
    tool_name TEXT NOT NULL,
    arguments_json TEXT,
    before_json TEXT,
    after_json TEXT,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_sheet ON audit_log(sheet_id, created_at DESC);

CREATE TABLE IF NOT EXISTS webhook_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    sheet_id TEXT,
    webhook_id INTEGER,
    event_type TEXT,
    payload_json TEXT,
    received_at REAL NOT NULL,
    delivered INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_webhook_events_user ON webhook_events(user_id, received_at DESC);

CREATE TABLE IF NOT EXISTS bug_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,           -- nullable: bug can be reported pre-login
    session_id TEXT,           -- ephemeral session id (for traceability)
    sheet_id TEXT,
    reporter_email TEXT,       -- snapshot at time of report (may differ from current)
    reporter_name TEXT,
    description TEXT NOT NULL,
    steps TEXT,                -- "what were you doing" optional
    severity TEXT NOT NULL DEFAULT 'normal',  -- low / normal / high / blocker
    context_json TEXT,         -- raw client-side context bundle (metrics, last messages, ua, ...)
    status TEXT NOT NULL DEFAULT 'open',      -- open / triaged / fixed / wontfix
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bug_reports_status ON bug_reports(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bug_reports_user ON bug_reports(user_id, created_at DESC);
"""

_initialized = False
_init_lock = asyncio.Lock()


def _hash_token(token: str) -> str:
    """One-way hash of the Smartsheet token for identification. Salt is the SS user ID, set after lookup."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@contextmanager
def _conn():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def _init_sync() -> None:
    with _conn() as c:
        c.executescript(SCHEMA)


async def init_db() -> None:
    global _initialized
    async with _init_lock:
        if _initialized:
            return
        await asyncio.to_thread(_init_sync)
        _initialized = True
        log.info("SQLite DB ready at %s", DB_PATH)


# ─────────────── Users ───────────────

def _upsert_user_sync(token: str, profile: dict) -> int:
    now = time.time()
    th = _hash_token(token)
    ssid = str(profile.get("id") or "")
    with _conn() as c:
        cur = c.execute("SELECT id FROM users WHERE smartsheet_user_id = ?", (ssid,))
        row = cur.fetchone()
        if row:
            c.execute(
                "UPDATE users SET email=?, first_name=?, last_name=?, token_hash=?, last_login=? WHERE id=?",
                (profile.get("email"), profile.get("firstName"), profile.get("lastName"), th, now, row["id"]),
            )
            return row["id"]
        cur = c.execute(
            "INSERT INTO users (smartsheet_user_id, email, first_name, last_name, token_hash, created_at, last_login) VALUES (?,?,?,?,?,?,?)",
            (ssid, profile.get("email"), profile.get("firstName"), profile.get("lastName"), th, now, now),
        )
        return cur.lastrowid


async def upsert_user(token: str, profile: dict) -> int:
    return await asyncio.to_thread(_upsert_user_sync, token, profile)


# ─────────────── Auth sessions (cookie-based) ───────────────

def _create_auth_session_sync(user_id: int, ttl_days: int = 30) -> str:
    cookie_token = secrets.token_urlsafe(32)
    now = time.time()
    expires = now + ttl_days * 86400
    with _conn() as c:
        c.execute(
            "INSERT INTO auth_sessions (cookie_token, user_id, created_at, last_seen, expires_at) VALUES (?,?,?,?,?)",
            (cookie_token, user_id, now, now, expires),
        )
    return cookie_token


async def create_auth_session(user_id: int, ttl_days: int = 30) -> str:
    return await asyncio.to_thread(_create_auth_session_sync, user_id, ttl_days)


def _get_user_by_cookie_sync(cookie_token: str) -> dict | None:
    if not cookie_token:
        return None
    now = time.time()
    with _conn() as c:
        row = c.execute(
            """SELECT u.id, u.smartsheet_user_id, u.email, u.first_name, u.last_name, a.expires_at
               FROM auth_sessions a JOIN users u ON u.id = a.user_id
               WHERE a.cookie_token = ?""",
            (cookie_token,),
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] < now:
            c.execute("DELETE FROM auth_sessions WHERE cookie_token = ?", (cookie_token,))
            return None
        c.execute("UPDATE auth_sessions SET last_seen = ? WHERE cookie_token = ?", (now, cookie_token))
        return dict(row)


async def get_user_by_cookie(cookie_token: str) -> dict | None:
    return await asyncio.to_thread(_get_user_by_cookie_sync, cookie_token)


def _delete_auth_session_sync(cookie_token: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM auth_sessions WHERE cookie_token = ?", (cookie_token,))


async def delete_auth_session(cookie_token: str) -> None:
    await asyncio.to_thread(_delete_auth_session_sync, cookie_token)


# ─────────────── Conversations & messages ───────────────

def _save_conversation_sync(conv_id: str, user_id: int, sheet_id: str | None, title: str | None) -> None:
    now = time.time()
    with _conn() as c:
        c.execute(
            """INSERT INTO conversations (id, user_id, sheet_id, title, created_at, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET sheet_id=excluded.sheet_id, title=excluded.title, updated_at=excluded.updated_at""",
            (conv_id, user_id, sheet_id, title, now, now),
        )


async def save_conversation(conv_id: str, user_id: int, sheet_id: str | None, title: str | None) -> None:
    await asyncio.to_thread(_save_conversation_sync, conv_id, user_id, sheet_id, title)


def _append_message_sync(conv_id: str, role: str, content: str | None, payload: dict | None = None) -> int:
    now = time.time()
    payload_json = json.dumps(payload) if payload else None
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO messages (conversation_id, role, content, payload_json, created_at) VALUES (?,?,?,?,?)",
            (conv_id, role, content, payload_json, now),
        )
        c.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conv_id))
        return cur.lastrowid


async def append_message(conv_id: str, role: str, content: str | None, payload: dict | None = None) -> int:
    return await asyncio.to_thread(_append_message_sync, conv_id, role, content, payload)


def _list_conversations_sync(user_id: int, limit: int = 100) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT id, sheet_id, title, pinned, created_at, updated_at FROM conversations WHERE user_id = ? ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


async def list_conversations(user_id: int, limit: int = 100) -> list[dict]:
    return await asyncio.to_thread(_list_conversations_sync, user_id, limit)


def _get_conversation_messages_sync(conv_id: str, user_id: int) -> list[dict]:
    with _conn() as c:
        owner = c.execute("SELECT user_id FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        if not owner or owner["user_id"] != user_id:
            return []
        rows = c.execute(
            "SELECT id, role, content, payload_json, created_at, pinned FROM messages WHERE conversation_id = ? ORDER BY created_at",
            (conv_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("payload_json"):
                try:
                    d["payload"] = json.loads(d["payload_json"])
                except json.JSONDecodeError:
                    d["payload"] = None
            d.pop("payload_json", None)
            out.append(d)
        return out


async def get_conversation_messages(conv_id: str, user_id: int) -> list[dict]:
    return await asyncio.to_thread(_get_conversation_messages_sync, conv_id, user_id)


def _delete_conversation_sync(conv_id: str, user_id: int) -> bool:
    with _conn() as c:
        owner = c.execute("SELECT user_id FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        if not owner or owner["user_id"] != user_id:
            return False
        c.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
        return True


async def delete_conversation(conv_id: str, user_id: int) -> bool:
    return await asyncio.to_thread(_delete_conversation_sync, conv_id, user_id)


def _toggle_pin_message_sync(message_id: int, user_id: int) -> bool | None:
    with _conn() as c:
        row = c.execute(
            """SELECT m.pinned, c.user_id FROM messages m
               JOIN conversations c ON c.id = m.conversation_id
               WHERE m.id = ?""",
            (message_id,),
        ).fetchone()
        if not row or row["user_id"] != user_id:
            return None
        new_state = 0 if row["pinned"] else 1
        c.execute("UPDATE messages SET pinned = ? WHERE id = ?", (new_state, message_id))
        return bool(new_state)


async def toggle_pin_message(message_id: int, user_id: int) -> bool | None:
    return await asyncio.to_thread(_toggle_pin_message_sync, message_id, user_id)


# ─────────────── Favorites ───────────────

def _add_favorite_sync(user_id: int, sheet_id: str, sheet_name: str | None) -> None:
    now = time.time()
    with _conn() as c:
        c.execute(
            """INSERT INTO favorites (user_id, sheet_id, sheet_name, created_at) VALUES (?,?,?,?)
               ON CONFLICT(user_id, sheet_id) DO UPDATE SET sheet_name = excluded.sheet_name""",
            (user_id, sheet_id, sheet_name, now),
        )


async def add_favorite(user_id: int, sheet_id: str, sheet_name: str | None) -> None:
    await asyncio.to_thread(_add_favorite_sync, user_id, sheet_id, sheet_name)


def _remove_favorite_sync(user_id: int, sheet_id: str) -> None:
    with _conn() as c:
        c.execute("DELETE FROM favorites WHERE user_id = ? AND sheet_id = ?", (user_id, sheet_id))


async def remove_favorite(user_id: int, sheet_id: str) -> None:
    await asyncio.to_thread(_remove_favorite_sync, user_id, sheet_id)


def _list_favorites_sync(user_id: int) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT sheet_id, sheet_name, created_at FROM favorites WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]


async def list_favorites(user_id: int) -> list[dict]:
    return await asyncio.to_thread(_list_favorites_sync, user_id)


# ─────────────── Audit log ───────────────

def _log_audit_sync(user_id: int, sheet_id: str | None, tool_name: str,
                    arguments: dict | None, before: Any, after: Any, status: str) -> int:
    now = time.time()
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO audit_log (user_id, sheet_id, tool_name, arguments_json, before_json, after_json, status, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                user_id, sheet_id, tool_name,
                json.dumps(arguments) if arguments is not None else None,
                json.dumps(before, default=str) if before is not None else None,
                json.dumps(after, default=str) if after is not None else None,
                status, now,
            ),
        )
        return cur.lastrowid


async def log_audit(user_id: int, sheet_id: str | None, tool_name: str,
                    arguments: dict | None, before: Any, after: Any, status: str) -> int:
    return await asyncio.to_thread(_log_audit_sync, user_id, sheet_id, tool_name, arguments, before, after, status)


def _list_audit_sync(user_id: int, limit: int = 200, sheet_id: str | None = None) -> list[dict]:
    with _conn() as c:
        if sheet_id:
            rows = c.execute(
                "SELECT * FROM audit_log WHERE user_id = ? AND sheet_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, sheet_id, limit),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM audit_log WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            for k in ("arguments_json", "before_json", "after_json"):
                if d.get(k):
                    try:
                        d[k.replace("_json", "")] = json.loads(d[k])
                    except json.JSONDecodeError:
                        d[k.replace("_json", "")] = None
                d.pop(k, None)
            out.append(d)
        return out


async def list_audit(user_id: int, limit: int = 200, sheet_id: str | None = None) -> list[dict]:
    return await asyncio.to_thread(_list_audit_sync, user_id, limit, sheet_id)


# ─────────────── Webhook events ───────────────

def _record_webhook_event_sync(user_id: int | None, sheet_id: str | None,
                                webhook_id: int | None, event_type: str | None,
                                payload: dict) -> int:
    now = time.time()
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO webhook_events (user_id, sheet_id, webhook_id, event_type, payload_json, received_at) VALUES (?,?,?,?,?,?)",
            (user_id, sheet_id, webhook_id, event_type, json.dumps(payload, default=str), now),
        )
        return cur.lastrowid


async def record_webhook_event(user_id: int | None, sheet_id: str | None,
                                webhook_id: int | None, event_type: str | None,
                                payload: dict) -> int:
    return await asyncio.to_thread(_record_webhook_event_sync, user_id, sheet_id, webhook_id, event_type, payload)


def _list_webhook_events_sync(user_id: int, limit: int = 50, since: float = 0.0) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM webhook_events WHERE user_id = ? AND received_at > ? ORDER BY received_at DESC LIMIT ?",
            (user_id, since, limit),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            if d.get("payload_json"):
                try:
                    d["payload"] = json.loads(d["payload_json"])
                except json.JSONDecodeError:
                    d["payload"] = None
            d.pop("payload_json", None)
            out.append(d)
        return out


async def list_webhook_events(user_id: int, limit: int = 50, since: float = 0.0) -> list[dict]:
    return await asyncio.to_thread(_list_webhook_events_sync, user_id, limit, since)


# ─────────────── Bug reports ───────────────

_BUG_VALID_SEVERITY = {"low", "normal", "high", "blocker"}
_BUG_VALID_STATUS = {"open", "triaged", "fixed", "wontfix"}


def _create_bug_report_sync(
    user_id: int | None,
    session_id: str | None,
    sheet_id: str | None,
    reporter_email: str | None,
    reporter_name: str | None,
    description: str,
    steps: str | None,
    severity: str,
    context: dict | None,
) -> int:
    now = time.time()
    if severity not in _BUG_VALID_SEVERITY:
        severity = "normal"
    ctx_json = json.dumps(context, default=str) if context else None
    with _conn() as c:
        cur = c.execute(
            """INSERT INTO bug_reports
               (user_id, session_id, sheet_id, reporter_email, reporter_name,
                description, steps, severity, context_json, status,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?, 'open', ?, ?)""",
            (
                user_id, session_id, sheet_id, reporter_email, reporter_name,
                description, steps, severity, ctx_json, now, now,
            ),
        )
        return cur.lastrowid


async def create_bug_report(
    *,
    user_id: int | None,
    session_id: str | None,
    sheet_id: str | None,
    reporter_email: str | None,
    reporter_name: str | None,
    description: str,
    steps: str | None,
    severity: str = "normal",
    context: dict | None = None,
) -> int:
    return await asyncio.to_thread(
        _create_bug_report_sync, user_id, session_id, sheet_id,
        reporter_email, reporter_name, description, steps, severity, context,
    )


def _list_bug_reports_sync(
    status: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    with _conn() as c:
        if status and status in _BUG_VALID_STATUS:
            rows = c.execute(
                "SELECT * FROM bug_reports WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            ).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM bug_reports ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            raw_ctx = d.get("context_json")
            if raw_ctx:
                try:
                    d["context"] = json.loads(raw_ctx)
                except json.JSONDecodeError:
                    d["context"] = None
            else:
                d["context"] = None
            d.pop("context_json", None)
            out.append(d)
        return out


async def list_bug_reports(
    status: str | None = None, limit: int = 200, offset: int = 0,
) -> list[dict]:
    return await asyncio.to_thread(_list_bug_reports_sync, status, limit, offset)


def _count_bug_reports_sync(status: str | None = None) -> int:
    with _conn() as c:
        if status and status in _BUG_VALID_STATUS:
            return int(c.execute(
                "SELECT COUNT(*) AS n FROM bug_reports WHERE status = ?",
                (status,),
            ).fetchone()["n"])
        return int(c.execute(
            "SELECT COUNT(*) AS n FROM bug_reports"
        ).fetchone()["n"])


async def count_bug_reports(status: str | None = None) -> int:
    return await asyncio.to_thread(_count_bug_reports_sync, status)


def _update_bug_report_status_sync(report_id: int, new_status: str) -> bool:
    if new_status not in _BUG_VALID_STATUS:
        return False
    now = time.time()
    with _conn() as c:
        cur = c.execute(
            "UPDATE bug_reports SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, now, report_id),
        )
        return cur.rowcount > 0


async def update_bug_report_status(report_id: int, new_status: str) -> bool:
    return await asyncio.to_thread(_update_bug_report_status_sync, report_id, new_status)


# ─────────────── Export full account (RGPD) ───────────────

async def export_user_data(user_id: int) -> dict:
    """Returns a complete JSON dump of all user-owned data."""
    convs = await list_conversations(user_id, limit=10000)
    out_convs = []
    for c in convs:
        msgs = await get_conversation_messages(c["id"], user_id)
        out_convs.append({**c, "messages": msgs})
    favs = await list_favorites(user_id)
    audit = await list_audit(user_id, limit=10000)

    def _user_row():
        with _conn() as conn:
            r = conn.execute("SELECT id, smartsheet_user_id, email, first_name, last_name, created_at, last_login FROM users WHERE id = ?", (user_id,)).fetchone()
            return dict(r) if r else {}

    user = await asyncio.to_thread(_user_row)
    return {
        "exported_at": time.time(),
        "user": user,
        "conversations": out_convs,
        "favorites": favs,
        "audit_log": audit,
    }
