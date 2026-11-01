"""Audit log and change steps.

- The audit log answers "who changed what, when, from what to what".
- Change steps store the "before" value of every write, which is exactly
  what rollback needs to undo it.
"""

import json
import sqlite3

from shared.db import now_iso


def _dump(value):
    return None if value is None else json.dumps(value, sort_keys=True)


def _load(text):
    return None if text is None else json.loads(text)


def write_audit(conn, user, change_id, system, action, entity, before, after) -> None:
    conn.execute(
        "INSERT INTO audit_log "
        "(ts, user, change_id, system, action, entity, before_value, after_value) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (now_iso(), user, change_id, system, action, entity, _dump(before), _dump(after)),
    )


def record_step(conn, change_id, system, action, entity, before, after, status="done") -> int:
    """Save one step of a change with its before value. Returns the step number.

    For writes outside the database (files, git), record the step as 'pending'
    BEFORE acting and call mark_step_done afterwards. If the agent crashes in
    between, rollback still knows about the step."""
    row = conn.execute(
        "SELECT COALESCE(MAX(step_no), 0) + 1 AS n FROM change_steps WHERE change_id = ?",
        (change_id,),
    ).fetchone()
    step_no = row["n"]
    conn.execute(
        "INSERT INTO change_steps "
        "(change_id, step_no, system, action, entity, before_value, after_value, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (change_id, step_no, system, action, entity, _dump(before), _dump(after), status, now_iso()),
    )
    return step_no


def mark_step_done(conn, change_id, step_no) -> None:
    conn.execute(
        "UPDATE change_steps SET status = 'done' WHERE change_id = ? AND step_no = ?",
        (change_id, step_no),
    )


def log_write(conn, user, change_id, system, action, entity, before, after) -> int:
    """Record a write as both a change step (for rollback) and an audit row.
    Use this for writes inside the same database transaction (all-or-nothing)."""
    step_no = record_step(conn, change_id, system, action, entity, before, after)
    write_audit(conn, user, change_id, system, action, entity, before, after)
    return step_no


def get_audit_trail(conn: sqlite3.Connection, entity: str, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        "SELECT ts, user, change_id, system, action, entity, before_value, after_value "
        "FROM audit_log WHERE entity = ? ORDER BY id DESC LIMIT ?",
        (entity, limit),
    ).fetchall()
    return [
        {
            "time": r["ts"],
            "user": r["user"],
            "change_id": r["change_id"],
            "system": r["system"],
            "action": r["action"],
            "before": _load(r["before_value"]),
            "after": _load(r["after_value"]),
        }
        for r in rows
    ]