"""Entity-level locks with expiry and fencing tokens.

- A lock covers one entity only (for example "package:pricekit"), so changes to
  other entities can run in parallel.
- Every lock expires, so a crashed agent cannot hold it forever.
- Every lock gets a new, ever-increasing fencing token. A change whose token
  no longer matches the lock (because its lock expired and someone else took
  over) is refused, so a slow agent can never write late.
"""

import sqlite3
import time

from shared.db import AgentError

DEFAULT_TTL_SECONDS = 300  # 5 minutes


def _next_fencing_token(conn: sqlite3.Connection) -> int:
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'fencing_token'")
    row = conn.execute("SELECT value FROM counters WHERE name = 'fencing_token'").fetchone()
    return row["value"]


def acquire_lock(
    conn: sqlite3.Connection,
    entity: str,
    change_id: str,
    owner: str,
    ttl_seconds: float = DEFAULT_TTL_SECONDS,
) -> int:
    """Take the lock on an entity and return its fencing token.
    Call this inside a transaction."""
    now = time.time()
    row = conn.execute("SELECT * FROM locks WHERE entity = ?", (entity,)).fetchone()
    if row is not None:
        if row["expires_at"] > now and row["change_id"] != change_id:
            raise AgentError(
                f"{entity} is locked by change {row['change_id']} "
                f"(requested by {row['owner']}). Try again after it finishes."
            )
        # Expired lock (or our own): clear it before taking a fresh one.
        conn.execute("DELETE FROM locks WHERE entity = ?", (entity,))

    token = _next_fencing_token(conn)
    conn.execute(
        "INSERT INTO locks (entity, change_id, owner, fencing_token, expires_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (entity, change_id, owner, token, now + ttl_seconds),
    )
    return token


def guard_write(conn: sqlite3.Connection, change_id: str, entity: str) -> sqlite3.Row:
    """Every write tool calls this first, inside its transaction.
    It refuses the write unless the change is open, is for this entity,
    and still holds a valid lock with a matching fencing token."""
    change = conn.execute("SELECT * FROM changes WHERE id = ?", (change_id,)).fetchone()
    if change is None:
        raise AgentError(f"Unknown change {change_id}. Call begin_change first.")
    if change["status"] != "open":
        raise AgentError(f"Change {change_id} is {change['status']}, not open.")
    if change["entity"] != entity:
        raise AgentError(f"Change {change_id} is for {change['entity']}, not {entity}.")

    lock = conn.execute("SELECT * FROM locks WHERE entity = ?", (entity,)).fetchone()
    if lock is None or lock["expires_at"] <= time.time():
        if lock is None or lock["change_id"] == change_id:
            raise AgentError(
                f"The lock on {entity} has expired. "
                f"Roll back change {change_id} and start a new change."
            )
    if lock["change_id"] != change_id or lock["fencing_token"] != change["fencing_token"]:
        raise AgentError(
            f"Change {change_id} no longer holds the lock on {entity} "
            "(stale fencing token). Roll it back and start a new change."
        )
    return change


MAX_TTL_SECONDS = 3600  # 1 hour: an extension can never hold a lock longer than this


def extend_lock(conn: sqlite3.Connection, change_id: str, entity: str,
                ttl_seconds: float = DEFAULT_TTL_SECONDS) -> float:
    """Push the lock's expiry forward ("heartbeat"), for example during a long test run.
    guard_write runs first, so a change that already lost its lock cannot extend it."""
    if not 0 < ttl_seconds <= MAX_TTL_SECONDS:
        raise AgentError(f"Lock time must be between 1 and {MAX_TTL_SECONDS} seconds.")
    guard_write(conn, change_id, entity)
    expires_at = time.time() + ttl_seconds
    conn.execute(
        "UPDATE locks SET expires_at = ? WHERE entity = ? AND change_id = ?",
        (expires_at, entity, change_id),
    )
    return expires_at


def release_lock(conn: sqlite3.Connection, entity: str, change_id: str) -> None:
    """Release the lock, but only if this change still owns it."""
    conn.execute("DELETE FROM locks WHERE entity = ? AND change_id = ?", (entity, change_id))