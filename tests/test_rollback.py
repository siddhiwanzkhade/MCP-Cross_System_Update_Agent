"""Rollback tests: a failed rollout must leave every service as it was."""

import pytest
from conftest import heads, versions

from shared import workspace
from shared.db import AgentError

SERVICES = ["billing", "checkout", "reports"]


def _bump_all(services, change_id, version):
    for name in SERVICES:
        services.bump_dependency_impl(change_id, name, "pricekit", version)


def test_breaking_upgrade_rolls_back_everything(system, db):
    coordinator, services = system["coordinator"], system["services"]
    original_heads = heads()

    change_id = coordinator.begin_change_impl("package:pricekit", "upgrade to 2.0")["change_id"]
    _bump_all(services, change_id, "2.0")
    results = {name: services.run_tests_impl(name)["passed"] for name in SERVICES}
    assert results == {"billing": False, "checkout": True, "reports": True}

    result = coordinator.rollback_change_impl(change_id)

    assert result == {"change_id": change_id, "status": "rolled_back", "steps_undone": 3}
    assert versions() == {name: "1.0" for name in SERVICES}
    assert heads() == original_heads
    assert all(services.run_tests_impl(name)["passed"] for name in SERVICES)
    assert db.execute("SELECT COUNT(*) AS n FROM locks").fetchone()["n"] == 0
    actions = [row["action"] for row in coordinator.get_audit_trail_impl("package:pricekit")]
    assert actions.count("undo_bump_dependency") == 3
    assert "rollback_change" in actions


def test_safe_upgrade_commits(system):
    coordinator, services = system["coordinator"], system["services"]
    change_id = coordinator.begin_change_impl("package:pricekit", "upgrade to 1.1")["change_id"]
    _bump_all(services, change_id, "1.1")

    assert all(services.run_tests_impl(name)["passed"] for name in SERVICES)
    committed = coordinator.commit_change_impl(change_id)

    assert committed == {"change_id": change_id, "status": "committed", "steps": 3}
    assert versions() == {name: "1.1" for name in SERVICES}
    users = {row["user"] for row in coordinator.get_audit_trail_impl("package:pricekit")}
    assert users == {"test-user"}


def test_rollback_twice_is_safe(system):
    coordinator = system["coordinator"]
    change = coordinator.begin_change_impl("package:pricekit", "test")
    coordinator.rollback_change_impl(change["change_id"])
    again = coordinator.rollback_change_impl(change["change_id"])
    assert again["steps_undone"] == 0


def test_cannot_roll_back_a_committed_change(system):
    coordinator = system["coordinator"]
    change = coordinator.begin_change_impl("package:pricekit", "test")
    coordinator.commit_change_impl(change["change_id"])
    with pytest.raises(AgentError, match="already committed"):
        coordinator.rollback_change_impl(change["change_id"])


@pytest.mark.parametrize("crash_at", ["edit", "commit"])
def test_a_crash_mid_bump_is_still_rolled_back(system, monkeypatch, crash_at):
    """Intent-first: the step is recorded before acting, so a crash can't hide it."""
    coordinator, services = system["coordinator"], system["services"]
    original_heads = heads()
    change_id = coordinator.begin_change_impl("package:pricekit", "test")["change_id"]
    real_git = workspace.git

    def crashing_git(service, *args):
        if args[0] == "commit":
            if crash_at == "commit":
                real_git(service, *args)  # the commit lands, then the agent dies
            raise RuntimeError("simulated crash")
        return real_git(service, *args)

    monkeypatch.setattr(workspace, "git", crashing_git)
    with pytest.raises(AgentError, match="Roll back"):
        services.bump_dependency_impl(change_id, "billing", "pricekit", "2.0")
    monkeypatch.setattr(workspace, "git", real_git)

    steps = coordinator.get_change_impl(change_id)["steps"]
    assert [s["status"] for s in steps] == ["pending"]
    with pytest.raises(AgentError, match="did not finish"):
        coordinator.commit_change_impl(change_id)

    result = coordinator.rollback_change_impl(change_id)

    assert result["status"] == "rolled_back"
    assert versions()["billing"] == "1.0"
    assert heads() == original_heads
    assert workspace.git("billing", "status", "--porcelain") == ""  # no half-written file left
