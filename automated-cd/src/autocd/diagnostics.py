"""Bounded public diagnostics for encrypted hosted state."""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

# Failures raised by the Claude CLI wrapper, keyed by the phrase after the role name. The
# first match wins, so each specific phrase precedes the broader "returned " catch-all.
_ROLE = r"(?:planner|developer|reviewer|qa) "
_AGENT_FAILURES = (
    (re.compile(_ROLE + r"could not start"), "agent_start_failed"),
    (re.compile(_ROLE + r"timed out"), "agent_timeout"),
    (re.compile(_ROLE + r"stalled"), "agent_stalled"),
    (re.compile(_ROLE + r"failed \(exit"), "agent_exit"),
    (re.compile(_ROLE + r"emitted an oversized"), "agent_output_oversized"),
    # The CLI reported is_error; the wrapper appends its fixed result subtype when it has one.
    (re.compile(_ROLE + r"returned an error \((error_[a-z_]{1,40})\)"), None),
    (re.compile(_ROLE + r"returned an error"), "agent_error"),
    (
        re.compile(_ROLE + r"returned (?:no JSON object|invalid JSON|a non-object JSON value)"),
        "agent_output_not_json",
    ),
    (re.compile(_ROLE + r"returned an invalid [a-z ]+ schema"), "agent_output_schema"),
    (re.compile(_ROLE + r"returned "), "agent_output"),
)
_OTHER_PREFIXES = (
    ("Daily Claude spend cannot be verified", "budget_unverifiable"),
    ("Daily Claude budget exhausted", "budget_exhausted"),
    ("Setup failed:", "setup_failed"),
    ("Documentation sync failed", "docs_sync_failed"),
    ("Source diff exceeds", "diff_too_large"),
    ("Candidate diff exceeds", "diff_too_large"),
    ("Interrupted before PR", "interrupted"),
)


def _unclassified(value: str) -> str:
    """Name the failures that previously all collapsed into ``other``.

    Specific legacy codes are matched first by the callers so existing labels keep
    their meaning; this only refines what would otherwise be reported as ``other``.
    """
    for pattern, code in _AGENT_FAILURES:
        match = pattern.match(value)
        if match:
            return code or f"agent_{match.group(1)}"
    for prefix, code in _OTHER_PREFIXES:
        if value.startswith(prefix):
            return code
    return "other"


def failure_code(value: str | None) -> str:
    """Classify a private failure without printing model or command output."""
    if not value:
        return "none"
    for role in ("reviewer", "qa"):
        if value.startswith(f"{role} returned"):
            # Keep the role in the label but name which way the call ended, e.g.
            # reviewer_error_max_turns or reviewer_output_schema.
            refined = _unclassified(value)
            if refined.startswith(("agent_error", "agent_output_")):
                return role + "_" + refined.removeprefix("agent_")
            return f"{role}_output"
    for prefix, code in (
        ("Developer blocker:", "developer_blocker"),
        ("Review/QA did not pass within", "review_qa_exhausted"),
        ("Issue-specific QA commands", "issue_qa_missing"),
        ("Developer changed a path", "scope_violation"),
        ("Unattended merge cannot change", "protected_path"),
        ("QA commands changed", "qa_mutation"),
        ("Developer produced no changed files", "no_changes"),
        ("Claim did not persist", "claim_failure"),
    ):
        if value.startswith(prefix):
            return code
    return _unclassified(value)


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
    return _unclassified(value)


_JOB = re.compile(r"[a-z][a-z0-9-]{0,30}")
# Fixed phrases the coordinator itself appends to a failing command row. Command output
# from the repository or a model is never matched or echoed.
_CHECK_REASONS = (
    ("QA command timed out", "timeout"),
    ("is not greater than 95%", "coverage_threshold"),
    ("Coverage JSON missing", "coverage_missing"),
    ("Invalid coverage JSON", "coverage_invalid"),
    ("Required CI parity job was skipped", "parity_skipped"),
    ("No backend test references", "no_selected_tests"),
    ("Issue-specific QA commands are required", "issue_qa_missing"),
    ("Invalid command", "invalid_command"),
)


def check_label(command: object) -> str:
    """Name a configured QA command by a fixed label."""
    if not isinstance(command, str):
        return "other"
    if command.startswith("git diff --check"):
        return "diff-check"
    if ".agents/bin/check.mjs" in command:
        return "agents-check"
    if "ci-local.sh" in command:
        jobs = [token for token in command.split()[1:] if _JOB.fullmatch(token)]
        return "ci-local:" + "+".join(jobs[:8])
    if "pytest" in command and "--cov" in command:
        return "coverage"
    if command == "changed-module coverage":
        return "coverage-selection"
    if command == "affected-component checks":
        return "affected-component"
    return "other"


def check_failure(row: dict) -> dict:
    """Describe one failing command row by label and coordinator-owned reason only."""
    command = row.get("command")
    raw_output = row.get("output")
    output = raw_output if isinstance(raw_output, str) else ""
    reason = next((code for phrase, code in _CHECK_REASONS if phrase in output), None)
    if reason is None:
        code = row.get("exit_code")
        reason = f"exit_{code}" if type(code) is int and -255 <= code <= 255 else "failed"
    failure: dict[str, object] = {"check": check_label(command), "reason": reason}
    if isinstance(command, str) and "ci-local.sh" in command:
        requested = {token for token in command.split()[1:] if _JOB.fullmatch(token)}
        failed = sorted(
            job
            for job in re.findall(r"^FAIL\s+([a-z][a-z0-9-]{0,30})\b", output, re.MULTILINE)
            if job in requested
        )
        if failed:
            failure["jobs"] = failed
    return failure


def _verdict(items: object, approved: object) -> dict:
    summary: dict = {
        "approved": approved is True,
        "finding_count": len(items) if isinstance(items, list) else -1,
    }
    if isinstance(items, list):
        severity: dict[str, int] = {}
        blocking = 0
        for item in items:
            level = item.get("severity") if isinstance(item, dict) else None
            key = level if level in {"P0", "P1", "P2", "P3"} else "unrated"
            severity[key] = severity.get(key, 0) + 1
            if not (isinstance(item, dict) and level == "P3" and item.get("blocking") is False):
                blocking += 1
        summary["severity"] = severity
        summary["blocking"] = blocking
    return summary


def cycle_trail(db: sqlite3.Connection, run_id: str, issue: int) -> list[dict]:
    """Summarize each review cycle of one attempt with fixed labels and counts."""
    trail: list[dict] = []
    rows = db.execute(
        "SELECT kind, detail FROM events WHERE run_id=? AND issue=? AND kind IN "
        "('handoff_gap', 'developer_result', 'qa_commands', 'review_cycle', 'qa_verdict') "
        "ORDER BY id",
        (run_id, issue),
    ).fetchall()
    for row in rows:
        kind = row["kind"]
        detail = json.loads(row["detail"])
        if kind in {"handoff_gap", "developer_result"}:
            entry = {"handoff_gap": kind == "handoff_gap"}
            attempt = detail.get("fix_attempt") if isinstance(detail, dict) else None
            if type(attempt) is int and 0 <= attempt <= 10:
                entry["fix_attempt"] = attempt
            trail.append(entry)
            continue
        if not trail:
            trail.append({"handoff_gap": False})
        entry = trail[-1]
        if kind == "qa_commands" and isinstance(detail, list):
            rows_ = [item for item in detail if isinstance(item, dict)]
            failed = [check_failure(item) for item in rows_ if item.get("passed") is False]
            entry["checks"] = {"ran": len(rows_), "failed": failed}
        elif kind == "review_cycle" and isinstance(detail, dict):
            raw = detail.get("result")
            result = raw if isinstance(raw, dict) else {}
            entry["review"] = _verdict(result.get("findings"), result.get("approved"))
        elif kind == "qa_verdict" and isinstance(detail, dict):
            entry["qa"] = _verdict(detail.get("gaps"), detail.get("approved"))
    return trail[-10:]


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
    """Return fixed task labels; optionally constrain them to the newest run.

    A hosted attempt can create a follow-up reconciliation/refill run with no
    task row after a task is blocked.  The default diagnostic must still show
    that task rather than returning ``no_tasks`` for the empty follow-up run.
    """
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
        trail = cycle_trail(db, task["run_id"], task["issue"])
        if trail:
            result["trail"] = trail
        return result


def main() -> None:
    path = Path(os.environ["AUTOCD_STATE_DIR"]) / "workflow.sqlite3"
    # Prefer the newest task across the durable state.  The hosted runner may
    # record an empty monitor/refill run after a blocked task, and limiting the
    # lookup to that run would hide the actionable failure classification.
    result = latest_diagnostic(path)
    result["run"] = latest_run_diagnostic(
        path, os.environ.get("AUTOCD_TIMEZONE", "America/Los_Angeles")
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
