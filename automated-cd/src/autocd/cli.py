"""Manual run, cron tick, and read-only status interface."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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
        result = [dict(row) for row in store.tasks()]
    elif args.action == "plan":
        from .planner import assign, eligible, view_label

        engine = Engine(settings)
        snapshot = engine.github.snapshot()
        label = view_label(snapshot, settings.project_view_number)
        day = datetime.now(ZoneInfo(settings.timezone)).date().isoformat()
        done = engine._daily_done(snapshot, label, day)
        candidates = eligible(
            snapshot,
            allowlist=settings.issue_allowlist,
            pathmap=settings.issue_paths,
            scope_label=label,
        )
        configured = [task for task in candidates if task.issue in settings.issue_checks]
        remaining = max(0, settings.daily_done_target - len(done))
        selected = assign(
            configured, settings.max_workers, min(settings.max_tasks_per_run, remaining)
        )
        result = {
            "view": snapshot["view"],
            "daily_done_target": settings.daily_done_target,
            "done_today": done,
            "remaining_to_target": remaining,
            "eligible": [task.issue for task in candidates],
            "configured": [task.issue for task in configured],
            "selected": [task.issue for task in selected],
        }
    else:
        result = Engine(settings).run(scheduled=args.action == "tick")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
