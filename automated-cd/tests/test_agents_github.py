from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from autocd.agents import AgentError, ClaudeAgent, role_prompt
from autocd.github import CommandError, GitHub, command


def _fake_cli(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "claude"
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o700)
    return path


def test_claude_agent_read_only_and_json_result(settings, tmp_path: Path, monkeypatch):
    _fake_cli(tmp_path, 'printf \'%s\\n\' \'{"type":"result","result":"{\\"approved\\":true}"}\'')
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    events = []
    result = ClaudeAgent(settings, lambda kind, detail: events.append(kind)).run(
        "qa", "test", tmp_path
    )
    assert result == {"approved": True}
    assert events == ["agent_started", "agent_finished"]
    assert "Never merge" in role_prompt("qa")
    with pytest.raises(ValueError):
        role_prompt("merge")


def test_claude_agent_failure_and_stall(settings, tmp_path: Path, monkeypatch):
    _fake_cli(tmp_path, "echo bad >&2; exit 2")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    agent = ClaudeAgent(settings, lambda kind, detail: None)
    with pytest.raises(AgentError, match="failed"):
        agent.run("qa", "test", tmp_path)
    _fake_cli(tmp_path, "sleep 2")
    with pytest.raises(AgentError, match="stalled"):
        ClaudeAgent(
            replace(settings, stall_seconds=1, agent_timeout_seconds=3), lambda kind, detail: None
        ).run("qa", "test", tmp_path)


def test_claude_agent_stream_variants_and_timeout(settings, tmp_path: Path, monkeypatch):
    _fake_cli(
        tmp_path,
        'printf \'%s\\n\' \'not-json\' \'{"type":"assistant"}\' \'{"type":"result","result":"plain text"}\'',
    )
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    events = []
    assert ClaudeAgent(settings, lambda kind, detail: events.append(kind)).run(
        "reviewer", "test", tmp_path
    ) == {"text": "plain text"}
    assert "agent_progress" in events
    _fake_cli(tmp_path, 'echo \'{"type":"result","result":{"ok":true}}\'')
    assert ClaudeAgent(settings, lambda *a: None).run("qa", "test", tmp_path) == {"ok": True}
    _fake_cli(tmp_path, 'echo \'{"type":"result","is_error":true}\'')
    with pytest.raises(AgentError, match="returned an error"):
        ClaudeAgent(settings, lambda *a: None).run("qa", "test", tmp_path)
    _fake_cli(tmp_path, "sleep 2")
    with pytest.raises(AgentError, match="timed out"):
        ClaudeAgent(
            replace(settings, stall_seconds=2, agent_timeout_seconds=1), lambda *a: None
        ).run("qa", "test", tmp_path)


def test_command_reports_failure_without_shell(tmp_path: Path):
    assert command(["/bin/echo", "safe"], tmp_path).strip() == "safe"
    with pytest.raises(CommandError, match="exited 3"):
        command(["/bin/sh", "-c", "exit 3"], tmp_path)


def test_project_snapshot_rejects_wrong_repo(settings, monkeypatch):
    github = GitHub(settings)
    monkeypatch.setattr(
        "autocd.github.command",
        lambda *a, **k: json.dumps(
            {"repository": {"nameWithOwner": "other/repo", "defaultBranch": "main"}}
        ),
    )
    with pytest.raises(CommandError, match="repository"):
        github.snapshot()


def test_project_snapshot_and_overview(settings, monkeypatch):
    github = GitHub(settings)
    calls = []

    def capture(args, cwd, **kwargs):
        calls.append(args)
        if args[0] == "node":
            return json.dumps(
                {
                    "repository": {
                        "nameWithOwner": settings.repository,
                        "defaultBranch": settings.base_branch,
                    }
                }
            )
        if args[:3] == ["gh", "api", "user"]:
            return "worker\n"
        if args[:3] == ["gh", "pr", "list"]:
            return "[]"
        if args[:3] == ["gh", "run", "list"]:
            return "[]"
        if "branches?" in args[-1]:
            return '[[{"name":"main"}]]'
        return '[{"sha":"a"}]'

    monkeypatch.setattr("autocd.github.command", capture)
    assert github.snapshot()["repository"]["nameWithOwner"] == settings.repository
    assert github.overview()["branches"][0][0]["name"] == "main"
    assert github.login() == "worker"
    assert len(calls) == 6


def test_project_snapshot_rejects_wrong_branch(settings, monkeypatch):
    github = GitHub(settings)
    monkeypatch.setattr(
        "autocd.github.command",
        lambda *a, **k: json.dumps(
            {"repository": {"nameWithOwner": settings.repository, "defaultBranch": "trunk"}}
        ),
    )
    with pytest.raises(CommandError, match="base branch"):
        github.snapshot()


def test_transition_dry_run_before_apply(settings, monkeypatch):
    github = GitHub(settings)
    calls = []

    def capture(args, cwd, **kwargs):
        calls.append(args)
        return "{}"

    monkeypatch.setattr("autocd.github.command", capture)
    github.transition("release:Beta", 12, "claim", {"owner": "worker", "branch": "autocd/12"})
    assert len(calls) == 2
    assert "--apply" not in calls[0]
    assert calls[1][-1] == "--apply"


def test_mark_ready_body_is_idempotent(settings, monkeypatch):
    github = GitHub(settings)
    marker = "- [ ] This PR is ready for review only after all applicable validation passes; merge requires the gates in `AGENTS.md`."
    calls = []
    body = {"value": marker}

    def capture(args, cwd, **kwargs):
        calls.append(args)
        if args[:3] == ["gh", "pr", "view"]:
            return json.dumps({"body": body["value"]})
        if args[:3] == ["gh", "pr", "edit"]:
            body["value"] = Path(args[-1]).read_text()
        return ""

    monkeypatch.setattr("autocd.github.command", capture)
    github.mark_ready_body(88, [{"name": "CI", "state": "PASS"}])
    assert "- [x] This PR" in body["value"] and "CI=PASS" in body["value"]
    count = len(calls)
    github.mark_ready_body(88, [{"name": "CI", "state": "PASS"}])
    assert len(calls) == count + 1


def test_github_worktree_commit_pr_and_checks(settings, tmp_path: Path, monkeypatch):
    github = GitHub(settings)
    calls = []

    def capture(args, cwd, **kwargs):
        calls.append(args)
        if args[:3] == ["git", "status", "--porcelain"]:
            return "?? src/task.py\n M tests/test_task.py\n"
        if args[:3] == ["git", "rev-parse", "HEAD"]:
            return "a" * 40 + "\n"
        if args[:3] == ["gh", "pr", "create"]:
            return "https://github.com/rrahimi-uci/caliber-suite/pull/88\n"
        if args[:3] == ["gh", "pr", "view"]:
            return json.dumps({"headRefOid": "a" * 40})
        if args[:3] == ["gh", "pr", "checks"]:
            return '[{"name":"CI","state":"PASS"}]'
        return ""

    monkeypatch.setattr("autocd.github.command", capture)
    branch, worktree = github.prepare_worktree(12, "abcdef1234")
    assert branch == "autocd/issue-12-abcdef12"
    assert worktree.exists() is False  # command is mocked
    assert github.changed(tmp_path) == ["src/task.py", "tests/test_task.py"]
    assert github.commit_and_push(tmp_path, branch, 12) == "a" * 40
    assert github.create_pr(tmp_path, "descriptive title", "## Summary\ntext") == 88
    assert github.pr_state(88)["headRefOid"] == "a" * 40
    assert github.required_checks(88, {"statusCheckRollup": []}) == [
        {"name": "CI", "state": "PASS"},
        {"name": "Gate execution ledger", "state": "MISSING"},
    ]
    assert any(args[:3] == ["git", "push", "-u"] for args in calls)
    worktree.mkdir(parents=True)
    with pytest.raises(CommandError, match="already exists"):
        github.prepare_worktree(12, "abcdef1234")


def test_pr_creation_and_empty_commit_fail(settings, tmp_path: Path, monkeypatch):
    github = GitHub(settings)
    monkeypatch.setattr(github, "changed", lambda _: [])
    with pytest.raises(CommandError, match="no changes"):
        github.commit_and_push(tmp_path, "branch", 12)
    monkeypatch.setattr("autocd.github.command", lambda *a, **k: "unparseable")
    with pytest.raises(CommandError, match="identify"):
        github.create_pr(tmp_path, "title", "body")


def test_reviewer_requests_record_unavailable(settings, tmp_path: Path, monkeypatch):
    github = GitHub(settings)

    def capture(args, cwd, **kwargs):
        if args[-1] == "owner":
            raise CommandError("cannot request author")
        return ""

    monkeypatch.setattr("autocd.github.command", capture)
    monkeypatch.setattr(
        github,
        "pr_state",
        lambda pr: {"author": {"login": "owner"}, "reviewRequests": [], "latestReviews": []},
    )
    status = github.request_reviewers(tmp_path, 88, "owner")
    assert status["owner"].startswith("unavailable")
    assert status["copilot"] == "not confirmed by GitHub"


def test_reviewer_requests_use_observed_review_state(settings, tmp_path: Path, monkeypatch):
    github = GitHub(settings)
    monkeypatch.setattr("autocd.github.command", lambda *a, **k: "")
    monkeypatch.setattr(
        github,
        "pr_state",
        lambda pr: {
            "author": {"login": "worker"},
            "reviewRequests": [{"login": "owner"}],
            "latestReviews": [{"author": {"login": "copilot-pull-request-reviewer[bot]"}}],
        },
    )
    assert github.request_reviewers(tmp_path, 88, "owner") == {
        "owner": "requested",
        "copilot": "reviewed",
    }


def test_review_thread_pagination(settings, monkeypatch):
    github = GitHub(settings)
    calls = []

    def capture(args, cwd, **kwargs):
        calls.append(args)
        more = len(calls) == 1
        return json.dumps(
            {
                "data": {
                    "repository": {
                        "pullRequest": {
                            "reviewThreads": {
                                "nodes": [{"isResolved": False}, {"isResolved": True}],
                                "pageInfo": {"hasNextPage": more, "endCursor": "cursor"},
                            }
                        }
                    }
                }
            }
        )

    monkeypatch.setattr("autocd.github.command", capture)
    assert github.unresolved_threads(88) == 2
    assert "after=cursor" in calls[1]


def test_missing_ready_marker_is_error(settings, monkeypatch):
    github = GitHub(settings)
    monkeypatch.setattr("autocd.github.command", lambda *a, **k: '{"body":"changed externally"}')
    with pytest.raises(CommandError, match="marker missing"):
        github.mark_ready_body(88, [])


def test_record_reviewers_updates_pr_body(settings, monkeypatch):
    github = GitHub(settings)
    current = {"body": "## Summary\nhello\n"}

    def capture(args, cwd, **kwargs):
        if args[:3] == ["gh", "pr", "view"]:
            return json.dumps(current)
        current["body"] = Path(args[-1]).read_text()
        return ""

    monkeypatch.setattr("autocd.github.command", capture)
    github.record_reviewers(88, {"owner": "unavailable", "copilot": "requested"})
    assert "owner=unavailable" in current["body"]
    with pytest.raises(CommandError, match="already recorded"):
        github.record_reviewers(88, {})


def test_required_check_rollup_fails_missing_skipped_and_pending(settings, monkeypatch):
    settings = replace(
        settings, required_checks=("ledger", "security", "pending", "skipped", "missing")
    )
    github = GitHub(settings)
    monkeypatch.setattr("autocd.github.command", lambda *a, **k: "")
    checks = github.required_checks(
        88,
        {
            "statusCheckRollup": [
                {"name": "ledger", "status": "COMPLETED", "conclusion": "SUCCESS"},
                {"name": "security", "status": "COMPLETED", "conclusion": "FAILURE"},
                {"name": "pending", "status": "IN_PROGRESS", "conclusion": None},
                {"name": "skipped", "status": "COMPLETED", "conclusion": "SKIPPED"},
            ]
        },
    )
    assert {row["name"]: row["state"] for row in checks} == {
        "ledger": "PASS",
        "security": "FAIL",
        "pending": "PENDING",
        "skipped": "SKIPPING",
        "missing": "MISSING",
    }
    own_change = github.required_checks(
        88, {"files": [{"path": "automated-cd/src/autocd/engine.py"}], "statusCheckRollup": []}
    )
    assert any(row == {"name": "Automated CD tests", "state": "MISSING"} for row in own_change)
