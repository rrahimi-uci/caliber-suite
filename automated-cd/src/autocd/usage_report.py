"""Public, bounded spend and duration report for one GitHub execution."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path


def execution_report(
    path: Path,
    github_run_id: str,
    github_run_attempt: str = "1",
    *,
    now: datetime | None = None,
) -> dict:
    """Aggregate numeric Claude estimates without exposing prompts or task output."""
    if (
        not github_run_id.isdecimal()
        or not github_run_attempt.isdecimal()
        or int(github_run_attempt) < 1
    ):
        raise ValueError("Numeric GitHub run ID and positive attempt are required")
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        starts = db.execute(
            "SELECT run_id,at,detail FROM events WHERE kind='run_started' ORDER BY id"
        )
        matched = [
            row
            for row in starts
            if (detail := json.loads(row["detail"])).get("github_run_id") == github_run_id
            and detail.get("github_run_attempt", "1") == github_run_attempt
        ]
        if not matched:
            return {
                "github_run_id": github_run_id,
                "github_run_attempt": github_run_attempt,
                "status": "no_task_runs",
            }
        run_ids = [row["run_id"] for row in matched]
        placeholders = ",".join("?" for _ in run_ids)
        events = db.execute(
            f"SELECT run_id,issue,at,kind,detail FROM events WHERE run_id IN ({placeholders}) "
            "ORDER BY id",
            run_ids,
        )
        costs: list[Decimal] = []
        calls_started = calls_reported = claims = 0
        planner_cost = Decimal(0)
        by_issue: dict[int, Decimal] = {}
        claimed_at: dict[int, datetime] = {}
        terminal_at: dict[int, datetime] = {}
        for event in events:
            kind = event["kind"]
            if kind == "agent_started":
                calls_started += 1
            elif kind == "task_claimed":
                claims += 1
                claimed_at[int(event["issue"])] = datetime.fromisoformat(event["at"])
            elif kind == "phase" and event["issue"] is not None:
                if json.loads(event["detail"]).get("phase") in {"done", "blocked", "failed"}:
                    terminal_at[int(event["issue"])] = datetime.fromisoformat(event["at"])
            elif kind == "agent_usage":
                detail = json.loads(event["detail"])
                cost = detail.get("cost_usd")
                if type(cost) not in {int, float} or not 0 <= cost < float("inf"):
                    continue
                amount = Decimal(str(cost))
                costs.append(amount)
                calls_reported += 1
                if event["issue"] is None:
                    planner_cost += amount
                else:
                    issue = int(event["issue"])
                    by_issue[issue] = by_issue.get(issue, Decimal(0)) + amount
        tasks = list(
            db.execute(
                f"SELECT issue,phase FROM tasks WHERE run_id IN ({placeholders}) ORDER BY rowid",
                run_ids,
            )
        )
    completed = sum(task["phase"] == "done" for task in tasks)
    total = sum(costs, Decimal(0))
    report_time = now or datetime.now(UTC)
    elapsed = report_time - datetime.fromisoformat(matched[0]["at"])
    complete = calls_started == calls_reported
    return {
        "github_run_id": github_run_id,
        "github_run_attempt": github_run_attempt,
        "status": "measured" if complete else "partial_cost_data",
        "coordinator_elapsed_seconds": max(0, round(elapsed.total_seconds(), 1)),
        "task_attempts": claims,
        "completed_tasks": completed,
        "claude_calls_started": calls_started,
        "claude_calls_with_cost": calls_reported,
        "reported_model_cost_usd": round(float(total), 6),
        "cost_per_completed_task_usd": round(float(total / completed), 6)
        if complete and completed
        else None,
        "planner_cost_usd": round(float(planner_cost), 6),
        "tasks": [
            {
                "issue": int(task["issue"]),
                "phase": task["phase"],
                "reported_model_cost_usd": round(
                    float(by_issue.get(int(task["issue"]), Decimal(0))), 6
                ),
                "elapsed_seconds": max(
                    0,
                    round(
                        (
                            terminal_at.get(int(task["issue"]), report_time)
                            - claimed_at[int(task["issue"])]
                        ).total_seconds(),
                        1,
                    ),
                ),
            }
            for task in tasks
        ],
    }


def main() -> None:
    path = Path(os.environ["AUTOCD_STATE_DIR"]) / "workflow.sqlite3"
    print(
        json.dumps(
            execution_report(
                path,
                os.environ["GITHUB_RUN_ID"],
                os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
            ),
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
