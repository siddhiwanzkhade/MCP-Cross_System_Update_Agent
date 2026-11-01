"""Cloud entry point: one HTTP server with every tool from both MCP servers.

Endpoints:
    GET  /        HTML page: what the demo is and how to connect
    GET  /info    service information and tool list (JSON)
    GET  /health  health check for cloud platforms
    POST /mcp     MCP endpoint (Streamable HTTP) for Claude and other MCP clients

Run locally:   python app.py              (listens on $PORT, default 8080)
Run in Docker: see Dockerfile

Everyone who connects shares one demo workspace. It is created on first start,
a background job rolls back abandoned changes every 60 seconds, and the
reset_demo tool starts the demo over.
"""

import html
import importlib.util
import os
import shutil
import sys
import threading
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from fastmcp import FastMCP  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import HTMLResponse, JSONResponse  # noqa: E402

from shared.db import db_path, get_connection, init_db  # noqa: E402
from shared.workspace import workspace_path  # noqa: E402

SERVICE_NAME = "cross-system-update-agent"
VERSION = "1.0"
CLEANUP_INTERVAL_SECONDS = 60


def _load(relative_path: str, name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup_demo = _load("scripts/setup_demo.py", "setup_demo")
coordinator = _load("servers/coordinator/server.py", "coordinator_server")
services = _load("servers/services/server.py", "services_server")


# Demo workspace

def prepare_demo() -> None:
    """Create the demo workspace and database if they don't exist yet."""
    if not workspace_path().exists():
        setup_demo.create_demo(workspace_path())
    with closing(get_connection()) as conn:
        init_db(conn)


def reset_demo_impl() -> dict:
    """Start the demo over, unless a change is still in progress."""
    with closing(get_connection()) as conn:
        active = conn.execute(
            "SELECT c.id FROM changes c JOIN locks l ON l.change_id = c.id "
            "WHERE c.status = 'open' AND l.expires_at > ?",
            (time.time(),),
        ).fetchone()
    if active is not None:
        raise ToolError(f"Change {active['id']} is still in progress. Commit or roll it back first.")
    shutil.rmtree(workspace_path(), ignore_errors=True)
    for suffix in ("", "-wal", "-shm"):
        Path(f"{db_path()}{suffix}").unlink(missing_ok=True)
    prepare_demo()
    return {"status": "reset", "message": "All services are back on pricekit 1.0 with a fresh change log."}


def _cleanup_loop() -> None:
    while True:
        time.sleep(CLEANUP_INTERVAL_SECONDS)
        try:
            coordinator.cleanup_expired_changes_impl()
        except Exception as exc:  # keep running; try again on the next check
            print(f"Cleanup failed: {exc}", flush=True)


# One MCP server with every tool

mcp = FastMCP(SERVICE_NAME)
mcp.mount(coordinator.mcp)
mcp.mount(services.mcp)


@mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True})
def reset_demo() -> dict:
    """Start the shared demo over: every service back on pricekit 1.0 and an
    empty change log. Refused while a change is still in progress."""
    return reset_demo_impl()


# Plain HTTP endpoints

def _base_url(request: Request) -> str:
    return str(request.base_url).rstrip("/")


async def _tool_list() -> list[dict]:
    tools = await mcp.get_tools()
    return [{"name": name, "description": (tool.description or "").strip().splitlines()[0]}
            for name, tool in sorted(tools.items())]


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy", "service": SERVICE_NAME})


@mcp.custom_route("/info", methods=["GET"])
async def info(request: Request) -> JSONResponse:
    return JSONResponse({
        "service": SERVICE_NAME,
        "version": VERSION,
        "description": "Rolls out a library version to every service that uses it, "
                       "with locking, automatic rollback and an audit trail.",
        "mcp_endpoint": f"{_base_url(request)}/mcp",
        "transport": "streamable-http",
        "endpoints": {"/": "HTML documentation", "/info": "this JSON",
                      "/health": "health check", "/mcp": "MCP endpoint"},
        "tools": await _tool_list(),
    })


@mcp.custom_route("/", methods=["GET"])
async def home(request: Request) -> HTMLResponse:
    url = html.escape(f"{_base_url(request)}/mcp")
    rows = "".join(
        f"<tr><td><code>{html.escape(t['name'])}</code></td><td>{html.escape(t['description'])}</td></tr>"
        for t in await _tool_list()
    )
    return HTMLResponse(f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><title>Cross-System Update Agent</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 860px; margin: 40px auto; padding: 0 16px; line-height: 1.5; }}
code, pre {{ background: #f4f4f4; padding: 2px 5px; border-radius: 4px; }}
pre {{ padding: 12px; overflow-x: auto; }}
table {{ border-collapse: collapse; width: 100%; }}
td, th {{ border: 1px solid #ddd; padding: 6px 8px; text-align: left; vertical-align: top; }}
</style></head><body>
<h1>Cross-System Update Agent</h1>
<p>An MCP server that lets an AI agent roll out a new library version to every service that uses it,
as one unit. If any service's tests fail, every service goes back to the old version.</p>

<h2>Connect</h2>
<p>MCP endpoint (Streamable HTTP): <code>{url}</code></p>
<p>In Claude, add a custom connector with this URL. In a client that uses a JSON config:</p>
<pre>{{
  "mcpServers": {{
    "cross-system-update-agent": {{ "url": "{url}", "transport": "http" }}
  }}
}}</pre>

<h2>Try it</h2>
<p>The demo has one library, <code>pricekit</code> (versions 1.0, 1.1 and 2.0), and three services
(<code>checkout</code>, <code>billing</code>, <code>reports</code>) on 1.0. Version 2.0 breaks billing.
Ask your agent:</p>
<pre>Upgrade pricekit to 2.0 in every service, run their tests, and roll back if anything fails.</pre>
<p>The demo is shared by everyone who connects. Call <code>reset_demo</code> to start over.</p>

<h2>Tools</h2>
<table><tr><th>Tool</th><th>Description</th></tr>{rows}</table>

<h2>Other endpoints</h2>
<p><a href="/info">/info</a> (JSON) and <a href="/health">/health</a></p>
</body></html>""")


prepare_demo()
app = mcp.http_app(stateless_http=True)  # ASGI app, e.g. for: uvicorn app:app


if __name__ == "__main__":
    import uvicorn

    threading.Thread(target=_cleanup_loop, daemon=True).start()
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
