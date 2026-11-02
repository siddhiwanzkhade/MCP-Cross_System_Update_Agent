# MCP-Cross_System_Update_Agent
A set of MCP servers that let an AI agent roll out a new library version to every service that depends on it, as one unit, with library-level locking, automatic rollback and an audit trail.

Designed to work with LLMs via the MCP (Model Context Protocol). Runs locally over stdio or in the cloud over HTTP. Tested with Claude Code and Claude.ai.

**Live demo:** https://mcp-cross-system-update-agent.onrender.com

> **Note:** `pricekit` is a fake library made for this demo. It stands in for a real package like pydantic, numpy or pandas, so the demo runs offline and fails the same way every time. The system itself works with real libraries too. See [Use a Real Library](#use-a-real-library).

## Try It

The server is live. Connect your own AI app to this URL, no install needed:

```
https://mcp-cross-system-update-agent.onrender.com/mcp
```

> **First request may be slow.** The server runs on free hosting and sleeps when idle. Open the [live demo](https://mcp-cross-system-update-agent.onrender.com) in your browser first to wake it up (up to a minute).

### Claude.ai or Claude Desktop

1. Go to **Settings → Connectors → Add custom connector**
2. Name: `Rollout Agent`, URL: the MCP URL above. Leave the OAuth settings empty.
3. In a new chat, turn on **Rollout Agent** from the tools menu under the message box.

### Claude Code

```bash
claude mcp add --transport http rollout https://mcp-cross-system-update-agent.onrender.com/mcp
```

### Cursor

Add to `~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "rollout": { "url": "https://mcp-cross-system-update-agent.onrender.com/mcp" }
  }
}
```

### VS Code (GitHub Copilot)

Add to `.vscode/mcp.json`:

```json
{
  "servers": {
    "rollout": { "type": "http", "url": "https://mcp-cross-system-update-agent.onrender.com/mcp" }
  }
}
```

### ChatGPT (Plus and above)

Turn on **Developer mode** in the Connectors settings, then create a connector with the MCP URL above and **No authentication**. Menu names change often in ChatGPT.

Any other app that supports remote MCP servers (Streamable HTTP) works the same way.

### Things to ask

| Ask | What happens |
|-----|--------------|
| "Upgrade pricekit to 2.0 in every service, run the tests, and roll back if anything fails." | billing fails, so all three services roll back to 1.0 |
| "Upgrade pricekit to 1.1 in every service, run the tests, and roll back if anything fails." | all tests pass, so the change is committed |
| "Show me the audit trail for pricekit." | every step, who made it and when |
| "Reset the demo." | back to a clean start, all services on 1.0 |

- Your AI app may ask permission before each tool that changes something. Set the connector's tools to **Always allow** for a smooth run.
- Everyone shares one demo. If someone else is mid-rollout, you'll see "locked by another change". That's the lock doing its job.
- The demo resets whenever the server restarts.

## How It Works

1. **Request**: The user asks in plain English, for example "Upgrade pricekit to 2.0 in every service, run their tests, and roll back if anything fails." (In a real team, this would be a library like pydantic 1.10 → 2.0.)
2. **Discovery**: The host connects to the MCP server and lists its tools. The agent asks which services depend on the library.
3. **Lock**: The Coordinator locks the library (`package:pricekit`) and returns a change ID and a fencing token.
4. **Apply**: The agent bumps the library version in each service, with the change ID. Each step is recorded *before* it happens, then the service's `requirements.txt` is edited and committed in its git repository.
5. **Test**: The agent runs each service's tests against the new version, with a time limit.
6. **Commit or rollback**: If every test passes, the change is committed. If any test fails, every service is reset to the commit it had before, newest step first.
7. **Audit**: Every step is logged with the user, the change ID and the before and after values.

If an agent crashes halfway, a cleanup job rolls back its change once the lock expires. If rolling back could destroy newer work (a later change on the same library, or someone else's commit on top), the change is marked `needs_review` for a person instead.

## Demo

`scripts/setup_demo.py` creates `pricekit`, a fake shared library, and three services, each its own git repository, all on `pricekit==1.0`:

| Version | What changed | checkout | billing | reports |
|---------|--------------|:--------:|:-------:|:-------:|
| 1.0 | Today's version | ✅ | ✅ | ✅ |
| 1.1 | Adds `parse_price` (safe) | ✅ | ✅ | ✅ |
| 2.0 | `format_price` now requires a currency (breaking, like pydantic 2.0 moving `BaseSettings` out) | ✅ | ❌ | ✅ |

Upgrading to 1.1 commits. Upgrading to 2.0 fails billing's tests, so all three services roll back to 1.0.

## MCP Servers

| Server | System | Purpose |
|--------|--------|---------|
| Coordinator | SQLite | Locks, change log, rollback, audit, cleanup |
| Services | Local git repositories | Dependency versions, bumps and test runs |

The two servers never call each other. They share one SQLite database for bookkeeping, and the `change_id` ties the steps of one rollout together.

Locally they run as two separate stdio servers. In the cloud, `app.py` serves both from one HTTP endpoint, plus a `reset_demo` tool.

## Available Tools

| Tool | Server | Description | Parameters |
|------|--------|-------------|------------|
| `begin_change` | Coordinator | Lock an entity and start a change | `entity`, `reason` |
| `extend_lock` | Coordinator | Keep holding the lock during long steps (max 1 hour) | `change_id`, `seconds` |
| `commit_change` | Coordinator | Complete a change and release the lock (refused if a step is unfinished) | `change_id` |
| `rollback_change` | Coordinator | Undo every step in reverse order | `change_id` |
| `get_change` | Coordinator | Show a change's status and steps | `change_id` |
| `get_audit_trail` | Coordinator | Show the history of an entity | `entity`, `limit` |
| `cleanup_expired_changes` | Coordinator | Roll back open changes whose lock expired | |
| `list_services` | Services | List services, their dependencies and current commit | |
| `list_dependents` | Services | List the services that use a package, and which version | `package` |
| `list_versions` | Services | List a package's available versions | `package` |
| `bump_dependency` | Services | Change a service's package version and commit it | `change_id`, `service`, `package`, `version` |
| `run_tests` | Services | Run a service's tests against its current versions | `service`, `timeout_seconds` |
| `reset_demo` | Cloud only | Start the demo over (refused while a change is in progress) | |

Every write tool requires a `change_id`, checks the lock and fencing token, records the before state first, and writes an audit row.

## Endpoints (Cloud)

| Endpoint | Description |
|----------|-------------|
| `/` | Home page: how to connect, list of tools |
| `/info` | Server info and tool list as JSON |
| `/health` | Health check |
| `/mcp` | MCP endpoint (Streamable HTTP) |

## LLM MCP Configuration

**Cloud** (any MCP app): use the URL `https://mcp-cross-system-update-agent.onrender.com/mcp`, as shown in [Try It](#try-it).

**Local**: `.mcp.json` (used by Claude Code):

```json
{
  "mcpServers": {
    "coordinator": {
      "command": ".venv/bin/python",
      "args": ["servers/coordinator/server.py"],
      "env": { "AGENT_USER": "siddhi" }
    },
    "services": {
      "command": ".venv/bin/python",
      "args": ["servers/services/server.py"],
      "env": { "AGENT_USER": "siddhi" }
    }
  }
}
```

## Local Development

Requires Python 3.10+ and git.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# Create the demo library, services and database (run again for a clean start)
.venv/bin/python scripts/setup_demo.py

# Run the tests (temporary database and workspace, so your demo data is untouched)
.venv/bin/pytest

# Optional: roll back abandoned changes every 60 seconds
.venv/bin/python scripts/cleanup_expired_changes.py
```

Then open the project in Claude Code, which starts both servers from `.mcp.json`, and ask it to upgrade pricekit.

To run the cloud version on your own machine instead:

```bash
.venv/bin/python app.py    # then open http://localhost:8080
```

## Use a Real Library

The system doesn't care where a library comes from: each version is just a folder on `PYTHONPATH` during test runs. So you can install real versions from PyPI with `pip install --target` (local setup only):

```bash
# 1. Install two real versions into the workspace
.venv/bin/pip install pydantic==1.10.21 --target data/workspace/libraries/pydantic/1.10.21
.venv/bin/pip install pydantic==2.10.6  --target data/workspace/libraries/pydantic/2.10.6

# 2. Create a service that uses it (any git repo with requirements.txt and tests/ works)
mkdir -p data/workspace/services/payment/tests && cd data/workspace/services/payment
echo "pydantic==1.10.21" > requirements.txt
printf '[pytest]\n' > pytest.ini
printf 'from pydantic import BaseSettings\n\nclass Settings(BaseSettings):\n    currency: str = "USD"\n' > app.py
printf 'from app import Settings\n\ndef test_settings():\n    assert Settings().currency == "USD"\n' > tests/test_app.py
git init -q -b main && git add -A && git commit -qm "Initial version"
cd -
```

Then ask your agent: *"Upgrade pydantic to 2.10.6 in every service and roll back if anything fails."* `payment` fails, because `BaseSettings` moved to the separate `pydantic-settings` package in 2.0, so the change is rolled back to 1.10.21.

Any library on PyPI works the same way: install each version into `data/workspace/libraries/<package>/<version>`, and pin it in each service's `requirements.txt` as `package==version`. pip may warn about dependency conflicts with `.venv`; you can ignore that, because `--target` doesn't change `.venv`.

## Cloud Deployment

The project includes a `Dockerfile`, so it runs on any platform that hosts Docker containers.

### Docker

```bash
docker build -t rollout-agent .
docker run -p 8080:8080 rollout-agent
```

### Render (used for the live demo)

1. **New → Web Service**, connect this GitHub repository
2. Language: **Docker**, Instance type: **Free**, Health check path: `/health`
3. **Deploy Web Service**. Every push to `main` redeploys automatically.

### Google Cloud Run

```bash
gcloud run deploy rollout-agent --source . --region us-central1 \
  --allow-unauthenticated --max-instances 1
```

Keep it to **one instance**: the demo stores its locks in a SQLite file inside the container, so every request must reach the same copy.

## Project Structure

```
MCP-Cross-System-Update-Agent/
├── servers/
│   ├── coordinator/server.py     # Locks, change log, rollback, audit, cleanup
│   └── services/server.py        # Dependency bumps and test runs
├── shared/
│   ├── db.py                     # Database connection and schema
│   ├── locks.py                  # Expiring locks with fencing tokens
│   ├── audit.py                  # Change steps (pending/done) and audit log
│   └── workspace.py              # Library versions, service repositories, git
├── scripts/
│   ├── setup_demo.py             # Creates the demo workspace and database
│   └── cleanup_expired_changes.py  # Background cleanup job
├── tests/                        # 30 tests: rollback, races, crashes, cleanup, cloud app
├── docs/ARCHITECTURE.md          # Diagrams of how everything connects
├── data/                         # Created by setup_demo.py (not in git)
├── app.py                        # Cloud: both servers over HTTP, plus /health and /info
├── Dockerfile                    # Container image for cloud hosting
├── .mcp.json                     # Local: starts both servers for Claude Code
├── .env.example
├── pytest.ini
└── requirements.txt
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_USER` | `unknown-user` | Who is making changes (placeholder for a verified identity). `demo-user` in the Dockerfile |
| `AGENT_DB_PATH` | `data/agent.db` | SQLite database for changes, locks and audit |
| `AGENT_WORKSPACE` | `data/workspace` | Folder with the libraries and service repositories |
| `PORT` | `8080` | HTTP port for the cloud app |

## Roadmap

- Install library versions from PyPI automatically during a rollout, instead of by hand
- Move the services to GitHub repositories and run their tests in CI
- Login for the cloud server (OAuth or Keycloak), so every step traces to a verified user
- PostgreSQL instead of SQLite, so the cloud server can run more than one instance
- Notifications and tickets: Slack and GitHub Issues when a rollout fails or needs review
- An evaluation set of scripted rollout requests

## Notes

- Runs locally (stdio) or in the cloud (HTTP). Library versions are "installed" by putting their folder on `PYTHONPATH` during test runs.
- Steps are recorded before acting, so a crash in the middle of a bump is still rolled back.
- Rollback resets a service only if its latest commit is the one this change made; otherwise the change goes to `needs_review`.
- The agent decides which services to include. The system guarantees that whatever it includes changes together or not at all.
- The lock covers one library, so rollouts of two different libraries can touch the same service at once. If they collide, the undo refuses instead of destroying work.
- Nothing forces the agent to run tests before committing yet; that is the agent's job, as the tool descriptions say.
- The live demo has no login: its tools only touch the demo inside the container. `AGENT_USER` is a placeholder until real login is added.
- For learning and portfolio use, not production.
