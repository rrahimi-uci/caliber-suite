from __future__ import annotations

import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar

import pytest

from autocd.engine import Engine
from autocd.github import pr_ready
from autocd.planner import Assignment
from autocd.state import Store


class FakeGitHub:
    def __init__(self, settings, snapshot):
        self.settings = settings
        self.data = snapshot
        self.transitions = []
        self.created = []
        self.reviewers = []
        self.worktree = settings.state_dir / "fake-worktree"

    def snapshot(self):
        return self.data

    def overview(self):
        return {"open_prs": [], "recent_runs": [], "recent_commits": [], "branches": [[]]}

    def prepare_worktree(self, issue, run):
        self.worktree.mkdir(parents=True, exist_ok=True)
        self.current_branch = f"autocd/issue-{issue}-{run[:8]}"
        return self.current_branch, self.worktree

    def login(self):
        return "worker"

    def transition(self, target, issue, phase, payload):
        self.transitions.append((issue, phase, payload))
        if phase == "claim":
            self.data["items"][0]["fields"]["Readiness"] = "In Progress"

    def changed(self, worktree):
        return ["src/task.py", "tests/test_task.py"]

    def commit_and_push(self, worktree, branch, issue):
        return "f" * 40

    def create_pr(self, worktree, title, body):
        self.created.append((title, body))
        return 88

    def request_reviewers(self, worktree, pr, owner):
        self.reviewers.append((pr, owner))
        return {"owner": "unavailable: author", "copilot": "requested"}

    def record_reviewers(self, pr, status):
        self.reviewers_recorded = (pr, status)

    def pr_state(self, pr):
        return {
            "url": "https://github.com/rrahimi-uci/caliber-suite/pull/88",
            "headRefOid": "f" * 40,
            "headRefName": getattr(self, "current_branch", "branch"),
            "baseRefName": "main",
            "mergeable": "MERGEABLE",
            "isDraft": True,
            "reviewRequests": [],
            "latestReviews": [],
            "author": {"login": "rrahimi-uci"},
        }

    def required_checks(self, pr, state):
        return [{"name": "CI", "state": "PENDING"}]

    def unresolved_threads(self, pr):
        return 0

    def mark_ready_body(self, pr, checks):
        raise AssertionError("CI is pending in this test")


class FakeAgent:
    calls: ClassVar[list[tuple[str, bool]]] = []
    review_count = 0

    def __init__(self, settings, event):
        self.event = event

    def run(self, role, prompt, cwd, editable=False):
        self.calls.append((role, editable))
        self.event("fake_agent", {"role": role})
        if role == "planner":
            return {"selected": [12], "reason": "eligible"}
        if role == "developer":
            return {
                "summary": "Implemented contract",
                "decisions": "Used existing interface",
                "risks": "No known runtime risk",
                "compatibility": "Compatible with current callers",
                "migrations": "None",
                "documentation": "Updated task docs",
                "deployment": "No deployment",
                "tests_suggested": "Run contract tests",
                "tests_run": "Contract tests passed",
                "remaining_work": "None",
                "blocker": None,
            }
        if role == "reviewer":
            self.review_count += 1
            if self.review_count == 1:
                return {"approved": False, "findings": [{"detail": "Missing edge case"}]}
            return {"approved": True, "findings": []}
        if role == "qa":
            return {"approved": True, "gaps": []}
        raise AssertionError(role)


def test_engine_review_qa_pr_lifecycle(settings, snapshot, monkeypatch):
    data, _ = snapshot
    settings = replace(settings, max_review_cycles=2)
    github = FakeGitHub(settings, data)
    FakeAgent.calls = []
    FakeAgent.review_count = 0
    engine = Engine(settings, github=github, agent_factory=FakeAgent)
    monkeypatch.setattr(engine, "_diff", lambda worktree: "complete diff")
    monkeypatch.setattr(
        engine,
        "_checks",
        lambda issue, worktree: [
            {"command": "pytest --cov=task", "passed": True, "output": "100%"}
        ],
    )
    result = engine.run()
    assert result["results"][0]["status"] == "waiting_for_ci_or_review"
    assert [role for role, _ in FakeAgent.calls] == [
        "planner",
        "developer",
        "reviewer",
        "developer",
        "reviewer",
        "qa",
    ]
    assert github.transitions[0][1] == "claim"
    assert github.transitions[1][1] == "review"
    assert github.reviewers == [(88, "worker")]
    assert github.reviewers_recorded[0] == 88
    title, body = github.created[0]
    assert "Task 12" in title
    assert "## Summary" in body and "## Copilot handoff" in body
    assert "Implemented contract" in body and "pytest --cov=task" in body
    store = Store(settings.state_dir)
    assert store.task(result["run"], 12)["phase"] == "ci_wait"
    assert len(list(store.db.execute("SELECT * FROM events WHERE kind='review_cycle'"))) == 2


def test_interrupted_edit_blocks_but_ci_wait_resumes(settings, snapshot):
    data, _ = snapshot
    github = FakeGitHub(settings, data)
    engine = Engine(settings, github=github, agent_factory=FakeAgent)
    store = Store(settings.state_dir)
    assert store.start("old")
    store.add_task("old", 12, "release:Beta", "branch", settings.state_dir / "w")
    store.phase("old", 12, "developing")
    assert engine._recover(store)[0]["status"] == "blocked_after_interruption"
    assert store.task("old", 12)["phase"] == "blocked"
    assert store.start("new")
    store.add_task("new", 13, "release:Beta", "branch2", settings.state_dir / "w2")
    store.pr("new", 13, 88)
    store.head("new", 13, "f" * 40)
    store.phase("new", 13, "ci_wait")
    github.current_branch = "branch2"
    assert engine._recover(store)[0]["status"] == "waiting_for_ci_or_review"
    assert store.task("new", 13)["phase"] == "ci_wait"


def test_due_date_uses_configured_timezone(settings):
    engine = Engine(settings)
    assert engine.due_date(datetime(2026, 11, 5, 16, 59, tzinfo=UTC)) is None
    assert engine.due_date(datetime(2026, 11, 5, 17, 0, tzinfo=UTC)) == "2026-11-05"


def test_unconfigured_task_is_not_executed(settings, snapshot):
    data, _ = snapshot
    settings = replace(settings, issue_checks={})
    engine = Engine(settings, github=FakeGitHub(settings, data), agent_factory=FakeAgent)
    result = engine.run()
    assert result["eligible"] == 3 and result["configured"] == 0
    assert result["results"] == []


def test_owned_path_guard(settings):
    assert Engine._in_owned_paths("src/task.py", frozenset({"src/task.py"}))
    assert Engine._in_owned_paths("src/sub/a.py", frozenset({"src"}))
    assert not Engine._in_owned_paths("src-other/a.py", frozenset({"src"}))


def test_pr_readiness_rejects_gaps():
    state = {"mergeable": "MERGEABLE", "reviewRequests": [{"login": "owner"}], "latestReviews": []}
    assert pr_ready(state, [{"state": "PASS"}], 0, "owner", "worker") == (True, [])
    ok, reasons = pr_ready(
        {
            **state,
            "mergeable": "CONFLICTING",
            "reviewRequests": [],
            "latestReviews": [{"state": "CHANGES_REQUESTED"}],
        },
        [{"state": "SKIPPING"}],
        2,
        "owner",
        "worker",
    )
    assert not ok and len(reasons) == 5
    assert pr_ready(state, [], 0, "owner", "owner")[0] is False
    assert pr_ready(
        {
            **state,
            "reviewRequests": [],
            "latestReviews": [{"state": "APPROVED", "author": {"login": "owner"}}],
        },
        [{"state": "PASS"}],
        0,
        "owner",
        "worker",
    )[0]


def test_checks_require_coverage_and_reject_shell(settings, tmp_path: Path, monkeypatch):
    engine = Engine(settings)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    assert not engine._checks(12, worktree)[0]["passed"]  # configured base command absent
    settings2 = replace(
        settings,
        base_checks=(f"{sys.executable} -c 'print(1)' | cat",),
        issue_checks={
            12: (f"{sys.executable} -m pytest --cov=autocd --cov-fail-under=95 --version",)
        },
    )
    assert engine.__class__(settings2)._checks(12, worktree)[0]["output"] == "Invalid command"
    settings3 = replace(
        settings,
        base_checks=(f"{sys.executable} -c 'print(1)'",),
        issue_checks={
            12: (f"{sys.executable} -m pytest --cov=autocd --cov-fail-under=95 --version",)
        },
    )

    class Result:
        returncode = 0
        stdout = "TOTAL 10 0 100%\n"
        stderr = ""

    monkeypatch.setattr("autocd.engine.subprocess.run", lambda *a, **k: Result())
    assert all(item["passed"] for item in engine.__class__(settings3)._checks(12, worktree))


def test_coverage_command_rejects_weak_or_malformed_gates():
    assert Engine._coverage_command("python -m pytest tests --cov=app --cov-fail-under=95")
    assert Engine._coverage_command("pytest tests --cov app --cov-fail-under 96")
    assert not Engine._coverage_command("pytest tests --cov=app --cov-fail-under=94")
    assert not Engine._coverage_command("python -m pytest tests --cov=app")
    assert not Engine._coverage_command("echo pytest --cov=app --cov-fail-under=oops")
    assert not Engine._coverage_command("pytest 'unterminated")


def test_checks_fail_without_coverage_or_missing_executable(settings, tmp_path: Path):
    no_cov = replace(settings, base_checks=(), issue_checks={12: ("echo hello",)})
    assert Engine(no_cov)._checks(12, tmp_path)[0]["command"] == "coverage"
    missing = replace(settings, base_checks=("does-not-exist",))
    assert not Engine(missing)._checks(12, tmp_path)[0]["passed"]


def test_setup_runs_once_and_records_failure(settings, tmp_path: Path, monkeypatch):
    from autocd.state import Store

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    store = Store(settings.state_dir)
    store.start("run")
    settings2 = replace(settings, setup_commands=("uv venv caliber/.venv",))
    engine = Engine(settings2)

    class Proc:
        returncode = 0
        stdout = "installed"
        stderr = ""

    calls = []
    monkeypatch.setattr(
        "autocd.engine.subprocess.run", lambda *a, **k: (calls.append(a), Proc())[1]
    )
    engine._setup(worktree, store, "run", 12)
    assert len(calls) == 1
    assert store.db.execute("SELECT 1 FROM events WHERE kind='setup_command'").fetchone()
    (worktree / "caliber/.venv/bin").mkdir(parents=True)
    (worktree / "caliber/.venv/bin/python").touch()
    engine._setup(worktree, store, "run", 12)
    assert len(calls) == 1


def test_setup_rejects_bad_command_and_failure(settings, tmp_path: Path, monkeypatch):
    from autocd.state import Store

    store = Store(settings.state_dir)
    store.start("run")
    invalid = Engine(replace(settings, setup_commands=("uv venv x | cat",)))
    with pytest.raises(RuntimeError, match="Invalid setup"):
        invalid._setup(tmp_path, store, "run", 12)

    class Proc:
        returncode = 2
        stdout = ""
        stderr = "failed"

    monkeypatch.setattr("autocd.engine.subprocess.run", lambda *a, **k: Proc())
    failing = Engine(replace(settings, setup_commands=("uv venv x",)))
    with pytest.raises(RuntimeError, match="Setup failed"):
        failing._setup(tmp_path, store, "run", 12)


def test_ci_parity_skip_fails_qa(settings, tmp_path: Path, monkeypatch):
    settings2 = replace(
        settings,
        base_checks=("scripts/ci-local.sh test",),
        issue_checks={12: ("python -m pytest x --cov=x --cov-fail-under=95",)},
    )

    class Proc:
        returncode = 0
        stdout = "PASS  lint\nSKIP  test  missing environment\n"
        stderr = ""

    monkeypatch.setattr("autocd.engine.subprocess.run", lambda *a, **k: Proc())
    row = Engine(settings2)._checks(12, tmp_path)[0]
    assert row["passed"] is False and "skipped" in row["output"]


def test_scheduled_disabled_and_deduplicated(settings, snapshot, monkeypatch):
    data, _ = snapshot
    github = FakeGitHub(settings, data)
    assert Engine(settings, github=github).run(scheduled=True) == {"status": "disabled"}
    enabled = replace(settings, enabled=True, issue_checks={})
    engine = Engine(enabled, github=github)
    monkeypatch.setattr(engine, "due_date", lambda: "2026-11-05")
    assert engine.run(scheduled=True)["results"] == []
    assert engine.run(scheduled=True)["status"] == "not_due_or_already_started"
    monkeypatch.setattr(engine, "due_date", lambda: None)
    assert engine.run(scheduled=True)["status"] == "not_due_or_already_started"


def test_claim_must_be_visible(settings, snapshot):
    data, _ = snapshot
    github = FakeGitHub(settings, data)
    github.transition = lambda *a, **k: None
    engine = Engine(settings, github=github, agent_factory=FakeAgent)
    store = Store(settings.state_dir)
    store.start("run")
    task = Assignment(12, "Task 12", "contract", "release:Beta", frozenset({"src/task.py"}), 1)
    result = engine._task("run", task)
    assert result["status"] == "blocked"
    assert "Claim did not persist" in result["error"]
    assert store.task("run", 12)["phase"] == "blocked"


def test_developer_blocker_and_path_violation(settings, snapshot, monkeypatch):
    data, _ = snapshot
    github = FakeGitHub(settings, data)
    store = Store(settings.state_dir)
    store.start("run")

    class BlockerAgent(FakeAgent):
        def run(self, role, prompt, cwd, editable=False):
            if role == "developer":
                return {"blocker": "missing producer"}
            return super().run(role, prompt, cwd, editable)

    task = Assignment(12, "Task 12", "contract", "release:Beta", frozenset({"src/task.py"}), 1)
    engine = Engine(settings, github=github, agent_factory=BlockerAgent)
    assert "missing producer" in engine._task("run", task)["error"]
    store.start("run2")
    data["items"][0]["fields"]["Readiness"] = "Ready"
    github.changed = lambda _: ["unowned/file.py"]
    engine = Engine(settings, github=github, agent_factory=FakeAgent)
    assert "outside" in engine._task("run2", task)["error"]


def test_incomplete_handoff_and_no_change_block_safely(settings, snapshot):
    data, _ = snapshot
    settings = replace(settings, max_review_cycles=1)
    github = FakeGitHub(settings, data)

    class IncompleteAgent(FakeAgent):
        def run(self, role, prompt, cwd, editable=False):
            if role == "developer":
                return {"summary": "partial", "blocker": None}
            return super().run(role, prompt, cwd, editable)

    store = Store(settings.state_dir)
    store.start("run")
    task = Assignment(12, "Task 12", "contract", "release:Beta", frozenset(), 1)
    engine = Engine(settings, github=github, agent_factory=IncompleteAgent)
    assert "handoff omitted" in engine._task("run", task)["error"]
    assert store.db.execute("SELECT kind FROM events WHERE kind='handoff_gap'").fetchone()

    data["items"][0]["fields"]["Readiness"] = "Ready"
    store.start("run2")
    github.changed = lambda _: []
    engine = Engine(settings, github=github, agent_factory=FakeAgent)
    assert "no changed files" in engine._task("run2", task)["error"]


def test_review_qa_failure_exhaustion(settings, snapshot, monkeypatch):
    data, _ = snapshot
    settings = replace(settings, max_review_cycles=1)
    github = FakeGitHub(settings, data)
    FakeAgent.review_count = 0
    engine = Engine(settings, github=github, agent_factory=FakeAgent)
    monkeypatch.setattr(engine, "_diff", lambda _: "diff")
    store = Store(settings.state_dir)
    store.start("run")
    task = Assignment(
        12,
        "Task 12",
        "contract",
        "release:Beta",
        frozenset({"src/task.py", "tests/test_task.py"}),
        1,
    )
    assert "did not pass" in engine._task("run", task)["error"]

    class QARejectAgent(FakeAgent):
        def run(self, role, prompt, cwd, editable=False):
            if role == "reviewer":
                return {"approved": True, "findings": []}
            if role == "qa":
                return {"approved": False, "gaps": ["coverage missing"]}
            return super().run(role, prompt, cwd, editable)

    data["items"][0]["fields"]["Readiness"] = "Ready"
    store.start("run2")
    engine = Engine(settings, github=github, agent_factory=QARejectAgent)
    monkeypatch.setattr(engine, "_diff", lambda _: "diff")
    monkeypatch.setattr(engine, "_checks", lambda *a: [{"passed": True, "command": "pytest --cov"}])
    assert "QA gaps" in engine._task("run2", task)["error"]


def test_qa_command_failure_returns_to_developer(settings, snapshot, monkeypatch):
    data, _ = snapshot
    settings = replace(settings, max_review_cycles=1)
    github = FakeGitHub(settings, data)

    class ApproveReview(FakeAgent):
        def run(self, role, prompt, cwd, editable=False):
            if role == "reviewer":
                return {"approved": True, "findings": []}
            return super().run(role, prompt, cwd, editable)

    engine = Engine(settings, github=github, agent_factory=ApproveReview)
    monkeypatch.setattr(engine, "_diff", lambda _: "diff")
    monkeypatch.setattr(engine, "_checks", lambda *a: [{"passed": False, "command": "pytest"}])
    store = Store(settings.state_dir)
    store.start("run")
    task = Assignment(
        12,
        "Task 12",
        "contract",
        "release:Beta",
        frozenset({"src/task.py", "tests/test_task.py"}),
        1,
    )
    assert "QA command failures" in engine._task("run", task)["error"]


def test_worktree_failure_and_changed_pr_head(settings, snapshot):
    data, _ = snapshot
    github = FakeGitHub(settings, data)
    github.prepare_worktree = lambda *a: (_ for _ in ()).throw(RuntimeError("git failed"))
    engine = Engine(settings, github=github)
    store = Store(settings.state_dir)
    store.start("run")
    task = Assignment(12, "Task 12", "contract", "release:Beta", frozenset(), 1)
    assert "git failed" in engine._task("run", task)["error"]
    assert store.db.execute("SELECT kind FROM events WHERE kind='worktree_failure'").fetchone()
    store.start("run2")
    store.add_task("run2", 12, "release:Beta", "branch", settings.state_dir / "worktree")
    store.head("run2", 12, "wrong")
    store.pr("run2", 12, 88)
    assert engine._check_pr(store, "run2", 12, 88)["status"] == "blocked_changed_head"


def test_pr_ready_marks_open_without_merge(settings, snapshot, monkeypatch):
    data, _ = snapshot
    github = FakeGitHub(settings, data)
    github.required_checks = lambda pr, state: [{"name": "CI", "state": "PASS"}]
    github.mark_ready_body = lambda pr, checks: setattr(github, "marked", (pr, checks))
    engine = Engine(settings, github=github)
    commands = []
    monkeypatch.setattr("autocd.engine.command", lambda args, cwd: commands.append(args))
    store = Store(settings.state_dir)
    store.start("run")
    store.add_task("run", 12, "release:Beta", "branch", settings.state_dir / "w")
    store.head("run", 12, "f" * 40)
    store.pr("run", 12, 88)
    result = engine._check_pr(store, "run", 12, 88)
    assert result["status"] == "ready_for_owner"
    assert github.marked[0] == 88
    assert commands == [["gh", "pr", "ready", "88", "--repo", settings.repository]]
    assert all("merge" not in item for item in commands[0])
    assert store.task("run", 12)["phase"] == "ready"


def test_pr_check_error_keeps_wait_state(settings, snapshot):
    data, _ = snapshot
    github = FakeGitHub(settings, data)
    github.pr_state = lambda pr: (_ for _ in ()).throw(RuntimeError("GitHub unavailable"))
    engine = Engine(settings, github=github)
    store = Store(settings.state_dir)
    store.start("run")
    store.add_task("run", 12, "release:Beta", "branch", settings.state_dir / "w")
    store.head("run", 12, "f" * 40)
    store.pr("run", 12, 88)
    store.phase("run", 12, "ci_wait")
    assert engine._check_pr(store, "run", 12, 88)["status"] == "pr_check_error"
    assert store.task("run", 12)["phase"] == "ci_wait"


def test_failed_ci_escalates_to_project_without_merging(settings, snapshot):
    data, _ = snapshot
    github = FakeGitHub(settings, data)
    github.required_checks = lambda pr, state: [{"name": "Gate execution ledger", "state": "FAIL"}]
    engine = Engine(settings, github=github)
    store = Store(settings.state_dir)
    store.start("run")
    store.add_task("run", 12, "release:Beta", "branch", settings.state_dir / "w")
    store.head("run", 12, "f" * 40)
    store.pr("run", 12, 88)
    store.phase("run", 12, "ci_wait")
    result = engine._check_pr(store, "run", 12, 88)
    assert result["status"] == "blocked_ci_or_review"
    assert store.task("run", 12)["phase"] == "blocked"
    assert github.transitions[-1][1] == "blocked"


def test_stalled_ci_escalates(settings, snapshot, monkeypatch):
    data, _ = snapshot
    settings = replace(settings, ci_timeout_seconds=1)
    github = FakeGitHub(settings, data)
    engine = Engine(settings, github=github)
    store = Store(settings.state_dir)
    store.start("run")
    store.add_task("run", 12, "release:Beta", "branch", settings.state_dir / "w")
    store.head("run", 12, "f" * 40)
    store.pr("run", 12, 88)
    store.phase("run", 12, "ci_wait")
    from datetime import timedelta

    monkeypatch.setattr(store, "phase_started", lambda *a: datetime.now(UTC) - timedelta(seconds=2))
    assert engine._check_pr(store, "run", 12, 88)["status"] == "blocked_ci_or_review"


def test_diff_includes_new_file_and_rejects_large_patch(settings, tmp_path: Path, monkeypatch):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / "new.py").write_text("print('new')")
    github = FakeGitHub(settings, {"items": []})
    github.changed = lambda _: ["new.py"]
    engine = Engine(settings, github=github)
    monkeypatch.setattr("autocd.engine.command", lambda *a, **k: "")
    assert "print('new')" in engine._diff(worktree)
    monkeypatch.setattr("autocd.engine.command", lambda *a, **k: "x" * 250_001)
    try:
        engine._diff(worktree)
    except RuntimeError as error:
        assert "exceeds" in str(error)
    else:
        raise AssertionError("Large diff must be refused")
