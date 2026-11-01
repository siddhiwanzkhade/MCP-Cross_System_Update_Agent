# Architecture

This project lets an AI agent roll out a new version of a shared library to
every service that uses it, as one unit. If any service breaks on the new
version, every service goes back to the old one. A Coordinator server locks the
library, records each step before it happens, and can undo every step. A
background cleanup job rolls back rollouts that an agent abandoned halfway.

The two MCP servers never call each other. They share one SQLite database
(`data/agent.db`) for bookkeeping, and the `change_id` passed to every write
tool ties the steps of one rollout together. The services themselves are git
repositories in the workspace (`data/workspace`).

## File tree

```
MCP-Cross-System-Update-Agent/
│
├── .mcp.json ···························· starts the 2 servers below
├── requirements.txt ····················· fastmcp, pytest
├── .env.example ························· documents AGENT_USER, AGENT_DB_PATH, AGENT_WORKSPACE
├── .gitignore ··························· ignores .venv/, data/, *.db
│
├── servers/
│   ├── coordinator/server.py ············ THE REFEREE (generic: works for any entity type)
│   │   ├── ENTITY_TYPES ········· registered kinds: "package" (name format, exists?)
│   │   ├── @undo_handler ········ registers how to reverse each kind of write
│   │   │     └── _undo_bump ····· git reset to the saved commit (refuses if others committed on top)
│   │   ├── 🔧 begin_change(entity, reason)
│   │   │     └── locks.acquire_lock → INSERT changes(open) → audit
│   │   ├── 🔧 extend_lock(change_id, seconds) ·· heartbeat for long test runs (max 1 hour)
│   │   ├── 🔧 commit_change(change_id)
│   │   │     └── guard_write → refuse if any step is still pending → committed → release lock
│   │   ├── 🔧 rollback_change(change_id)
│   │   │     ├── a later change started on this package? → needs_review, stop
│   │   │     └── for each step (pending or done), newest first:
│   │   │           ├── UNDO_HANDLERS[(system, action)](before, after)
│   │   │           ├── service changed by someone else? → needs_review, stop
│   │   │           └── mark undone, audit "undo_..."
│   │   │         → status=rolled_back → release lock
│   │   ├── 🔍 get_change(change_id)
│   │   ├── 🔍 get_audit_trail(entity)
│   │   └── 🔧 cleanup_expired_changes()
│   │         └── open changes with an expired lock → rollback_change each
│   │
│   └── services/server.py ··············· THE SERVICES
│       ├── 🔍 list_services()
│       ├── 🔍 list_dependents(package)
│       ├── 🔍 list_versions(package)
│       ├── ✏️  bump_dependency(change_id, service, package, version)
│       │     ├── 1. record step as "pending" (old version + current commit)
│       │     ├── 2. edit requirements.txt + git commit
│       │     └── 3. mark step "done" + audit
│       └── 🔍 run_tests(service, timeout_seconds)
│             └── pytest with that version on PYTHONPATH, with a time limit
│
├── shared/ ······························ HELPERS (imported by every server)
│   ├── db.py
│   │   ├── get_connection(), init_db(), transaction()
│   │   ├── current_user()     → reads AGENT_USER
│   │   ├── AgentError         → error message shown to the agent
│   │   └── UndoConflict       → undo refused, a person must review
│   ├── locks.py
│   │   ├── acquire_lock()     → lock one entity, issue a new fencing token
│   │   ├── guard_write()      → the 5-check gate before every ✏️ write
│   │   ├── extend_lock()      → push expiry forward (checks guard_write first)
│   │   └── release_lock()
│   ├── audit.py
│   │   ├── record_step()      → saves the "before" value (status pending or done)
│   │   ├── mark_step_done()
│   │   ├── log_write()        → step + audit in one go (for writes inside the database)
│   │   ├── write_audit()      → who / what / when
│   │   └── get_audit_trail()
│   └── workspace.py
│       ├── library_versions(), library_path()
│       ├── service_names(), service_path()
│       ├── read_requirements(), write_requirement()
│       └── git(), head_commit()
│
├── scripts/
│   ├── setup_demo.py ···················· creates the workspace + a fresh database
│   └── cleanup_expired_changes.py ······· background job: cleanup every 60 s
│                                          (audit user "auto-cleanup")
│
├── tests/
│   ├── conftest.py ······················ temp database + temp workspace per test
│   ├── test_rollback.py ················· breaking upgrade rolls back, safe upgrade commits, crashes
│   ├── test_clash.py ···················· locking, races, stale tokens
│   ├── test_cleanup.py ·················· expired rollouts cleaned up, newer work kept
│   ├── test_coordinator.py ·············· entity types, extend_lock, plugging in a new type
│   └── test_services.py ················· bumps, test runs, time limit, undo conflicts
│
└── data/ (created by setup_demo.py, not in git)
    ├── agent.db ························· changes, change_steps, locks, counters, audit_log
    └── workspace/
        ├── libraries/pricekit/{1.0,1.1,2.0}/
        └── services/{checkout,billing,reports}/ ·· each its own git repository

Legend:  🔧 coordinator tool   🔍 read-only tool   ✏️  write tool (needs a change_id)
```

## The demo

One shared library, `pricekit`, and three services that all use version 1.0:

| Version | What changed | checkout | billing | reports |
|---|---|---|---|---|
| 1.0 | today's version | ✅ | ✅ | ✅ |
| 1.1 | adds `parse_price` (safe) | ✅ | ✅ | ✅ |
| 2.0 | `format_price` now **requires** a currency (breaking) | ✅ | ❌ | ✅ |

`billing` calls `format_price(amount)` without a currency, so its tests fail on 2.0.

## Flow of one rollout

Example: upgrade `pricekit` to 2.0.

```
Agent request: "Upgrade pricekit to 2.0 everywhere"
│
├── 1. services.list_dependents("pricekit") ········· → billing, checkout, reports (all 1.0)
│
├── 2. coordinator.begin_change("package:pricekit")
│      ├── package exists? ✔
│      ├── acquire_lock ─► locks row + fencing token #N (expires in 5 min)
│      └── returns change_id "chg-xxxx"
│
├── 3. for each service: services.bump_dependency(chg-xxxx, service, "pricekit", "2.0")
│      ├── guard_write ✔
│      ├── step recorded as PENDING (old version 1.0 + current commit)
│      ├── requirements.txt → pricekit==2.0, git commit "Bump pricekit 1.0 -> 2.0 [chg-xxxx]"
│      └── step marked DONE
│
├── 4. coordinator.extend_lock(chg-xxxx) ············ if the tests may take long
│
├── 5. for each service: services.run_tests(service)
│      └── billing ❌  checkout ✅  reports ✅
│
└── 6. Did every test pass?
       │
       ├── ✅ YES → coordinator.commit_change
       │            ├── any step still pending? → refused, roll back instead
       │            ├── status = committed
       │            └── lock released
       │
       └── ❌ NO  → coordinator.rollback_change
                    ├── a later change started on this package? → needs_review ■ stop
                    ├── undo step 3 → reports: git reset to its old commit
                    ├── undo step 2 → checkout: git reset to its old commit
                    ├── undo step 1 → billing:  git reset to its old commit
                    ├── status = rolled_back
                    └── lock released     → every service is back on pricekit 1.0
```

## Record first, then act

Changes to git repositories are not covered by a database transaction, so a
crash could leave a change that rollback doesn't know about. Every bump
therefore follows three steps, like a database's write-ahead log:

```
1. record  "about to bump billing 1.0 → 2.0, current commit abc123"   (status: pending)
2. act     edit requirements.txt, git commit
3. confirm mark the step done                                          (status: done)
```

If the agent crashes between 1 and 3, the step is still there as "pending".
`commit_change` refuses to commit it, and rollback resets the service to
`abc123`, whether or not the commit actually happened.

The undo is safe to run more than once: if the service is already on `abc123`,
it only discards a half-written `requirements.txt`. It also refuses to reset a
service if someone else has committed on top of the bump, because a reset
would destroy that work. The change is marked "needs review" instead, and its
lock is released.

## The write gate: `guard_write()`

Every write tool, `extend_lock` and `commit_change` run these checks inside a transaction:

```
write tool called with change_id
        │
        ├── change exists? ─────────────── no → "Unknown change. Call begin_change first."
        ├── status == open? ────────────── no → "Change is committed/rolled_back, not open."
        ├── change is for this entity? ─── no → "Change is for package:X, not package:Y."
        ├── lock still valid? ──────────── no → "Lock expired. Roll back and start again."
        ├── lock's change_id + fencing
        │   token match? ───────────────── no → "Stale fencing token."
        │
        └── ✅ write allowed
```

The fencing token stops a slow agent from writing late. If agent A's lock
expires and agent B takes the lock, B gets a newer token. When A tries to
write, the token no longer matches, so the write is refused.

## Cleaning up abandoned rollouts

If an agent crashes in the middle of a rollout, it never commits or rolls back.
Its lock expires after 5 minutes, and then the cleanup job takes over:

```
Agent crashes mid-rollout
        │
   5 minutes pass → lock expires
        │
Cleanup job checks (every 60 seconds), or an agent calls cleanup_expired_changes
        │
   ┌────┴──────────────────────────────┐
   ▼                                   ▼
No later change on the package        A later change was started on the package
→ roll back automatically ✅           (even one that already committed)
                                      → mark "needs_review" ⚠️
                                        (newer work stays safe)
```

"A later change" means a newer change (one with a bigger fencing token) that was
not itself rolled back. This check lives in `rollback_change`, so it also
protects manual rollbacks. Rollbacks done by the script are recorded in the
audit log under the user `auto-cleanup`.

Run it in its own terminal with `.venv/bin/python scripts/cleanup_expired_changes.py`
(add `--once` to check one time and exit, or `--interval 10` to check every 10 seconds).

## Adding a new kind of change

The coordinator doesn't know about packages specifically. To support another
kind of entity, register it and its undo handler; nothing else changes
(see `test_a_new_entity_type_works_end_to_end`):

```python
ENTITY_TYPES["config"] = EntityType(example="config:timeout", valid_id=..., exists=...)

@undo_handler("settings", "set_value")
def _undo_set(conn, before, after): ...
```

## Limitations in version 1

- **Identity:** the user name comes from the `AGENT_USER` setting, a placeholder
  until real login with Keycloak is added.
- **Approval:** nothing forces the agent to run tests before committing. That is
  the agent's job, as described in the tool descriptions.
- **Locks cover one package:** two rollouts of *different* packages can touch the
  same service at once. If they collide, the undo refuses and asks for review
  instead of destroying work.
- **Local only:** libraries are "installed" by putting their folder on
  `PYTHONPATH`. Nothing is published or downloaded.

## Setup

```
1. python -m venv .venv && .venv/bin/pip install -r requirements.txt
2. .venv/bin/python scripts/setup_demo.py   → creates data/workspace and data/agent.db
3. Claude reads .mcp.json                   → starts the 2 servers
4. Agent: list_dependents → begin_change → bump_dependency → run_tests → commit / rollback
5. .venv/bin/python scripts/cleanup_expired_changes.py   → optional, in a second terminal
6. .venv/bin/pytest                         → runs the tests on a temporary database and workspace
```

Run `setup_demo.py` again any time you want a clean start.
