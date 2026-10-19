"""Cookbook 09 (self-healing workflows): the workflow files, the dataset, and the numbers in the README.

Every row of ``failing-inputs.jsonl`` is run through both versions of the lab's workflow with the
real runtime (``execute``: the real Python sandbox, the real graph interpreter); only the HTTP call to
the account service is replaced, by the stand-in service's own ``account_response`` function, so the
suite stays offline. The expectations stored in the dataset must hold, which is what lets the README
quote them.
"""

from __future__ import annotations

import importlib.util
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml

from caliber.egress import EgressPolicy
from caliber.workflows.compiler import build_ir
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, execute
from tests.workflow_helpers import fake_resolver

LAB = (
    Path(__file__).resolve().parents[2]
    / "docs-site/cookbooks/09-workflow-debugger-self-healing-lab"
)
ASSETS = LAB / "assets"
ROWS = [
    json.loads(line)
    for line in (ASSETS / "dataset/failing-inputs.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip()
]


def _account_service() -> Any:
    spec = importlib.util.spec_from_file_location(
        "cookbook09_account_service", ASSETS / "upstream/account_service.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ACCOUNT_SERVICE = _account_service()


def _sender(request: dict[str, Any]) -> dict[str, Any]:
    """The stand-in service's answer for the request the api_request node built."""
    body = request.get("body") or {}
    account_id = body.get("account_id") if isinstance(body, dict) else None
    if not account_id:
        return {
            "status_code": 400,
            "text": '{"error": "account_id is required"}',
            "json": {"error": "account_id is required"},
            "headers": {},
        }
    status, payload = ACCOUNT_SERVICE.account_response(account_id)
    return {"status_code": status, "text": json.dumps(payload), "json": payload, "headers": {}}


def _plan(version: str) -> RuntimePlan:
    manifest = yaml.safe_load(
        (ASSETS / f"workflow/ticket-intake.{version}.workflow.yaml").read_text()
    )
    resolver = fake_resolver()
    ir = build_ir(parse_manifest(manifest), resolver, version="1")
    # The lab allows its stand-in service's host, as CALIBER_EGRESS_ALLOWED_HOSTS does on a real stack.
    policy = EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"}))
    return RuntimePlan(ir=ir, resolver=resolver, webhook_sender=_sender, egress_policy=policy)


@pytest.fixture(scope="module")
def results() -> dict[str, dict[str, Any]]:
    """``{version: {row id: WorkflowRunResult}}``: both versions over the whole dataset."""
    out: dict[str, dict[str, Any]] = {}
    for version in ("v1", "v2"):
        plan = _plan(version)
        out[version] = {
            row["id"]: execute(plan, row["input"]["payload"], executor=FakeWorkflowExecutor())
            for row in ROWS
        }
    return out


def _failing_node(error: str | None) -> str | None:
    marker = "node '"
    if not error or marker not in error:
        return None
    return error.split(marker, 1)[1].split("'", 1)[0]


def test_dataset_has_the_documented_shape() -> None:
    assert len(ROWS) == 35
    assert [row["id"] for row in ROWS] == [f"F{n:02d}" for n in range(1, 36)]
    for row in ROWS:
        assert set(row) == {"id", "tags", "input", "expected"}
        assert isinstance(row["input"]["payload"], str)
        assert row["expected"]["decision"] in {"approve", "manual_review", "rejected_input"}
    tags = Counter(tag for row in ROWS for tag in row["tags"])
    # The README quotes these counts.
    assert tags["healthy"] == 7
    assert tags["schema_violation"] == 17
    assert tags["poison"] == 3
    assert tags["upstream"] == 4
    failing = Counter(row["expected"]["v1_failing_node"] for row in ROWS)
    assert failing == {None: 11, "validate": 17, "check_reference": 3, "decide": 4}


@pytest.mark.parametrize("row", ROWS, ids=lambda row: row["id"])
def test_version_1_fails_where_the_dataset_says(row: dict[str, Any], results: dict) -> None:
    expected = row["expected"]
    run = results["v1"][row["id"]]
    if expected["v1_status"] == "completed":
        assert run.status == "completed", run.error
        output = json.loads(run.output)
        assert (output["decision"], output["reason_code"]) == (
            expected["decision"],
            expected["reason_code"],
        )
        return
    assert run.status == "error"
    assert _failing_node(run.error) == expected["v1_failing_node"]
    assert expected["v1_error_contains"] in run.error


@pytest.mark.parametrize("row", ROWS, ids=lambda row: row["id"])
def test_version_2_completes_every_row_with_the_documented_decision(
    row: dict[str, Any], results: dict
) -> None:
    expected = row["expected"]
    run = results["v2"][row["id"]]
    assert run.status == "completed", run.error
    output = json.loads(run.output)
    assert (output["decision"], output["reason_code"]) == (
        expected["decision"],
        expected["reason_code"],
    )
    if "upstream_status" in expected:
        assert output["upstream_status"] == expected["upstream_status"]
    if "v2_errors_contain" in expected:
        assert any(expected["v2_errors_contain"] in error for error in output["errors"])


def test_the_patch_changes_nothing_for_tickets_that_already_worked(results: dict) -> None:
    """The regression slice: every row version 1 completed has byte-identical output in version 2."""
    completed = [row["id"] for row in ROWS if row["expected"]["v1_status"] == "completed"]
    assert len(completed) == 11
    for row_id in completed:
        assert results["v1"][row_id].output == results["v2"][row_id].output, row_id


def test_summary_counts_quoted_in_the_readme(results: dict) -> None:
    v1 = Counter(run.status for run in results["v1"].values())
    v2 = Counter(run.status for run in results["v2"].values())
    assert (v1["completed"], v1["error"]) == (11, 24)
    assert (v2["completed"], v2["error"]) == (35, 0)
    decisions = Counter(json.loads(run.output)["decision"] for run in results["v2"].values())
    assert decisions == {"approve": 10, "manual_review": 13, "rejected_input": 12}


def test_the_patch_files_are_exactly_the_code_in_version_2() -> None:
    manifest = yaml.safe_load((ASSETS / "workflow/ticket-intake.v2.workflow.yaml").read_text())
    for node in ("validate", "check_reference", "decide"):
        assert (ASSETS / f"workflow/patch/{node}.py").read_text() == manifest["nodes"][node]["code"]


def test_versions_differ_only_in_the_three_patched_nodes() -> None:
    v1 = yaml.safe_load((ASSETS / "workflow/ticket-intake.v1.workflow.yaml").read_text())
    v2 = yaml.safe_load((ASSETS / "workflow/ticket-intake.v2.workflow.yaml").read_text())
    assert v1["edges"] == v2["edges"]
    assert v1["nodes"].keys() == v2["nodes"].keys()
    changed = {name for name in v1["nodes"] if v1["nodes"][name] != v2["nodes"][name]}
    assert changed == {"validate", "check_reference", "decide"}
    for name in changed:
        assert {k: v for k, v in v1["nodes"][name].items() if k != "code"} == {
            k: v for k, v in v2["nodes"][name].items() if k != "code"
        }


@pytest.mark.parametrize(
    ("account_id", "status"),
    [("A-1007", 200), ("A-2210", 200), ("A-9001", 503), ("A-9002", 500), ("A-4040", 404)],
)
def test_the_stand_in_account_service(account_id: str, status: int) -> None:
    code, body = ACCOUNT_SERVICE.account_response(account_id)
    assert code == status
    assert ("error" in body) == (status != 200)
    if status == 200:
        assert body["account_id"] == account_id


def test_the_stand_in_service_is_deterministic_and_marks_suspended_accounts() -> None:
    assert ACCOUNT_SERVICE.account_response("A-1188") == ACCOUNT_SERVICE.account_response("A-1188")
    assert ACCOUNT_SERVICE.account_response("A-2210")[1]["status"] == "suspended"
    assert ACCOUNT_SERVICE.account_response("A-1007")[1]["tier"] == "premium"
