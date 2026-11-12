"""GitHub-hosted execution: refill safe task batches until the daily target."""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from datetime import time as day_time
from zoneinfo import ZoneInfo

from .config import load
from .engine import Engine
from .planner import view_label
from .state import Store


def _public_result(value: dict) -> dict:
    """Print progress without exposing model, QA, or GitHub error text."""
    safe = {
        key: value[key]
        for key in (
            "run",
            "status",
            "pending",
            "awaiting_owner",
            "eligible",
            "configured",
            "selected",
            "decision_candidates",
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
    if "decision_proposals" in value:
        safe["decision_proposals"] = [
            {
                field: item[field]
                for field in ("issue", "status", "gate", "option_count", "failure_code")
                if field in item
            }
            for item in value["decision_proposals"]
            if isinstance(item, dict)
        ]
    return safe


def run_hosted(
    mode: str,
    *,
    max_seconds: int = 16_800,
    poll_seconds: int = 60,
    refill_seconds: int = 10_800,
) -> None:
    """Start, monitor and refill within the job's time.

    ``max_seconds`` (280 min) stays below the workflow's 300-minute step timeout,
    and a new batch starts only with ``refill_seconds`` (3 h, a worst-case task)
    left, so a task is never killed mid-flight by the runner.
    """
    if mode not in {"schedule", "manual"}:
        raise ValueError("AUTOCD_RUN_MODE must be schedule or manual")
    retry_raw = os.environ.get("AUTOCD_RETRY_ISSUE", "").strip()
    if retry_raw and (mode != "manual" or not retry_raw.isdecimal() or int(retry_raw) <= 0):
        raise ValueError("AUTOCD_RETRY_ISSUE must be a positive issue number on a manual run")
    retry_issue = int(retry_raw) if retry_raw else None
    trial_raw = os.environ.get("AUTOCD_TRIAL_TASKS", "0").strip()
    if not trial_raw.isdecimal() or not 0 <= int(trial_raw) <= 5:
        raise ValueError("AUTOCD_TRIAL_TASKS must be an integer from 0 through 5")
    trial_tasks = int(trial_raw)
    if trial_tasks and mode != "manual":
        raise ValueError("A measured task trial requires manual GitHub dispatch")
    settings = load()
    engine = Engine(settings)
    if trial_tasks:
        day = datetime.now(ZoneInfo(settings.timezone)).date()
        midnight = datetime.combine(
            day,
            day_time.min,
            ZoneInfo(settings.timezone),
        )
        store = Store(settings.state_dir)
        try:
            prior_attempts = store.attempt_count_since(midnight.astimezone(UTC))
        finally:
            store.db.close()
        snapshot = engine.github.snapshot()
        label = view_label(snapshot, settings.project_view_number)
        done_today = len(engine._daily_done(snapshot, label, day.isoformat()))
        settings = replace(
            settings,
            max_tasks_per_run=prior_attempts + trial_tasks,
            daily_done_target=max(settings.daily_done_target, done_today + trial_tasks),
            # A measured task trial is reserved for task attempts. Decision
            # packets are read-only and must not consume that end-to-end budget.
            max_decision_proposals_per_run=0,
        )
        engine = Engine(settings)
    max_attempts = trial_tasks or settings.max_tasks_per_run
    deadline = time.monotonic() + max_seconds
    blocked: list[dict] = []
    pr_errors: set[int] = set()
    decision_errors: set[int] = set()

    def record(value: dict) -> None:
        for proposal in value.get("decision_proposals", []):
            if not isinstance(proposal, dict) or type(proposal.get("issue")) is not int:
                continue
            issue = proposal["issue"]
            if proposal.get("status") == "proposal_error":
                decision_errors.add(issue)
            elif proposal.get("status") == "proposal_ready":
                decision_errors.discard(issue)
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
        if trial_tasks and len(attempted) < trial_tasks:
            # A manual trial is an explicit end-to-end measurement request. Do
            # not let a clean no-op (or a short refill caused by an exhausted
            # Project backlog) look like a successful five-task trial. The
            # report contains only issue numbers and counts; task details remain
            # in the encrypted state/diagnostic path.
            print(
                json.dumps(
                    {
                        "trial": {
                            "attempted": len(attempted),
                            "requested": trial_tasks,
                            "selected": sorted(attempted),
                            "status": "shortfall",
                        }
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            if not blocked and not pr_errors:
                raise RuntimeError(
                    f"Manual trial requested {trial_tasks} task attempts but selected "
                    f"{len(attempted)}"
                )
        if blocked:
            issues = sorted({item.get("issue") for item in blocked if item.get("issue")})
            raise RuntimeError(f"Automated CD blocked issues: {issues}; inspect state artifact")
        if pr_errors:
            raise RuntimeError(f"Automated CD PR checks could not be verified: {sorted(pr_errors)}")
        if decision_errors:
            raise RuntimeError(
                "Automated CD decision proposals could not be generated: "
                f"{sorted(decision_errors)}; inspect state artifact"
            )

    initial_options = {"scheduled": mode == "schedule"}
    if retry_issue is not None:
        initial_options["retry_issue"] = retry_issue
    result = engine.run(**initial_options)
    print(json.dumps({"start": _public_result(result)}, sort_keys=True), flush=True)
    record(result)
    attempted = set(result.get("selected", []))
    can_refill = (retry_issue is None or trial_tasks > 0) and (
        bool(attempted) or result.get("status") == "waiting_on_published_prs"
    )
    while True:
        status = engine.monitor()
        print(json.dumps({"monitor": _public_result(status)}, sort_keys=True), flush=True)
        record(status)
        if status["pending"] == 0:
            if (
                not can_refill
                or len(attempted) >= max_attempts
                or time.monotonic() + max(poll_seconds, refill_seconds) >= deadline
            ):
                finish()
                return
            refill_options: dict = {"scheduled": False, "exclude_issues": frozenset(attempted)}
            if mode == "schedule":
                refill_options["auto_retry"] = True
            result = engine.run(**refill_options)
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
