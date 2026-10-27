"""SQLite storage with versioned migrations.

SQLite in WAL mode on a persistent disk comfortably serves an early-stage
product. Every query that touches user data filters by ``user_id``; the
helpers here never cross users.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Any, Iterable

from app import config

_local = threading.local()
_init_lock = threading.Lock()
_initialized_path: str | None = None


def now() -> int:
    return int(time.time())


def new_id() -> str:
    return uuid.uuid4().hex


MIGRATIONS: list[str] = [
    # 1 — core
    """
    CREATE TABLE users (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        name TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        lang TEXT NOT NULL DEFAULT 'ar',
        is_admin INTEGER NOT NULL DEFAULT 0,
        token_version INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    );
    CREATE TABLE subscriptions (
        user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        plan TEXT NOT NULL DEFAULT 'free',
        status TEXT NOT NULL DEFAULT 'active',
        current_period_end INTEGER,
        updated_at INTEGER NOT NULL
    );
    CREATE TABLE usage (
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        period TEXT NOT NULL,
        metric TEXT NOT NULL,
        amount INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (user_id, period, metric)
    );
    CREATE TABLE invoices (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        provider_id TEXT UNIQUE,
        plan TEXT NOT NULL,
        amount_halalas INTEGER NOT NULL,
        status TEXT NOT NULL,
        url TEXT,
        created_at INTEGER NOT NULL,
        paid_at INTEGER
    );
    CREATE TABLE conversations (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        persona TEXT NOT NULL,
        title TEXT NOT NULL DEFAULT '',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX ix_conv_user ON conversations(user_id, updated_at DESC);
    CREATE TABLE messages (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        created_at INTEGER NOT NULL
    );
    CREATE INDEX ix_msg_conv ON messages(conversation_id, created_at);
    CREATE TABLE projects (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        goal TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE TABLE tasks (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        details TEXT NOT NULL DEFAULT '',
        assignee TEXT NOT NULL DEFAULT 'najd',
        status TEXT NOT NULL DEFAULT 'new',
        priority TEXT NOT NULL DEFAULT 'medium',
        due_date TEXT,
        output TEXT NOT NULL DEFAULT '',
        reminded_at INTEGER,
        position INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX ix_task_project ON tasks(project_id, position);
    CREATE TABLE activity (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        summary TEXT NOT NULL,
        ref TEXT,
        created_at INTEGER NOT NULL
    );
    CREATE INDEX ix_activity_user ON activity(user_id, created_at DESC);
    CREATE TABLE notifications (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        title TEXT NOT NULL,
        body TEXT NOT NULL DEFAULT '',
        link TEXT,
        read_at INTEGER,
        created_at INTEGER NOT NULL
    );
    CREATE TABLE approvals (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        persona TEXT NOT NULL,
        action TEXT NOT NULL,
        title TEXT NOT NULL,
        payload TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        result TEXT,
        created_at INTEGER NOT NULL,
        decided_at INTEGER
    );
    CREATE INDEX ix_approvals_user ON approvals(user_id, status);
    """,
    # 2 — channels, posts, media
    """
    CREATE TABLE channels (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        platform TEXT NOT NULL,
        external_id TEXT NOT NULL,
        name TEXT NOT NULL,
        secret TEXT NOT NULL DEFAULT '',
        meta TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'connected',
        last_error TEXT,
        created_at INTEGER NOT NULL,
        UNIQUE (user_id, platform, external_id)
    );
    CREATE TABLE media (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        kind TEXT NOT NULL,
        filename TEXT NOT NULL,
        content_type TEXT NOT NULL,
        size INTEGER NOT NULL,
        public_token TEXT NOT NULL UNIQUE,
        created_at INTEGER NOT NULL
    );
    CREATE TABLE posts (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        caption TEXT NOT NULL,
        media_ids TEXT NOT NULL DEFAULT '[]',
        channel_ids TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'draft',
        scheduled_at INTEGER,
        published_at INTEGER,
        results TEXT NOT NULL DEFAULT '{}',
        insights TEXT NOT NULL DEFAULT '{}',
        insights_at INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE INDEX ix_posts_due ON posts(status, scheduled_at);
    CREATE TABLE contacts (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL DEFAULT '',
        phone TEXT,
        email TEXT,
        tags TEXT NOT NULL DEFAULT '',
        opted_in INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    );
    CREATE INDEX ix_contacts_user ON contacts(user_id);
    """,
    # 3 — sites, automations, oauth state
    """
    CREATE TABLE sites (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        slug TEXT NOT NULL UNIQUE,
        title TEXT NOT NULL,
        html TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft',
        version INTEGER NOT NULL DEFAULT 1,
        published_html TEXT,
        published_at INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE TABLE automations (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        trigger TEXT NOT NULL,
        steps TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'draft',
        webhook_token TEXT NOT NULL UNIQUE,
        next_run_at INTEGER,
        last_run_at INTEGER,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
    );
    CREATE TABLE automation_runs (
        id TEXT PRIMARY KEY,
        automation_id TEXT NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
        user_id TEXT NOT NULL,
        trigger_kind TEXT NOT NULL,
        status TEXT NOT NULL,
        log TEXT NOT NULL DEFAULT '[]',
        started_at INTEGER NOT NULL,
        finished_at INTEGER
    );
    CREATE INDEX ix_runs_auto ON automation_runs(automation_id, started_at DESC);
    CREATE TABLE oauth_states (
        state TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        provider TEXT NOT NULL,
        extra TEXT NOT NULL DEFAULT '{}',
        created_at INTEGER NOT NULL
    );
    CREATE TABLE login_attempts (
        key TEXT NOT NULL,
        at INTEGER NOT NULL
    );
    CREATE INDEX ix_login_attempts ON login_attempts(key, at);
    """,
    # 4 — processed webhook events (idempotency)
    """
    CREATE TABLE webhook_events (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        created_at INTEGER NOT NULL
    );
    """,
    # 5 — business profile shared by all personas, leads from site forms
    """
    CREATE TABLE business_profiles (
        user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        business_name TEXT NOT NULL DEFAULT '',
        industry TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        audience TEXT NOT NULL DEFAULT '',
        products TEXT NOT NULL DEFAULT '',
        tone TEXT NOT NULL DEFAULT '',
        city TEXT NOT NULL DEFAULT '',
        contact TEXT NOT NULL DEFAULT '',
        brand_color TEXT NOT NULL DEFAULT '#6d3df5',
        faq TEXT NOT NULL DEFAULT '',
        updated_at INTEGER NOT NULL
    );
    ALTER TABLE contacts ADD COLUMN source TEXT NOT NULL DEFAULT 'manual';
    ALTER TABLE contacts ADD COLUMN note TEXT NOT NULL DEFAULT '';
    """,
]


def _connect() -> sqlite3.Connection:
    path = config.settings.db_path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def conn() -> sqlite3.Connection:
    path = str(config.settings.db_path)
    c = getattr(_local, "conn", None)
    if c is None or getattr(_local, "path", None) != path:
        init()
        c = _connect()
        _local.conn = c
        _local.path = path
    return c


def init() -> None:
    """Create the database and apply pending migrations (idempotent)."""
    global _initialized_path
    path = str(config.settings.db_path)
    if _initialized_path == path:
        return
    with _init_lock:
        if _initialized_path == path:
            return
        c = _connect()
        try:
            c.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
            row = c.execute("SELECT version FROM schema_version").fetchone()
            current = row[0] if row else 0
            if row is None:
                c.execute("INSERT INTO schema_version (version) VALUES (0)")
            for i, sql in enumerate(MIGRATIONS[current:], start=current + 1):
                c.execute("BEGIN")
                try:
                    for stmt in [s.strip() for s in sql.split(";") if s.strip()]:
                        c.execute(stmt)
                    c.execute("UPDATE schema_version SET version = ?", (i,))
                    c.execute("COMMIT")
                except Exception:
                    c.execute("ROLLBACK")
                    raise
        finally:
            c.close()
        _initialized_path = path


def close_thread() -> None:
    """Close this thread's connection (call at the end of short-lived threads)."""
    c = getattr(_local, "conn", None)
    if c is not None:
        c.close()
    _local.conn = None
    _local.path = None


def reset_for_tests() -> None:
    global _initialized_path
    c = getattr(_local, "conn", None)
    if c is not None:
        c.close()
    _local.conn = None
    _local.path = None
    _initialized_path = None


# ---- query helpers -------------------------------------------------------

def one(sql: str, params: Iterable[Any] = ()) -> dict | None:
    row = conn().execute(sql, tuple(params)).fetchone()
    return dict(row) if row else None


def all_(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in conn().execute(sql, tuple(params)).fetchall()]


def run(sql: str, params: Iterable[Any] = ()) -> int:
    cur = conn().execute(sql, tuple(params))
    return cur.rowcount


def insert(table: str, values: dict) -> dict:
    cols = ", ".join(values)
    qs = ", ".join("?" for _ in values)
    conn().execute(f"INSERT INTO {table} ({cols}) VALUES ({qs})", tuple(values.values()))
    return values


def update(table: str, where: dict, values: dict) -> int:
    sets = ", ".join(f"{k} = ?" for k in values)
    conds = " AND ".join(f"{k} = ?" for k in where)
    cur = conn().execute(
        f"UPDATE {table} SET {sets} WHERE {conds}", tuple(values.values()) + tuple(where.values())
    )
    return cur.rowcount


@contextmanager
def transaction():
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        raise


def jload(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def log_activity(user_id: str, kind: str, summary: str, ref: str | None = None) -> None:
    insert("activity", {"id": new_id(), "user_id": user_id, "kind": kind, "summary": summary[:500], "ref": ref, "created_at": now()})


def notify(user_id: str, title: str, body: str = "", link: str | None = None) -> None:
    insert("notifications", {"id": new_id(), "user_id": user_id, "title": title[:200], "body": body[:1000], "link": link, "read_at": None, "created_at": now()})
