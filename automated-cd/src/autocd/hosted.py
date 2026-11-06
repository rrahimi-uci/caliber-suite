"""GitHub-hosted execution: refill safe task batches until the daily target."""

from __future__ import annotations

import json
import os
import sys
import time

from .config import load
from .engine import Engine


def _public_result(value: dict) -> dict:
    """Print progress without exposing model, QA, or GitHub error text."""
    safe = {
        key: value[key]
        for key in (
            "run",
            "status",
            "pending",
            "eligible",
            "configured",
            "selected",
            "daily_done_target",
            "done_today",
            "remaining_to_target",
        )
        if key in value
    }
    for key in ("results", "recovered"):
        if key in value:
            safe[key] = [
                {field: item[field] for field in ("issue", "status") if field in item}
                for item in value[key]
            ]
    return safe


def run_hosted(mode: str, *, max_seconds: int = 18_000, poll_seconds: int = 60) -> None:
    if mode not in {"schedule", "manual"}:
        raise ValueError("AUTOCD_RUN_MODE must be schedule or manual")
    engine = Engine(load())
    max_attempts = engine.settings.max_tasks_per_run
    deadline = time.monotonic() + max_seconds
    blocked: list[dict] = []
    pr_errors: set[int] = set()

    def record(value: dict) -> None:
        for item in [*value.get("results", []), *value.get("recovered", [])]:
            status = str(item.get("status", ""))
            issue = item.get("issue")
            if status.startswith("blocked"):
                blocked.append(item)
            if type(issue) is int:
                if status == "pr_check_error":
                    pr_errors.add(issue)
                else:
                    pr_errors.discard(issue)

    def finish() -> None:
        if blocked:
            issues = sorted({item.get("issue") for item in blocked if item.get("issue")})
            raise RuntimeError(f"Automated CD blocked issues: {issues}; inspect state artifact")
        if pr_errors:
            raise RuntimeError(f"Automated CD PR checks could not be verified: {sorted(pr_errors)}")

    result = engine.run(scheduled=mode == "schedule")
    print(json.dumps({"start": _public_result(result)}, sort_keys=True), flush=True)
    record(result)
    attempted = set(result.get("selected", []))
    can_refill = bool(attempted) or result.get("status") == "waiting_on_published_prs"
    while True:
        status = engine.monitor()
        print(json.dumps({"monitor": _public_result(status)}, sort_keys=True), flush=True)
        record(status)
        if status["pending"] == 0:
            if (
                not can_refill
                or len(attempted) >= max_attempts
                or time.monotonic() + poll_seconds >= deadline
            ):
                finish()
                return
            result = engine.run(scheduled=False, exclude_issues=frozenset(attempted))
            print(json.dumps({"refill": _public_result(result)}, sort_keys=True), flush=True)
            record(result)
            selected = set(result.get("selected", []))
            if not selected:
                finish()
                return
            attempted.update(selected)
            continue
        if time.monotonic() + poll_seconds >= deadline:
            print(
                "Monitoring window ended; pending PR state will resume from the next run.",
                flush=True,
            )
            finish()
            return
        time.sleep(poll_seconds)


if __name__ == "__main__":
    try:
        run_hosted(os.environ.get("AUTOCD_RUN_MODE", "schedule"))
    except Exception:  # noqa: BLE001 - do not print untrusted task or command output
        print("Automated CD failed; inspect encrypted state artifact", file=sys.stderr)
        raise SystemExit(1) from None
