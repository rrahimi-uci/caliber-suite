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
    assert failure_code("reviewer returned invalid JSON SECRET") == "reviewer_output_not_json"
    assert failure_code("qa returned an invalid verdict schema SECRET") == "qa_output_schema"
    assert failure_code("qa returned something else SECRET") == "qa_output"
    assert (
        failure_code("reviewer returned an error (error_max_turns)") == "reviewer_error_max_turns"
    )
    assert failure_code("Issue-specific QA commands are required SECRET") == "issue_qa_missing"
    assert failure_code("Developer changed a path SECRET") == "scope_violation"
    assert failure_code("Unattended merge cannot change SECRET") == "protected_path"
    assert failure_code("QA commands changed SECRET") == "qa_mutation"
    assert failure_code("Developer produced no changed files SECRET") == "no_changes"
    assert failure_code("Claim did not persist SECRET") == "claim_failure"
    assert failure_code("unexpected SECRET") == "other"
    # Failures that used to collapse into "other" are named without echoing text.
    for text, code in (
        ("reviewer could not start: E2BIG", "agent_start_failed"),
        ("reviewer timed out", "agent_timeout"),
        ("developer stalled without output", "agent_stalled"),
        ("qa failed (exit 3): SECRET", "agent_exit"),
        ("reviewer emitted an oversized stream line", "agent_output_oversized"),
        ("developer returned no JSON object", "agent_output_not_json"),
        ("developer returned invalid JSON", "agent_output_not_json"),
        ("developer returned a non-object JSON value", "agent_output_not_json"),
        ("developer returned an invalid selection schema", "agent_output_schema"),
        ("developer returned something unforeseen SECRET", "agent_output"),
        ("developer returned an error", "agent_error"),
        ("developer returned an error (error_max_turns)", "agent_error_max_turns"),
        ("developer returned an error (error_during_execution)", "agent_error_during_execution"),
        # A subtype outside the fixed token shape never reaches a published code.
        ("developer returned an error (SECRET text)", "agent_error"),
        ("Daily Claude spend cannot be verified; stopping safely", "budget_unverifiable"),
        ("Daily Claude budget exhausted ($30.00)", "budget_exhausted"),
        ("Setup failed: uv pip install SECRET (exit 1)", "setup_failed"),
        ("Documentation sync failed (exit 1)", "docs_sync_failed"),
        ("Source diff exceeds reviewer context limit; split task", "diff_too_large"),
        ("Candidate diff exceeds integrity comparison limit; split task", "diff_too_large"),
        ("Interrupted before PR; inspect worktree", "interrupted"),
    ):
        assert failure_code(text) == code
        assert run_failure_code(text) == code
        assert "SECRET" not in code
    # Established labels keep precedence over the broader agent classification.
    assert failure_code("reviewer returned an error") == "reviewer_error"
    assert run_failure_code("planner timed out") == "planner_timeout"
    assert run_failure_code("planner stalled without output") == "planner_stall"
    assert run_failure_code("planner failed (exit 1): SECRET") == "planner_error"
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


def test_decision_diagnostic_exposes_metadata_without_model_prose(tmp_path):
    store = Store(tmp_path)
    store.start("run")
    store.event(
        "run",
        1086,
        "decision_proposal",
        {
            "fingerprint": "a" * 64,
            "gate": "H04: start",
            "packet": {
                "options": [{"id": "a"}, {"id": "b"}],
                "recommendation": "a",
                "rationale": "SECRET model text",
            },
        },
    )
    result = latest_diagnostic(store.path)
    assert result == {
        "status": "no_tasks",
        "decision_proposals": [
            {"issue": 1086, "status": "proposal_ready", "gate": "H04: start", "option_count": 2}
        ],
    }
    assert "SECRET" not in json.dumps(result)


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
        "trail": [
            {
                "handoff_gap": False,
                "checks": {"ran": 2, "failed": [{"check": "other", "reason": "failed"}]},
                "review": {
                    "approved": False,
                    "finding_count": 1,
                    "severity": {"unrated": 1},
                    "blocking": 1,
                },
                "qa": {
                    "approved": False,
                    "finding_count": 1,
                    "severity": {"unrated": 1},
                    "blocking": 1,
                },
            }
        ],
        "run": {"phase": "running", "failure_code": "none", "claims_today": 1},
    }


def test_cycle_trail_names_each_cycle_without_private_text(tmp_path):
    store = Store(tmp_path)
    store.start("run")
    store.add_task("run", 7, "Pilot", "branch", tmp_path / "worktree")
    store.event("run", 7, "handoff_gap", {"missing": ["SECRET"]})
    store.event("run", 7, "developer_result", {"changed": ["SECRET.py"], "summary": "SECRET"})
    store.event(
        "run",
        7,
        "qa_commands",
        [
            {"command": "git diff --check", "passed": True},
            {"command": "node .agents/bin/check.mjs", "passed": True},
            {
                "command": "scripts/ci-local.sh docs-validation lint test-smoke SECRET;",
                "passed": False,
                "exit_code": 1,
                "output": "SECRET\nFAIL  test-smoke  3s\nFAIL  SECRET\nPASS  lint 1s",
            },
        ],
    )
    store.event(
        "run",
        7,
        "review_cycle",
        {
            "cycle": 2,
            "result": {
                "approved": False,
                "findings": [
                    {"severity": "P1", "blocking": True, "detail": "SECRET"},
                    {"severity": "P3", "blocking": False, "detail": "SECRET"},
                    {"severity": "SECRET"},
                ],
            },
        },
    )
    store.event("run", 7, "developer_result", {"changed": [], "summary": "", "fix_attempt": 1})
    store.event(
        "run",
        7,
        "qa_commands",
        [
            {
                "command": "caliber/.venv/bin/python -m pytest caliber/tests/test_x.py --cov=caliber",
                "passed": False,
                "output": "SECRET\nCoverage for caliber/src/caliber/x.py is not greater than 95%",
            }
        ],
    )
    store.event("run", 7, "review_cycle", {"cycle": 3, "result": {"findings": "SECRET"}})
    store.event("run", 7, "qa_verdict", {"approved": True, "gaps": []})
    trail = latest_diagnostic(store.path)["trail"]
    assert trail == [
        {"handoff_gap": True},
        {
            "handoff_gap": False,
            "checks": {
                "ran": 3,
                "failed": [
                    {
                        "check": "ci-local:docs-validation+lint+test-smoke",
                        "reason": "exit_1",
                        "jobs": ["test-smoke"],
                    }
                ],
            },
            "review": {
                "approved": False,
                "finding_count": 3,
                "severity": {"P1": 1, "P3": 1, "unrated": 1},
                "blocking": 2,
            },
        },
        {
            "handoff_gap": False,
            "fix_attempt": 1,
            "checks": {"ran": 1, "failed": [{"check": "coverage", "reason": "coverage_threshold"}]},
            "review": {"approved": False, "finding_count": -1},
            "qa": {"approved": True, "finding_count": 0, "severity": {}, "blocking": 0},
        },
    ]
    assert "SECRET" not in json.dumps(trail)


def test_check_labels_and_reasons_are_fixed():
    from autocd.diagnostics import check_failure, check_label

    assert check_label(None) == "other"
    assert check_label("changed-module coverage") == "coverage-selection"
    assert check_label("affected-component checks") == "affected-component"
    assert check_label("make SECRET") == "other"
    for output, reason in (
        ("QA command timed out after 7200 seconds", "timeout"),
        ("Coverage JSON missing", "coverage_missing"),
        ("Invalid coverage JSON: KeyError", "coverage_invalid"),
        ("x\nRequired CI parity job was skipped", "parity_skipped"),
        (
            "No backend test references the changed production module(s): SECRET",
            "no_selected_tests",
        ),
        ("Issue-specific QA commands are required for: SECRET", "issue_qa_missing"),
        ("Invalid command", "invalid_command"),
    ):
        assert check_failure({"command": "x", "output": output})["reason"] == reason
    assert check_failure({"command": "x", "exit_code": 9999, "output": 3})["reason"] == "failed"
    assert check_failure({"command": "scripts/ci-local.sh lint", "output": "SECRET"}) == {
        "check": "ci-local:lint",
        "reason": "failed",
    }


def test_cycle_trail_tolerates_events_before_a_developer_result(tmp_path):
    store = Store(tmp_path)
    store.start("run")
    store.add_task("run", 7, "Pilot", "branch", tmp_path / "worktree")
    store.event("run", 7, "qa_commands", [{"command": "git diff --check", "passed": True}])
    assert latest_diagnostic(store.path)["trail"] == [
        {"handoff_gap": False, "checks": {"ran": 1, "failed": []}}
    ]


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
