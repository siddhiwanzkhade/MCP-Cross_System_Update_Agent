"""Locking tests: two rollouts of the same package can never run at once."""

import threading
import time

import pytest
from conftest import versions

from shared import workspace
from shared.db import AgentError


def test_second_change_on_same_package_is_rejected(system):
    coordinator = system["coordinator"]
    first = coordinator.begin_change_impl("package:pricekit", "upgrade to 1.1")

    with pytest.raises(AgentError, match="locked by change"):
        coordinator.begin_change_impl("package:pricekit", "upgrade to 2.0")

    # After the first change finishes, the package is free again.
    coordinator.commit_change_impl(first["change_id"])
    second = coordinator.begin_change_impl("package:pricekit", "upgrade to 2.0")
    assert second["change_id"] != first["change_id"]


def test_different_packages_can_change_in_parallel(system):
    (workspace.libraries_dir() / "logkit" / "1.0").mkdir(parents=True)
    coordinator = system["coordinator"]
    a = coordinator.begin_change_impl("package:pricekit", "upgrade")
    b = coordinator.begin_change_impl("package:logkit", "upgrade")
    assert a["entity"] != b["entity"]


def test_concurrent_begin_only_one_wins(system):
    """Ten agents try to lock the same package at the same instant."""
    coordinator = system["coordinator"]
    barrier = threading.Barrier(10)
    results = []

    def attempt():
        barrier.wait()
        try:
            coordinator.begin_change_impl("package:pricekit", "race")
            results.append("won")
        except AgentError:
            results.append("blocked")

    threads = [threading.Thread(target=attempt) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert results.count("won") == 1
    assert results.count("blocked") == 9


def test_stale_change_cannot_write_after_lock_expires(system):
    """Fencing: a slow agent whose lock expired must not overwrite a newer change."""
    coordinator, services = system["coordinator"], system["services"]

    slow = coordinator.begin_change_impl("package:pricekit", "slow change", ttl_seconds=0.2)
    time.sleep(0.3)  # the slow agent's lock expires
    fresh = coordinator.begin_change_impl("package:pricekit", "fresh change")
    services.bump_dependency_impl(fresh["change_id"], "billing", "pricekit", "1.1")

    with pytest.raises(AgentError, match="no longer holds the lock"):
        services.bump_dependency_impl(slow["change_id"], "billing", "pricekit", "2.0")

    assert versions()["billing"] == "1.1"


def test_write_without_a_change_is_rejected(system):
    with pytest.raises(AgentError, match="Unknown change"):
        system["services"].bump_dependency_impl("chg-missing", "billing", "pricekit", "2.0")
    assert versions()["billing"] == "1.0"
