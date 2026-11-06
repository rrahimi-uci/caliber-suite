"""GitHub-hosted execution: refill safe task batches until the daily target."""

from __future__ import annotations

import json
import os
import time

from .config import load
from .engine import Engine


def run_hosted(mode: str, *, max_seconds: int = 18_000, poll_seconds: int = 60) -> None:
    if mode not in {"schedule", "manual"}:
        raise ValueError("AUTOCD_RUN_MODE must be schedule or manual")
    engine = Engine(load())
    max_attempts = engine.settings.max_tasks_per_run
    deadline = time.monotonic() + max_seconds
    result = engine.run(scheduled=mode == "schedule")
    print(json.dumps({"start": result}, sort_keys=True), flush=True)
    attempted = set(result.get("selected", []))
    can_refill = bool(attempted) or result.get("status") == "waiting_on_published_prs"
    while True:
        status = engine.monitor()
        print(json.dumps({"monitor": status}, sort_keys=True), flush=True)
        if status["pending"] == 0:
            if (
                not can_refill
                or len(attempted) >= max_attempts
                or time.monotonic() + poll_seconds >= deadline
            ):
                return
            result = engine.run(scheduled=False, exclude_issues=frozenset(attempted))
            print(json.dumps({"refill": result}, sort_keys=True), flush=True)
            selected = set(result.get("selected", []))
            if not selected:
                return
            attempted.update(selected)
            continue
        if time.monotonic() + poll_seconds >= deadline:
            print(
                "Monitoring window ended; pending PR state will resume from the next run.",
                flush=True,
            )
            return
        time.sleep(poll_seconds)


if __name__ == "__main__":
    run_hosted(os.environ.get("AUTOCD_RUN_MODE", "schedule"))
