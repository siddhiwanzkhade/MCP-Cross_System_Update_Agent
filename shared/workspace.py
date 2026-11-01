"""The workspace: a library registry and the services that depend on it.

Layout (created by scripts/setup_demo.py):

    <workspace>/libraries/<package>/<version>/<package>/__init__.py
    <workspace>/services/<service>/        each one is its own git repository
        requirements.txt                    for example "pricekit==1.0"
        app.py
        tests/

"Installing" a version is done by putting that version's folder on PYTHONPATH
when a service's tests run, so everything works offline.
"""

import os
import re
import subprocess
from pathlib import Path

from shared.db import PROJECT_ROOT, AgentError

DEFAULT_WORKSPACE = PROJECT_ROOT / "data" / "workspace"
NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
GIT_IDENTITY = ["-c", "user.name=rollout-agent", "-c", "user.email=rollout-agent@localhost"]


def workspace_path() -> Path:
    """The workspace folder. Override with the AGENT_WORKSPACE environment variable."""
    return Path(os.environ.get("AGENT_WORKSPACE", DEFAULT_WORKSPACE))


def libraries_dir() -> Path:
    return workspace_path() / "libraries"


def services_dir() -> Path:
    return workspace_path() / "services"


def check_name(name: str, what: str) -> None:
    """Names become folder names, so only allow safe characters (no '/' or '..')."""
    if not NAME_PATTERN.match(name) or name in (".", ".."):
        raise AgentError(f"Invalid {what} name: {name!r}.")


# Libraries

def _version_key(version: str):
    return tuple(int(part) if part.isdigit() else 0 for part in version.split("."))


def library_versions(package: str) -> list[str]:
    """Every available version of a library, oldest first."""
    check_name(package, "package")
    folder = libraries_dir() / package
    if not folder.is_dir():
        return []
    return sorted((p.name for p in folder.iterdir() if p.is_dir()), key=_version_key)


def library_path(package: str, version: str) -> Path:
    check_name(package, "package")
    check_name(version, "version")
    return libraries_dir() / package / version


# Services

def service_names() -> list[str]:
    if not services_dir().is_dir():
        return []
    return sorted(p.name for p in services_dir().iterdir() if (p / "requirements.txt").is_file())


def service_path(service: str) -> Path:
    check_name(service, "service")
    path = services_dir() / service
    if not (path / "requirements.txt").is_file():
        raise AgentError(f"Service {service} not found.")
    return path


def read_requirements(service: str) -> dict[str, str]:
    """{package: version} from lines like 'pricekit==1.0'."""
    requirements = {}
    for line in (service_path(service) / "requirements.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        name, sep, version = line.partition("==")
        if sep:
            requirements[name.strip()] = version.strip()
    return requirements


def write_requirement(service: str, package: str, version: str) -> None:
    path = service_path(service) / "requirements.txt"
    lines = [
        f"{package}=={version}" if line.partition("==")[0].strip() == package else line
        for line in path.read_text().splitlines()
    ]
    path.write_text("\n".join(lines) + "\n")


# Git

def git(service: str, *args: str) -> str:
    result = subprocess.run(
        ["git", *GIT_IDENTITY, "-C", str(service_path(service)), *args],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AgentError(f"git {args[0]} failed in {service}: {result.stderr.strip()}")
    return result.stdout.strip()


def head_commit(service: str) -> str:
    return git(service, "rev-parse", "HEAD")
