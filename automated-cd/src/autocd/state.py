"""Durable state, audit events, and local non-overlap lock."""

from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path


class LockBusyError(RuntimeError):
    """Another local coordinator invocation owns the worktree/state lock."""


class Store:
    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
        self.path = directory / "workflow.sqlite3"
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, schedule_date TEXT UNIQUE, phase TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tasks (
              run_id TEXT NOT NULL, issue INTEGER NOT NULL, target TEXT NOT NULL,
              phase TEXT NOT NULL, branch TEXT NOT NULL, worktree TEXT NOT NULL,
              pr INTEGER, head TEXT, cycles INTEGER NOT NULL DEFAULT 0, failure TEXT,
              PRIMARY KEY(run_id, issue));
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
              issue INTEGER, at TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL);
            """
        )
        self.db.commit()

    @staticmethod
    def now() -> str:
        return datetime.now(UTC).isoformat()

    def event(self, run: str, issue: int | None, kind: str, detail: dict | str) -> None:
        self.db.execute(
            "INSERT INTO events(run_id,issue,at,kind,detail) VALUES(?,?,?,?,?)",
            (run, issue, self.now(), kind, json.dumps(detail, sort_keys=True)),
        )
        self.db.commit()

    def start(self, run: str, schedule_date: str | None = None) -> bool:
        try:
            self.db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?)",
                (run, schedule_date, "running", self.now(), self.now()),
            )
            self.db.commit()
            self.event(
                run,
                None,
                "run_started",
                {
                    "schedule_date": schedule_date,
                    "github_run_id": os.environ.get("GITHUB_RUN_ID"),
                    "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
                },
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def scheduled(self, day: str) -> bool:
        return bool(self.db.execute("SELECT 1 FROM runs WHERE schedule_date=?", (day,)).fetchone())

    def add_task(self, run: str, issue: int, target: str, branch: str, worktree: Path) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO tasks(run_id,issue,target,phase,branch,worktree) VALUES(?,?,?,?,?,?)",
                (run, issue, target, "claimed", branch, str(worktree)),
            )
            self.db.execute(
                "INSERT INTO events(run_id,issue,at,kind,detail) VALUES(?,?,?,?,?)",
                (
                    run,
                    issue,
                    self.now(),
                    "task_claimed",
                    json.dumps({"branch": branch, "worktree": str(worktree)}, sort_keys=True),
                ),
            )

    def phase(self, run: str, issue: int, phase: str, *, failure: str | None = None) -> None:
        self.db.execute(
            "UPDATE tasks SET phase=?, failure=? WHERE run_id=? AND issue=?",
            (phase, failure, run, issue),
        )
        self.db.commit()
        self.event(run, issue, "phase", {"phase": phase, "failure": failure})

    def cycle(self, run: str, issue: int) -> int:
        self.db.execute("UPDATE tasks SET cycles=cycles+1 WHERE run_id=? AND issue=?", (run, issue))
        self.db.commit()
        return int(self.task(run, issue)["cycles"])

    def pr(self, run: str, issue: int, number: int) -> None:
        self.db.execute("UPDATE tasks SET pr=? WHERE run_id=? AND issue=?", (number, run, issue))
        self.db.commit()
        self.event(run, issue, "pr_opened", {"number": number})

    def head(self, run: str, issue: int, sha: str) -> None:
        self.db.execute("UPDATE tasks SET head=? WHERE run_id=? AND issue=?", (sha, run, issue))
        self.db.commit()
        self.event(run, issue, "commit_pushed", {"head": sha})

    def task(self, run: str, issue: int) -> sqlite3.Row:
        row = self.db.execute(
            "SELECT * FROM tasks WHERE run_id=? AND issue=?", (run, issue)
        ).fetchone()
        if row is None:
            raise KeyError((run, issue))
        return row

    def latest_task(self, issue: int) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM tasks WHERE issue=? ORDER BY rowid DESC LIMIT 1", (issue,)
        ).fetchone()

    def pending(self) -> list[sqlite3.Row]:
        return list(
            self.db.execute("SELECT * FROM tasks WHERE phase NOT IN ('done','blocked','failed')")
        )

    def active(self) -> list[sqlite3.Row]:
        """Pending tasks that still need automation, excluding PRs awaiting the owner.

        With automatic merge disabled a verified PR waits in ``ready`` until the owner
        merges it. Each tick still reconciles it, but it must not stop the next task:
        otherwise one unmerged PR halts the whole daily pipeline.
        """
        return [row for row in self.pending() if row["phase"] != "ready"]

    def event_count(self, issue: int, kind: str) -> int:
        return int(
            self.db.execute(
                "SELECT COUNT(*) FROM events WHERE issue=? AND kind=?", (issue, kind)
            ).fetchone()[0]
        )

    def blocked_with_pr(self) -> list[sqlite3.Row]:
        """Each issue's latest attempt when it is blocked with a published PR."""
        return [
            row
            for row in self.db.execute(
                "SELECT * FROM tasks t WHERE phase='blocked' AND pr IS NOT NULL AND rowid="
                "(SELECT MAX(rowid) FROM tasks WHERE issue=t.issue)"
            )
        ]

    def blocked_issues(self) -> frozenset[int]:
        """Never replay an ambiguous or failed task merely because Project is Ready."""
        return frozenset(
            row[0]
            for row in self.db.execute(
                "SELECT DISTINCT issue FROM tasks WHERE phase IN ('blocked','failed')"
            )
        )

    def attempted_since(self, instant: datetime) -> frozenset[int]:
        """Find tasks claimed since an instant, independent of run start time."""
        return frozenset(
            row[0]
            for row in self.db.execute(
                "SELECT DISTINCT issue FROM events WHERE kind='task_claimed' AND at>=?",
                (instant.astimezone(UTC).isoformat(),),
            )
        )

    def attempt_count_since(self, instant: datetime) -> int:
        """Count attempts, including an explicitly reconciled retry of one issue."""
        return int(
            self.db.execute(
                "SELECT COUNT(*) FROM events WHERE kind='task_claimed' AND at>=?",
                (instant.astimezone(UTC).isoformat(),),
            ).fetchone()[0]
        )

    def model_spend_since(self, instant: datetime) -> tuple[float, bool]:
        """Return reported model spend and whether every call has a known cost."""
        rows = self.db.execute(
            "SELECT kind,detail FROM events WHERE at>=? AND kind IN ('agent_started','agent_usage') ORDER BY id",
            (instant.astimezone(UTC).isoformat(),),
        )
        started = reported = 0
        total = 0.0
        complete = True
        for row in rows:
            if row["kind"] == "agent_started":
                started += 1
                continue
            cost = json.loads(row["detail"]).get("cost_usd")
            if type(cost) not in {int, float} or not math.isfinite(cost) or cost < 0:
                complete = False
                continue
            reported += 1
            total += float(cost)
        return total, complete and started == reported

    def unknown_cost_calls_since(self, instant: datetime) -> int:
        """Count started model calls with no usable reported cost."""
        rows = self.db.execute(
            "SELECT kind,detail FROM events WHERE at>=? AND kind IN ('agent_started','agent_usage') ORDER BY id",
            (instant.astimezone(UTC).isoformat(),),
        )
        started = known = 0
        for row in rows:
            if row["kind"] == "agent_started":
                started += 1
                continue
            cost = json.loads(row["detail"]).get("cost_usd")
            if type(cost) in {int, float} and math.isfinite(cost) and cost >= 0:
                known += 1
        return max(0, started - known)

    def tasks(self, limit: int = 100) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM tasks ORDER BY rowid DESC LIMIT ?", (limit,)))

    def phase_started(self, run: str, issue: int, phase: str) -> datetime | None:
        rows = self.db.execute(
            "SELECT at,detail FROM events WHERE run_id=? AND issue=? AND kind='phase' ORDER BY id DESC",
            (run, issue),
        )
        for row in rows:
            if json.loads(row["detail"]).get("phase") == phase:
                return datetime.fromisoformat(row["at"])
        return None

    def finish(self, run: str) -> None:
        self.db.execute(
            "UPDATE runs SET phase='finished', updated_at=? WHERE id=?", (self.now(), run)
        )
        self.db.commit()
        self.event(run, None, "run_finished", {})

    def fail(self, run: str, reason: str) -> None:
        # Release the one-per-day reservation so the next tick can retry.
        # Claimed tasks retain their own state for reconciliation.
        self.db.execute(
            "UPDATE runs SET phase='failed', schedule_date=NULL, updated_at=? WHERE id=?",
            (self.now(), run),
        )
        self.db.commit()
        self.event(run, None, "run_failed", {"reason": reason})


@contextlib.contextmanager
def exclusive(directory: Path, allow_overlap: bool = False) -> Iterator[None]:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)
    file = (directory / "orchestrator.lock").open("a+")
    try:
        if not allow_overlap:
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise LockBusyError("Another workflow run holds the local lock") from error
        yield
    finally:
        if not allow_overlap:
            fcntl.flock(file, fcntl.LOCK_UN)
        file.close()
