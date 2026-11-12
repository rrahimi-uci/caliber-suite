import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from autocd.state import Store, exclusive


def test_store_transitions_and_schedule_dedup(tmp_path: Path):
    store = Store(tmp_path)
    assert store.start("run1", "2026-11-05")
    assert not store.start("run2", "2026-11-05")
    assert store.scheduled("2026-11-05")
    store.add_task("run1", 12, "release:Beta", "autocd/12", tmp_path / "worktree")
    assert store.task("run1", 12)["phase"] == "claimed"
    assert store.cycle("run1", 12) == 1
    store.pr("run1", 12, 99)
    store.head("run1", 12, "a" * 40)
    store.phase("run1", 12, "ci_wait")
    assert store.phase_started("run1", 12, "ci_wait") is not None
    assert store.phase_started("run1", 12, "nonexistent") is None
    assert [row["issue"] for row in store.pending()] == [12]
    assert [row["issue"] for row in store.active()] == [12]
    store.phase("run1", 12, "ready")
    assert [row["issue"] for row in store.pending()] == [12]
    # A PR awaiting the owner's merge is still reconciled but no longer blocks new work.
    assert not store.active()
    store.phase("run1", 12, "done")
    assert not store.pending()
    assert [row["issue"] for row in store.tasks()] == [12]
    assert not store.tasks(limit=0)
    store.finish("run1")
    kinds = [row["kind"] for row in store.db.execute("SELECT kind FROM events ORDER BY id")]
    assert kinds == [
        "run_started",
        "task_claimed",
        "pr_opened",
        "commit_pushed",
        "phase",
        "phase",
        "phase",
        "run_finished",
    ]


def test_event_fingerprint_deduplicates_same_input_only(tmp_path: Path):
    store = Store(tmp_path)
    assert store.start("run")
    store.event("run", 821, "decision_proposal", {"fingerprint": "same"})
    store.event("run", 821, "decision_proposal", {"fingerprint": "other"})
    assert store.has_event_fingerprint(821, "decision_proposal", "same")
    assert store.has_event_fingerprint(821, "decision_proposal", "other")
    assert not store.has_event_fingerprint(821, "decision_proposal", "missing")


def test_local_lock_rejects_overlap(tmp_path: Path):
    with exclusive(tmp_path), pytest.raises(RuntimeError, match="local lock"), exclusive(tmp_path):
        pass
    with exclusive(tmp_path):
        pass


def test_missing_task_is_error(tmp_path: Path):
    with pytest.raises(KeyError):
        Store(tmp_path).task("none", 1)


def test_failed_run_releases_schedule_without_erasing_task(tmp_path: Path):
    store = Store(tmp_path)
    assert store.start("run", "2026-11-05")
    store.add_task("run", 12, "release:Beta", "branch", tmp_path / "w")
    store.fail("run", "Project unavailable")
    assert not store.scheduled("2026-11-05")
    assert store.task("run", 12)["phase"] == "claimed"
    assert store.start("retry", "2026-11-05")


def test_attempt_count_includes_retries_of_same_issue(tmp_path: Path):
    store = Store(tmp_path)
    for run in ("first", "retry"):
        assert store.start(run)
        store.add_task(run, 12, "release:Beta", f"branch-{run}", tmp_path / run)
        store.phase(run, 12, "blocked", failure="QA failed")
    since = datetime.now(UTC) - timedelta(minutes=1)
    assert store.attempted_since(since) == frozenset({12})
    assert store.attempt_count_since(since) == 2
    assert store.latest_task(12)["run_id"] == "retry"


def test_attempts_use_claim_time_across_midnight(tmp_path: Path):
    store = Store(tmp_path)
    assert store.start("late-run")
    midnight = datetime.now(UTC) - timedelta(seconds=1)
    store.db.execute("UPDATE runs SET created_at=? WHERE id='late-run'", (midnight.isoformat(),))
    store.db.commit()
    after_start = datetime.now(UTC)
    store.add_task("late-run", 821, "release:Pilot", "branch", tmp_path / "work")
    assert store.attempted_since(after_start) == frozenset({821})
    assert store.attempt_count_since(after_start) == 1


def test_model_spend_requires_a_reported_cost_for_every_call(tmp_path: Path):
    store = Store(tmp_path)
    since = datetime.now(UTC) - timedelta(minutes=1)
    assert store.start("run")
    store.event("run", None, "agent_started", {"role": "planner"})
    store.event("run", None, "agent_usage", {"role": "planner", "cost_usd": 1.25})
    assert store.model_spend_since(since) == (1.25, True)
    store.event("run", None, "agent_started", {"role": "developer"})
    store.event("run", None, "agent_usage", {"role": "developer", "cost_usd": None})
    assert store.model_spend_since(since) == (1.25, False)


def test_task_and_claim_event_are_atomic(tmp_path: Path):
    store = Store(tmp_path)
    assert store.start("run")
    store.db.execute(
        "CREATE TRIGGER reject_claim BEFORE INSERT ON events "
        "WHEN NEW.kind='task_claimed' BEGIN SELECT RAISE(ABORT, 'claim event failed'); END"
    )
    with pytest.raises(sqlite3.IntegrityError, match="claim event failed"):
        store.add_task("run", 821, "release:Pilot", "branch", tmp_path / "work")
    assert store.latest_task(821) is None
