"""Cleanup tests: half-finished rollouts with expired locks get cleaned up."""

import time

from conftest import heads, versions


def test_expired_change_is_rolled_back_automatically(system):
    coordinator, services = system["coordinator"], system["services"]
    original_heads = heads()

    # An agent starts a rollout, bumps two services, then "crashes" (never commits).
    change_id = coordinator.begin_change_impl("package:pricekit", "crashy agent", ttl_seconds=0.2)["change_id"]
    services.bump_dependency_impl(change_id, "billing", "pricekit", "2.0")
    services.bump_dependency_impl(change_id, "checkout", "pricekit", "2.0")
    time.sleep(0.3)  # lock expires

    results = coordinator.cleanup_expired_changes_impl()

    assert [r["status"] for r in results] == ["rolled_back"]
    assert versions() == {"billing": "1.0", "checkout": "1.0", "reports": "1.0"}
    assert heads() == original_heads


def test_taken_over_change_is_marked_needs_review(system):
    coordinator, services = system["coordinator"], system["services"]

    old = coordinator.begin_change_impl("package:pricekit", "slow agent", ttl_seconds=0.2)["change_id"]
    services.bump_dependency_impl(old, "billing", "pricekit", "2.0")
    time.sleep(0.3)
    new = coordinator.begin_change_impl("package:pricekit", "new agent")["change_id"]
    services.bump_dependency_impl(new, "checkout", "pricekit", "1.1")

    results = coordinator.cleanup_expired_changes_impl()

    assert len(results) == 1 and results[0]["change_id"] == old
    assert results[0]["status"] == "needs_review"
    assert versions()["checkout"] == "1.1"  # the newer change's work is untouched


def test_newer_committed_change_is_not_overwritten(system):
    """The newer change has already committed and released its lock before cleanup runs."""
    coordinator, services = system["coordinator"], system["services"]

    old = coordinator.begin_change_impl("package:pricekit", "slow agent", ttl_seconds=0.2)["change_id"]
    services.bump_dependency_impl(old, "billing", "pricekit", "2.0")
    time.sleep(0.3)
    new = coordinator.begin_change_impl("package:pricekit", "new agent")["change_id"]
    services.bump_dependency_impl(new, "billing", "pricekit", "1.1")
    coordinator.commit_change_impl(new)

    results = coordinator.cleanup_expired_changes_impl()

    assert [r["status"] for r in results] == ["needs_review"]
    assert versions()["billing"] == "1.1"


def test_active_changes_are_left_alone(system):
    coordinator = system["coordinator"]
    change_id = coordinator.begin_change_impl("package:pricekit", "still working")["change_id"]

    assert coordinator.cleanup_expired_changes_impl() == []
    assert coordinator.get_change_impl(change_id)["status"] == "open"
