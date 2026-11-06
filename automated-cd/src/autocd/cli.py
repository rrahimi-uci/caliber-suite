"""Manual run, cron tick, and read-only status interface."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .config import load
from .engine import Engine
from .state import Store


def main() -> None:
    parser = argparse.ArgumentParser(prog="caliber-autocd")
    parser.add_argument("action", choices=["plan", "run", "tick", "status"])
    parser.add_argument("--config", type=Path)
    args = parser.parse_args()
    settings = load(args.config)
    if args.action == "status":
        store = Store(settings.state_dir)
        result = [dict(row) for row in store.pending()]
    elif args.action == "plan":
        from .planner import assign, eligible

        snapshot = Engine(settings).github.snapshot()
        candidates = eligible(
            snapshot, allowlist=settings.issue_allowlist, pathmap=settings.issue_paths
        )
        configured = [task for task in candidates if task.issue in settings.issue_checks]
        selected = assign(configured, settings.max_workers, settings.max_tasks_per_run)
        result = {
            "eligible": [task.issue for task in candidates],
            "configured": [task.issue for task in configured],
            "selected": [task.issue for task in selected],
        }
    else:
        result = Engine(settings).run(scheduled=args.action == "tick")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
