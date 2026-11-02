# MCP-Cross_System_Update_Agent
A set of MCP servers that let an AI agent roll out a new library version to every service that depends on it, as one unit, with library-level locking, automatic rollback and an audit trail.

Designed to work with LLMs via the MCP (Model Context Protocol). Tested with Claude Code as the host.
The demo uses `pricekit`, a small stand-in library built for this project, in place of a real package like pydantic, numpy or pandas, so it runs offline and fails the same way every time.

## How It Works

1. **Request**: The user asks in plain English, for example "Upgrade pricekit to 2.0 in every service, run their tests, and roll back if anything fails." (In a real team, this would be a library like pydantic 1.10 → 2.0.)
2. **Discovery**: The host connects to each MCP server and lists its tools. The agent asks which services depend on the library.
3. **Lock**: The Coordinator locks the library (`package:pricekit`) and returns a change ID and a fencing token.
4. **Apply**: The agent bumps the library version in each service, with the change ID. Each step is recorded *before* it happens, then the service's `requirements.txt` is edited and committed in its git repository.
5. **Test**: The agent runs each service's tests against the new version, with a time limit.
6. **Commit or rollback**: If every test passes, the change is committed. If any test fails, every service is reset to the commit it had before, newest step first.
7. **Audit**: Every step is logged with the user, the change ID and the before and after values.

If an agent crashes halfway, a cleanup job rolls back its change once the lock expires. If rolling back could destroy newer work (a later change on the same library, or someone else's commit on top), the change is marked `needs_review` for a person instead.

## Demo

`scripts/setup_demo.py` creates `pricekit`, a stand-in for a shared library, and three services, each its own git repository, all on `pricekit==1.0`:

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

Every write tool requires a `change_id`, checks the lock and fencing token, records the before state first, and writes an audit row.

## LLM MCP Configuration

`.mcp.json` (used by Claude Code):

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

Both servers run locally over stdio.

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

## Try It

Connect your own MCP client to the live demo, no install needed:

    https://your-project.run.app/mcp

- **Claude Code:** `claude mcp add --transport http rollout https://your-project.run.app/mcp`
- **Claude Desktop / Cursor / VS Code / ChatGPT:** add it as a custom MCP server or connector with that URL

Then ask: *"Upgrade pricekit to 2.0 in every service and roll back if anything fails."*

The demo is shared by everyone and resets when the server restarts. Use the `reset_demo` tool for a clean start.


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
├── tests/                        # 28 tests: rollback, races, crashes, cleanup
├── docs/ARCHITECTURE.md          # Diagrams of how everything connects
├── data/                         # Created by setup_demo.py (not in git)
├── .mcp.json
├── pytest.ini
└── requirements.txt
```

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AGENT_USER` | `unknown-user` | Who is making changes (placeholder for a verified identity) |
| `AGENT_DB_PATH` | `data/agent.db` | SQLite database for changes, locks and audit |
| `AGENT_WORKSPACE` | `data/workspace` | Folder with the libraries and service repositories |

## Roadmap

- Move the services to GitHub repositories and run their tests in CI
- Publish library versions to a local package registry, as the last step of a rollout
- Notifications and tickets: Slack and GitHub Issues when a rollout fails or needs review
- Keycloak login with token exchange, so every step traces to a verified user
- Docker Compose and an evaluation set of scripted rollout requests
-  Run against real PyPI packages (for example pydantic 1.10 → 2.0) instead of the stand-in library

## Notes

- Everything in the current version runs locally and offline. Library versions are "installed" by putting their folder on `PYTHONPATH` during test runs.
- Steps are recorded before acting, so a crash in the middle of a bump is still rolled back.
- Rollback resets a service only if its latest commit is the one this change made; otherwise the change goes to `needs_review`.
- The lock covers one library, so rollouts of two different libraries can touch the same service at once. If they collide, the undo refuses instead of destroying work.
- Nothing forces the agent to run tests before committing yet; that is the agent's job, as the tool descriptions say.
- `AGENT_USER` is a placeholder until Keycloak login is added.
- For learning and portfolio use, not production.
