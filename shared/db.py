"""Database connection, schema and small helpers shared by every server.

The database holds the coordinator's bookkeeping: changes, their steps, locks
and the audit log. The systems being changed (the services) live outside it,
as git repositories in the workspace (see shared/workspace.py).
"""

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "agent.db"


class AgentError(Exception):
    """A business-rule error that should be shown to the AI agent as-is."""


class UndoConflict(AgentError):
    """An undo was refused because the system changed since the step was made.
    Undoing anyway could destroy someone else's work, so a person must review."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (
    id            TEXT PRIMARY KEY,
    entity        TEXT NOT NULL,
    reason        TEXT NOT NULL,
    requested_by  TEXT NOT NULL,
    status        TEXT NOT NULL
                  CHECK (status IN ('open', 'committed', 'rolled_back', 'needs_review')),
    fencing_token INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    closed_at     TEXT
);

CREATE TABLE IF NOT EXISTS change_steps (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    change_id    TEXT NOT NULL REFERENCES changes(id),
    step_no      INTEGER NOT NULL,
    system       TEXT NOT NULL,
    action       TEXT NOT NULL,
    entity       TEXT NOT NULL,
    before_value TEXT NOT NULL,
    after_value  TEXT NOT NULL,
    -- 'pending': recorded before acting, the write may or may not have happened.
    -- 'done': the write finished. Rollback undoes both.
    status       TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('pending', 'done')),
    undone       INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    UNIQUE (change_id, step_no)
);

CREATE TABLE IF NOT EXISTS locks (
    entity        TEXT PRIMARY KEY,
    change_id     TEXT NOT NULL,
    owner         TEXT NOT NULL,
    fencing_token INTEGER NOT NULL,
    expires_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS counters (
    name  TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    user         TEXT NOT NULL,
    change_id    TEXT,
    system       TEXT NOT NULL,
    action       TEXT NOT NULL,
    entity       TEXT NOT NULL,
    before_value TEXT,
    after_value  TEXT
);
"""


def db_path() -> Path:
    """The database file. Override with the AGENT_DB_PATH environment variable."""
    return Path(os.environ.get("AGENT_DB_PATH", DEFAULT_DB_PATH))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_user() -> str:
    """Who is making the change. Stand-in for a verified identity until Keycloak is added."""
    return os.environ.get("AGENT_USER", "unknown-user")


def get_connection() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: we control transactions ourselves with BEGIN IMMEDIATE.
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # lets readers and one writer work at the same time
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute("INSERT OR IGNORE INTO counters (name, value) VALUES ('fencing_token', 0)")


@contextmanager
def transaction(conn: sqlite3.Connection):
    """All-or-nothing block. BEGIN IMMEDIATE takes the write lock up front,
    so two servers can never interleave writes to the same rows."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")