"""Shared test setup: a fresh database and demo workspace for every test."""

import importlib.util
import sys
from contextlib import closing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from shared import workspace  # noqa: E402
from shared.db import get_connection, init_db  # noqa: E402


def _load(relative_path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def system(tmp_path, monkeypatch):
    """Point every server at a temporary database and workspace, and return the servers."""
    monkeypatch.setenv("AGENT_DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setenv("AGENT_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("AGENT_USER", "test-user")

    _load("scripts/setup_demo.py", "setup_demo").create_demo(tmp_path / "workspace")
    with closing(get_connection()) as conn:
        init_db(conn)

    return {
        "coordinator": _load("servers/coordinator/server.py", "coordinator_server"),
        "services": _load("servers/services/server.py", "services_server"),
    }


@pytest.fixture
def db():
    """Open a connection to the test database (use after the system fixture)."""
    conn = get_connection()
    yield conn
    conn.close()


def versions(package="pricekit") -> dict[str, str]:
    """{service: version of package} for every service."""
    return {name: workspace.read_requirements(name)[package] for name in workspace.service_names()}


def heads() -> dict[str, str]:
    """{service: current git commit} for every service."""
    return {name: workspace.head_commit(name) for name in workspace.service_names()}
