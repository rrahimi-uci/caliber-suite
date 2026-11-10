from __future__ import annotations

import json
import os
import shlex
from dataclasses import replace
from pathlib import Path

import pytest

from autocd.agents import (
    ROLE_SCHEMAS,
    AgentError,
    ClaudeAgent,
    CodexAgent,
    credential_values,
    make_agent,
    strict_schema,
    untrusted_env,
)


def _fake_codex(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "codex"
    path.write_text("#!/bin/sh\n" + body + "\n")
    path.chmod(0o700)
    return path


def _events(*lines: dict) -> str:
    return "\n".join("printf '%s\\n' " + shlex.quote(json.dumps(line)) for line in lines)


USAGE = {
    "input_tokens": 1_000_000,
    "cached_input_tokens": 400_000,
    "cache_write_input_tokens": 100_000,
    "output_tokens": 50_000,
    "reasoning_output_tokens": 20_000,
}


def _run(settings, tmp_path, monkeypatch, body, role="reviewer", editable=False):
    _fake_codex(tmp_path, body)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    events = []
    agent = CodexAgent(
        replace(settings, provider="codex_cli", worker_user=""), lambda *a: events.append(a)
    )
    try:
        return agent.run(role, "x" * 300_000, tmp_path, editable=editable), events
    except AgentError as error:
        return error, events


def test_codex_agent_returns_the_last_schema_message_and_costs_tokens(
    settings, tmp_path, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "oooooooooooooooooooo")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "aaaaaaaaaaaaaaaaaaaa")
    record = tmp_path / "record"
    body = (
        f'printf "%s\\n" "$@" > {record}; env > {record}.env; wc -c > {record}.stdin\n'
        + _events(
            {"type": "thread.started", "thread_id": "t"},
            {"type": "item.completed", "item": {"type": "error", "message": "warning"}},
            {"type": "turn.started"},
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": '{"approved":false,"findings":[]}'},
            },
            {
                "type": "item.completed",
                "item": {"type": "command_execution", "command": "rg -n add", "exit_code": 0},
            },
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": '{"approved":true,"findings":[]}'},
            },
            {"type": "turn.completed", "usage": USAGE},
        )
    )
    result, events = _run(settings, tmp_path, monkeypatch, body)
    assert result == {"approved": True, "findings": []}
    args = record.read_text().split("\n")
    assert args[:4] == ["exec", "--json", "--ephemeral", "--skip-git-repo-check"]
    assert args[args.index("--model") + 1] == settings.model
    assert 'model_reasoning_effort="xhigh"' in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert args[-2] == "-"
    env = (tmp_path / "record.env").read_text()
    assert "CODEX_API_KEY=oooooooooooooooooooo" in env
    assert "OPENAI_API_KEY" not in env and "ANTHROPIC_API_KEY" not in env
    assert int((tmp_path / "record.stdin").read_text().strip()) > 300_000
    usage = [detail for kind, detail in events if kind == "agent_usage"]
    # 500k fresh * 0.20 + 400k cached * 0.02 + 100k writes * 0.25 + 50k out * 1.20 per 1M.
    assert usage[0]["cost_usd"] == pytest.approx(0.1 + 0.008 + 0.025 + 0.06)
    assert any(kind == "agent_progress" for kind, _ in events)
    assert events[-1][0] == "agent_finished"


def test_codex_developer_gets_a_writable_sandbox(settings, tmp_path, monkeypatch):
    record = tmp_path / "record"
    developer = {key: "x" for key in ROLE_SCHEMAS["developer"]["required"]} | {"blocker": None}
    body = f'printf "%s\\n" "$@" > {record}\n' + _events(
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": json.dumps(developer)},
        },
        {"type": "turn.completed", "usage": USAGE},
    )
    result, _ = _run(settings, tmp_path, monkeypatch, body, role="developer", editable=True)
    assert result["blocker"] is None
    args = record.read_text().split("\n")
    assert args[args.index("--sandbox") + 1] == "workspace-write"
    # The schema file is written for the worker and removed after the call.
    assert not Path(args[args.index("--output-schema") + 1]).exists()


def test_codex_request_rejected_before_output_bills_nothing(settings, tmp_path, monkeypatch):
    body = _events(
        {"type": "turn.started"},
        {"type": "error", "message": "model requires a newer version"},
        {"type": "turn.failed", "error": {"message": "400"}},
    )
    body += "\nexit 1"
    error, events = _run(settings, tmp_path, monkeypatch, body)
    assert isinstance(error, AgentError) and str(error) == "reviewer returned an error"
    usage = [detail for kind, detail in events if kind == "agent_usage"]
    assert usage[0]["cost_usd"] == 0.0


def test_codex_failure_after_model_output_has_unknown_cost(settings, tmp_path, monkeypatch):
    body = _events(
        {"type": "item.completed", "item": {"type": "agent_message", "text": "{}"}},
        {"type": "turn.failed", "error": {"message": "context exceeded"}},
    )
    error, events = _run(settings, tmp_path, monkeypatch, body)
    assert str(error) == "reviewer returned an error"
    assert next(d for k, d in events if k == "agent_usage")["cost_usd"] is None


def test_codex_transient_error_then_completion_still_succeeds(settings, tmp_path, monkeypatch):
    body = _events(
        {"type": "error", "message": "Reconnecting... 1/5"},
        {
            "type": "item.completed",
            "item": {"type": "agent_message", "text": '{"approved":true,"gaps":[]}'},
        },
        {"type": "turn.completed", "usage": {"input_tokens": "bad"}},
    )
    result, events = _run(settings, tmp_path, monkeypatch, body, role="qa")
    assert result == {"approved": True, "gaps": []}
    assert next(d for k, d in events if k == "agent_usage")["cost_usd"] is None


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("echo boom >&2; exit 3", "failed (exit 3)"),
        (
            _events({"type": "item.completed", "item": {"type": "agent_message", "text": "{}"}}),
            "failed (exit 0)",
        ),
        (
            "echo not-json; printf '%s\\n' '[1]'; "
            + _events({"type": "turn.completed", "usage": USAGE}),
            "failed (exit 0)",
        ),
        (
            _events(
                {"type": "item.completed", "item": {"type": "agent_message", "text": "prose"}},
                {"type": "turn.completed", "usage": USAGE},
            ),
            "invalid JSON",
        ),
    ],
)
def test_codex_incomplete_or_invalid_runs_fail(settings, tmp_path, monkeypatch, body, message):
    error, _ = _run(settings, tmp_path, monkeypatch, body)
    assert isinstance(error, AgentError) and message in str(error)


def test_codex_spawn_failure_and_stall(settings, tmp_path, monkeypatch):
    original = os.environ["PATH"]
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    events = []
    agent = CodexAgent(replace(settings, worker_user=""), lambda *a: events.append(a))
    with pytest.raises(AgentError, match="could not start: ENOENT"):
        agent.run("qa", "x", tmp_path)
    assert events[-1][1]["spawn_failed"] is True
    monkeypatch.setenv("PATH", original)
    error, _ = _run(
        replace(settings, stall_seconds=1, agent_timeout_seconds=5),
        tmp_path,
        monkeypatch,
        "sleep 3",
    )
    assert "stalled" in str(error)


def test_codex_timeout_and_oversized_line(settings, tmp_path, monkeypatch):
    error, _ = _run(
        replace(settings, stall_seconds=1, agent_timeout_seconds=2),
        tmp_path,
        monkeypatch,
        "while true; do echo tick; sleep 0.5; done",
    )
    assert "timed out" in str(error)
    error, _ = _run(settings, tmp_path, monkeypatch, "head -c 9000000 /dev/zero | tr '\\0' a")
    assert "oversized" in str(error)


def test_codex_worker_invocation_uses_sudo(settings, tmp_path, monkeypatch):
    captured = {}

    class Stop(Exception):
        pass

    def popen(args, **kwargs):
        captured["args"] = args
        raise Stop

    monkeypatch.setattr("autocd.agents.subprocess.Popen", popen)
    with pytest.raises(Stop):
        CodexAgent(replace(settings, worker_user="autocdqa"), lambda *a: None).run(
            "qa", "x", tmp_path
        )
    assert captured["args"][:5] == ["sudo", "-E", "-H", "-u", "autocdqa"]
    assert "codex" in captured["args"]


def test_strict_schema_meets_structured_output_rules():
    for role, schema in ROLE_SCHEMAS.items():
        strict = strict_schema(schema)

        def walk(node, role=role):
            if isinstance(node, dict):
                assert "minLength" not in node, role
                if node.get("type") == "object":
                    assert node["additionalProperties"] is False
                    assert node["required"] == sorted(node["properties"])
                if "enum" in node:
                    assert node["type"] == "string"
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)

        walk(strict)
    assert strict_schema([{"type": "object", "properties": {}}]) == [
        {"type": "object", "properties": {}, "required": [], "additionalProperties": False}
    ]


def test_provider_selection_and_credential_hygiene(settings, monkeypatch):
    assert isinstance(make_agent(replace(settings, provider="codex_cli"), None), CodexAgent)
    assert isinstance(make_agent(replace(settings, provider="claude_cli"), None), ClaudeAgent)
    monkeypatch.setenv("OPENAI_API_KEY", "pppppppppppppppppppp")
    monkeypatch.setenv("CODEX_API_KEY", "short")
    monkeypatch.setenv("GH_TOKEN", "gggggggggggggggggggg")
    env = untrusted_env()
    assert "OPENAI_API_KEY" not in env and "CODEX_API_KEY" not in env and "GH_TOKEN" not in env
    assert untrusted_env(model=True)["OPENAI_API_KEY"] == "pppppppppppppppppppp"
    values = credential_values()
    assert "pppppppppppppppppppp" in values and "gggggggggggggggggggg" in values
    assert "short" not in values
