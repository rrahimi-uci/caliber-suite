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
    store.phase("run1", 12, "ready")
    assert [row["issue"] for row in store.pending()] == [12]
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
