"""Coordinator tests: entity types, lock extension, and plugging in new kinds of change."""

import time
from contextlib import closing

import pytest

from shared.audit import log_write
from shared.db import AgentError, get_connection, transaction
from shared.locks import guard_write


def test_unknown_entity_kind_is_rejected(system):
    with pytest.raises(AgentError, match="'package:pricekit'"):
        system["coordinator"].begin_change_impl("planet:mars", "test")


def test_bad_package_name_is_rejected(system):
    with pytest.raises(AgentError, match="Entity must look like"):
        system["coordinator"].begin_change_impl("package:../etc", "test")


def test_missing_package_is_rejected(system):
    with pytest.raises(AgentError, match="package:nosuchlib not found"):
        system["coordinator"].begin_change_impl("package:nosuchlib", "test")


def test_extend_lock_keeps_the_change_alive(system):
    coordinator, services = system["coordinator"], system["services"]
    change_id = coordinator.begin_change_impl("package:pricekit", "slow tests", ttl_seconds=0.3)["change_id"]

    coordinator.extend_lock_impl(change_id, 60)
    time.sleep(0.4)  # past the original expiry

    services.bump_dependency_impl(change_id, "reports", "pricekit", "1.1")  # still allowed
    assert coordinator.cleanup_expired_changes_impl() == []


def test_lost_lock_cannot_be_extended(system):
    coordinator = system["coordinator"]
    change_id = coordinator.begin_change_impl("package:pricekit", "slow", ttl_seconds=0.2)["change_id"]
    time.sleep(0.3)
    coordinator.begin_change_impl("package:pricekit", "someone else")

    with pytest.raises(AgentError, match="no longer holds the lock"):
        coordinator.extend_lock_impl(change_id, 60)


def test_extend_lock_has_a_maximum(system):
    coordinator = system["coordinator"]
    change_id = coordinator.begin_change_impl("package:pricekit", "test")["change_id"]
    with pytest.raises(AgentError, match="between 1 and 3600"):
        coordinator.extend_lock_impl(change_id, 10_000)


def test_a_new_entity_type_works_end_to_end(system):
    """Register a made-up 'config' type and its undo handler; nothing else changes."""
    coordinator = system["coordinator"]
    settings = {"timeout": "30"}  # stands in for an outside system

    coordinator.ENTITY_TYPES["config"] = coordinator.EntityType(
        example="config:timeout",
        valid_id=lambda ident: ident.isidentifier(),
        exists=lambda conn, ident: ident in settings,
    )

    @coordinator.undo_handler("settings", "set_value")
    def _undo_set(conn, before, after):
        settings[before["key"]] = before["value"]

    def set_value(change_id, value):
        with closing(get_connection()) as conn, transaction(conn):
            guard_write(conn, change_id, "config:timeout")
            before = {"key": "timeout", "value": settings["timeout"]}
            settings["timeout"] = value
            log_write(conn, "test-user", change_id, "settings", "set_value",
                      "config:timeout", before, {"key": "timeout", "value": value})

    change_id = coordinator.begin_change_impl("config:timeout", "raise timeout")["change_id"]
    with pytest.raises(AgentError, match="locked by change"):
        coordinator.begin_change_impl("config:timeout", "second change")
    set_value(change_id, "60")
    assert settings["timeout"] == "60"

    result = coordinator.rollback_change_impl(change_id)

    assert result == {"change_id": change_id, "status": "rolled_back", "steps_undone": 1}
    assert settings["timeout"] == "30"
    actions = [row["action"] for row in coordinator.get_audit_trail_impl("config:timeout")]
    assert actions == ["rollback_change", "undo_set_value", "set_value", "begin_change"]
