"""Background cleanup job: rolls back changes that were left half-finished.

Every minute, it finds changes that were left half-finished (their lock expired,
for example because the agent crashed) and rolls them back automatically.
If a later change was already started on the same entity, it marks the old change
"needs_review" instead, so a person can decide.

Run it in its own terminal:
    python scripts/cleanup_expired_changes.py               # check every 60 seconds
    python scripts/cleanup_expired_changes.py --interval 10 # check every 10 seconds
    python scripts/cleanup_expired_changes.py --once        # check one time and exit
"""

import argparse
import importlib.util
import os
import sys
import time
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# The audit log shows "auto-cleanup" as the one who rolled things back.
os.environ["AGENT_USER"] = "auto-cleanup"

from shared.db import get_connection, init_db  # noqa: E402


def load_coordinator():
    spec = importlib.util.spec_from_file_location(
        "coordinator_server", ROOT / "servers" / "coordinator" / "server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_once(coordinator) -> None:
    results = coordinator.cleanup_expired_changes_impl()
    stamp = time.strftime("%H:%M:%S")
    if not results:
        print(f"[{stamp}] Nothing to clean up.")
    for result in results:
        print(f"[{stamp}] {result['change_id']}: {result['status']}"
              + (f" ({result['message']})" if result.get("message") else ""))


def main() -> None:
    parser = argparse.ArgumentParser(description="Roll back changes whose lock expired.")
    parser.add_argument("--interval", type=float, default=60, help="seconds between checks")
    parser.add_argument("--once", action="store_true", help="check one time and exit")
    args = parser.parse_args()

    with closing(get_connection()) as conn:
        init_db(conn)
    coordinator = load_coordinator()
    if args.once:
        run_once(coordinator)
        return

    print(f"Cleanup job running every {args.interval:g} seconds. Press Ctrl+C to stop.")
    try:
        while True:
            try:
                run_once(coordinator)
            except Exception as exc:  # keep running; try again on the next check
                print(f"[{time.strftime('%H:%M:%S')}] Error: {exc}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Cleanup job stopped.")


if __name__ == "__main__":
    main()
