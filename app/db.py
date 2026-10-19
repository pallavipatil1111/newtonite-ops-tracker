"""SQLite storage. Every write goes through a short transaction; WAL mode lets readers continue while one writer works."""
import sqlite3
from contextlib import contextmanager

from flask import current_app

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE
);
-- A user can belong to several teams with a different role in each.
CREATE TABLE IF NOT EXISTS memberships (
    user_id INTEGER NOT NULL REFERENCES users(id),
    team_id INTEGER NOT NULL REFERENCES teams(id),
    role TEXT NOT NULL CHECK (role IN ('viewer', 'member', 'lead')),
    PRIMARY KEY (user_id, team_id)
);
CREATE TABLE IF NOT EXISTS work_items (
    id INTEGER PRIMARY KEY,
    team_id INTEGER NOT NULL REFERENCES teams(id),
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL CHECK (kind IN ('customer_issue', 'engineering', 'payment', 'incident', 'compliance', 'task')),
    priority TEXT NOT NULL CHECK (priority IN ('low', 'medium', 'high', 'urgent')),
    status TEXT NOT NULL CHECK (status IN ('open', 'in_progress', 'awaiting_approval', 'approved', 'resolved', 'closed')),
    requires_approval INTEGER NOT NULL DEFAULT 0,
    assignee_id INTEGER REFERENCES users(id),
    created_by INTEGER NOT NULL REFERENCES users(id),
    version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS ix_items_team_status ON work_items (team_id, status, updated_at);
CREATE INDEX IF NOT EXISTS ix_items_assignee ON work_items (assignee_id, status);
CREATE INDEX IF NOT EXISTS ix_items_priority ON work_items (priority, status);

-- Append-only history: nothing that happens to an item is silently lost.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES work_items(id),
    actor_id INTEGER NOT NULL REFERENCES users(id),
    type TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS ix_events_item ON events (item_id, id);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL REFERENCES work_items(id),
    author_id INTEGER NOT NULL REFERENCES users(id),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS ix_comments_item ON comments (item_id, id);

-- Idempotency: a retried request with the same key returns the first result instead of acting twice.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    user_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (user_id, key)
);

-- Transactional outbox: written in the same transaction as the change, delivered later by a worker.
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL UNIQUE REFERENCES events(id),
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'done', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    available_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS ix_outbox_pending ON outbox (status, available_at);

-- Notifications produced by the worker. UNIQUE makes re-delivery harmless.
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    event_id INTEGER NOT NULL REFERENCES events(id),
    message TEXT NOT NULL,
    read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (user_id, event_id)
);
"""


def connect(path):
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)  # autocommit; we open transactions explicitly
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def get_conn():
    return connect(current_app.config["DATABASE"])


@contextmanager
def write_tx(conn):
    """BEGIN IMMEDIATE takes the write lock up front, so check-then-update sequences can't interleave."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def init_db(path):
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.close()
