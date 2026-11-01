"""Services server tests: dependencies, bumps, test runs and safe undo."""

import pytest
from conftest import versions

from shared import workspace
from shared.db import AgentError


def test_list_dependents_and_versions(system):
    services = system["services"]
    assert services.list_dependents_impl("pricekit") == [
        {"service": "billing", "version": "1.0"},
        {"service": "checkout", "version": "1.0"},
        {"service": "reports", "version": "1.0"},
    ]
    assert services.list_versions_impl("pricekit") == {
        "package": "pricekit", "versions": ["1.0", "1.1", "2.0"], "latest": "2.0",
    }


def test_bump_edits_requirements_and_commits(system):
    coordinator, services = system["coordinator"], system["services"]
    change_id = coordinator.begin_change_impl("package:pricekit", "upgrade")["change_id"]

    result = services.bump_dependency_impl(change_id, "billing", "pricekit", "1.1")

    assert (result["old_version"], result["new_version"]) == ("1.0", "1.1")
    assert versions()["billing"] == "1.1"
    assert workspace.git("billing", "log", "-1", "--format=%s") == f"Bump pricekit 1.0 -> 1.1 [{change_id}]"
    assert workspace.git("billing", "status", "--porcelain") == ""
    assert [s["status"] for s in coordinator.get_change_impl(change_id)["steps"]] == ["done"]


def test_bump_rejects_unknown_version_and_wrong_package(system):
    coordinator, services = system["coordinator"], system["services"]
    change_id = coordinator.begin_change_impl("package:pricekit", "upgrade")["change_id"]

    with pytest.raises(AgentError, match="does not exist"):
        services.bump_dependency_impl(change_id, "billing", "pricekit", "9.9")
    with pytest.raises(AgentError, match="Service nowhere not found"):
        services.bump_dependency_impl(change_id, "nowhere", "pricekit", "1.1")
    with pytest.raises(AgentError, match="Invalid service name"):
        services.bump_dependency_impl(change_id, "../billing", "pricekit", "1.1")


def test_run_tests_catches_the_breaking_change(system):
    coordinator, services = system["coordinator"], system["services"]
    assert services.run_tests_impl("billing")["passed"] is True

    change_id = coordinator.begin_change_impl("package:pricekit", "upgrade")["change_id"]
    services.bump_dependency_impl(change_id, "billing", "pricekit", "2.0")
    result = services.run_tests_impl("billing")

    assert result["passed"] is False
    assert "missing 1 required positional argument: 'currency'" in result["output"]
    assert workspace.git("billing", "status", "--porcelain") == ""  # test run left no files behind


def test_run_tests_has_a_time_limit(system):
    slow_test = workspace.service_path("reports") / "tests" / "test_slow.py"
    slow_test.write_text("import time\n\ndef test_slow():\n    time.sleep(30)\n")

    result = system["services"].run_tests_impl("reports", 1)

    assert result["passed"] is False and result["timed_out"] is True


def test_undo_refuses_when_someone_else_committed_on_top(system):
    """Resetting would destroy the extra commit, so the change goes to review instead."""
    coordinator, services = system["coordinator"], system["services"]
    change_id = coordinator.begin_change_impl("package:pricekit", "upgrade")["change_id"]
    services.bump_dependency_impl(change_id, "billing", "pricekit", "2.0")

    (workspace.service_path("billing") / "NOTES.md").write_text("A teammate's work\n")
    workspace.git("billing", "add", "NOTES.md")
    workspace.git("billing", "commit", "-m", "Teammate's commit")

    result = coordinator.rollback_change_impl(change_id)

    assert result["status"] == "needs_review"
    assert "has new commits" in result["message"]
    assert (workspace.service_path("billing") / "NOTES.md").exists()
    coordinator.begin_change_impl("package:pricekit", "next rollout")  # lock was released
