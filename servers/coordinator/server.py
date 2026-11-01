"""Coordinator MCP server: locks, change log, rollback and audit.

Tools: begin_change, extend_lock, commit_change, rollback_change, get_change,
       get_audit_trail, cleanup_expired_changes

Every rollout follows the same pattern:
    begin_change  ->  write tools on other servers (each with the change ID)
                  ->  commit_change   (or rollback_change if anything fails)

The coordinator is generic. Each kind of entity (today "package") is registered
in ENTITY_TYPES, and each kind of write registers how to undo it with
@undo_handler. Locks, fencing, audit and cleanup work the same for all.
"""

import json
import sqlite3
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastmcp import FastMCP  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

from shared import audit, workspace  # noqa: E402
from shared.db import AgentError, UndoConflict, current_user, get_connection, init_db, now_iso, transaction  # noqa: E402
from shared.locks import DEFAULT_TTL_SECONDS, acquire_lock, guard_write, release_lock  # noqa: E402
from shared.locks import extend_lock as _extend_lock  # noqa: E402  (the tool below is also named extend_lock)

mcp = FastMCP("coordinator")


# Entity types: what the coordinator can lock and change

@dataclass(frozen=True)
class EntityType:
    """How the coordinator handles one kind of entity, for example 'package'."""
    example: str                                         # shown in error messages
    valid_id: Callable[[str], bool]                      # is this ID well-formed?
    exists: Callable[[sqlite3.Connection, str], bool]    # does it exist right now?


ENTITY_TYPES: dict[str, EntityType] = {
    "package": EntityType(
        example="package:pricekit",
        valid_id=lambda ident: bool(workspace.NAME_PATTERN.match(ident)),
        exists=lambda conn, ident: bool(workspace.library_versions(ident)),
    ),
}


# Undo handlers: how to reverse each kind of write
# A handler gets the step's before and after values. It must be safe to run
# more than once, and safe to run for a 'pending' step whose write never happened.

UndoHandler = Callable[[sqlite3.Connection, dict, dict], None]
UNDO_HANDLERS: dict[tuple[str, str], UndoHandler] = {}


def undo_handler(system: str, *actions: str):
    """Register a function that reverses the given write action(s) of a system."""
    def register(fn):
        for action in actions:
            UNDO_HANDLERS[(system, action)] = fn
        return fn
    return register


@undo_handler("services", "bump_dependency")
def _undo_bump(conn, before: dict, after: dict) -> None:
    """Put the service's repository back on the commit it had before the bump."""
    service = before["service"]
    if workspace.head_commit(service) == before["commit"]:
        # The commit never happened (or was already undone). Also throw away a
        # half-written requirements.txt, in case the agent crashed mid-edit.
        workspace.git(service, "checkout", before["commit"], "--", "requirements.txt")
        return
    parent = workspace.git(service, "rev-parse", "HEAD^")
    subject = workspace.git(service, "log", "-1", "--format=%s")
    if parent != before["commit"] or subject != after["commit_message"]:
        raise UndoConflict(
            f"Service {service} has new commits since this change bumped it. "
            "Resetting it could destroy that work."
        )
    workspace.git(service, "reset", "--hard", before["commit"])


#  Business logic

def _parse_entity(entity) -> tuple[EntityType, str, str]:
    """Turn 'package:pricekit' into (type, 'pricekit', 'package:pricekit')."""
    text = str(entity).strip()
    kind, _, ident = text.partition(":")
    entity_type = ENTITY_TYPES.get(kind)
    if entity_type is None or not entity_type.valid_id(ident):
        examples = ", ".join(f"'{t.example}'" for t in ENTITY_TYPES.values())
        raise AgentError(f"Entity must look like {examples}.")
    return entity_type, ident, text


def _normalize_entity(entity) -> str:
    return _parse_entity(entity)[2]


def _get_change(conn, change_id: str) -> sqlite3.Row:
    change = conn.execute("SELECT * FROM changes WHERE id = ?", (change_id,)).fetchone()
    if change is None:
        raise AgentError(f"Unknown change {change_id}.")
    return change


def begin_change_impl(entity, reason: str, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> dict:
    entity_type, ident, entity = _parse_entity(entity)
    user = current_user()
    change_id = "chg-" + uuid.uuid4().hex[:8]
    with closing(get_connection()) as conn, transaction(conn):
        if not entity_type.exists(conn, ident):
            raise AgentError(f"{entity} not found.")
        token = acquire_lock(conn, entity, change_id, user, ttl_seconds)
        conn.execute(
            "INSERT INTO changes (id, entity, reason, requested_by, status, fencing_token, created_at) "
            "VALUES (?, ?, ?, ?, 'open', ?, ?)",
            (change_id, entity, reason, user, token, now_iso()),
        )
        audit.write_audit(conn, user, change_id, "coordinator", "begin_change", entity, None, {"reason": reason})
    return {
        "change_id": change_id,
        "entity": entity,
        "requested_by": user,
        "fencing_token": token,
        "lock_expires_in_seconds": ttl_seconds,
        "next": "Call write tools with this change_id, then commit_change or rollback_change.",
    }


def extend_lock_impl(change_id: str, seconds: float = DEFAULT_TTL_SECONDS) -> dict:
    with closing(get_connection()) as conn, transaction(conn):
        change = _get_change(conn, change_id)
        _extend_lock(conn, change_id, change["entity"], seconds)
    return {"change_id": change_id, "entity": change["entity"], "lock_expires_in_seconds": seconds}


def commit_change_impl(change_id: str) -> dict:
    user = current_user()
    with closing(get_connection()) as conn, transaction(conn):
        change = _get_change(conn, change_id)
        guard_write(conn, change_id, change["entity"])  # must still hold a valid lock
        pending = conn.execute(
            "SELECT step_no FROM change_steps WHERE change_id = ? AND status = 'pending' ORDER BY step_no",
            (change_id,),
        ).fetchone()
        if pending is not None:
            raise AgentError(
                f"Step {pending['step_no']} of change {change_id} did not finish. "
                "Roll the change back instead of committing."
            )
        steps = conn.execute(
            "SELECT COUNT(*) AS n FROM change_steps WHERE change_id = ?", (change_id,)
        ).fetchone()["n"]
        conn.execute(
            "UPDATE changes SET status = 'committed', closed_at = ? WHERE id = ?",
            (now_iso(), change_id),
        )
        release_lock(conn, change["entity"], change_id)
        audit.write_audit(conn, user, change_id, "coordinator", "commit_change", change["entity"],
                          None, {"steps": steps})
    return {"change_id": change_id, "status": "committed", "steps": steps}


def _mark_needs_review(conn, user, change_id, entity, details: dict, message: str, **extra) -> dict:
    conn.execute("UPDATE changes SET status = 'needs_review' WHERE id = ?", (change_id,))
    release_lock(conn, entity, change_id)  # only removes the lock if this change still owns it
    audit.write_audit(conn, user, change_id, "coordinator", "needs_review", entity, None, details)
    return {"change_id": change_id, "status": "needs_review", **extra,
            "message": message + " A person must review before undoing."}


def rollback_change_impl(change_id: str) -> dict:
    user = current_user()
    with closing(get_connection()) as conn, transaction(conn):
        change = _get_change(conn, change_id)
        if change["status"] == "committed":
            raise AgentError(f"Change {change_id} is already committed and cannot be rolled back.")
        if change["status"] == "rolled_back":
            return {"change_id": change_id, "status": "rolled_back", "steps_undone": 0,
                    "message": "Already rolled back."}

        entity = change["entity"]
        # A later change (bigger fencing token) that was not itself rolled back may have
        # written to this entity, even if it has already committed and released its lock.
        # Undoing now could overwrite its work.
        newer = conn.execute(
            "SELECT id FROM changes WHERE entity = ? AND fencing_token > ? "
            "AND status != 'rolled_back' ORDER BY fencing_token LIMIT 1",
            (entity, change["fencing_token"]),
        ).fetchone()
        if newer is not None:
            return _mark_needs_review(conn, user, change_id, entity, {"blocked_by": newer["id"]},
                                      f"{entity} was changed later by change {newer['id']}.")

        steps = conn.execute(
            "SELECT * FROM change_steps WHERE change_id = ? AND undone = 0 ORDER BY step_no DESC",
            (change_id,),
        ).fetchall()
        undone = 0
        for step in steps:  # reverse order: last write is undone first
            handler = UNDO_HANDLERS.get((step["system"], step["action"]))
            if handler is None:
                raise AgentError(f"No undo handler for {step['system']}.{step['action']}.")
            before, after = json.loads(step["before_value"]), json.loads(step["after_value"])
            try:
                handler(conn, before, after)
            except UndoConflict as exc:
                # Keep the steps already undone, stop here, and ask a person.
                return _mark_needs_review(conn, user, change_id, entity,
                                          {"step": step["step_no"], "reason": str(exc)},
                                          f"Could not undo step {step['step_no']}: {exc}",
                                          steps_undone=undone)
            conn.execute("UPDATE change_steps SET undone = 1 WHERE id = ?", (step["id"],))
            audit.write_audit(conn, user, change_id, step["system"], "undo_" + step["action"], entity,
                              after, before)
            undone += 1

        conn.execute(
            "UPDATE changes SET status = 'rolled_back', closed_at = ? WHERE id = ?",
            (now_iso(), change_id),
        )
        release_lock(conn, entity, change_id)
        audit.write_audit(conn, user, change_id, "coordinator", "rollback_change", entity, None,
                          {"steps_undone": undone})
    return {"change_id": change_id, "status": "rolled_back", "steps_undone": undone}


def get_change_impl(change_id: str) -> dict:
    with closing(get_connection()) as conn:
        change = _get_change(conn, change_id)
        steps = conn.execute(
            "SELECT step_no, system, action, status, undone, created_at FROM change_steps "
            "WHERE change_id = ? ORDER BY step_no",
            (change_id,),
        ).fetchall()
    return {**dict(change), "steps": [dict(s) for s in steps]}


def get_audit_trail_impl(entity, limit: int = 50) -> list[dict]:
    entity = _normalize_entity(entity)
    with closing(get_connection()) as conn:
        return audit.get_audit_trail(conn, entity, limit)


def cleanup_expired_changes_impl() -> list[dict]:
    """Find open changes whose lock has expired (or is gone) and roll each one back.
    If a later change was started on the same entity, rollback marks it 'needs_review' instead."""
    with closing(get_connection()) as conn:
        rows = conn.execute(
            "SELECT c.id FROM changes c "
            "LEFT JOIN locks l ON l.entity = c.entity AND l.change_id = c.id "
            "WHERE c.status = 'open' AND (l.entity IS NULL OR l.expires_at <= ?) "
            "ORDER BY c.created_at",
            (time.time(),),
        ).fetchall()
    results = []
    for row in rows:
        try:
            results.append(rollback_change_impl(row["id"]))
        except AgentError as exc:
            results.append({"change_id": row["id"], "status": "error", "message": str(exc)})
    return results


def _call(fn, *args):
    try:
        return fn(*args)
    except AgentError as exc:
        raise ToolError(str(exc)) from exc


#  MCP tools

@mcp.tool(annotations={"readOnlyHint": False})
def begin_change(entity: str, reason: str) -> dict:
    """Start a change: lock one entity and get a change ID. Call this FIRST,
    before any write tool. entity looks like 'package:pricekit'."""
    return _call(begin_change_impl, entity, reason)


@mcp.tool(annotations={"readOnlyHint": False})
def extend_lock(change_id: str, seconds: float = DEFAULT_TTL_SECONDS) -> dict:
    """Keep holding the lock for another `seconds` (max 3600). Call this before
    long steps, such as test runs, so the lock does not expire mid-change."""
    return _call(extend_lock_impl, change_id, seconds)


@mcp.tool(annotations={"readOnlyHint": False})
def commit_change(change_id: str) -> dict:
    """Finish a change after every step succeeded and every test passed.
    Releases the entity's lock."""
    return _call(commit_change_impl, change_id)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
def rollback_change(change_id: str) -> dict:
    """Undo every step of a change in reverse order, then release the lock.
    Call this if any step or any test fails."""
    return _call(rollback_change_impl, change_id)


@mcp.tool(annotations={"readOnlyHint": True})
def get_change(change_id: str) -> dict:
    """Show a change's status and its steps."""
    return _call(get_change_impl, change_id)


@mcp.tool(annotations={"readOnlyHint": True})
def get_audit_trail(entity: str, limit: int = 50) -> list[dict]:
    """Show who changed what and when for an entity, newest first. entity looks like 'package:pricekit'."""
    return _call(get_audit_trail_impl, entity, limit)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
def cleanup_expired_changes() -> list[dict]:
    """Clean up: roll back every open change whose lock has expired.
    Changes whose entity was changed later are marked 'needs_review' instead."""
    return _call(cleanup_expired_changes_impl)


if __name__ == "__main__":
    with closing(get_connection()) as conn:
        init_db(conn)
    mcp.run()
