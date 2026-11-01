"""Services MCP server: the services that depend on shared libraries.

Tools: list_services, list_dependents, list_versions, bump_dependency, run_tests

Each service is its own git repository in the workspace (see shared/workspace.py).
A bump edits the service's requirements.txt and commits it. The coordinator
undoes a bump by resetting the repository to the commit saved before it.
"""

import os
import subprocess
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastmcp import FastMCP  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402

from shared import workspace  # noqa: E402
from shared.audit import mark_step_done, record_step, write_audit  # noqa: E402
from shared.db import AgentError, current_user, get_connection, init_db, transaction  # noqa: E402
from shared.locks import guard_write  # noqa: E402

mcp = FastMCP("services")

DEFAULT_TEST_TIMEOUT_SECONDS = 120
MAX_TEST_TIMEOUT_SECONDS = 300
MAX_OUTPUT_CHARS = 2000


# Business logic

def list_services_impl() -> list[dict]:
    return [
        {"service": name, "requirements": workspace.read_requirements(name),
         "commit": workspace.head_commit(name)[:10]}
        for name in workspace.service_names()
    ]


def list_dependents_impl(package: str) -> list[dict]:
    workspace.check_name(package, "package")
    dependents = []
    for name in workspace.service_names():
        version = workspace.read_requirements(name).get(package)
        if version is not None:
            dependents.append({"service": name, "version": version})
    return dependents


def list_versions_impl(package: str) -> dict:
    versions = workspace.library_versions(package)
    if not versions:
        raise AgentError(f"Package {package} not found.")
    return {"package": package, "versions": versions, "latest": versions[-1]}


def bump_dependency_impl(change_id: str, service: str, package: str, version: str) -> dict:
    entity = f"package:{package}"
    requirements = workspace.read_requirements(service)
    if package not in requirements:
        raise AgentError(f"Service {service} does not depend on {package}.")
    available = workspace.library_versions(package)
    if version not in available:
        raise AgentError(f"{package} {version} does not exist. Available: {', '.join(available)}.")
    old_version = requirements[package]
    if old_version == version:
        with closing(get_connection()) as conn:
            guard_write(conn, change_id, entity)
        return {"change_id": change_id, "service": service, "package": package,
                "version": version, "message": f"{service} already uses {package} {version}."}

    message = f"Bump {package} {old_version} -> {version} [{change_id}]"
    before = {"service": service, "package": package, "version": old_version,
              "commit": workspace.head_commit(service)}
    after = {"service": service, "package": package, "version": version, "commit_message": message}

    # 1. Record the intent first, so rollback knows about this step even if we crash next.
    with closing(get_connection()) as conn, transaction(conn):
        guard_write(conn, change_id, entity)
        step = record_step(conn, change_id, "services", "bump_dependency", entity, before, after,
                           status="pending")

    # 2. Act: edit requirements.txt and commit it in the service's repository.
    try:
        workspace.write_requirement(service, package, version)
        workspace.git(service, "commit", "-am", message)
    except Exception as exc:
        raise AgentError(f"Step {step} failed ({exc}). Roll back change {change_id}.") from exc

    # 3. Confirm: mark the step done and write the audit row.
    with closing(get_connection()) as conn, transaction(conn):
        mark_step_done(conn, change_id, step)
        write_audit(conn, current_user(), change_id, "services", "bump_dependency", entity, before, after)
    return {"change_id": change_id, "step": step, "service": service, "package": package,
            "old_version": old_version, "new_version": version,
            "commit": workspace.head_commit(service)[:10]}


def run_tests_impl(service: str, timeout_seconds: float = DEFAULT_TEST_TIMEOUT_SECONDS) -> dict:
    """Run a service's tests against the library versions in its requirements.txt."""
    if not 0 < timeout_seconds <= MAX_TEST_TIMEOUT_SECONDS:
        raise AgentError(f"timeout_seconds must be between 1 and {MAX_TEST_TIMEOUT_SECONDS}.")
    path = workspace.service_path(service)
    requirements = workspace.read_requirements(service)
    library_paths = []
    for package, version in requirements.items():
        library = workspace.library_path(package, version)
        if not library.is_dir():
            return {"service": service, "requirements": requirements, "passed": False,
                    "output": f"{package} {version} is not available."}
        library_paths.append(str(library))

    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([*library_paths, str(path)]),  # stands in for pip install
        "PYTHONDONTWRITEBYTECODE": "1",                               # keep the repository clean
    }
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
            cwd=path, env=env, capture_output=True, text=True, timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        return {"service": service, "requirements": requirements, "passed": False, "timed_out": True,
                "output": f"Tests did not finish within {timeout_seconds:g} seconds."}
    output = (result.stdout + result.stderr).strip()
    return {"service": service, "requirements": requirements, "passed": result.returncode == 0,
            "output": output[-MAX_OUTPUT_CHARS:]}


def _call(fn, *args):
    try:
        return fn(*args)
    except AgentError as exc:
        raise ToolError(str(exc)) from exc


# MCP tools

@mcp.tool(annotations={"readOnlyHint": True})
def list_services() -> list[dict]:
    """List every service, its dependencies (package -> version) and current commit."""
    return _call(list_services_impl)


@mcp.tool(annotations={"readOnlyHint": True})
def list_dependents(package: str) -> list[dict]:
    """List the services that depend on a package, and which version each uses."""
    return _call(list_dependents_impl, package)


@mcp.tool(annotations={"readOnlyHint": True})
def list_versions(package: str) -> dict:
    """List the available versions of a package, oldest first."""
    return _call(list_versions_impl, package)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
def bump_dependency(change_id: str, service: str, package: str, version: str) -> dict:
    """Change the version of a package that a service uses, and commit it.
    Requires an open change for 'package:<package>' from the Coordinator's begin_change.
    The previous commit is saved so rollback can restore it."""
    return _call(bump_dependency_impl, change_id, service, package, version)


@mcp.tool(annotations={"readOnlyHint": True})
def run_tests(service: str, timeout_seconds: float = DEFAULT_TEST_TIMEOUT_SECONDS) -> dict:
    """Run a service's test suite against the versions in its requirements.txt.
    Call the Coordinator's extend_lock first if the tests may take a long time."""
    return _call(run_tests_impl, service, timeout_seconds)


if __name__ == "__main__":
    with closing(get_connection()) as conn:
        init_db(conn)
    mcp.run()
