"""Cookbook 05's runnable example, replayed offline against ``cookbook_05_fake_server``.

The fake runs the lab's own MCP server in process and copies CALIBER's admission and policy rules,
so these tests prove the script's logic, the datasets and the server agree; the live run in the lab
README
proves CALIBER's gateway, policy and traces.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from cookbook_05_fake_server import FakeMcpServer

from examples.cookbooks import cookbook_05_governed_tool_connectivity as lab


def run_lab(server: FakeMcpServer, ledger: Path) -> dict[str, Any]:
    admin = server.client("calpat_admin")
    operator = server.client("calpat_operator")
    approver = server.client("calpat_approver")
    viewer = server.client("calpat_viewer")
    with admin, operator, approver, viewer:
        return lab.run(
            admin, operator=operator, approver=approver, viewer=viewer, ledger=ledger, suffix="-t"
        )


def test_the_lab_runs_end_to_end_and_a_refusal_never_reaches_the_server(tmp_path: Path) -> None:
    server = FakeMcpServer()
    ledger = tmp_path / "ledger.jsonl"
    summary = run_lab(server, ledger)
    assert summary["calibration_cases"] == 28
    assert summary["calibration"]["search_repositories"] == {
        "passed": 16,
        "total": 16,
        "pass_rate": 1.0,
    }
    assert sorted(summary["tools"]) == sorted(lab.EXPECTED_TOOLS)
    executed = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert not [e for e in executed if e["tool"] == "create_release"]
    assert [e["tool"] for e in executed].count("issue_write") == 2
    calls = server.calls
    # the first invoke is the refused, unclassified one; classification comes before the second
    invokes = [i for i, c in enumerate(calls) if c.endswith("/invoke-tool")]
    first_policy = next(i for i, c in enumerate(calls) if c.endswith("/policy"))
    assert invokes[0] < first_policy < invokes[1]
    approve = next(i for i, c in enumerate(calls) if c.endswith("/approval/approve"))
    resume = next(i for i, c in enumerate(calls) if c.endswith("/resume"))
    assert approve < resume
    # a refusal leaves no trace; a tool's own error is traced as an error
    names = [t["name"] for t in server.traces]
    assert not [n for n in names if n.endswith(".create_release") or n.endswith(".drop_database")]
    assert any(t["status"] == "ERROR" for t in server.traces)


def test_the_script_fails_loudly_when_a_blocked_tool_reaches_the_server(tmp_path: Path) -> None:
    class Leaky(FakeMcpServer):
        def _gateway(
            self, server: dict[str, Any], tool: str, arguments: dict[str, Any]
        ) -> dict[str, Any]:
            if tool == "create_release":
                server["tool_policies"][tool]["allowed"] = True
            return super()._gateway(server, tool, arguments)

    with pytest.raises(AssertionError, match=r"create_release|blocked"):
        run_lab(Leaky(), tmp_path / "ledger.jsonl")


def test_the_script_fails_loudly_when_an_unclassified_tool_can_be_invoked(tmp_path: Path) -> None:
    class Open(FakeMcpServer):
        @staticmethod
        def _effective(server: dict[str, Any], tool: str) -> dict[str, Any]:
            return {"allowed": True, "side_effect_level": "read", "requires_approval": False}

        def _gateway(
            self, server: dict[str, Any], tool: str, arguments: dict[str, Any]
        ) -> dict[str, Any]:
            server["tool_policies"].setdefault(
                tool, {"allowed": True, "side_effect_level": "read", "requires_approval": False}
            )
            return super()._gateway(server, tool, arguments)

    with pytest.raises(AssertionError):
        run_lab(Open(), tmp_path / "ledger.jsonl")


def test_workflow_yaml_fills_every_placeholder() -> None:
    text = lab.workflow_yaml("MCP-1", "issue_write", "write", True)
    assert "__" not in text
    assert "server_id: MCP-1" in text and "requires_approval: true" in text
    assert "requires_approval: false" in lab.workflow_yaml("MCP-1", "get_issue", "read", False)


def test_ledger_helpers_read_the_servers_own_record(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.jsonl"
    assert lab.ledger_entries(ledger) == [] and lab.executed(ledger, "issue_write") == 0
    ledger.write_text(
        json.dumps({"tool": "issue_write", "ok": True, "arguments": {}})
        + "\n"
        + json.dumps({"tool": "get_issue", "ok": False, "arguments": {}})
        + "\n"
    )
    assert lab.executed(ledger, "issue_write") == 1 and lab.executed(ledger, "get_issue") == 1


def test_ledger_path_prefers_the_explicit_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LAB05_LEDGER", str(tmp_path / "explicit.jsonl"))
    monkeypatch.setenv("CALIBER_STATE_DIR", str(tmp_path / "state"))
    assert lab.ledger_path() == tmp_path / "explicit.jsonl"
    monkeypatch.delenv("LAB05_LEDGER")
    assert lab.ledger_path() == tmp_path / "state" / "lab05-sdk-ledger.jsonl"
    monkeypatch.delenv("CALIBER_STATE_DIR")
    with pytest.raises(SystemExit):
        lab.ledger_path()


def test_server_args_name_the_lab_script_and_the_ledger(tmp_path: Path) -> None:
    args = lab.server_args(tmp_path / "ledger.jsonl")
    assert args[0].endswith("assets/server/repo_tracker_mcp.py")
    assert args[1:] == ["--ledger", str(tmp_path / "ledger.jsonl")]
