from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import ClassVar
from zoneinfo import ZoneInfo

import pytest

from autocd.agents import decode_result
from autocd.engine import Engine
from autocd.github import pr_ready
from autocd.planner import Assignment
from autocd.state import LockBusyError, Store, exclusive


class FakeGitHub:
    def __init__(self, settings, snapshot):
        self.settings = settings
        self.data = snapshot
        self.transitions = []
        self.created = []
        self.reviewers = []
        self.merges = []
        self.verified = []
        self.pr_status = "OPEN"
        self.draft = True
        self.merge_state_status = "CLEAN"
        self.review_decision = ""
        self.up_to_date = True
        self.worktree = settings.state_dir / "fake-worktree"
        self.remote_branches = set()
        self.ready_reconciled = False

    def remote_branch_exists(self, branch):
        return branch in self.remote_branches

    def snapshot(self):
        return self.data

    def closed_issues_since(self, since, label):
        return []

    def overview(self):
        return {"open_prs": [], "recent_runs": [], "recent_commits": [], "branches": [[]]}

    def prepare_worktree(self, issue, run):
        self.worktree.mkdir(parents=True, exist_ok=True)
        self.current_branch = f"autocd/issue-{issue}-{run[:8]}"
        return self.current_branch, self.worktree

    def login(self):
        return "worker"

    def has_ready_reconciliation(self, issue, blocked_at):
        assert issue > 0 and blocked_at.tzinfo is not None
        return self.ready_reconciled

    def transition(self, target, issue, phase, payload):
        self.transitions.append((issue, phase, payload))
        if phase == "claim":
            self.data["items"][0]["fields"]["Readiness"] = "In Progress"
        if phase == "done":
            self.data["items"][0]["fields"].update({"Status": "Done", "Readiness": "Done"})

    def changed(self, worktree):
        return ["src/task.py", "tests/test_task.py"]

    def commit_and_push(self, worktree, branch, issue, reviewed_patch, reviewed_files):
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
            "state": self.pr_status,
            "mergedAt": "2026-11-05T10:00:00Z" if self.pr_status == "MERGED" else None,
            "mergeCommit": {"oid": "c" * 40} if self.pr_status == "MERGED" else None,
            "mergeStateStatus": self.merge_state_status,
            "reviewDecision": self.review_decision,
            "isDraft": self.draft,
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

    def merge_pr(self, pr, head):
        self.merges.append((pr, head))
        self.pr_status = "MERGED"

    def branch_up_to_date(self, head, branch):
        return self.up_to_date

    def verify_merged_on_main(self, merge_oid):
        self.verified.append(merge_oid)

    def close_issue(self, issue):
        self.data["items"][0]["issue"].update({"state": "CLOSED", "stateReason": "COMPLETED"})


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
            assert "commands" in json.loads(prompt)
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
        lambda issue, worktree, changed: [
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
    assert github.reviewers == [(88, "rrahimi-uci")]
    assert github.reviewers_recorded[0] == 88
    title, body = github.created[0]
    assert "Task 12" in title
    assert "## Summary" in body and "## Copilot handoff" in body
    assert "Implemented contract" in body and "pytest --cov=task" in body
    store = Store(settings.state_dir)
    assert store.task(result["run"], 12)["phase"] == "ci_wait"
    assert len(list(store.db.execute("SELECT * FROM events WHERE kind='review_cycle'"))) == 2


def test_docs_sync_is_included_before_qa_and_review(settings, snapshot, monkeypatch):
    data, _ = snapshot
    settings = replace(settings, issue_paths={})
    github = FakeGitHub(settings, data)
    generated = False

    def changed(worktree):
        paths = ["docs/api/reference.md"]
        if generated:
            paths.append("docs-site/m-29-rest-api-reference.html")
        return paths

    github.changed = changed

    class ApprovingAgent(FakeAgent):
        def run(self, role, prompt, cwd, editable=False):
            if role == "reviewer":
                review = json.loads(prompt)
                assert "docs-site/m-29-rest-api-reference.html" in review["changed"]
                assert review["generated_artifacts"] == [
                    {
                        "path": "docs-site/m-29-rest-api-reference.html",
                        "bytes": 10,
                        "sha256": hashlib.sha256(b"generated\n").hexdigest(),
                    }
                ]
                return {"approved": True, "findings": []}
            return super().run(role, prompt, cwd, editable)

    engine = Engine(settings, github=github, agent_factory=ApprovingAgent)

    def sync(worktree, store, run, issue):
        nonlocal generated
        generated = True
        target = worktree / "docs-site/m-29-rest-api-reference.html"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("generated\n")

    monkeypatch.setattr(engine, "_sync_generated_docs", sync)
    monkeypatch.setattr(engine, "_diff", lambda worktree: "reviewed generated diff")
    monkeypatch.setattr(
        engine,
        "_checks",
        lambda issue, worktree, paths: [
            {"command": "docs and ui", "passed": "docs-site/m-29-rest-api-reference.html" in paths}
        ],
    )
    result = engine.run()
    assert result["results"][0]["status"] == "waiting_for_ci_or_review"
    assert github.created


def test_generated_docs_follow_owned_sources_but_other_paths_do_not(
    settings, snapshot, monkeypatch
):
    data, _ = snapshot
    settings = replace(settings, issue_paths={12: frozenset({"docs/api/reference.md"})})
    github = FakeGitHub(settings, data)
    generated: list[str] = []

    def changed(worktree):
        return ["docs/api/reference.md", *generated]

    github.changed = changed
    engine = Engine(settings, github=github, agent_factory=FakeAgent)

    def sync(worktree, store, run, issue):
        generated.append("docs-site/m-29-rest-api-reference.html")

    checked = []
    monkeypatch.setattr(engine, "_sync_generated_docs", sync)
    monkeypatch.setattr(engine, "_diff", lambda _: "diff")
    monkeypatch.setattr(
        engine,
        "_checks",
        lambda issue, worktree, paths: (
            checked.append(paths) or [{"command": "x", "passed": True, "output": ""}]
        ),
    )
    assert engine.run()["results"][0]["status"] == "waiting_for_ci_or_review"
    assert checked[0] == ["docs/api/reference.md", "docs-site/m-29-rest-api-reference.html"]
    # A non-generated path outside ownership is still refused, as is a workflow file.
    with pytest.raises(RuntimeError, match="outside the reviewed task ownership"):
        engine._validate_task_paths(["docs/other.md"], frozenset({"docs/api/reference.md"}))
    with pytest.raises(RuntimeError, match="cannot publish: .github/workflows/ci.yml"):
        engine._validate_task_paths([".github/workflows/ci.yml"], frozenset())


def test_qa_patch_mutation_blocks_before_review(settings, snapshot, monkeypatch):
    data, _ = snapshot
    github = FakeGitHub(settings, data)
    engine = Engine(settings, github=github, agent_factory=FakeAgent)
    patches = iter(("candidate before QA", "candidate after QA"))
    monkeypatch.setattr(engine, "_diff", lambda worktree: next(patches))
    monkeypatch.setattr(engine, "_checks", lambda *args: [{"command": "qa", "passed": True}])
    result = engine.run()
    assert result["results"][0]["status"] == "blocked"
    assert "QA commands changed" in result["results"][0]["error"]
    assert github.created == []


def test_docs_sync_has_no_credentials_and_records_failure(settings, tmp_path, monkeypatch):
    engine = Engine(settings)
    store = Store(settings.state_dir)
    assert store.start("run")
    monkeypatch.setenv("GH_TOKEN", "private-project-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-model-key")
    captured = []

    def run(args, **kwargs):
        captured.append((args, kwargs))
        return subprocess.CompletedProcess(args, 1, "", "generation failed")

    monkeypatch.setattr("autocd.engine.subprocess.run", run)
    with pytest.raises(RuntimeError, match="Documentation sync failed"):
        engine._sync_generated_docs(tmp_path, store, "run", 821)
    args, kwargs = captured[0]
    assert args == ["node", "caliber/caliber-ui/scripts/sync-docs.mjs"]
    assert kwargs["env"]["CALIBER_DOCS_STRICT"] == "1"
    assert kwargs["env"]["CALIBER_DOCS_PYTHON"] == str(tmp_path / "caliber/.venv/bin/python")
    assert "GH_TOKEN" not in kwargs["env"]
    assert "ANTHROPIC_API_KEY" not in kwargs["env"]
    assert store.db.execute("SELECT detail FROM events WHERE kind='docs_sync'").fetchone()


def test_unattended_task_cannot_publish_policy_changes(settings, snapshot, monkeypatch):
    data, _ = snapshot
    settings = replace(settings, auto_merge=True)
    github = FakeGitHub(settings, data)
    github.changed = lambda worktree: [".github/workflows/ci.yml"]
    FakeAgent.calls = []
    engine = Engine(settings, github=github, agent_factory=FakeAgent)
    monkeypatch.setattr(engine, "_checks", lambda *args: pytest.fail("QA must not run"))
    result = engine.run()
    assert result["results"][0]["status"] == "blocked"
    assert "Unattended merge" in result["results"][0]["error"]
    assert github.created == []
    assert [role for role, _ in FakeAgent.calls] == ["planner", "developer"]


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
    assert engine.due_date(datetime(2026, 11, 5, 11, 59, tzinfo=UTC)) is None
    assert engine.due_date(datetime(2026, 11, 5, 12, 0, tzinfo=UTC)) == "2026-11-05"


def test_model_call_uses_remaining_daily_budget(settings, tmp_path):
    settings = replace(settings, daily_budget_usd=30, max_budget_usd=0)
    store = Store(settings.state_dir)
    assert store.start("run")
    store.event("run", None, "agent_started", {"role": "planner"})
    store.event("run", None, "agent_usage", {"role": "planner", "cost_usd": 29.25})
    budgets = []

    class CapturingAgent:
        def __init__(self, call_settings, event):
            budgets.append(call_settings.max_budget_usd)

        def run(self, role, prompt, cwd, editable=False):
            return {"selected": [], "reason": "budget test"}

    Engine(settings, agent_factory=CapturingAgent)._run_agent(
        store, "run", None, "planner", "{}", tmp_path
    )
    assert budgets == [0.75]


@pytest.mark.parametrize(
    ("cost", "message"),
    [
        (30, "Daily Claude budget exhausted"),
        (None, "Daily Claude spend cannot be verified"),
    ],
)
def test_model_call_stops_when_daily_budget_is_not_safe(settings, cost, message, tmp_path):
    settings = replace(settings, daily_budget_usd=30, max_budget_usd=0, unknown_call_cost_usd=0)
    store = Store(settings.state_dir)
    assert store.start("run")
    store.event("run", None, "agent_started", {"role": "planner"})
    store.event("run", None, "agent_usage", {"role": "planner", "cost_usd": cost})
    with pytest.raises(RuntimeError, match=message):
        Engine(settings)._run_agent(store, "run", None, "planner", "{}", tmp_path)


def test_unknown_cost_calls_are_charged_an_estimate_instead_of_stopping_the_day(settings, tmp_path):
    settings = replace(settings, daily_budget_usd=30, max_budget_usd=0, unknown_call_cost_usd=3)
    store = Store(settings.state_dir)
    assert store.start("run")
    for cost in (None, 1.5):
        store.event("run", None, "agent_started", {"role": "developer"})
        store.event("run", None, "agent_usage", {"role": "developer", "cost_usd": cost})
    store.event("run", None, "agent_started", {"role": "reviewer"})  # killed, no usage
    budgets = []

    class Capture:
        def __init__(self, call_settings, event):
            budgets.append(call_settings.max_budget_usd)

        def run(self, role, prompt, cwd, editable=False):
            return {"selected": [], "reason": "ok"}

    Engine(settings, agent_factory=Capture)._run_agent(
        store, "run", None, "planner", "{}", tmp_path
    )
    # 30 - (1.5 known + 2 unknown x 3) = 22.5 left for this call.
    assert budgets == [22.5]
    for _ in range(8):
        store.event("run", None, "agent_started", {"role": "developer"})
    with pytest.raises(RuntimeError, match="budget exhausted"):
        Engine(settings, agent_factory=Capture)._run_agent(
            store, "run2", None, "planner", "{}", tmp_path
        )


def test_ready_task_uses_standard_checks_without_issue_override(settings, snapshot):
    data, _ = snapshot
    settings = replace(settings, issue_checks={})
    engine = Engine(settings, github=FakeGitHub(settings, data), agent_factory=FakeAgent)
    engine._task = lambda run, task, retry_branch=None: {"issue": task.issue, "status": "queued"}
    result = engine.run()
    assert result["eligible"] == 3 and result["configured"] == 0
    assert result["selected"] == [12]
    assert result["results"] == [{"issue": 12, "status": "queued"}]


def test_planned_executable_task_is_promoted_through_ready_transition(settings, snapshot):
    data, _ = snapshot
    data["items"][0]["fields"]["Readiness"] = "Planned"
    github = FakeGitHub(settings, data)
    engine = Engine(settings, github=github, agent_factory=FakeAgent)
    task = Assignment(
        12,
        "Task 12",
        "contract",
        "release:Beta",
        frozenset({"src/task.py"}),
        1,
        1000,
        "Planned",
    )

    engine._ensure_ready(task)

    assert github.transitions[0][0:2] == (12, "ready")
    assert "execution order 1000" in github.transitions[0][2]["evidence"][0]


def test_run_reports_when_no_executable_tasks_exist(settings, snapshot):
    data, item = snapshot
    data["items"] = [item(12, fields={"Readiness": "Blocked"})]
    engine = Engine(settings, github=FakeGitHub(settings, data), agent_factory=FakeAgent)
    result = engine.run()

    assert result["status"] == "no_executable_tasks"
    assert result["eligible"] == 0
    assert result["selected"] == []


def test_prior_blocked_issue_is_excluded_from_future_run(settings, snapshot):
    data, _ = snapshot
    store = Store(settings.state_dir)
    assert store.start("prior")
    store.add_task("prior", 12, "release:Beta", "old-branch", settings.state_dir / "old")
    store.phase("prior", 12, "blocked", failure="interrupted before PR")

    class PlanNext(FakeAgent):
        def run(self, role, prompt, cwd, editable=False):
            assert role == "planner"
            assert 12 not in [row["issue"] for row in json.loads(prompt)["eligible"]]
            return {"selected": [13], "reason": "next safe task"}

    engine = Engine(settings, github=FakeGitHub(settings, data), agent_factory=PlanNext)
    engine._task = lambda run, task: {"issue": task.issue, "status": "queued"}
    result = engine.run()
    assert result["selected"] == [13]


def test_scheduled_run_resumes_reconciled_pre_pr_block_after_day_reset(
    settings, snapshot, monkeypatch
):
    data, _ = snapshot
    settings = replace(settings, enabled=True)
    store = Store(settings.state_dir)
    assert store.start("prior")
    store.add_task("prior", 12, "release:Beta", "old-branch", settings.state_dir / "old")
    store.phase("prior", 12, "blocked", failure="review gaps")
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE events SET at='2000-01-01T00:00:00+00:00' "
            "WHERE run_id='prior' AND kind='task_claimed'"
        )
    github = FakeGitHub(settings, data)
    github.ready_reconciled = True
    engine = Engine(settings, github=github, agent_factory=FakeAgent)
    today = datetime.now(ZoneInfo(settings.timezone)).date().isoformat()
    monkeypatch.setattr(engine, "due_date", lambda: today)
    launched = []
    monkeypatch.setattr(
        engine,
        "_task",
        lambda run, task, retry_branch=None: (
            launched.append((task.issue, retry_branch)),
            {"issue": task.issue, "status": "queued"},
        )[1],
    )
    result = engine.run(scheduled=True)
    assert result["selected"] == [12]
    assert launched == [(12, "old-branch")]
    authorization = store.db.execute(
        "SELECT detail FROM events WHERE run_id=? AND issue=12 AND kind='retry_authorized'",
        (result["run"],),
    ).fetchone()
    assert json.loads(authorization["detail"]) == {
        "prior_run": "prior",
        "source": "scheduled_reconciled_ready",
    }


def test_scheduled_retry_does_not_repeat_same_day(settings, snapshot, monkeypatch):
    data, _ = snapshot
    settings = replace(settings, enabled=True)
    store = Store(settings.state_dir)
    assert store.start("prior")
    store.add_task("prior", 12, "release:Beta", "old-branch", settings.state_dir / "old")
    store.phase("prior", 12, "blocked", failure="review gaps")
    github = FakeGitHub(settings, data)
    engine = Engine(settings, github=github, agent_factory=FakeAgent)
    monkeypatch.setattr(
        engine, "due_date", lambda: datetime.now(ZoneInfo(settings.timezone)).date().isoformat()
    )
    result = engine.run(scheduled=True)
    assert 12 not in result["selected"]
    assert not store.db.execute("SELECT 1 FROM events WHERE kind='retry_authorized'").fetchone()


def test_scheduled_retry_refuses_stale_ready_after_failed_claim_and_block(
    settings, snapshot, monkeypatch
):
    data, _ = snapshot  # Project stayed Todo/Ready when both transitions failed.
    settings = replace(settings, enabled=True)
    store = Store(settings.state_dir)
    assert store.start("prior")
    store.add_task("prior", 12, "release:Beta", "old-branch", settings.state_dir / "old")
    store.phase("prior", 12, "blocked", failure="claim and block transitions failed")
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE events SET at='2000-01-01T00:00:00+00:00' "
            "WHERE run_id='prior' AND kind='task_claimed'"
        )
    github = FakeGitHub(settings, data)  # No post-block Ready transition comment.
    engine = Engine(settings, github=github, agent_factory=FakeAgent)
    monkeypatch.setattr(
        engine, "due_date", lambda: datetime.now(ZoneInfo(settings.timezone)).date().isoformat()
    )
    result = engine.run(scheduled=True)
    assert 12 not in result["selected"]
    assert github.transitions == []
    assert not store.db.execute("SELECT 1 FROM events WHERE kind='retry_authorized'").fetchone()


def test_scheduled_retry_rejects_remote_branch(settings, snapshot, monkeypatch):
    data, _ = snapshot
    settings = replace(settings, enabled=True)
    store = Store(settings.state_dir)
    assert store.start("prior")
    store.add_task("prior", 12, "release:Beta", "old-branch", settings.state_dir / "old")
    store.phase("prior", 12, "blocked", failure="review gaps")
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE events SET at='2000-01-01T00:00:00+00:00' "
            "WHERE run_id='prior' AND kind='task_claimed'"
        )
    github = FakeGitHub(settings, data)
    github.overview = lambda: {
        "open_prs": [],
        "recent_runs": [],
        "recent_commits": [],
        "branches": [[{"name": "old-branch"}]],
    }
    engine = Engine(settings, github=github, agent_factory=FakeAgent)
    monkeypatch.setattr(
        engine, "due_date", lambda: datetime.now(ZoneInfo(settings.timezone)).date().isoformat()
    )
    result = engine.run(scheduled=True)
    assert 12 not in result["selected"]
    assert not store.db.execute("SELECT 1 FROM events WHERE kind='retry_authorized'").fetchone()


def test_explicit_retry_requires_ready_unclaimed_issue_and_absent_remote_branch(settings, snapshot):
    data, _ = snapshot
    store = Store(settings.state_dir)
    assert store.start("prior")
    store.add_task("prior", 12, "release:Beta", "old-branch", settings.state_dir / "old")
    store.phase("prior", 12, "blocked", failure="reviewer JSON")
    github = FakeGitHub(settings, data)
    engine = Engine(settings, github=github, agent_factory=FakeAgent)
    engine._task = lambda run, task, retry_branch=None: {"issue": task.issue, "status": "queued"}
    result = engine.run(retry_issue=12)
    assert result["selected"] == [12]
    assert result["results"] == [{"issue": 12, "status": "queued"}]
    assert store.db.execute("SELECT kind FROM events WHERE kind='retry_authorized'").fetchone()

    github.overview = lambda: {
        "open_prs": [],
        "recent_runs": [],
        "recent_commits": [],
        "branches": [[{"name": "old-branch"}]],
    }
    with pytest.raises(RuntimeError, match="absent remote branch"):
        engine.run(retry_issue=12)
    github.overview = FakeGitHub.overview.__get__(github)
    data["items"][0]["fields"]["Readiness"] = "Blocked"
    with pytest.raises(RuntimeError, match="freshly Ready"):
        engine.run(retry_issue=12)
    with pytest.raises(ValueError, match="manual run"):
        engine.run(scheduled=True, retry_issue=12)


def test_retry_respects_daily_attempt_cap(settings, snapshot):
    data, _ = snapshot
    settings = replace(settings, max_tasks_per_run=1)
    store = Store(settings.state_dir)
    assert store.start("prior")
    store.add_task("prior", 12, "release:Beta", "old-branch", settings.state_dir / "old")
    store.phase("prior", 12, "blocked", failure="reviewer JSON")
    engine = Engine(settings, github=FakeGitHub(settings, data), agent_factory=FakeAgent)
    with pytest.raises(RuntimeError, match="daily attempt cap"):
        engine.run(retry_issue=12)


def test_retry_rechecks_remote_branch_after_planning(settings, snapshot):
    data, _ = snapshot
    store = Store(settings.state_dir)
    assert store.start("prior")
    store.add_task("prior", 12, "release:Beta", "old-branch", settings.state_dir / "old")
    store.phase("prior", 12, "blocked", failure="interrupted")
    github = FakeGitHub(settings, data)
    github.prepare_worktree = lambda *args: pytest.fail("retry must not create a worktree")

    class PlannerWithRemoteRace(FakeAgent):
        def run(self, role, prompt, cwd, editable=False):
            if role == "planner":
                github.remote_branches.add("old-branch")
            return super().run(role, prompt, cwd, editable)

    result = Engine(settings, github=github, agent_factory=PlannerWithRemoteRace).run(
        retry_issue=12
    )
    assert result["results"][0]["status"] == "blocked"
    assert "appeared remotely" in result["results"][0]["error"]
    assert github.transitions == []


def test_daily_attempt_cap_includes_prior_manual_runs(settings, snapshot):
    data, _ = snapshot
    settings = replace(settings, max_tasks_per_run=1)
    store = Store(settings.state_dir)
    assert store.start("prior")
    store.add_task("prior", 12, "release:Beta", "old-branch", settings.state_dir / "old")
    store.phase("prior", 12, "blocked", failure="QA failed")
    engine = Engine(settings, github=FakeGitHub(settings, data))
    result = engine.run()
    assert result["selected"] == []


def test_daily_target_caps_new_tasks_by_actual_merged_done_count(settings, snapshot, monkeypatch):
    data, item = snapshot
    data["items"] = [item(n) for n in range(12, 20)]
    for number in (30, 31):
        data["items"].append(
            item(
                number,
                fields={"Status": "Done", "Readiness": "Done"},
                issue={
                    "state": "CLOSED",
                    "stateReason": "COMPLETED",
                    "prs": [{"state": "MERGED", "mergedAt": "2026-11-05T10:00:00Z"}],
                },
            )
        )
    settings = replace(
        settings,
        enabled=True,
        daily_done_target=6,
        issue_checks={n: ("pytest x --cov=x --cov-fail-under=95",) for n in range(12, 20)},
        issue_paths={n: frozenset({f"src/{n}.py"}) for n in range(12, 20)},
    )
    github = FakeGitHub(settings, data)
    github.closed_issues_since = lambda *a: [
        {"number": n, "closedAt": "2026-11-05T10:00:00Z", "stateReason": "COMPLETED"}
        for n in (30, 31)
    ]

    class PlanAll(FakeAgent):
        def run(self, role, prompt, cwd, editable=False):
            assert role == "planner"
            return {"selected": [12, 13, 14, 15, 16, 17, 18, 19], "reason": "independent"}

    engine = Engine(settings, github=github, agent_factory=PlanAll)
    monkeypatch.setattr(engine, "due_date", lambda: "2026-11-05")
    launched = []
    monkeypatch.setattr(
        engine, "_task", lambda run, task: (launched.append(task.issue), {"issue": task.issue})[1]
    )
    result = engine.run(scheduled=True)
    assert result["done_today"] == [30, 31]
    assert result["remaining_to_target"] == 4
    assert sorted(launched) == [12, 13, 14, 15]
    assert result["daily_done_target"] == 6


def test_owned_path_guard(settings):
    assert Engine._in_owned_paths("src/task.py", frozenset({"src/task.py"}))
    assert Engine._in_owned_paths("src/sub/a.py", frozenset({"src"}))
    assert not Engine._in_owned_paths("src-other/a.py", frozenset({"src"}))
    for path in (
        ".github/workflows/ci.yml",
        ".agents/engineering-policy.md",
        "automated-cd/config/workflow.toml",
        "AGENTS.md",
        "scripts/ci-local.sh",
    ):
        assert Engine._protected_path(path)
    assert not Engine._protected_path("caliber/src/caliber/agent.py")


def test_hosted_untrusted_commands_run_as_isolated_user(settings):
    isolated = Engine(replace(settings, worker_user="autocdqa"))
    worker = isolated._as_worker(["pytest", "tests"])
    assert worker[:7] == [
        "sudo",
        "-E",
        "-H",
        "-u",
        "autocdqa",
        "--",
        "env",
    ]
    assert worker[7].startswith("PATH=") and worker[8:] == ["pytest", "tests"]
    assert Engine(settings)._as_worker(["pytest", "tests"]) == ["pytest", "tests"]


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
    assert not engine._checks(12, worktree, [])[0]["passed"]  # configured base command absent
    settings2 = replace(
        settings,
        base_checks=(f"{sys.executable} -c 'print(1)' | cat",),
        issue_checks={
            12: (f"{sys.executable} -m pytest --cov=autocd --cov-fail-under=95 --version",)
        },
    )
    assert engine.__class__(settings2)._checks(12, worktree, [])[0]["output"] == "Invalid command"
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

    def run_with_coverage(args, **kwargs):
        for arg in args:
            if arg.startswith("--cov-report=json:"):
                Path(arg.split(":", 1)[1]).write_text(
                    json.dumps(
                        {"totals": {"num_statements": 100, "covered_lines": 100}, "files": {}}
                    )
                )
        return Result()

    monkeypatch.setattr("autocd.engine.subprocess.run", run_with_coverage)
    assert all(item["passed"] for item in engine.__class__(settings3)._checks(12, worktree, []))


def test_coverage_command_rejects_weak_or_malformed_gates():
    assert Engine._coverage_command("python -m pytest tests --cov=app --cov-fail-under=95")
    assert Engine._coverage_command("pytest tests --cov app --cov-fail-under 96")
    assert not Engine._coverage_command("pytest tests --cov=app --cov-fail-under=94")
    assert Engine._coverage_command("pytest tests --cov=app --cov-fail-under=85", 85)
    assert not Engine._coverage_command("pytest tests --cov=app --cov-fail-under=84", 85)
    assert not Engine._coverage_command("python -m pytest tests --cov=app")
    assert not Engine._coverage_command("echo pytest --cov=app --cov-fail-under=oops")
    assert not Engine._coverage_command("pytest 'unterminated")


def test_coverage_requires_each_changed_production_file_to_meet_the_threshold(tmp_path: Path):
    source = tmp_path / "src" / "changed.py"
    source.parent.mkdir()
    source.write_text("x = 1\n")
    report = tmp_path / "coverage.json"
    report.write_text(
        json.dumps(
            {
                "totals": {"num_statements": 1000, "covered_lines": 990},
                "files": {
                    "src/changed.py": {"num_statements": 100, "covered_lines": 84},
                    "src/unrelated.py": {"num_statements": 900, "covered_lines": 895},
                },
            }
        )
    )
    evidence = Engine._coverage_evidence(report, tmp_path, ["src/changed.py"], 85)
    assert not evidence["passed"] and "changed.py" in evidence["reason"]
    assert "below the 85% threshold" in evidence["reason"]
    data = json.loads(report.read_text())
    data["files"].pop("src/changed.py")
    report.write_text(json.dumps(data))
    assert not Engine._coverage_evidence(report, tmp_path, ["src/changed.py"], 85)["passed"]
    data["files"]["src/changed.py"] = {"num_statements": 100, "covered_lines": 85}
    report.write_text(json.dumps(data))
    assert Engine._coverage_evidence(report, tmp_path, ["src/changed.py"], 85)["passed"]
    # The default keeps the stricter historical gate for callers that pass none.
    assert not Engine._coverage_evidence(report, tmp_path, ["src/changed.py"])["passed"]


def test_checks_use_standard_fallback_or_fail_missing_executable(settings, tmp_path: Path):
    no_cov = replace(settings, base_checks=(), issue_checks={12: ("echo hello",)})
    assert Engine(no_cov)._checks(12, tmp_path, [])[0]["command"] == "echo hello"
    missing = replace(settings, base_checks=("does-not-exist",))
    assert not Engine(missing)._checks(12, tmp_path, [])[0]["passed"]
    unsupported = Engine(replace(settings, base_checks=(), issue_checks={}))._checks(
        821, tmp_path, ["caliber/ui/src/App.tsx"]
    )
    assert not unsupported[0]["passed"] and "Issue-specific" in unsupported[0]["output"]
    assert Engine._standard_check_path("docs/guide.md")
    assert Engine._standard_check_path("ARCHITECTURE.md")
    assert (
        Engine(replace(settings, base_checks=(), issue_checks={}))._checks(
            821, tmp_path, ["ARCHITECTURE.md"]
        )
        == []
    )
    assert not Engine._standard_check_path("nested/ARCHITECTURE.md")
    assert not Engine._standard_check_path("SECURITY.md")
    assert not Engine._standard_check_path("CLAUDE.md")
    for policy_path in ("SECURITY.md", "CLAUDE.md"):
        blocked = Engine(replace(settings, base_checks=(), issue_checks={}))._checks(
            821, tmp_path, [policy_path]
        )
        assert not blocked[0]["passed"] and "Issue-specific" in blocked[0]["output"]
    assert not Engine._standard_check_path("sdk/python/client.py")
    for path in (
        "docs-site/m-29-rest-api-reference.html",
        "docs-site/m-29-rest-api-reference.md",
        "docs-site/docs-nav.js",
        "docs-site/search-index.json",
        "docs-site/llms.txt",
        "caliber/caliber-ui/public/docs/m-29-rest-api-reference.html",
    ):
        assert Engine._generated_docs_path(path)
        assert Engine._standard_check_path(path)
    for path in ("docs-site/build-docs.mjs", "docs-site/index.html", "docs-site/cookbooks/a.md"):
        assert not Engine._standard_check_path(path)
    assert Engine._docs_source_path("sdk/caliber-sdk/src/caliber_sdk/client.py")
    assert Engine._docs_source_path("sdk/caliber-sdk/coverage_allowlist.toml")
    assert Engine._docs_source_path("docs-site/cookbooks/01-prompt-regression-lab/build.yaml")
    assert not Engine._docs_source_path("sdk/caliber-plugin-sdk/src/plugin.py")


def test_generated_docs_require_ui_parity(settings, tmp_path, monkeypatch):
    called = []

    def run(args, **kwargs):
        called.append(args)
        return subprocess.CompletedProcess(args, 0, "PASS ui", "")

    monkeypatch.setattr("autocd.engine.subprocess.run", run)
    engine = Engine(replace(settings, base_checks=(), issue_checks={}))
    result = engine._checks(821, tmp_path, ["docs-site/m-29-rest-api-reference.html"])
    assert [row["command"] for row in result] == ["scripts/ci-local.sh ui"]
    assert result[0]["passed"]
    assert called == [["scripts/ci-local.sh", "ui"]]


def test_standard_coverage_fallback_measures_changed_python(settings, tmp_path, monkeypatch):
    path = tmp_path / "caliber/src/caliber/example.py"
    path.parent.mkdir(parents=True)
    path.write_text("answer = 42\n")
    seen = []

    class Proc:
        returncode = 0
        stdout = "1 passed"
        stderr = ""

    def run(args, **kwargs):
        seen.append(args)
        report = next(
            arg.split("json:", 1)[1] for arg in args if arg.startswith("--cov-report=json:")
        )
        Path(report).write_text(
            json.dumps(
                {
                    "totals": {"num_statements": 100, "covered_lines": 20},
                    "files": {
                        "caliber/src/caliber/example.py": {
                            "num_statements": 1,
                            "covered_lines": 1,
                        }
                    },
                }
            )
        )
        return Proc()

    monkeypatch.setattr("autocd.engine.subprocess.run", run)
    engine = Engine(replace(settings, base_checks=(), issue_checks={}))
    tests = tmp_path / "caliber/tests"
    tests.mkdir(parents=True)
    (tests / "test_example.py").write_text("from caliber.example import answer\n")
    (tests / "test_other.py").write_text("import caliber.examples_unrelated\n")
    rows = engine._checks(821, tmp_path, ["caliber/src/caliber/example.py"])
    assert len(rows) == 1 and rows[0]["passed"]
    # Only the tests that reference the changed module run, never the two-hour suite.
    assert "caliber/tests/test_example.py" in seen[0]
    assert "caliber/tests" not in seen[0] and "caliber/tests/test_other.py" not in seen[0]
    assert seen[0][seen[0].index("-n") + 1] == "4"
    assert seen[0][seen[0].index("--dist") + 1] == "loadgroup"
    assert "caliber/src/caliber/example.py=100.00%" in rows[0]["coverage"]


def test_changed_module_test_selection(tmp_path):
    src = tmp_path / "caliber/src/caliber/api"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text("")
    (src / "sse.py").write_text("x = 1\n")
    (tmp_path / "caliber/src/caliber/other.py").write_text("y = 1\n")
    tests = tmp_path / "caliber/tests/api"
    tests.mkdir(parents=True)
    (tests / "test_dotted.py").write_text("import caliber.api.sse as s\n")
    (tests / "test_from.py").write_text("from caliber.api import sse\n")
    (tests / "test_paren.py").write_text("from caliber.api import (\n    other,\n    sse,\n)\n")
    (tests / "test_prefix_only.py").write_text("import caliber.api.sse_extra\n")
    (tests / "test_unrelated.py").write_text("from caliber.api import ssex\n")
    (tests / "test_package.py").write_text("import caliber.api\n")
    selected, missing = Engine._coverage_tests(tmp_path, ["caliber/src/caliber/api/sse.py"])
    assert missing == []
    assert selected == [
        "caliber/tests/api/test_dotted.py",
        "caliber/tests/api/test_from.py",
        "caliber/tests/api/test_paren.py",
    ]
    # A package __init__ maps to the package name; changed tests are always included.
    (tests / "test_new.py").write_text("assert True\n")
    selected, missing = Engine._coverage_tests(
        tmp_path, ["caliber/src/caliber/api/__init__.py", "caliber/tests/api/test_new.py"]
    )
    assert "caliber/tests/api/test_package.py" in selected
    assert "caliber/tests/api/test_new.py" in selected and missing == []
    # No test references the module: report it instead of running an empty selection.
    assert Engine._coverage_tests(tmp_path, ["caliber/src/caliber/other.py"])[1] == [
        "caliber/src/caliber/other.py"
    ]
    # Production Python outside caliber/src keeps the full-suite behaviour.
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/tool.py").write_text("")
    assert Engine._coverage_tests(tmp_path, ["scripts/tool.py"]) == (None, [])


def test_missing_module_tests_fail_with_actionable_feedback(settings, tmp_path, monkeypatch):
    path = tmp_path / "caliber/src/caliber/lonely.py"
    path.parent.mkdir(parents=True)
    path.write_text("z = 1\n")
    (tmp_path / "caliber/tests").mkdir(parents=True)
    calls = []
    monkeypatch.setattr(
        "autocd.engine.subprocess.run",
        lambda args, **kwargs: calls.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )
    engine = Engine(replace(settings, base_checks=("git diff --check",), issue_checks={}))
    rows = engine._checks(821, tmp_path, ["caliber/src/caliber/lonely.py"])
    assert [row["passed"] for row in rows] == [True, False]
    assert "caliber/src/caliber/lonely.py" in rows[-1]["output"]
    assert calls == [["git", "diff", "--check"]]
    # A failing base check is reported first; the coverage hint is not added on top.
    monkeypatch.setattr(
        "autocd.engine.subprocess.run",
        lambda args, **kwargs: subprocess.CompletedProcess(args, 1, "", "bad"),
    )
    rows = engine._checks(821, tmp_path, ["caliber/src/caliber/lonely.py"])
    assert len(rows) == 1 and not rows[0]["passed"]


def test_full_suite_fallback_for_python_outside_the_backend_package(
    settings, tmp_path, monkeypatch
):
    (tmp_path / "sdk").mkdir()
    (tmp_path / "sdk/client.py").write_text("")
    seen = []
    monkeypatch.setattr(
        "autocd.engine.subprocess.run",
        lambda args, **kwargs: seen.append(args) or subprocess.CompletedProcess(args, 0, "", ""),
    )
    engine = Engine(replace(settings, base_checks=(), issue_checks={821: ("git diff --check",)}))
    engine._checks(821, tmp_path, ["sdk/client.py"])
    assert any("caliber/tests" in args for args in seen)


def test_setup_verifies_even_existing_venv_and_records_failure(
    settings, tmp_path: Path, monkeypatch
):
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
    assert len(calls) == 2


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
    row = Engine(settings2)._checks(12, tmp_path, [])[0]
    assert row["passed"] is False and "skipped" in row["output"]


def test_qa_timeout_records_duration_without_worker_command_line(
    settings, tmp_path: Path, monkeypatch
):
    def timeout(args, **kwargs):
        assert kwargs["timeout"] == settings.qa_command_timeout_seconds
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr("autocd.engine.subprocess.run", timeout)
    engine = Engine(replace(settings, base_checks=("scripts/ci-local.sh test-smoke",)))
    row = engine._checks(12, tmp_path, [])[0]
    assert row == {
        "command": "scripts/ci-local.sh test-smoke",
        "passed": False,
        "output": "QA command timed out after 7200 seconds",
    }


def test_test_only_backend_change_still_runs_full_suite(settings, tmp_path: Path, monkeypatch):
    class Proc:
        returncode = 0
        stdout = "PASS  test\n"
        stderr = ""

    monkeypatch.setattr("autocd.engine.subprocess.run", lambda *args, **kwargs: Proc())
    engine = Engine(replace(settings, base_checks=(), issue_checks={}))
    result = engine._checks(12, tmp_path, ["caliber/tests/test_verification.py"])
    assert [row["command"] for row in result] == ["scripts/ci-local.sh test"]
    assert result[0]["passed"] is True


def test_scheduled_disabled_and_deduplicated(settings, snapshot, monkeypatch):
    data, _ = snapshot
    github = FakeGitHub(settings, data)
    assert Engine(settings, github=github).run(scheduled=True) == {"status": "disabled"}
    enabled = replace(settings, enabled=True, issue_checks={})
    engine = Engine(enabled, github=github, agent_factory=FakeAgent)
    monkeypatch.setattr(engine, "_task", lambda run, task: {"issue": task.issue})
    monkeypatch.setattr(engine, "due_date", lambda: "2026-11-05")
    assert engine.run(scheduled=True)["results"] == [{"issue": 12}]
    assert engine.run(scheduled=True)["status"] == "not_due_or_already_started"
    monkeypatch.setattr(engine, "due_date", lambda: None)
    assert engine.run(scheduled=True)["status"] == "not_due_or_already_started"


def test_hosted_monitor_reconciles_without_starting_conflicting_work(settings, snapshot):
    data, _ = snapshot
    enabled = replace(settings, enabled=True)
    github = FakeGitHub(enabled, data)
    github.current_branch = "branch"
    engine = Engine(enabled, github=github)
    store = Store(settings.state_dir)
    store.start("old")
    store.add_task("old", 12, "release:Beta", "branch", settings.state_dir / "old-worktree")
    store.head("old", 12, "f" * 40)
    store.pr("old", 12, 88)
    store.phase("old", 12, "ci_wait")
    assert engine.run(scheduled=True)["status"] == "waiting_on_published_prs"
    assert engine.monitor()["pending"] == 1
    assert not store.db.execute("SELECT 1 FROM runs WHERE schedule_date IS NOT NULL").fetchone()


def test_pr_awaiting_owner_merge_does_not_stop_the_next_task(settings, snapshot, monkeypatch):
    """Regression: with auto_merge off, one unmerged PR blocked every later daily run."""
    data, _ = snapshot
    enabled = replace(settings, enabled=True, auto_merge=False)
    github = FakeGitHub(enabled, data)
    engine = Engine(enabled, github=github, agent_factory=FakeAgent)
    store = Store(settings.state_dir)
    store.start("old")
    store.add_task("old", 99, "release:Beta", "autocd/99", settings.state_dir / "old-worktree")
    store.head("old", 99, "f" * 40)
    store.pr("old", 99, 88)
    store.phase("old", 99, "ready")
    checked = []
    monkeypatch.setattr(
        engine,
        "_check_pr",
        lambda store, run, issue, pr: checked.append(pr) or {"issue": issue, "status": "ready"},
    )
    monkeypatch.setattr(engine, "_task", lambda run, task: {"issue": task.issue})
    monkeypatch.setattr(engine, "due_date", lambda: "2026-11-10")
    result = engine.run(scheduled=True)
    assert result["results"] == [{"issue": 12}]
    assert checked == [88]  # The waiting PR is still reconciled on the same tick.
    status = engine.monitor()
    assert status["pending"] == 0 and status["awaiting_owner"] == 1


def test_cron_tick_reports_busy_during_active_run(settings, snapshot):
    data, _ = snapshot
    engine = Engine(replace(settings, enabled=True), github=FakeGitHub(settings, data))
    with exclusive(settings.state_dir):
        assert engine.run(scheduled=True) == {"status": "busy"}
        with pytest.raises(LockBusyError):
            engine.run()


def test_failed_project_read_releases_daily_reservation(settings, snapshot, monkeypatch):
    data, _ = snapshot
    settings = replace(settings, enabled=True, issue_checks={})
    github = FakeGitHub(settings, data)
    original = github.snapshot
    reads = 0

    def intermittent_snapshot():
        nonlocal reads
        reads += 1
        if reads == 1:
            raise RuntimeError("Project unavailable")
        return original()

    github.snapshot = intermittent_snapshot
    engine = Engine(settings, github=github, agent_factory=FakeAgent)
    monkeypatch.setattr(engine, "_task", lambda run, task: {"issue": task.issue})
    monkeypatch.setattr(engine, "due_date", lambda: "2026-11-05")
    with pytest.raises(RuntimeError, match="Project unavailable"):
        engine.run(scheduled=True)
    store = Store(settings.state_dir)
    assert not store.scheduled("2026-11-05")
    assert store.db.execute("SELECT phase FROM runs").fetchone()["phase"] == "failed"
    assert engine.run(scheduled=True)["results"] == [{"issue": 12}]
    assert store.scheduled("2026-11-05")


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
    assert "missing producer" not in str(github.transitions[-1][2])
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
    assert github.transitions[-1][1] == "blocked"

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


def test_failed_checks_and_findings_reach_the_developer_together(settings, snapshot, monkeypatch):
    data, _ = snapshot
    settings = replace(settings, max_review_cycles=2)
    github = FakeGitHub(settings, data)
    prompts: dict[str, list[dict]] = {"developer": [], "reviewer": []}

    class Recording(FakeAgent):
        def run(self, role, prompt, cwd, editable=False):
            if role in prompts:
                prompts[role].append(json.loads(prompt))
            if role == "reviewer":
                return {
                    "approved": False,
                    "findings": [{"severity": "P1", "blocking": True, "detail": "edge"}],
                }
            return super().run(role, prompt, cwd, editable)

    engine = Engine(settings, github=github, agent_factory=Recording)
    monkeypatch.setattr(engine, "_diff", lambda _: "diff")
    monkeypatch.setattr(
        engine,
        "_checks",
        lambda *a: [
            {"passed": True, "command": "git diff --check", "output": ""},
            {"passed": False, "command": "pytest", "output": "E assert 1 == 2"},
        ],
    )
    Store(settings.state_dir).start("run")
    task = Assignment(
        12,
        "Task 12",
        "contract",
        "release:Beta",
        frozenset({"src/task.py", "tests/test_task.py"}),
        1,
    )
    assert "did not pass" in engine._task("run", task)["error"]
    second = prompts["developer"][1]["previous_feedback"]
    assert "Review findings" in second and '"edge"' in second
    assert "QA command failures" in second and "E assert 1 == 2" in second
    assert "git diff --check" not in second
    assert prompts["reviewer"][0]["previous_feedback"] == ""
    assert prompts["reviewer"][1]["previous_feedback"] == second


def test_non_blocking_p3_findings_do_not_send_the_candidate_back(settings, snapshot, monkeypatch):
    data, _ = snapshot
    settings = replace(settings, max_review_cycles=1)
    github = FakeGitHub(settings, data)
    note = {"severity": "P3", "blocking": False, "title": "Naming", "detail": "rename helper"}

    class NoteOnly(FakeAgent):
        def run(self, role, prompt, cwd, editable=False):
            if role == "reviewer":
                return {"approved": True, "findings": [note]}
            return super().run(role, prompt, cwd, editable)

    FakeAgent.calls = []
    engine = Engine(settings, github=github, agent_factory=NoteOnly)
    monkeypatch.setattr(engine, "_diff", lambda _: "diff")
    monkeypatch.setattr(
        engine, "_checks", lambda *a: [{"passed": True, "command": "pytest --cov", "output": ""}]
    )
    assert engine.run()["results"][0]["status"] == "waiting_for_ci_or_review"
    assert "rename helper" in github.created[0][1]


def test_only_explicit_non_blocking_p3_findings_pass():
    blocking = Engine._blocking_findings
    p3 = {"severity": "P3", "blocking": False}
    assert blocking([p3]) == []
    assert blocking([{"severity": "P3", "blocking": True}]) == [
        {"severity": "P3", "blocking": True}
    ]
    assert blocking([{"severity": "P2", "blocking": False}]) == [
        {"severity": "P2", "blocking": False}
    ]
    assert blocking([{"severity": "P3"}]) == [{"severity": "P3"}]
    assert blocking(["legacy text", p3]) == ["legacy text"]
    assert blocking("SECRET") == ["SECRET"]
    assert blocking(None) == [None]


def test_unapproved_review_without_findings_still_returns(settings, snapshot, monkeypatch):
    data, _ = snapshot
    settings = replace(settings, max_review_cycles=1)
    github = FakeGitHub(settings, data)

    class Unapproved(FakeAgent):
        def run(self, role, prompt, cwd, editable=False):
            if role == "reviewer":
                return {"approved": False, "findings": [{"severity": "P3", "blocking": False}]}
            return super().run(role, prompt, cwd, editable)

    engine = Engine(settings, github=github, agent_factory=Unapproved)
    monkeypatch.setattr(engine, "_diff", lambda _: "diff")
    monkeypatch.setattr(engine, "_checks", lambda *a: [{"passed": True, "command": "pytest"}])
    Store(settings.state_dir).start("run")
    task = Assignment(
        12,
        "Task 12",
        "contract",
        "release:Beta",
        frozenset({"src/task.py", "tests/test_task.py"}),
        1,
    )
    error = engine._task("run", task)["error"]
    assert "Review findings" in error and "P3" in error


def test_string_false_qa_verdict_cannot_publish(settings, snapshot, monkeypatch):
    data, _ = snapshot
    github = FakeGitHub(settings, data)

    class InvalidQA(FakeAgent):
        def run(self, role, prompt, cwd, editable=False):
            if role == "reviewer":
                return {"approved": True, "findings": []}
            if role == "qa":
                return decode_result(role, {"approved": "false", "gaps": []})
            return super().run(role, prompt, cwd, editable)

    engine = Engine(replace(settings, max_review_cycles=1), github=github, agent_factory=InvalidQA)
    monkeypatch.setattr(engine, "_diff", lambda _: "diff")
    monkeypatch.setattr(engine, "_checks", lambda *a: [{"passed": True, "command": "pytest"}])
    store = Store(settings.state_dir)
    store.start("run")
    task = Assignment(12, "Task 12", "contract", "release:Beta", frozenset(), 1)
    assert "invalid verdict schema" in engine._task("run", task)["error"]
    assert github.created == []
    assert github.transitions[-1][1] == "blocked"


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


def test_automatic_merge_waits_for_draft_ready_then_reconciles_done(
    settings, snapshot, monkeypatch
):
    data, _ = snapshot
    settings = replace(settings, auto_merge=True)
    github = FakeGitHub(settings, data)
    github.current_branch = "branch"
    github.required_checks = lambda *a: [{"name": "Gate execution ledger", "state": "PASS"}]
    github.mark_ready_body = lambda *a: None
    engine = Engine(settings, github=github)
    commands = []
    monkeypatch.setattr("autocd.engine.command", lambda args, cwd: commands.append(args))
    store = Store(settings.state_dir)
    store.start("run")
    store.add_task("run", 12, "release:Beta", "branch", settings.state_dir / "w")
    store.head("run", 12, "f" * 40)
    store.pr("run", 12, 88)
    store.phase("run", 12, "ci_wait")
    assert engine._check_pr(store, "run", 12, 88)["status"] == "waiting_for_merge"
    assert commands == [["gh", "pr", "ready", "88", "--repo", settings.repository]]
    assert github.merges == []
    github.draft = False
    assert engine._recover(store)[0]["status"] == "merged_done"
    assert github.merges == [(88, "f" * 40)]
    assert github.verified == ["c" * 40]
    assert store.task("run", 12)["phase"] == "done"
    assert data["items"][0]["fields"]["Status"] == "Done"
    assert github.transitions[-1][1] == "done"
    assert not store.pending()


def test_automatic_merge_refuses_missing_review_or_changed_candidate(settings, snapshot):
    data, _ = snapshot
    settings = replace(settings, auto_merge=True)
    github = FakeGitHub(settings, data)
    github.current_branch = "branch"
    github.draft = False
    github.review_decision = "REVIEW_REQUIRED"
    github.required_checks = lambda *a: [{"name": "Gate execution ledger", "state": "PASS"}]
    github.mark_ready_body = lambda *a: None
    engine = Engine(settings, github=github)
    store = Store(settings.state_dir)
    store.start("run")
    store.add_task("run", 12, "release:Beta", "branch", settings.state_dir / "w")
    store.head("run", 12, "f" * 40)
    store.pr("run", 12, 88)
    store.phase("run", 12, "ci_wait")
    result = engine._check_pr(store, "run", 12, 88)
    assert result["status"] == "waiting_for_ci_or_review"
    assert "independent review" in result["reasons"][-1]
    assert not github.merges
    github.review_decision = ""
    github.merge_state_status = "BEHIND"
    assert engine._check_pr(store, "run", 12, 88)["status"] == "waiting_for_ci_or_review"
    github.merge_state_status = "CLEAN"
    first = github.pr_state
    calls = 0

    def changing_head(pr):
        nonlocal calls
        calls += 1
        state = first(pr)
        if calls == 2:
            state["headRefOid"] = "e" * 40
        return state

    github.pr_state = changing_head
    assert engine._check_pr(store, "run", 12, 88)["status"] == "waiting_for_revalidation"
    assert not github.merges
    github.pr_state = first
    github.up_to_date = False
    assert engine._check_pr(store, "run", 12, 88)["status"] == "blocked_base_moved"
    assert store.task("run", 12)["phase"] == "blocked"
    assert store.db.execute("SELECT 1 FROM events WHERE kind='base_moved'").fetchone()
    assert not github.merges


def test_merge_reconciliation_resumes_without_merging_again(settings, snapshot):
    data, _ = snapshot
    settings = replace(settings, auto_merge=True)
    github = FakeGitHub(settings, data)
    github.current_branch = "branch"
    github.pr_status = "MERGED"
    data["items"][0]["issue"].update({"state": "CLOSED", "stateReason": "COMPLETED"})
    data["items"][0]["fields"].update({"Status": "Done", "Readiness": "Done"})
    engine = Engine(settings, github=github)
    store = Store(settings.state_dir)
    store.start("run")
    store.add_task("run", 12, "release:Beta", "branch", settings.state_dir / "w")
    store.head("run", 12, "f" * 40)
    store.pr("run", 12, 88)
    store.phase("run", 12, "merged_unreconciled")
    assert engine._recover(store)[0]["status"] == "merged_done"
    assert github.merges == []
    assert not github.transitions


def test_merge_queue_and_revalidation_fail_closed(settings, snapshot):
    data, _ = snapshot
    settings = replace(settings, auto_merge=True)
    github = FakeGitHub(settings, data)
    github.current_branch = "branch"
    github.draft = False
    github.mark_ready_body = lambda *a: None
    engine = Engine(settings, github=github)
    store = Store(settings.state_dir)
    store.start("run")
    store.add_task("run", 12, "release:Beta", "branch", settings.state_dir / "w")
    store.head("run", 12, "f" * 40)
    store.pr("run", 12, 88)
    store.phase("run", 12, "ci_wait")
    checks = 0

    def changing_checks(*args):
        nonlocal checks
        checks += 1
        return [{"name": "CI", "state": "PASS" if checks == 1 else "PENDING"}]

    github.required_checks = changing_checks
    assert engine._check_pr(store, "run", 12, 88)["status"] == "waiting_for_revalidation"
    assert not github.merges
    github.required_checks = lambda *a: [{"name": "CI", "state": "PASS"}]
    github.merge_pr = lambda pr, head: github.merges.append((pr, head))
    assert engine._check_pr(store, "run", 12, 88)["status"] == "waiting_for_merge_queue"
    assert github.merges == [(88, "f" * 40)]
    assert store.task("run", 12)["phase"] == "ci_wait"


@pytest.mark.parametrize("gap", ["missing_issue", "not_completed", "done_not_persisted"])
def test_post_merge_reconciliation_preserves_retryable_state(settings, snapshot, gap):
    data, _ = snapshot
    settings = replace(settings, auto_merge=True)
    github = FakeGitHub(settings, data)
    github.current_branch = "branch"
    github.pr_status = "MERGED"
    if gap == "missing_issue":
        data["items"] = []
    elif gap == "not_completed":
        data["items"][0]["issue"].update({"state": "CLOSED", "stateReason": "NOT_PLANNED"})
    else:
        data["items"][0]["issue"].update({"state": "CLOSED", "stateReason": "COMPLETED"})
        github.transition = lambda *a: None
    engine = Engine(settings, github=github)
    store = Store(settings.state_dir)
    store.start("run")
    store.add_task("run", 12, "release:Beta", "branch", settings.state_dir / "w")
    store.head("run", 12, "f" * 40)
    store.pr("run", 12, 88)
    store.phase("run", 12, "ci_wait")
    assert engine._check_pr(store, "run", 12, 88)["status"] == "pr_check_error"
    assert store.task("run", 12)["phase"] == "merged_unreconciled"
    assert store.pending()[0]["issue"] == 12


def test_closed_unmerged_pr_blocks_and_manual_ready_is_monitored(settings, snapshot):
    data, _ = snapshot
    github = FakeGitHub(settings, data)
    github.current_branch = "branch"
    engine = Engine(settings, github=github)
    store = Store(settings.state_dir)
    store.start("run")
    store.add_task("run", 12, "release:Beta", "branch", settings.state_dir / "w")
    store.head("run", 12, "f" * 40)
    store.pr("run", 12, 88)
    store.phase("run", 12, "ready")
    github.draft = False
    assert engine._recover(store)[0]["status"] == "ready"
    github.pr_status = "CLOSED"
    assert engine._recover(store)[0]["status"] == "blocked_closed_pr"
    assert store.task("run", 12)["phase"] == "blocked"


def test_diff_includes_new_file_and_rejects_large_patch(settings, tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=worktree, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    (worktree / "README.md").write_text("base\n")
    git("add", "--all")
    git("commit", "-qm", "base")
    git("update-ref", "refs/remotes/origin/main", git("rev-parse", "HEAD"))
    (worktree / "new file.py").write_text("print('new')\n" + "# x\n" * 24_000)
    github = FakeGitHub(settings, {"items": []})
    engine = Engine(settings, github=github)
    assert "print('new')" in engine._diff(worktree)
    (worktree / "huge.py").write_text("# x\n" * 70_000)
    patch = engine._diff(worktree)
    try:
        engine._review_view(worktree, ["new file.py", "huge.py"], patch)
    except RuntimeError as error:
        assert "Source diff exceeds reviewer context limit" in str(error)
    else:
        raise AssertionError("Large source diff must be refused")


def test_large_generated_diff_uses_hashed_review_manifest(settings, tmp_path: Path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=worktree, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    (worktree / "README.md").write_text("base\n")
    git("add", "--all")
    git("commit", "-qm", "base")
    git("update-ref", "refs/remotes/origin/main", git("rev-parse", "HEAD"))
    (worktree / "README.md").write_text("reviewed source\n")
    generated = worktree / "docs-site/search-index.json"
    generated.parent.mkdir()
    generated.write_text("generated content\n" * 20_000)
    engine = Engine(settings, github=FakeGitHub(settings, {"items": []}))
    patch = engine._diff(worktree)
    assert len(patch) > 250_000
    view = engine._review_view(worktree, ["README.md", "docs-site/search-index.json"], patch)
    assert "reviewed source" in view["diff"]
    assert "generated content" not in view["diff"]
    assert view["candidate_diff_sha256"] == hashlib.sha256(patch.encode()).hexdigest()
    assert view["generated_artifacts"] == [
        {
            "path": "docs-site/search-index.json",
            "bytes": generated.stat().st_size,
            "sha256": hashlib.sha256(generated.read_bytes()).hexdigest(),
        }
    ]
    (worktree / "README.md").write_text("source change\n" * 20_000)
    with pytest.raises(RuntimeError, match="Source diff exceeds reviewer context limit"):
        engine._review_view(
            worktree, ["README.md", "docs-site/search-index.json"], engine._diff(worktree)
        )
    (worktree / "README.md").write_text("base\n")
    generated_only = engine._review_view(
        worktree, ["docs-site/search-index.json"], engine._diff(worktree)
    )
    assert generated_only["diff"] == ""
    magic = worktree / ":(exclude)*"
    magic.write_text("magic source must be reviewed\n")
    magic_view = engine._review_view(
        worktree, [":(exclude)*", "docs-site/search-index.json"], engine._diff(worktree)
    )
    assert "magic source must be reviewed" in magic_view["diff"]


def test_generated_review_manifest_rejects_symlink_and_directory(settings, tmp_path: Path):
    engine = Engine(settings, github=FakeGitHub(settings, {"items": []}))
    generated = tmp_path / "docs-site/search-index.json"
    generated.parent.mkdir()
    generated.symlink_to(tmp_path / "outside.json")
    with pytest.raises(RuntimeError, match="symlink"):
        engine._review_view(tmp_path, ["docs-site/search-index.json"], "small diff")
    generated.unlink()
    generated.mkdir()
    with pytest.raises(RuntimeError, match="not a regular file"):
        engine._review_view(tmp_path, ["docs-site/search-index.json"], "small diff")
    generated.rmdir()
    view = engine._review_view(tmp_path, ["docs-site/search-index.json"], "small diff")
    assert view["generated_artifacts"] == [{"path": "docs-site/search-index.json", "deleted": True}]


def _git(worktree: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=worktree,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
    )


def test_normalize_fixes_what_git_diff_check_and_ruff_format_reject(settings, tmp_path):
    worktree = tmp_path / "wt"
    (worktree / "caliber/src/caliber").mkdir(parents=True)
    (worktree / "docs").mkdir()
    (worktree / "docs-site").mkdir()
    venv = worktree / "caliber/.venv/bin"
    venv.mkdir(parents=True)
    (venv / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    (venv / "python").chmod(0o755)
    (worktree / ".gitignore").write_text("caliber/.venv/\n")
    module = worktree / "caliber/src/caliber/mod.py"
    note = worktree / "docs/note.md"
    generated = worktree / "docs-site/m-page.md"
    binary = worktree / "docs/blob.md"
    latin = worktree / "docs/latin.md"
    for path in (module, note, generated, binary, latin):
        path.write_text("x = 1\n" if path.suffix == ".py" else "base\n")
    assert _git(worktree, "init", "-q").returncode == 0
    _git(worktree, "add", "-A")
    assert _git(worktree, "commit", "-q", "-m", "base").returncode == 0
    module.write_text("import os\nx = {'a':1,   'b':2}   \n\n\n")
    note.write_text("title  \nline\t\n\n\n")
    generated.write_text("generated  \n")
    binary.write_bytes(b"a \x00 \n")
    latin.write_bytes(b"caf\xe9  \n")
    changed = [
        "caliber/src/caliber/mod.py",
        "docs/note.md",
        "docs-site/m-page.md",
        "docs/blob.md",
        "docs/latin.md",
        "docs/deleted.md",
    ]
    assert _git(worktree, "diff", "--check").returncode != 0
    store = Store(settings.state_dir)
    store.start("run")
    Engine(settings, github=object())._normalize(worktree, changed, store, "run", 12)
    assert module.read_text() == 'x = {"a": 1, "b": 2}\n'
    assert note.read_text() == "title\nline\n"
    assert generated.read_text() == "generated  \n"
    assert binary.read_bytes() == b"a \x00 \n"
    assert latin.read_bytes() == b"caf\xe9  \n"
    diff = _git(worktree, "diff", "--check", "--", "caliber", "docs/note.md")
    assert diff.returncode == 0, diff.stdout
    (detail,) = [
        json.loads(row[0])
        for row in store.db.execute("SELECT detail FROM events WHERE kind='normalize'")
    ]
    assert [step["step"] for step in detail["steps"]] == ["ruff-fix", "ruff-format", "whitespace"]
    assert all(step["exit_code"] == 0 for step in detail["steps"])


def test_normalize_without_a_backend_venv_still_fixes_whitespace(settings, tmp_path):
    worktree = tmp_path / "wt"
    (worktree / "caliber/src").mkdir(parents=True)
    module = worktree / "caliber/src/mod.py"
    module.write_text("x=1   \n")
    empty = worktree / "empty.md"
    empty.write_text("\n\n")
    store = Store(settings.state_dir)
    store.start("run")
    engine = Engine(settings, github=object())
    engine._normalize(worktree, ["caliber/src/mod.py", "empty.md"], store, "run", 12)
    assert module.read_text() == "x=1\n"
    assert empty.read_text() == ""
    engine._normalize(worktree, ["missing.md"], store, "run", 12)
    rows = list(store.db.execute("SELECT detail FROM events WHERE kind='normalize'"))
    assert len(rows) == 1


def test_normalize_records_a_step_that_cannot_start(settings, tmp_path, monkeypatch):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    (worktree / "a.md").write_text("a  \n")

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired("python3", 300)

    monkeypatch.setattr(subprocess, "run", boom)
    store = Store(settings.state_dir)
    store.start("run")
    Engine(settings, github=object())._normalize(worktree, ["a.md"], store, "run", 12)
    (row,) = store.db.execute("SELECT detail FROM events WHERE kind='normalize'")
    assert json.loads(row[0])["steps"][0]["step"] == "whitespace"
    assert "timed out" in json.loads(row[0])["steps"][0]["error"]


def _ci_local_transcript() -> str:
    bold = "\x1b[1m{}\x1b[0m"
    return "\n".join(
        [
            bold.format("── docs-validation ─────────────"),
            "docs ok",
            bold.format("── type-check ─────────────"),
            "src/caliber/routes/events_stream.py:42: error: Incompatible types",
            "Found 1 error in 1 file",
            "\x1b[31m✗ type-check (31s)\x1b[0m",
            bold.format("── test-smoke ─────────────"),
            *["noise " * 20] * 200,
            bold.format("── summary ─────────────"),
            "PASS  docs-validation  80s",
            "FAIL  type-check  31s",
            "PASS  test-smoke  70s",
            "",
            "\x1b[31mlocal CI FAILED\x1b[0m",
        ]
    )


def test_ci_local_output_keeps_summary_and_failing_job_section():
    from autocd.engine import _ANSI

    text = _ANSI.sub("", _ci_local_transcript())
    output = Engine._check_output(["scripts/ci-local.sh", "type-check"], text)
    assert output.startswith("── summary\nPASS  docs-validation")
    assert "FAIL  type-check" in output
    assert "events_stream.py:42: error: Incompatible types" in output
    assert "noise" not in output
    # Without a summary, or for other commands, the plain tail is kept.
    assert Engine._check_output(["scripts/ci-local.sh"], "x" * 5000) == "x" * 3000
    assert Engine._check_output(["pytest"], text) == text[-3000:]
    # A FAIL line whose section is missing still reports the summary.
    assert Engine._check_output(["scripts/ci-local.sh"], "── summary ──\nFAIL  ghost  1s") == (
        "── summary\nFAIL  ghost  1s"
    )


def test_checks_merge_streams_so_the_ci_local_summary_survives(
    settings, tmp_path: Path, monkeypatch
):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    transcript = tmp_path / "transcript.txt"
    transcript.write_text(_ci_local_transcript())
    script = scripts / "ci-local.sh"
    # Noise on stderr after the summary on stdout: appending stderr last used to bury it.
    script.write_text(
        "#!/bin/sh\n"
        f'cat "{transcript}"\n'
        "i=0; while [ $i -lt 300 ]; do echo 'pip warning noise' >&2; i=$((i+1)); done\n"
        "exit 1\n"
    )
    script.chmod(0o755)
    engine = Engine(replace(settings, base_checks=("scripts/ci-local.sh type-check",)))
    row = engine._checks(12, tmp_path, [])[0]
    assert row["passed"] is False and row["exit_code"] == 1
    assert "FAIL  type-check" in row["output"]
    assert "Incompatible types" in row["output"]
    assert "\x1b[" not in row["output"]


def _published(settings, store: Store) -> None:
    store.start("run")
    store.add_task("run", 12, "release:Beta", "branch", settings.state_dir / "w")
    store.head("run", 12, "f" * 40)
    store.pr("run", 12, 88)
    store.phase("run", 12, "ci_wait")


def test_unresolved_threads_do_not_keep_an_owner_pr_in_draft(settings, snapshot, monkeypatch):
    data, _ = snapshot
    github = FakeGitHub(settings, data)
    github.required_checks = lambda pr, state: [{"name": "CI", "state": "PASS"}]
    github.unresolved_threads = lambda pr: 3
    github.mark_ready_body = lambda pr, checks: None
    monkeypatch.setattr("autocd.engine.command", lambda args, cwd: None)
    store = Store(settings.state_dir)
    _published(settings, store)
    result = Engine(settings, github=github)._check_pr(store, "run", 12, 88)
    assert result["status"] == "ready_for_owner"
    assert store.task("run", 12)["phase"] == "ready"


def test_automatic_merge_still_waits_on_unresolved_threads(settings, snapshot):
    data, _ = snapshot
    auto = replace(settings, auto_merge=True)
    github = FakeGitHub(auto, data)
    github.required_checks = lambda pr, state: [{"name": "CI", "state": "PASS"}]
    github.unresolved_threads = lambda pr: 1
    store = Store(auto.state_dir)
    _published(auto, store)
    result = Engine(auto, github=github)._check_pr(store, "run", 12, 88)
    assert result["status"] not in {"ready_for_owner", "waiting_for_merge"}
    assert store.task("run", 12)["phase"] == "ci_wait"


def test_coverage_evidence_reads_a_real_coverage_py_report(tmp_path: Path):
    package = tmp_path / "caliber/src/pkg"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "mod.py").write_text(
        "def value(flag):\n    if flag:\n        return 1\n    return 2\n"
    )
    tests = tmp_path / "caliber/tests"
    tests.mkdir()
    (tests / "test_mod.py").write_text(
        "from pkg.mod import value\n\n\ndef test_value():\n"
        "    assert value(True) == 1\n    assert value(False) == 2\n"
    )
    report = tmp_path / "coverage.json"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "caliber/tests/test_mod.py",
            "--cov=pkg",
            "--cov-branch",
            f"--cov-report=json:{report}",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=tmp_path,
        env={**__import__("os").environ, "PYTHONPATH": str(tmp_path / "caliber/src")},
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    raw = json.loads(report.read_text())
    assert all("summary" in row for row in raw["files"].values())
    evidence = Engine._coverage_evidence(report, tmp_path, ["caliber/src/pkg/mod.py"])
    assert evidence["passed"], evidence
    assert "caliber/src/pkg/mod.py=100.00%" in evidence["summary"]
    (tests / "test_mod.py").write_text(
        "from pkg.mod import value\n\n\ndef test_value():\n    assert value(True) == 1\n"
    )
    subprocess.run(
        proc.args,
        cwd=tmp_path,
        env={**__import__("os").environ, "PYTHONPATH": str(tmp_path / "caliber/src")},
        capture_output=True,
        check=False,
    )
    weak = Engine._coverage_evidence(report, tmp_path, ["caliber/src/pkg/mod.py"])
    assert not weak["passed"] and "below the 95% threshold" in weak["reason"]
    assert "missing lines [4]" in weak["reason"] and "missing branches [[2, 4]]" in weak["reason"]
    migration = tmp_path / "caliber/src/caliber/db/migrations/0001_x.py"
    migration.parent.mkdir(parents=True)
    migration.write_text("x = 1\n")
    skipped = Engine._coverage_evidence(
        report, tmp_path, ["caliber/src/caliber/db/migrations/0001_x.py"]
    )
    assert skipped["passed"], skipped


class _FixLoopAgent(FakeAgent):
    prompts: ClassVar[list[tuple[str, dict]]] = []

    def run(self, role, prompt, cwd, editable=False):
        if role in {"developer", "reviewer"}:
            self.prompts.append((role, json.loads(prompt)))
        if role == "reviewer":
            return {"approved": True, "findings": []}
        return super().run(role, prompt, cwd, editable)


def _fix_loop_engine(settings, snapshot, monkeypatch, results):
    data, _ = snapshot
    github = FakeGitHub(settings, data)
    _FixLoopAgent.prompts = []
    _FixLoopAgent.calls = []
    engine = Engine(settings, github=github, agent_factory=_FixLoopAgent)
    monkeypatch.setattr(engine, "_diff", lambda _: "diff")
    rounds = iter(results)
    monkeypatch.setattr(engine, "_checks", lambda *a: next(rounds))
    return engine, github


def test_failing_checks_return_to_the_developer_before_any_review(settings, snapshot, monkeypatch):
    settings = replace(settings, max_fix_attempts=3, max_review_cycles=1)
    lint = {"command": "scripts/ci-local.sh lint", "passed": False, "output": "E501"}
    cov = {"command": "pytest --cov", "passed": False, "output": "Coverage for x is not greater"}
    ok = [{"command": "pytest --cov", "passed": True, "output": "100%"}]
    engine, github = _fix_loop_engine(settings, snapshot, monkeypatch, [[lint, cov], ok])
    result = engine.run()
    assert result["results"][0]["status"] == "waiting_for_ci_or_review"
    roles = [role for role, _ in _FixLoopAgent.prompts]
    assert roles == ["developer", "developer", "reviewer"]
    second = _FixLoopAgent.prompts[1][1]
    assert second["fix_attempt"] == 1 and second["review_cycle"] == 1
    assert "E501" in second["previous_feedback"] and "not greater" in second["previous_feedback"]
    assert second["instruction"].startswith("Fix every failing coordinator check")
    # The reviewer rechecks what the cycle started from, not the fixed check output.
    assert _FixLoopAgent.prompts[2][1]["previous_feedback"] == ""
    assert github.created


def test_fix_attempts_are_bounded_then_the_reviewer_sees_the_failure(
    settings, snapshot, monkeypatch
):
    settings = replace(settings, max_fix_attempts=2, max_review_cycles=1)
    bad = [{"command": "scripts/ci-local.sh lint", "passed": False, "output": "E501"}]
    engine, github = _fix_loop_engine(settings, snapshot, monkeypatch, [bad, bad, bad])
    Store(settings.state_dir).start("run")
    task = Assignment(
        12,
        "Task 12",
        "contract",
        "release:Beta",
        frozenset({"src/task.py", "tests/test_task.py"}),
        1,
    )
    assert "QA command failures" in engine._task("run", task)["error"]
    roles = [role for role, _ in _FixLoopAgent.prompts]
    assert roles == ["developer", "developer", "developer", "reviewer"]
    assert [p["fix_attempt"] for r, p in _FixLoopAgent.prompts if r == "developer"] == [0, 1, 2]
    assert not github.created
    store = Store(settings.state_dir)
    attempts = [
        json.loads(row[0])["fix_attempt"]
        for row in store.db.execute("SELECT detail FROM events WHERE kind='developer_result'")
    ]
    assert attempts == [0, 1, 2]
    assert store.task("run", 12)["cycles"] == 1


def test_handoff_gap_on_the_last_fix_attempt_skips_review(settings, snapshot, monkeypatch):
    settings = replace(settings, max_fix_attempts=1, max_review_cycles=1)
    data, _ = snapshot
    github = FakeGitHub(settings, data)
    turns = iter([False, True])
    calls: list[str] = []

    class Gappy(FakeAgent):
        def run(self, role, prompt, cwd, editable=False):
            calls.append(role)
            result = super().run(role, prompt, cwd, editable)
            if role == "developer" and next(turns):
                result = {**result, "risks": ""}
            return result

    engine = Engine(settings, github=github, agent_factory=Gappy)
    monkeypatch.setattr(engine, "_diff", lambda _: "diff")
    monkeypatch.setattr(
        engine, "_checks", lambda *a: [{"command": "pytest", "passed": False, "output": "x"}]
    )
    Store(settings.state_dir).start("run")
    task = Assignment(
        12,
        "Task 12",
        "contract",
        "release:Beta",
        frozenset({"src/task.py", "tests/test_task.py"}),
        1,
    )
    assert "omitted required PR context" in engine._task("run", task)["error"]
    assert calls == ["developer", "developer"]


def test_checks_keep_running_after_a_failing_gate(settings, tmp_path: Path, monkeypatch):
    ran = []

    class Proc:
        def __init__(self, code):
            self.returncode = code
            self.stdout = "out"
            self.stderr = None

    def fake_run(args, **kwargs):
        ran.append(args)
        return Proc(1 if args[0] == "first" else 0)

    monkeypatch.setattr("autocd.engine.subprocess.run", fake_run)
    engine = Engine(replace(settings, base_checks=("first gate", "second gate"), issue_checks={}))
    rows = engine._checks(12, tmp_path, ["notes/unsupported.txt"])
    assert [row["command"] for row in rows] == [
        "affected-component checks",
        "first gate",
        "second gate",
    ]
    assert [row["passed"] for row in rows] == [False, False, True]
    assert [args[0] for args in ran] == ["first", "second"]


class _ReconcileGitHub(FakeGitHub):
    def __init__(self, settings, data):
        super().__init__(settings, data)
        self.released = []
        self.fail_transition = False

    def release_claim(self, snapshot, issue, owner):
        self.released.append((issue, owner))

    def transition(self, target, issue, phase, payload):
        if self.fail_transition and phase == "ready":
            raise RuntimeError("helper refused SECRET")
        super().transition(target, issue, phase, payload)


def _blocked_yesterday(
    store: Store, issue: int = 12, failure: str = "Review/QA did not pass within 3 cycles: x"
):
    store.start("prior")
    store.add_task("prior", issue, "release:Beta", "autocd/issue-12-prior", store.path.parent / "w")
    store.phase("prior", issue, "blocked", failure=failure)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE events SET at='2000-01-01T00:00:00+00:00' WHERE run_id='prior'")


def _claimed(data, issue=12, **issue_fields):
    item = next(i for i in data["items"] if i["issue"]["number"] == issue)
    item["fields"].update({"Status": "In Progress", "Readiness": "Blocked"})
    item["issue"].update({"assignees": ["rrahimi-uci"], **issue_fields})
    return item


def _midnight(settings):
    day = datetime.now(ZoneInfo(settings.timezone)).date()
    from datetime import time as day_time

    return datetime.combine(day, day_time.min, ZoneInfo(settings.timezone))


def test_auto_reconcile_returns_a_clean_pre_pr_block_to_ready(settings, snapshot):
    data, _ = snapshot
    _claimed(data)
    store = Store(settings.state_dir)
    _blocked_yesterday(store)
    github = _ReconcileGitHub(settings, data)
    engine = Engine(replace(settings, max_auto_retries=2), github=github)
    store.start("run")
    assert engine._auto_reconcile(store, "run", data, set(), _midnight(settings)) == [12]
    assert github.released == [(12, "rrahimi-uci")]
    issue, phase, payload = github.transitions[-1]
    assert (issue, phase) == (12, "ready")
    assert "review_qa_exhausted" in payload["evidence"][0]
    assert "autocd/issue-12-prior" in payload["evidence"][0]
    assert store.event_count(12, "auto_reconciled") == 1
    # The cap stops a hopeless task from being retried every day.
    engine2 = Engine(replace(settings, max_auto_retries=1), github=github)
    assert engine2._auto_reconcile(store, "run", data, set(), _midnight(settings)) == []


@pytest.mark.parametrize(
    "case",
    [
        "developer_blocker",
        "unclassified",
        "pushed_head",
        "published_pr",
        "remote_branch",
        "blocked_today",
        "other_assignee",
        "open_pr",
        "not_blocked_in_project",
        "missing_item",
    ],
)
def test_auto_reconcile_leaves_ambiguous_or_human_blocks_alone(settings, snapshot, case):
    data, _ = snapshot
    item = _claimed(data)
    store = Store(settings.state_dir)
    failure = {
        "developer_blocker": "Developer blocker: needs profiling",
        "unclassified": "something unexpected",
    }.get(case, "Review/QA did not pass within 3 cycles: x")
    _blocked_yesterday(store, failure=failure)
    branches: set[str] = set()
    if case == "pushed_head":
        store.head("prior", 12, "a" * 40)
    elif case == "published_pr":
        store.pr("prior", 12, 5)
    elif case == "remote_branch":
        branches = {"autocd/issue-12-prior"}
    elif case == "blocked_today":
        store.phase("prior", 12, "blocked", failure=failure)
    elif case == "other_assignee":
        item["issue"]["assignees"] = ["someone-else"]
    elif case == "open_pr":
        item["issue"]["prs"] = [{"state": "OPEN"}]
    elif case == "not_blocked_in_project":
        item["fields"]["Readiness"] = "Ready"
    elif case == "missing_item":
        data["items"] = [i for i in data["items"] if i["issue"]["number"] != 12]
    github = _ReconcileGitHub(settings, data)
    engine = Engine(replace(settings, max_auto_retries=2), github=github)
    store.start("run")
    assert engine._auto_reconcile(store, "run", data, branches, _midnight(settings)) == []
    assert github.released == [] and github.transitions == []


def test_auto_reconcile_records_a_refused_transition_and_keeps_going(settings, snapshot):
    data, _ = snapshot
    _claimed(data)
    store = Store(settings.state_dir)
    _blocked_yesterday(store)
    github = _ReconcileGitHub(settings, data)
    github.fail_transition = True
    engine = Engine(replace(settings, max_auto_retries=2), github=github)
    store.start("run")
    assert engine._auto_reconcile(store, "run", data, set(), _midnight(settings)) == []
    row = store.db.execute("SELECT detail FROM events WHERE kind='auto_reconcile_error'").fetchone()
    assert "helper refused" in row[0]
    assert store.event_count(12, "auto_reconciled") == 0


def test_auto_reconcile_skips_release_when_claim_already_cleared(settings, snapshot):
    data, _ = snapshot
    item = _claimed(data)
    item["issue"]["assignees"] = []
    item["fields"]["Status"] = "Todo"
    store = Store(settings.state_dir)
    _blocked_yesterday(store, failure="reviewer returned an error (error_max_turns)")
    github = _ReconcileGitHub(settings, data)
    engine = Engine(replace(settings, max_auto_retries=2), github=github)
    store.start("run")
    assert engine._auto_reconcile(store, "run", data, set(), _midnight(settings)) == [12]
    assert github.released == []


def test_scheduled_run_auto_reconciles_then_retries_the_task(settings, snapshot, monkeypatch):
    data, _ = snapshot
    settings = replace(settings, enabled=True, max_auto_retries=2)
    store = Store(settings.state_dir)
    _blocked_yesterday(store)
    github = _ReconcileGitHub(settings, data)
    reconciled = []

    def reconcile(store_, run, snap, branches, midnight):
        reconciled.append(run)
        github.ready_reconciled = True
        return [12]

    engine = Engine(settings, github=github, agent_factory=FakeAgent)
    monkeypatch.setattr(engine, "_auto_reconcile", reconcile)
    monkeypatch.setattr(
        engine, "due_date", lambda: datetime.now(ZoneInfo(settings.timezone)).date().isoformat()
    )
    launched = []
    monkeypatch.setattr(
        engine,
        "_task",
        lambda run, task, retry_branch=None: (
            launched.append((task.issue, retry_branch)),
            {"issue": task.issue, "status": "queued"},
        )[1],
    )
    result = engine.run(scheduled=True)
    assert reconciled and result["selected"] == [12]
    assert launched == [(12, "autocd/issue-12-prior")]
    # Manual runs never auto-reconcile.
    reconciled.clear()
    engine.run()
    assert reconciled == []


def test_release_claim_unassigns_and_resets_status(settings, monkeypatch):
    from autocd.github import CommandError, GitHub

    calls = []
    monkeypatch.setattr("autocd.github.command", lambda args, cwd, **k: calls.append(args) or "")
    snap = {
        "project": {"id": "PVT_1"},
        "fields": [
            {"id": "F_status", "name": "Status", "options": [{"id": "opt_todo", "name": "Todo"}]}
        ],
        "items": [{"id": "ITEM_12", "issue": {"number": 12}}],
    }
    GitHub(settings).release_claim(snap, 12, "rrahimi-uci")
    assert calls[0][:3] == ["gh", "issue", "edit"] and calls[0][-1] == "rrahimi-uci"
    assert calls[1] == [
        "gh",
        "project",
        "item-edit",
        "--project-id",
        "PVT_1",
        "--id",
        "ITEM_12",
        "--field-id",
        "F_status",
        "--single-select-option-id",
        "opt_todo",
    ]
    with pytest.raises(CommandError):
        GitHub(settings).release_claim({**snap, "items": []}, 12, "rrahimi-uci")


def test_candidate_containing_a_coordinator_credential_is_never_published(
    settings, snapshot, monkeypatch
):
    data, _ = snapshot
    settings = replace(settings, max_review_cycles=1)
    github = FakeGitHub(settings, data)
    pushed = []
    github.commit_and_push = lambda *a: pushed.append(a) or "f" * 40

    class Approve(FakeAgent):
        def run(self, role, prompt, cwd, editable=False):
            if role == "reviewer":
                return {"approved": True, "findings": []}
            return super().run(role, prompt, cwd, editable)

    monkeypatch.setenv("OPENAI_API_KEY", "sk-live-secret-value-0123456789")
    engine = Engine(settings, github=github, agent_factory=Approve)
    monkeypatch.setattr(engine, "_diff", lambda _: "+ key = 'sk-live-secret-value-0123456789'")
    monkeypatch.setattr(
        engine, "_checks", lambda *a: [{"command": "pytest", "passed": True, "output": ""}]
    )
    Store(settings.state_dir).start("run")
    task = Assignment(
        12,
        "Task 12",
        "contract",
        "release:Beta",
        frozenset({"src/task.py", "tests/test_task.py"}),
        1,
    )
    error = engine._task("run", task)["error"]
    assert error == "Candidate contains a coordinator credential; not published"
    assert pushed == [] and github.created == []


def _published_task(settings, store, phase="ci_wait"):
    store.start("run")
    store.add_task("run", 12, "release:Beta", "branch", settings.state_dir / "w")
    store.head("run", 12, "f" * 40)
    store.pr("run", 12, 88)
    store.phase("run", 12, phase)


def test_post_pr_bookkeeping_failure_never_blocks_and_is_retried(settings, snapshot):
    data, _ = snapshot
    github = FakeGitHub(settings, data)
    attempts = []

    def flaky(target, issue, phase, payload):
        attempts.append(phase)
        if len(attempts) == 1:
            raise RuntimeError("PR not linked yet")
        github.transitions.append((issue, phase, payload))

    github.transition = flaky
    engine = Engine(settings, github=github)
    store = Store(settings.state_dir)
    _published_task(settings, store)
    store.event("run", 12, "publish_evidence", {"evidence": "PR #88 at fff; QA passed"})
    assert engine._post_pr_bookkeeping(store, "run", 12, 88) is False
    assert store.task("run", 12)["phase"] == "ci_wait"
    assert store.db.execute("SELECT 1 FROM events WHERE kind='post_pr_error'").fetchone()
    # The next PR check retries and completes both steps exactly once.
    engine._check_pr(store, "run", 12, 88)
    assert github.transitions[-1][1] == "review"
    assert github.transitions[-1][2] == {"evidence": ["PR #88 at fff; QA passed"]}
    assert github.reviewers == [(88, "rrahimi-uci")]
    assert engine._post_pr_bookkeeping(store, "run", 12, 88) is True
    assert attempts == ["review", "review"] and len(github.reviewers) == 1


def test_post_pr_bookkeeping_without_recorded_evidence(settings, snapshot):
    data, _ = snapshot
    github = FakeGitHub(settings, data)
    store = Store(settings.state_dir)
    _published_task(settings, store)
    assert Engine(settings, github=github)._post_pr_bookkeeping(store, "run", 12, 88)
    assert "PR #88 at " in github.transitions[-1][2]["evidence"][0]


def test_merged_pr_completes_even_after_the_owner_changed_the_head(settings, snapshot):
    data, _ = snapshot
    github = FakeGitHub(settings, data)
    github.pr_status = "MERGED"
    reconciled = []
    engine = Engine(settings, github=github)
    engine._reconcile_merged = lambda store, run, issue, pr, state: (
        reconciled.append(pr) or {"issue": issue, "status": "done"}
    )
    store = Store(settings.state_dir)
    _published_task(settings, store)
    store.head("run", 12, "a" * 40)  # the owner pushed a fix before merging
    assert engine._check_pr(store, "run", 12, 88)["status"] == "done"
    assert reconciled == [88]


def test_persistent_pr_check_error_times_out_instead_of_stalling(settings, snapshot, monkeypatch):
    data, _ = snapshot
    settings = replace(settings, ci_timeout_seconds=60)
    github = FakeGitHub(settings, data)

    def broken(pr):
        raise RuntimeError("HTTP 504")

    github.pr_state = broken
    engine = Engine(settings, github=github)
    store = Store(settings.state_dir)
    _published_task(settings, store)
    assert engine._check_pr(store, "run", 12, 88)["status"] == "pr_check_error"
    assert store.task("run", 12)["phase"] == "ci_wait"
    from datetime import timedelta

    monkeypatch.setattr(
        store, "phase_started", lambda *a: datetime.now(UTC) - timedelta(seconds=120)
    )
    assert engine._check_pr(store, "run", 12, 88)["status"] == "blocked_pr_check_error"
    assert store.task("run", 12)["phase"] == "blocked"
    assert store.active() == []


def test_interrupted_task_is_marked_blocked_in_project_too(settings, snapshot):
    data, _ = snapshot
    github = FakeGitHub(settings, data)
    engine = Engine(settings, github=github)
    store = Store(settings.state_dir)
    store.start("run")
    store.add_task("run", 12, "release:Beta", "branch", settings.state_dir / "w")
    store.phase("run", 12, "developing")
    assert engine._recover(store) == [{"issue": 12, "status": "blocked_after_interruption"}]
    assert github.transitions[-1][:2] == (12, "blocked")

    def refuse(*a):
        raise RuntimeError("helper refused")

    github.transition = refuse
    store.start("run2")
    store.add_task("run2", 13, "release:Beta", "branch2", settings.state_dir / "w2")
    store.phase("run2", 13, "qa")
    engine._recover(store)
    assert store.db.execute("SELECT 1 FROM events WHERE kind='remote_block_error'").fetchone()


def test_blocked_task_whose_pr_the_owner_merged_reaches_done(settings, snapshot):
    data, _ = snapshot
    github = FakeGitHub(settings, data)
    engine = Engine(settings, github=github)
    done = []
    engine._reconcile_merged = lambda store, run, issue, pr, state: (
        done.append((run, issue, pr)) or {"issue": issue, "status": "done"}
    )
    store = Store(settings.state_dir)
    _published_task(settings, store, phase="blocked")
    assert engine._recover_merged_blocked(store) == []  # still open: left alone
    github.pr_status = "MERGED"
    assert engine._recover_merged_blocked(store) == [{"issue": 12, "status": "done"}]
    assert done == [("run", 12, 88)]

    def broken(pr):
        raise RuntimeError("HTTP 502")

    github.pr_state = broken
    assert engine._recover_merged_blocked(store) == []
    assert store.db.execute("SELECT 1 FROM events WHERE kind='pr_check_error'").fetchone()
    # A newer attempt for the issue supersedes the old blocked one.
    store.start("run2")
    store.add_task("run2", 12, "release:Beta", "branch2", settings.state_dir / "w2")
    assert store.blocked_with_pr() == []
