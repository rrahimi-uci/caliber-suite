"""Cookbook 05: the lab's MCP server, its datasets and the checks the lab's script and spec rely on.

The server is exercised in-process (no transport), so this proves the datasets are consistent with
the server and with CALIBER's own scoring code. That the same cases pass through CALIBER's gateway,
policy and traces is what the lab's script proves against a live stack, not these tests.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import anyio
import pytest
import yaml
from mcp.server.mcpserver import MCPServer

from caliber.calibration import evaluate_assertion
from caliber.schemas import McpToolPolicySchema, require_approval_for_side_effect
from caliber.workflows.manifest import McpToolBinding, parse_manifest
from caliber.workflows.validation import validate_manifest

LAB = Path(__file__).resolve().parents[2] / "docs-site/cookbooks/05-mcp-connectivity-governance-lab"
DATASET = LAB / "assets/dataset"
EXPECTED_TOOLS = {
    "search_repositories",
    "list_issues",
    "get_issue",
    "scan_dependencies",
    "issue_write",
    "create_release",
}


def _load_server_module() -> Any:
    spec = importlib.util.spec_from_file_location(
        "lab05_repo_tracker_mcp", LAB / "assets/server/repo_tracker_mcp.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # No bytecode next to the lab's files: a __pycache__ in the lab folder is not a lab asset.
    previous, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
    finally:
        sys.dont_write_bytecode = previous
    return module


tracker = _load_server_module()


def _jsonl(name: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (DATASET / name).read_text().splitlines() if line.strip()]


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MCPServer:
    monkeypatch.setenv("REPO_TRACKER_LEDGER", str(tmp_path / "ledger.jsonl"))
    return tracker.build_server()


def _call(server: MCPServer, tool: str, arguments: dict[str, Any]) -> Any:
    async def go() -> Any:
        return await server.call_tool(tool, arguments)

    return anyio.run(go)


def _ledger(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "ledger.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_the_server_advertises_exactly_the_documented_tools(server: MCPServer) -> None:
    async def go() -> list[Any]:
        return await server.list_tools()

    tools = anyio.run(go)
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    assert all(tool.input_schema.get("properties") for tool in tools)
    write = next(tool for tool in tools if tool.name == "issue_write")
    assert set(write.input_schema["required"]) == {"repo", "title"}


@pytest.mark.parametrize("row", _jsonl("calibration-cases.jsonl"), ids=lambda row: row["id"])
def test_every_calibration_case_passes_against_the_server(
    server: MCPServer, row: dict[str, Any]
) -> None:
    result = _call(server, row["tool_name"], row["input"])
    assert not result.is_error
    assert evaluate_assertion(row["assertion"], result.structured_content, None), row["name"]


def test_calibration_dataset_is_varied_and_the_cases_are_valid_for_calibration() -> None:
    from caliber.schemas import CalibrationCase

    rows = _jsonl("calibration-cases.jsonl")
    assert len(rows) >= 25
    assert len({row["id"] for row in rows}) == len(rows)
    assert Counter(row["tool_name"] for row in rows) == {
        "search_repositories": 16,
        "list_issues": 8,
        "get_issue": 4,
    }
    tags = {tag for row in rows for tag in row["tags"]}
    assert {"search", "issues", "issue", "edge"} <= tags
    for row in rows:
        CalibrationCase(
            name=f"{row['id']} {row['name']}", input=row["input"], assertion=row["assertion"]
        )


def test_the_classification_covers_every_tool_and_is_acceptable_to_the_policy_rules() -> None:
    rows = _jsonl("tool-classification.jsonl")
    assert {row["tool"] for row in rows} == EXPECTED_TOOLS
    levels = {row["tool"]: row["side_effect_level"] for row in rows}
    assert levels["issue_write"] == "write" and levels["create_release"] == "external_action"
    for row in rows:
        policy = McpToolPolicySchema(
            allowed=row["allowed"],
            side_effect_level=row["side_effect_level"],
            requires_approval=row["requires_approval"],
            rate_limit_per_minute=row.get("rate_limit_per_minute"),
        )
        if policy.allowed:  # the same rule the PATCH route applies to the merged policy
            require_approval_for_side_effect(policy.side_effect_level, policy.requires_approval)
    # The lab's two interesting rows: the allowed write needs approval, the external action is blocked.
    by_tool = {row["tool"]: row for row in rows}
    assert by_tool["issue_write"]["allowed"] and by_tool["issue_write"]["requires_approval"]
    assert not by_tool["create_release"]["allowed"]
    assert by_tool["scan_dependencies"]["rate_limit_per_minute"] == 3


def test_a_write_without_approval_is_what_the_classification_rule_refuses() -> None:
    with pytest.raises(ValueError, match="requires_approval"):
        require_approval_for_side_effect("write", False)


def test_invoke_checks_name_real_tools_and_every_failure_has_a_reason() -> None:
    checks = _jsonl("invoke-checks.jsonl")
    assert len({row["id"] for row in checks}) == len(checks) == 10
    for row in checks:
        assert row["expect"]["error_contains"] or row["expect"]["success"] is True
        assert row["tool_name"] in EXPECTED_TOOLS | {"drop_database"}


SERVER_SIDE_FAILURES = [
    r for r in _jsonl("invoke-checks.jsonl") if r["id"] in {"F05", "F06", "F07", "F08"}
]


@pytest.mark.parametrize("row", SERVER_SIDE_FAILURES, ids=lambda row: row["id"])
def test_server_side_failures_match_the_invoke_checks(
    server: MCPServer, row: dict[str, Any], tmp_path: Path
) -> None:
    """The failures the server itself produces (the others are CALIBER refusing before the call).

    Called in-process, a tool error is raised; over the protocol the same text arrives as an error result.
    """
    with pytest.raises(Exception, match=row["expect"]["error_contains"]):
        _call(server, row["tool_name"], row["arguments"])
    # Arguments that fail schema validation never run the tool; a refused value runs it and is ledgered as failed.
    assert [e["ok"] for e in _ledger(tmp_path)] == ([] if row["id"] == "F07" else [False])


def test_the_write_tool_files_an_issue_and_the_ledger_proves_it(
    server: MCPServer, tmp_path: Path
) -> None:
    assert _ledger(tmp_path) == []
    created = _call(
        server,
        "issue_write",
        {"repo": "acme/release-notes-bot", "title": "Skips pre-release tags", "labels": ["bug"]},
    ).structured_content
    assert created == {
        "created": True,
        "repo": "acme/release-notes-bot",
        "number": 1,
        "title": "Skips pre-release tags",
    }
    listed = _call(
        server, "list_issues", {"repo": "acme/release-notes-bot", "state": "open"}
    ).structured_content
    assert listed["total_count"] == 1 and listed["issues"][0]["labels"] == ["bug"]
    assert [e["tool"] for e in _ledger(tmp_path) if e["tool"] == "issue_write"] == ["issue_write"]


def test_a_failed_write_is_ledgered_as_failed_and_creates_nothing(
    server: MCPServer, tmp_path: Path
) -> None:
    with pytest.raises(Exception, match="invalid label"):
        _call(
            server,
            "issue_write",
            {"repo": "acme/docs", "title": "Bad label", "labels": ["NOT VALID!"]},
        )
    assert [(e["tool"], e["ok"]) for e in _ledger(tmp_path)] == [("issue_write", False)]
    listed = _call(server, "list_issues", {"repo": "acme/docs", "state": "all"}).structured_content
    assert listed["total_count"] == 5  # the seed issues only


def test_create_release_is_idempotent_by_tag(server: MCPServer) -> None:
    first = _call(server, "create_release", {"repo": "acme/docs", "tag": "v1.2.3"})
    assert first.structured_content["published"] is True
    with pytest.raises(Exception, match="already exists"):
        _call(server, "create_release", {"repo": "acme/docs", "tag": "v1.2.3"})
    with pytest.raises(Exception, match="tag"):
        _call(server, "create_release", {"repo": "acme/docs", "tag": "1.2.3"})


def test_a_retried_write_with_the_same_idempotency_key_runs_once(
    server: MCPServer, tmp_path: Path
) -> None:
    """An MCP client may re-issue a call whose answer was lost: the key makes that safe."""
    args = {
        "repo": "acme/docs",
        "title": "Typo in the install page",
        "idempotency_key": "retry-key-0001",
    }
    first = _call(server, "issue_write", args).structured_content
    retry = _call(server, "issue_write", args).structured_content
    assert first["created"] is True and retry["created"] is False and retry["replayed"] is True
    assert retry["number"] == first["number"]
    listed = _call(server, "list_issues", {"repo": "acme/docs", "state": "all"}).structured_content
    assert listed["total_count"] == 6  # five seed issues and exactly one filed
    # The retry is on the ledger under its own name, so the executions are not counted twice.
    names = [e["tool"] for e in _ledger(tmp_path) if e["tool"].startswith("issue_write")]
    assert names == ["issue_write", "issue_write.replay"]
    # A key cannot be reused for something else.
    with pytest.raises(Exception, match="already used for a different issue"):
        _call(server, "issue_write", {**args, "title": "A different issue"})
    # No key, no protection: the same title files a second issue.
    plain = {"repo": "acme/docs", "title": "Typo in the install page"}
    _call(server, "issue_write", plain)
    again = _call(server, "issue_write", plain).structured_content
    assert again["created"] is True and again["number"] == first["number"] + 2


def test_a_retried_release_with_the_same_idempotency_key_publishes_once(
    server: MCPServer, tmp_path: Path
) -> None:
    args = {"repo": "acme/docs", "tag": "v2.0.0", "idempotency_key": "release-key-0001"}
    first = _call(server, "create_release", args).structured_content
    retry = _call(server, "create_release", args).structured_content
    assert first["published"] is True and retry == {**first, "published": False, "replayed": True}
    assert [e["tool"] for e in _ledger(tmp_path) if e["tool"].startswith("create_release")] == [
        "create_release",
        "create_release.replay",
    ]
    with pytest.raises(Exception, match="already used for a different release"):
        _call(server, "create_release", {**args, "tag": "v2.0.1"})


def test_without_a_ledger_nothing_is_recorded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("REPO_TRACKER_LEDGER", raising=False)
    fresh = tracker.build_server()
    assert (
        _call(fresh, "get_issue", {"repo": "acme/docs", "number": 1}).structured_content["number"]
        == 1
    )
    assert tracker._ledger() == []


def test_the_gateway_wrapper_requires_the_bearer_token_when_one_is_set() -> None:
    sent: list[dict[str, Any]] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 204, "headers": []})

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    async def call(path: str, token: str | None) -> int:
        sent.clear()
        headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
        await tracker._Gateway(app, "s3cret")(
            {"type": "http", "path": path, "headers": headers}, None, send
        )
        return int(sent[0]["status"])

    async def go() -> list[int]:
        return [
            await call("/healthz", None),
            await call("/mcp", None),
            await call("/mcp", "wrong"),
            await call("/mcp", "s3cret"),
            await call("/stats", "s3cret"),
        ]

    assert anyio.run(go) == [200, 401, 401, 204, 200]


def test_the_workflow_asset_parses_validates_and_binds_the_governed_tool() -> None:
    text = (LAB / "assets/workflow/file-issue.workflow.yaml").read_text()
    for placeholder, value in (
        ("__SERVER_ID__", "MCP-0123abcd"),
        ("__TOOL__", "issue_write"),
        ("__LEVEL__", "write"),
        ("__APPROVAL__", "true"),
    ):
        text = text.replace(placeholder, value)
    manifest = parse_manifest(yaml.safe_load(text))
    binding = manifest.tools["issue_write"]
    assert isinstance(binding, McpToolBinding)
    assert (binding.server_id, binding.tool_name) == ("MCP-0123abcd", "issue_write")
    assert binding.requires_approval is True and binding.side_effect_level == "write"
    report = validate_manifest(manifest)
    assert report.valid, report


def test_the_github_variant_body_is_marked_as_not_run() -> None:
    body = json.loads((LAB / "assets/real-world/github-mcp.server.json").read_text())
    assert body["uri"] == "https://api.githubcopilot.com/mcp/"
    assert "NOT RUN" in body["_comment"]
    assert body["auth_config"] == {"token_env_var": "GITHUB_PERSONAL_ACCESS_TOKEN"}


def test_repository_and_issue_data_are_internally_consistent() -> None:
    repos = json.loads((DATASET / "repositories.json").read_text())
    issues = json.loads((DATASET / "issues.json").read_text())
    names = {repo["full_name"] for repo in repos}
    assert len(names) == len(repos) >= 20
    assert {issue["repo"] for issue in issues} <= names
    assert len({(i["repo"], i["number"]) for i in issues}) == len(issues)
    assert {i["state"] for i in issues} == {"open", "closed"}
    assert any(repo["archived"] for repo in repos)
