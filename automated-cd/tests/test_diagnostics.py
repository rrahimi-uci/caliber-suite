import json
import sqlite3

from autocd.diagnostics import (
    failure_code,
    latest_diagnostic,
    latest_run_diagnostic,
    main,
    run_failure_code,
)
from autocd.state import Store


def test_failure_codes_never_echo_private_text():
    assert failure_code(None) == "none"
    assert failure_code("Developer blocker: SECRET") == "developer_blocker"
    assert failure_code("Review/QA did not pass within 3 cycles: SECRET") == "review_qa_exhausted"
    assert failure_code("reviewer returned invalid JSON SECRET") == "reviewer_output"
    assert failure_code("qa returned an invalid verdict SECRET") == "qa_output"
    assert failure_code("Issue-specific QA commands are required SECRET") == "issue_qa_missing"
    assert failure_code("Developer changed a path SECRET") == "scope_violation"
    assert failure_code("Unattended merge cannot change SECRET") == "protected_path"
    assert failure_code("QA commands changed SECRET") == "qa_mutation"
    assert failure_code("Developer produced no changed files SECRET") == "no_changes"
    assert failure_code("Claim did not persist SECRET") == "claim_failure"
    assert failure_code("unexpected SECRET") == "other"
    assert run_failure_code(None) == "none"
    assert (
        run_failure_code("Explicit retry was not selected by planner or daily attempt cap SECRET")
        == "retry_unselected_or_cap"
    )
    assert run_failure_code("planner failed (exit 1): SECRET") == "planner_error"
    assert run_failure_code("unexpected SECRET") == "other"


def test_run_diagnostic_reports_pre_task_failure_without_private_reason(
    tmp_path, monkeypatch, capsys
):
    store = Store(tmp_path)
    assert latest_run_diagnostic(store.path, "America/Los_Angeles") == {
        "phase": "none",
        "failure_code": "none",
        "claims_today": 0,
    }
    store.start("old")
    store.add_task("old", 821, "Pilot", "branch", tmp_path / "worktree")
    store.phase("old", 821, "blocked", failure="SECRET")
    store.fail("old", "SECRET")
    store.start("new")
    store.fail("new", "Explicit retry was not selected by planner or daily attempt cap SECRET")
    result = latest_run_diagnostic(store.path, "America/Los_Angeles")
    assert result == {
        "phase": "failed",
        "failure_code": "retry_unselected_or_cap",
        "claims_today": 1,
    }
    assert "SECRET" not in json.dumps(result)
    assert latest_diagnostic(store.path, latest_run_only=True) == {"status": "no_tasks"}
    assert latest_diagnostic(store.path) == {
        "issue": 821,
        "phase": "blocked",
        "failure_code": "other",
        "cycles": 0,
        "has_pr": False,
        "has_pushed_head": False,
    }
    monkeypatch.setenv("AUTOCD_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AUTOCD_TIMEZONE", "America/Los_Angeles")
    main()
    printed = capsys.readouterr().out
    assert json.loads(printed) == {
        "issue": 821,
        "phase": "blocked",
        "failure_code": "other",
        "cycles": 0,
        "has_pr": False,
        "has_pushed_head": False,
        "run": result,
    }
    assert "SECRET" not in printed


def test_latest_diagnostic_emits_bounded_structured_summary(tmp_path, monkeypatch, capsys):
    directory = tmp_path / "state"
    store = Store(directory)
    assert latest_diagnostic(store.path) == {"status": "no_tasks"}
    store.start("run")
    store.add_task("run", 821, "Pilot", "branch", tmp_path / "worktree")
    store.cycle("run", 821)
    store.event(
        "run",
        821,
        "qa_commands",
        [
            {"command": "SECRET", "passed": False},
            {"command": "safe", "passed": True},
        ],
    )
    store.event(
        "run",
        821,
        "review_cycle",
        {"cycle": 1, "result": {"approved": False, "findings": ["SECRET"]}},
    )
    store.event("run", 821, "qa_verdict", {"approved": False, "gaps": ["SECRET"]})
    store.phase("run", 821, "blocked", failure="Review/QA did not pass within 3 cycles: SECRET")
    monkeypatch.setenv("AUTOCD_STATE_DIR", str(directory))
    main()
    printed = capsys.readouterr().out
    assert "SECRET" not in printed
    assert json.loads(printed) == {
        "issue": 821,
        "phase": "blocked",
        "failure_code": "review_qa_exhausted",
        "cycles": 1,
        "has_pr": False,
        "has_pushed_head": False,
        "review": {"approved": False, "finding_count": 1},
        "qa": {"approved": False, "finding_count": 1},
        "commands": {"total": 2, "failed": 1},
        "run": {"phase": "running", "failure_code": "none", "claims_today": 1},
    }


def test_diagnostic_unknown_phase_and_malformed_verdicts(tmp_path):
    store = Store(tmp_path)
    store.start("run")
    store.add_task("run", 1, "Pilot", "branch", tmp_path / "worktree")
    store.event("run", 1, "review_cycle", {"result": {"findings": "SECRET"}})
    store.event("run", 1, "qa_verdict", {"gaps": "SECRET"})
    store.event("run", 1, "qa_commands", {"unexpected": "SECRET"})
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE tasks SET phase='SECRET', pr=1, head='SECRET' WHERE issue=1")
    result = latest_diagnostic(store.path)
    assert result["phase"] == "unknown"
    assert result["has_pr"] is True
    assert result["has_pushed_head"] is True
    assert result["review"] == {"approved": False, "finding_count": -1}
    assert result["qa"] == {"approved": False, "finding_count": -1}
    assert result["commands"] == {"total": -1, "failed": -1}
    assert "SECRET" not in json.dumps(result)
