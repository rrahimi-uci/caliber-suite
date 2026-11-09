from __future__ import annotations

import hashlib
import json
import os
import shlex
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from autocd.agents import (
    AgentError,
    ClaudeAgent,
    _feed_prompt,
    decode_result,
    role_prompt,
    untrusted_env,
)
from autocd.github import CommandError, GitHub, audit_commands, command
from autocd.state import Store


def _fake_cli(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "claude"
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o700)
    return path


def test_claude_agent_read_only_and_json_result(settings, tmp_path: Path, monkeypatch):
    _fake_cli(
        tmp_path,
        'printf \'%s\\n\' \'{"type":"result","total_cost_usd":0.42,"result":"{\\"approved\\":true,\\"gaps\\":[]}"}\'',
    )
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    events = []
    result = ClaudeAgent(settings, lambda kind, detail: events.append((kind, detail))).run(
        "qa", "test", tmp_path
    )
    assert result == {"approved": True, "gaps": []}
    assert [kind for kind, _ in events] == ["agent_started", "agent_usage", "agent_finished"]
    assert events[1][1]["cost_usd"] == 0.42
    assert events[1][1]["elapsed_seconds"] >= 0
    assert "Never merge" in role_prompt("qa")
    with pytest.raises(ValueError):
        role_prompt("merge")


def test_claude_agent_accepts_complete_fenced_json_result(settings, tmp_path: Path, monkeypatch):
    fenced = '```json\n{"selected":[821],"reason":"Ready and unclaimed"}\n```'
    message = json.dumps({"type": "result", "result": fenced})
    _fake_cli(tmp_path, "printf '%s\\n' " + shlex.quote(message))
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    assert ClaudeAgent(settings, lambda *a: None).run("planner", "test", tmp_path) == {
        "selected": [821],
        "reason": "Ready and unclaimed",
    }


def test_claude_agent_rejects_non_json_and_partial_fences():
    assert decode_result("qa", {"approved": True, "gaps": []}) == {
        "approved": True,
        "gaps": [],
    }
    assert decode_result("qa", '```\n{"approved":true,"gaps":[]}\n```') == {
        "approved": True,
        "gaps": [],
    }
    assert decode_result(
        "reviewer",
        'Review is nearly done.\n\n```json\n{"approved":false,"findings":[{"title":"QA failed"}]}\n```',
    ) == {"approved": False, "findings": [{"title": "QA failed"}]}
    for raw in (None, "plain text", '```json\n{"approved":true}\n``` trailing prose', "[1]"):
        with pytest.raises(AgentError, match="JSON"):
            decode_result("qa", raw)
    for raw in ({"approved": "true", "findings": []}, {"approved": True, "findings": "none"}):
        with pytest.raises(AgentError, match="verdict schema"):
            decode_result("reviewer", raw)
    for raw in (
        {"approved": "false", "gaps": []},
        {"approved": True},
        {"approved": True, "gaps": "none"},
    ):
        with pytest.raises(AgentError, match="verdict schema"):
            decode_result("qa", raw)


def test_planner_requires_explicit_selection_and_reason():
    assert decode_result("planner", {"selected": [], "reason": "No safe task"}) == {
        "selected": [],
        "reason": "No safe task",
    }
    for raw in (
        {},
        {"selected": "821", "reason": "Ready"},
        {"selected": [True], "reason": "Ready"},
        {"selected": [0], "reason": "Ready"},
        {"selected": [821], "reason": " "},
    ):
        with pytest.raises(AgentError, match="invalid selection schema"):
            decode_result("planner", raw)


def _recording_cli(tmp_path: Path, record: Path) -> None:
    """A fake CLI that records its argv and the exact bytes it received on stdin."""
    cli = tmp_path / "claude"
    cli.write_text(
        "#!/usr/bin/env python3\nimport hashlib,json,sys\n"
        "data = sys.stdin.buffer.read()\n"
        f"open({str(record)!r}, 'w').write(json.dumps({{'args': sys.argv[1:], "
        "'stdin_bytes': len(data), 'stdin_sha256': hashlib.sha256(data).hexdigest()}))\n"
        'print(\'{"type":"result","total_cost_usd":0.1,'
        '"result":{"approved":true,"findings":[]}}\')\n'
    )
    cli.chmod(0o700)


def test_issue_text_is_a_user_prompt_not_a_system_instruction(
    settings, tmp_path: Path, monkeypatch
):
    record = tmp_path / "arguments.json"
    _recording_cli(tmp_path, record)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    ClaudeAgent(settings, lambda *a: None).run(
        "developer", "untrusted issue text", tmp_path, editable=True
    )
    seen = json.loads(record.read_text())
    args = seen["args"]
    system = args[args.index("--append-system-prompt") + 1]
    assert "Never merge" in system and "untrusted issue text" not in system
    # The user prompt is delivered on stdin, never as a command-line argument.
    assert "untrusted issue text" not in args
    assert seen["stdin_bytes"] == len(b"untrusted issue text")
    assert seen["stdin_sha256"] == hashlib.sha256(b"untrusted issue text").hexdigest()
    assert "blockReadsOutsideWorkingDirectories" in args[args.index("--settings") + 1]
    assert "--max-budget-usd" not in args


def test_prompt_larger_than_linux_argument_limit_is_delivered_intact(
    settings, tmp_path: Path, monkeypatch
):
    """Regression: a 250 KB reviewer diff failed to spawn with E2BIG on Linux (128 KiB/arg)."""
    record = tmp_path / "arguments.json"
    _recording_cli(tmp_path, record)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    prompt = json.dumps({"diff": "+" + "x" * 400_000 + "\u00e9\n"})
    assert len(prompt.encode()) > 400_000
    result = ClaudeAgent(settings, lambda *a: None).run("reviewer", prompt, tmp_path)
    assert result == {"approved": True, "findings": []}
    seen = json.loads(record.read_text())
    assert max(len(arg.encode()) for arg in seen["args"]) < 100_000
    assert seen["stdin_bytes"] == len(prompt.encode())
    assert seen["stdin_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()


def test_large_prompt_does_not_hang_or_mask_a_cli_that_never_reads_stdin(
    settings, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    prompt = "x" * 500_000
    _fake_cli(tmp_path, "echo bad >&2; exit 2")
    with pytest.raises(AgentError, match=r"failed \(exit 2\)"):
        ClaudeAgent(settings, lambda *a: None).run("reviewer", prompt, tmp_path)
    _fake_cli(tmp_path, 'echo \'{"type":"result","result":{"approved":true,"findings":[]}}\'')
    assert ClaudeAgent(settings, lambda *a: None).run("reviewer", prompt, tmp_path) == {
        "approved": True,
        "findings": [],
    }


def test_prompt_feeder_survives_a_closed_reader_and_a_failing_close():
    """The feeder thread must never raise: the child's exit status reports failures."""
    read_end, write_end = os.pipe()
    os.close(read_end)  # Writing now fails with EPIPE, as when the CLI exits early.

    class Stream:
        closed = False

        def fileno(self):
            return write_end

        def close(self):
            self.closed = True
            raise OSError("close failed")

    stream = Stream()
    try:
        _feed_prompt(stream, b"x" * 1024)
    finally:
        os.close(write_end)
    assert stream.closed


def test_spawn_failure_is_reported_as_zero_spend_so_the_daily_guard_stays_usable(
    settings, tmp_path: Path, monkeypatch
):
    store = Store(settings.state_dir)
    store.start("run")

    def refuse(*args, **kwargs):
        raise OSError(7, "Argument list too long")

    monkeypatch.setattr("autocd.agents.subprocess.Popen", refuse)
    events = []

    def record(kind, detail):
        events.append((kind, detail))
        store.event("run", 12, kind, detail)

    with pytest.raises(AgentError, match="reviewer could not start: E2BIG$") as raised:
        ClaudeAgent(settings, record).run("reviewer", "prompt", tmp_path)
    assert "Argument list too long" not in str(raised.value)
    assert [kind for kind, _ in events] == ["agent_started", "agent_usage"]
    assert events[1][1]["cost_usd"] == 0.0 and events[1][1]["spawn_failed"] is True
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    assert store.model_spend_since(epoch) == (0.0, True)


def test_killed_call_without_a_reported_cost_keeps_spend_unverifiable(
    settings, tmp_path: Path, monkeypatch
):
    """A stalled call may have spent money, so the guard must still stop safely."""
    store = Store(settings.state_dir)
    store.start("run")
    _fake_cli(tmp_path, "sleep 2")
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    with pytest.raises(AgentError, match="stalled"):
        ClaudeAgent(
            replace(settings, stall_seconds=1, agent_timeout_seconds=3),
            lambda kind, detail: store.event("run", 12, kind, detail),
        ).run("reviewer", "prompt", tmp_path)
    assert store.model_spend_since(datetime(1970, 1, 1, tzinfo=UTC)) == (0.0, False)


def test_positive_model_budget_adds_claude_flag(settings, tmp_path: Path, monkeypatch):
    args_file = tmp_path / "arguments.json"
    cli = tmp_path / "claude"
    cli.write_text(
        "#!/usr/bin/env python3\nimport json,sys\n"
        f"open({str(args_file)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
        'print(\'{"type":"result","result":{"approved":true,"gaps":[]}}\')\n'
    )
    cli.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    ClaudeAgent(replace(settings, max_budget_usd=10), lambda *a: None).run("qa", "test", tmp_path)
    args = json.loads(args_file.read_text())
    assert args[args.index("--max-budget-usd") + 1] == "10"


def test_untrusted_subprocess_environments_strip_coordinator_credentials(monkeypatch):
    monkeypatch.setenv("GH_TOKEN", "gh-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "job-secret")
    monkeypatch.setenv("AUTOCD_GH_TOKEN", "project-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "model-secret")
    model = untrusted_env(model=True)
    checks = untrusted_env()
    assert model["ANTHROPIC_API_KEY"] == "model-secret"
    assert "ANTHROPIC_API_KEY" not in checks
    for key in ("GH_TOKEN", "GITHUB_TOKEN", "AUTOCD_GH_TOKEN"):
        assert key not in model and key not in checks


def test_model_uses_isolated_user_when_configured(settings, tmp_path: Path, monkeypatch):
    import subprocess

    _fake_cli(tmp_path, 'echo \'{"type":"result","result":{"approved":true,"gaps":[]}}\'')
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    original = subprocess.Popen
    seen = []

    def capture(args, **kwargs):
        seen.append(args[:8])
        return original(args[8:], **kwargs)

    monkeypatch.setattr("autocd.agents.subprocess.Popen", capture)
    ClaudeAgent(replace(settings, worker_user="autocdqa"), lambda *a: None).run(
        "qa", "test", tmp_path
    )
    assert seen[0][:7] == ["sudo", "-E", "-H", "-u", "autocdqa", "--", "env"]
    assert seen[0][7].startswith("PATH=")


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
    _fake_cli(tmp_path, "printf partial; sleep 2")
    with pytest.raises(AgentError, match="stalled"):
        ClaudeAgent(
            replace(settings, stall_seconds=1, agent_timeout_seconds=3), lambda *a: None
        ).run("qa", "test", tmp_path)


def test_claude_agent_stream_variants_and_timeout(settings, tmp_path: Path, monkeypatch):
    _fake_cli(
        tmp_path,
        'printf \'%s\\n\' \'not-json\' \'{"type":"assistant"}\' \'{"type":"result","result":"plain text"}\'',
    )
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    events = []
    with pytest.raises(AgentError, match="invalid JSON"):
        ClaudeAgent(settings, lambda kind, detail: events.append(kind)).run(
            "reviewer", "test", tmp_path
        )
    assert "agent_progress" in events
    progress = []
    message = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "Working on tests"}]},
    }
    _fake_cli(
        tmp_path,
        "echo '"
        + json.dumps(message)
        + '\'; echo \'{"type":"result","result":{"approved":true,"findings":[]}}\'',
    )
    ClaudeAgent(settings, lambda kind, detail: progress.append((kind, detail))).run(
        "reviewer", "test", tmp_path
    )
    assert any(
        kind == "agent_progress" and detail["update"] == "Working on tests"
        for kind, detail in progress
    )
    _fake_cli(tmp_path, 'echo \'{"type":"result","result":{"approved":true,"gaps":[]}}\'')
    assert ClaudeAgent(settings, lambda *a: None).run("qa", "test", tmp_path) == {
        "approved": True,
        "gaps": [],
    }
    _fake_cli(tmp_path, 'printf \'{"type":"result","result":{"approved":true,"gaps":[]}}\'')
    assert ClaudeAgent(settings, lambda *a: None).run("qa", "test", tmp_path) == {
        "approved": True,
        "gaps": [],
    }
    _fake_cli(tmp_path, 'echo \'{"type":"result","is_error":true}\'')
    with pytest.raises(AgentError, match="returned an error"):
        ClaudeAgent(settings, lambda *a: None).run("qa", "test", tmp_path)
    # The CLI's machine-readable result subtype names the cause; free text never does.
    _fake_cli(tmp_path, 'echo \'{"type":"result","is_error":true,"subtype":"error_max_turns"}\'')
    with pytest.raises(AgentError) as raised:
        ClaudeAgent(settings, lambda *a: None).run("developer", "test", tmp_path)
    assert str(raised.value) == "developer returned an error (error_max_turns)"
    _fake_cli(
        tmp_path,
        'echo \'{"type":"result","is_error":true,"subtype":"SECRET model text","result":"SECRET"}\'',
    )
    with pytest.raises(AgentError) as raised:
        ClaudeAgent(settings, lambda *a: None).run("developer", "test", tmp_path)
    assert str(raised.value) == "developer returned an error"
    _fake_cli(tmp_path, "sleep 2")
    with pytest.raises(AgentError, match="timed out"):
        ClaudeAgent(
            replace(settings, stall_seconds=2, agent_timeout_seconds=1), lambda *a: None
        ).run("qa", "test", tmp_path)


def test_command_reports_failure_without_shell(tmp_path: Path):
    assert command(["/bin/echo", "safe"], tmp_path).strip() == "safe"
    with pytest.raises(CommandError, match="exited 3"):
        command(["/bin/sh", "-c", "exit 3"], tmp_path)
    assert command(["/bin/sh", "-c", "exit 8"], tmp_path, allowed_exit_codes=(0, 8)) == ""


def test_command_audit_records_arguments_and_exit(tmp_path: Path):
    events = []
    with audit_commands(lambda kind, detail: events.append((kind, detail))):
        assert command(["/bin/echo", "safe"], tmp_path).strip() == "safe"
        with pytest.raises(OSError):
            command(["/no/such/executable"], tmp_path)
    assert [kind for kind, _ in events] == [
        "command_started",
        "command_completed",
        "command_started",
        "command_failed",
    ]
    assert events[1][1]["exit_code"] == 0
    assert events[0][1]["argv"] == ["/bin/echo", "safe"]


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
                    },
                    "view": {"number": settings.project_view_number},
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


def test_ready_reconciliation_requires_owner_marker_after_block(settings, monkeypatch):
    github = GitHub(settings)
    blocked_at = datetime(2026, 11, 6, 18, 11, tzinfo=UTC)
    comments = [
        [
            {
                "user": {"login": "rrahimi-uci"},
                "body": "Engineering workflow: **ready**\n\nold evidence",
                "created_at": "2026-11-06T16:46:38Z",
            },
            {
                "user": {"login": "foreign-user"},
                "body": "Engineering workflow: **ready**\n\nspoofed evidence",
                "created_at": "2026-11-06T20:03:06Z",
            },
        ]
    ]
    payload = {"pages": comments}
    calls = []

    def capture(args, cwd, **kwargs):
        calls.append(args)
        return json.dumps(payload["pages"])

    monkeypatch.setattr("autocd.github.command", capture)
    assert not github.has_ready_reconciliation(821, blocked_at)
    comments[0].append(
        {
            "user": {"login": "rrahimi-uci"},
            "body": "Engineering workflow: **ready**\n\nreviewed evidence",
            "created_at": "2026-11-06T20:03:06Z",
        }
    )
    assert github.has_ready_reconciliation(821, blocked_at)
    assert calls[0][:4] == ["gh", "api", "--paginate", "--slurp"]
    assert calls[0][-1].endswith("/issues/821/comments?per_page=100")
    payload["pages"] = {"incomplete": True}
    with pytest.raises(CommandError, match="incomplete"):
        github.has_ready_reconciliation(821, blocked_at)


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


def test_project_snapshot_rejects_wrong_view(settings, monkeypatch):
    monkeypatch.setattr(
        "autocd.github.command",
        lambda *a, **k: json.dumps(
            {
                "repository": {"nameWithOwner": settings.repository, "defaultBranch": "main"},
                "view": {"number": 42},
            }
        ),
    )
    with pytest.raises(CommandError, match="view"):
        GitHub(settings).snapshot()


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
    github = GitHub(replace(settings, worktree_dir=tmp_path / "worktrees"))
    calls = []

    def capture(args, cwd, **kwargs):
        calls.append(args)
        if args[:3] == ["git", "ls-files", "--modified"]:
            return "src/task.py\0tests/test_task.py\0"
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
    assert (
        github.commit_and_push(tmp_path, branch, 12, "", ["src/task.py", "tests/test_task.py"])
        == "a" * 40
    )
    assert github.create_pr(tmp_path, "descriptive title", "## Summary\ntext") == 88
    assert any(args[:3] == ["gh", "pr", "create"] and "--draft" in args for args in calls)
    assert github.pr_state(88)["headRefOid"] == "a" * 40
    assert github.required_checks(88, {"statusCheckRollup": []}) == [
        {"name": "CI", "state": "PASS"},
        {"name": "Gate execution ledger", "state": "MISSING"},
    ]
    assert any(args[:3] == ["git", "push", "-u"] for args in calls)
    worktree.mkdir(parents=True)
    with pytest.raises(CommandError, match="already exists"):
        github.prepare_worktree(12, "abcdef1234")


def test_isolated_worktree_handoff_uses_separate_unprivileged_owner(
    settings, monkeypatch, tmp_path
):
    settings = replace(settings, worker_user="autocdqa", worktree_dir=tmp_path / "worktrees")
    calls = []
    destination = settings.worktree_dir / "autocd-issue-12-abcdef12"

    def capture(args, cwd, **kwargs):
        calls.append(args)
        if args[:3] == ["git", "worktree", "add"]:
            destination.mkdir(parents=True)
            (destination / ".git").write_text("gitdir: /trusted/metadata\n")
            (destination / "README.md").write_text("worker file\n")
        return ""

    monkeypatch.setattr("autocd.github.command", capture)
    GitHub(settings).prepare_worktree(12, "abcdef1234")
    assert any(args[:4] == ["sudo", "chown", "-R", "autocdqa:autocdqa"] for args in calls)
    chown = next(args for args in calls if args[:2] == ["sudo", "chown"])
    assert str(destination / ".git") not in chown
    assert destination.stat().st_mode & 0o7777 == 0o1777
    assert (destination / ".git").stat().st_mode & 0o777 == 0o644
    assert any(
        args[:5] == ["git", "config", "--global", "--add", "safe.directory"] for args in calls
    )
    assert any(
        args[:7] == ["sudo", "-H", "-u", "autocdqa", "--", "git", "config"] for args in calls
    )


def test_isolated_worktree_rejects_missing_git_marker(settings, monkeypatch, tmp_path):
    settings = replace(settings, worker_user="autocdqa", worktree_dir=tmp_path / "worktrees")
    destination = settings.worktree_dir / "autocd-issue-12-abcdef12"

    def capture(args, cwd, **kwargs):
        if args[:3] == ["git", "worktree", "add"]:
            destination.mkdir(parents=True)
            (destination / "README.md").write_text("worker file\n")
        return ""

    monkeypatch.setattr("autocd.github.command", capture)
    with pytest.raises(CommandError, match="marker"):
        GitHub(settings).prepare_worktree(12, "abcdef1234")


def test_closed_issue_search_and_guarded_merge_commands(settings, monkeypatch):
    github = GitHub(settings)
    calls = []

    def capture(args, cwd, **kwargs):
        calls.append(args)
        if args[:3] == ["gh", "issue", "list"]:
            return '[{"number":12,"closedAt":"2026-11-05T08:00:00Z","stateReason":"COMPLETED"}]'
        return ""

    monkeypatch.setattr("autocd.github.command", capture)
    assert github.closed_issues_since("2026-11-05", "release:preview-1")[0]["number"] == 12
    assert "label:release:preview-1 closed:>=2026-11-05" in calls[0]
    assert github.closed_issues_since("2026-11-05", None)[0]["number"] == 12
    assert "closed:>=2026-11-05" in calls[1]
    assert not any("label:" in arg for arg in calls[1])
    github.merge_pr(88, "a" * 40)
    assert calls[2][-3:] == ["--squash", "--match-head-commit", "a" * 40]
    github.verify_merged_on_main("b" * 40)
    assert calls[-1] == ["git", "merge-base", "--is-ancestor", "b" * 40, "origin/main"]
    github.close_issue(12)
    assert calls[-1][-2:] == ["--reason", "completed"]
    with pytest.raises(CommandError, match="valid merge commit"):
        github.verify_merged_on_main("invalid")
    monkeypatch.setattr("autocd.github.command", lambda *a, **k: json.dumps([{}] * 1000))
    with pytest.raises(CommandError, match="CLI limit"):
        github.closed_issues_since("2026-11-05", "release:preview-1")


def test_merge_base_guard_refuses_stale_or_invalid_head(settings, monkeypatch):
    github = GitHub(settings)
    calls = []

    def capture(args, cwd, **kwargs):
        calls.append(args)
        if args[:3] == ["git", "rev-parse", "FETCH_HEAD"]:
            return "a" * 40
        return ""

    monkeypatch.setattr("autocd.github.command", capture)
    assert github.branch_up_to_date("a" * 40, "autocd/issue-12-abcdef12")
    assert calls[-1] == ["git", "merge-base", "--is-ancestor", "origin/main", "a" * 40]
    with pytest.raises(CommandError, match="valid commit SHA"):
        github.branch_up_to_date("wrong", "autocd/issue-12-abcdef12")
    with pytest.raises(CommandError, match="coordinator branch"):
        github.branch_up_to_date("a" * 40, "untrusted")

    def behind(args, cwd, **kwargs):
        if args[:3] == ["git", "merge-base", "--is-ancestor"]:
            raise CommandError("behind")
        return "a" * 40 if args[:3] == ["git", "rev-parse", "FETCH_HEAD"] else ""

    monkeypatch.setattr("autocd.github.command", behind)
    assert not github.branch_up_to_date("a" * 40, "autocd/issue-12-abcdef12")

    monkeypatch.setattr(
        "autocd.github.command",
        lambda args, cwd, **kwargs: (
            "b" * 40 if args[:3] == ["git", "rev-parse", "FETCH_HEAD"] else ""
        ),
    )
    assert not github.branch_up_to_date("a" * 40, "autocd/issue-12-abcdef12")


def test_pr_creation_and_empty_commit_fail(settings, tmp_path: Path, monkeypatch):
    github = GitHub(settings)
    monkeypatch.setattr(github, "changed", lambda _: [])
    with pytest.raises(CommandError, match="no changes"):
        github.commit_and_push(tmp_path, "branch", 12, "", [])
    monkeypatch.setattr("autocd.github.command", lambda *a, **k: "unparseable")
    with pytest.raises(CommandError, match="identify"):
        github.create_pr(tmp_path, "title", "body")


def test_changed_handles_spaced_paths_and_commit_rejects_unreviewed_bytes(settings, tmp_path: Path):
    import subprocess

    def git(*args):
        return subprocess.run(
            ["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True
        ).stdout

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    (tmp_path / "README.md").write_text("base\n")
    git("add", "--all")
    git("commit", "-qm", "base")
    git("update-ref", "refs/remotes/origin/main", git("rev-parse", "HEAD").strip())
    candidate = tmp_path / "file with space.py"
    candidate.write_text("first\n")
    github = GitHub(settings)
    assert github.changed(tmp_path) == ["file with space.py"]
    git("add", "-N", "--all")
    reviewed_patch = git("diff", "--binary", "origin/main")
    candidate.write_text("second\n")
    with pytest.raises(CommandError, match="differs from reviewed patch"):
        github.commit_and_push(tmp_path, "test-branch", 12, reviewed_patch, ["file with space.py"])


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
    auto = github.request_reviewers(tmp_path, 88, None)
    assert auto["owner"] == "not requested: automatic merge is enabled"


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
    monkeypatch.setattr("autocd.github.command", lambda *a, **k: "[]")
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
    monkeypatch.setattr("autocd.github.command", lambda *a, **k: "")
    with pytest.raises(CommandError, match="no JSON"):
        github.required_checks(88, {"statusCheckRollup": []})
