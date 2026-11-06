"""Bounded public diagnostics for encrypted hosted state."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo


def failure_code(value: str | None) -> str:
    """Classify a private failure without printing model or command output."""
    if not value:
        return "none"
    for prefix, code in (
        ("Developer blocker:", "developer_blocker"),
        ("Review/QA did not pass within", "review_qa_exhausted"),
        ("reviewer returned", "reviewer_output"),
        ("qa returned", "qa_output"),
        ("Issue-specific QA commands", "issue_qa_missing"),
        ("Developer changed a path", "scope_violation"),
        ("Unattended merge cannot change", "protected_path"),
        ("QA commands changed", "qa_mutation"),
        ("Developer produced no changed files", "no_changes"),
        ("Claim did not persist", "claim_failure"),
    ):
        if value.startswith(prefix):
            return code
    return "other"


def run_failure_code(value: str | None) -> str:
    """Classify a pre-task run failure without disclosing its private reason."""
    if not value:
        return "none"
    for prefix, code in (
        (
            "Explicit retry was not selected by planner or daily attempt cap",
            "retry_unselected_or_cap",
        ),
        ("Retry requires a blocked pre-PR attempt", "retry_precondition"),
        ("Cannot retry while a published PR is pending", "pending_pr"),
        ("planner timed out", "planner_timeout"),
        ("planner stalled", "planner_stall"),
        ("planner returned an error", "planner_error"),
        ("planner failed", "planner_error"),
    ):
        if value.startswith(prefix):
            return code
    return "other"


def latest_run_diagnostic(path: Path, timezone: str) -> dict:
    """Report a fixed run label and today's claim count, never raw event text."""
    midnight = datetime.combine(
        datetime.now(ZoneInfo(timezone)).date(), time.min, ZoneInfo(timezone)
    )
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        run = db.execute("SELECT id, phase FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
        if run is None:
            return {"phase": "none", "failure_code": "none", "claims_today": 0}
        failure = db.execute(
            "SELECT detail FROM events WHERE run_id=? AND issue IS NULL AND kind='run_failed' "
            "ORDER BY id DESC LIMIT 1",
            (run["id"],),
        ).fetchone()
        reason = json.loads(failure["detail"]).get("reason") if failure else None
        claims = db.execute(
            "SELECT COUNT(*) FROM events WHERE kind='task_claimed' AND at>=?",
            (midnight.astimezone(UTC).isoformat(),),
        ).fetchone()[0]
        return {
            "phase": run["phase"]
            if run["phase"] in {"running", "finished", "failed"}
            else "unknown",
            "failure_code": run_failure_code(reason),
            "claims_today": int(claims),
        }


def latest_diagnostic(path: Path, *, latest_run_only: bool = False) -> dict:
    """Return fixed task labels; optionally constrain them to the newest run."""
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        task = db.execute(
            "SELECT run_id, issue, phase, failure, cycles, pr, head "
            "FROM tasks "
            + (
                "WHERE run_id=(SELECT id FROM runs ORDER BY rowid DESC LIMIT 1) "
                if latest_run_only
                else ""
            )
            + "ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        if task is None:
            return {"status": "no_tasks"}
        result = {
            "issue": task["issue"],
            "phase": task["phase"]
            if task["phase"]
            in {
                "claimed",
                "developing",
                "reviewing",
                "qa",
                "pr_open",
                "ci_wait",
                "done",
                "blocked",
                "failed",
            }
            else "unknown",
            "failure_code": failure_code(task["failure"]),
            "cycles": task["cycles"],
            "has_pr": task["pr"] is not None,
            "has_pushed_head": task["head"] is not None,
        }
        for kind, field in (("review_cycle", "review"), ("qa_verdict", "qa")):
            row = db.execute(
                "SELECT detail FROM events WHERE run_id=? AND issue=? AND kind=? "
                "ORDER BY id DESC LIMIT 1",
                (task["run_id"], task["issue"], kind),
            ).fetchone()
            if row:
                detail = json.loads(row["detail"])
                verdict = detail.get("result", {}) if field == "review" else detail
                items = (
                    verdict.get("findings", []) if field == "review" else verdict.get("gaps", [])
                )
                result[field] = {
                    "approved": verdict.get("approved") is True,
                    "finding_count": len(items) if isinstance(items, list) else -1,
                }
        row = db.execute(
            "SELECT detail FROM events WHERE run_id=? AND issue=? AND kind='qa_commands' "
            "ORDER BY id DESC LIMIT 1",
            (task["run_id"], task["issue"]),
        ).fetchone()
        if row:
            commands = json.loads(row["detail"])
            result["commands"] = {
                "total": len(commands) if isinstance(commands, list) else -1,
                "failed": sum(item.get("passed") is False for item in commands)
                if isinstance(commands, list)
                else -1,
            }
        return result


def main() -> None:
    path = Path(os.environ["AUTOCD_STATE_DIR"]) / "workflow.sqlite3"
    result = latest_diagnostic(path, latest_run_only=True)
    result["run"] = latest_run_diagnostic(
        path, os.environ.get("AUTOCD_TIMEZONE", "America/Los_Angeles")
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
