from __future__ import annotations

import json
from pathlib import Path

from autocd.state import Store
from autocd.usage_report import execution_report, main


def test_execution_report_includes_planner_failed_spend_and_completed_task(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    store = Store(tmp_path)
    store.start("first")
    store.event("first", None, "agent_started", {"role": "planner"})
    store.event("first", None, "agent_usage", {"role": "planner", "cost_usd": 0.2})
    store.add_task("first", 821, "release:Pilot", "trial/821", tmp_path)
    store.event("first", 821, "agent_started", {"role": "developer"})
    store.event("first", 821, "agent_usage", {"role": "developer", "cost_usd": 1.3})
    store.phase("first", 821, "blocked")
    store.finish("first")
    store.start("second")
    store.add_task("second", 522, "release:Beta", "trial/522", tmp_path)
    store.event("second", 522, "agent_started", {"role": "developer"})
    store.event("second", 522, "agent_usage", {"role": "developer", "cost_usd": 2.5})
    store.phase("second", 522, "done")
    store.finish("second")
    store.db.close()
    report = execution_report(tmp_path / "workflow.sqlite3", "12345")
    assert report["status"] == "measured"
    assert report["task_attempts"] == 2
    assert report["completed_tasks"] == 1
    assert report["claude_calls_started"] == report["claude_calls_with_cost"] == 3
    assert report["reported_model_cost_usd"] == 4.0
    assert report["cost_per_completed_task_usd"] == 4.0
    assert report["planner_cost_usd"] == 0.2
    assert [
        {key: task[key] for key in ("issue", "phase", "reported_model_cost_usd")}
        for task in report["tasks"]
    ] == [
        {"issue": 821, "phase": "blocked", "reported_model_cost_usd": 1.3},
        {"issue": 522, "phase": "done", "reported_model_cost_usd": 2.5},
    ]
    assert all(task["elapsed_seconds"] >= 0 for task in report["tasks"])
    assert report["coordinator_elapsed_seconds"] >= 0


def test_execution_report_marks_missing_cost_and_excludes_other_github_runs(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    store = Store(tmp_path)
    store.start("first")
    store.add_task("first", 821, "release:Pilot", "trial/821", tmp_path)
    store.event("first", 821, "agent_started", {"role": "developer"})
    store.event("first", 821, "agent_usage", {"role": "developer", "cost_usd": None})
    store.phase("first", 821, "done")
    monkeypatch.setenv("GITHUB_RUN_ID", "99999")
    store.start("unrelated")
    store.event("unrelated", None, "agent_started", {"role": "planner"})
    store.event("unrelated", None, "agent_usage", {"role": "planner", "cost_usd": 20.0})
    store.db.close()
    report = execution_report(tmp_path / "workflow.sqlite3", "12345")
    assert report["status"] == "partial_cost_data"
    assert report["reported_model_cost_usd"] == 0
    assert report["cost_per_completed_task_usd"] is None
    assert report["claude_calls_started"] == 1
    assert report["claude_calls_with_cost"] == 0
    assert execution_report(tmp_path / "workflow.sqlite3", "77777")["status"] == "no_task_runs"


def test_execution_report_excludes_same_run_id_from_prior_attempt(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    store = Store(tmp_path)
    store.start("first-attempt")
    store.event("first-attempt", None, "agent_started", {"role": "planner"})
    store.event("first-attempt", None, "agent_usage", {"role": "planner", "cost_usd": 9.0})
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    store.start("rerun")
    store.event("rerun", None, "agent_started", {"role": "planner"})
    store.event("rerun", None, "agent_usage", {"role": "planner", "cost_usd": 1.0})
    store.db.close()
    report = execution_report(tmp_path / "workflow.sqlite3", "12345", "2")
    assert report["github_run_attempt"] == "2"
    assert report["reported_model_cost_usd"] == 1.0
    assert report["claude_calls_started"] == 1


def test_execution_report_rejects_non_numeric_run_id(tmp_path: Path):
    try:
        execution_report(tmp_path / "workflow.sqlite3", "secret")
    except ValueError as error:
        assert "Numeric GitHub run ID" in str(error)
    else:
        raise AssertionError("Expected invalid run ID rejection")


def test_usage_report_entrypoint_prints_bounded_data_only(tmp_path: Path, monkeypatch, capsys):
    Store(tmp_path).db.close()
    monkeypatch.setenv("AUTOCD_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    main()
    assert json.loads(capsys.readouterr().out) == {
        "github_run_id": "12345",
        "github_run_attempt": "1",
        "status": "no_task_runs",
    }
