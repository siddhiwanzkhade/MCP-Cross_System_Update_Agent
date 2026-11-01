"""Cloud app tests: HTTP endpoints, one server with every tool, and reset_demo."""

import pytest
from conftest import _load, versions
from fastmcp.exceptions import ToolError
from starlette.testclient import TestClient


@pytest.fixture
def cloud(system):
    return _load("app.py", "cloud_app")


def test_health_info_and_home_page(cloud):
    with TestClient(cloud.app) as client:
        assert client.get("/health").json() == {"status": "healthy", "service": "cross-system-update-agent"}

        info = client.get("/info").json()
        assert info["mcp_endpoint"] == "http://testserver/mcp"
        names = {tool["name"] for tool in info["tools"]}
        assert {"begin_change", "rollback_change", "bump_dependency", "run_tests", "reset_demo"} <= names

        page = client.get("/")
        assert page.status_code == 200 and "http://testserver/mcp" in page.text


def test_reset_demo_starts_over(cloud, system):
    coordinator, services = system["coordinator"], system["services"]
    change_id = coordinator.begin_change_impl("package:pricekit", "upgrade")["change_id"]
    services.bump_dependency_impl(change_id, "billing", "pricekit", "1.1")

    with pytest.raises(ToolError, match="still in progress"):
        cloud.reset_demo_impl()

    coordinator.commit_change_impl(change_id)
    assert cloud.reset_demo_impl()["status"] == "reset"
    assert versions() == {"billing": "1.0", "checkout": "1.0", "reports": "1.0"}
    assert coordinator.get_audit_trail_impl("package:pricekit") == []
