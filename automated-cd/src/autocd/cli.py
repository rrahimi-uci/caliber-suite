"""Manual run, scheduled tick, PR monitor, and read-only status interface."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import load
from .engine import Engine
from .state import Store


def main() -> None:
    parser = argparse.ArgumentParser(prog="caliber-autocd")
    parser.add_argument("action", choices=["plan", "run", "tick", "monitor", "status"])
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
        local_day = datetime.now(ZoneInfo(settings.timezone)).date()
        day = local_day.isoformat()
        done = engine._daily_done(snapshot, label, day)
        ready = eligible(
            snapshot,
            allowlist=settings.issue_allowlist,
            pathmap=settings.issue_paths,
            scope_label=label,
        )
        blocked: frozenset[int] = frozenset()
        attempted: frozenset[int] = frozenset()
        attempts_left = settings.max_tasks_per_run
        pending_tasks = 0
        if (settings.state_dir / "workflow.sqlite3").is_file():
            store = Store(settings.state_dir)
            try:
                midnight = datetime.combine(local_day, time.min, ZoneInfo(settings.timezone))
                blocked = store.blocked_issues()
                attempted = store.attempted_since(midnight)
                attempts_left = max(
                    0, settings.max_tasks_per_run - store.attempt_count_since(midnight)
                )
                pending_tasks = len(store.active())
            finally:
                store.db.close()
        candidates = [task for task in ready if task.issue not in blocked | attempted]
        configured = [task for task in candidates if task.issue in settings.issue_checks]
        remaining = max(0, settings.daily_done_target - len(done))
        selected = (
            assign(candidates, settings.max_workers, min(attempts_left, remaining))
            if pending_tasks == 0
            else []
        )
        result = {
            "view": snapshot["view"],
            "daily_done_target": settings.daily_done_target,
            "done_today": done,
            "remaining_to_target": remaining,
            "eligible": [task.issue for task in candidates],
            "requires_explicit_retry": sorted(
                task.issue for task in ready if task.issue in blocked
            ),
            "attempted_today": sorted(task.issue for task in ready if task.issue in attempted),
            "remaining_attempts": attempts_left,
            "pending_tasks": pending_tasks,
            "configured": [task.issue for task in configured],
            "selected": [task.issue for task in selected],
        }
    elif args.action == "monitor":
        result = Engine(settings).monitor()
    else:
        result = Engine(settings).run(scheduled=args.action == "tick")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
