"""Cookbook 16 (observability and triage): the dataset against the real workflow runtime.

The 40 tickets in ``prod-regression.sample.jsonl`` are what the lab replays through cookbook 09's
version 1 workflow to make real traces. This runs the same 40 through the real runtime (real Python
sandbox, real graph interpreter; only the account-service call is replaced, by the stand-in's own
``account_response``) and checks that what the README says about them holds: 12 fail, 4 of the
28 that complete give the wrong answer, and the gold outcome is what a corrected workflow produces.
"""

from __future__ import annotations

import ast
import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from caliber.workflows.runtime import FakeWorkflowExecutor, execute
from tests.test_cookbook_09_self_healing import _failing_node, _plan

LAB = Path(__file__).resolve().parents[2] / "docs-site/cookbooks/16-observability-triage"
ASSETS = LAB / "assets"
ROWS = [
    json.loads(line)
    for line in (ASSETS / "dataset/prod-regression.sample.jsonl").read_text("utf-8").splitlines()
    if line.strip()
]
NODE_FOR = {"validate": "validate", "poison": "check_reference", "upstream": "decide"}


def _kind(row: dict[str, Any]) -> str | None:
    return next((tag for tag in row["tags"] if tag in NODE_FOR), None)


@pytest.fixture(scope="module")
def v1() -> dict[str, Any]:
    plan = _plan("v1")
    return {
        r["id"]: execute(plan, r["input"]["payload"], executor=FakeWorkflowExecutor()) for r in ROWS
    }


@pytest.fixture(scope="module")
def v2() -> dict[str, Any]:
    plan = _plan("v2")
    return {
        r["id"]: execute(plan, r["input"]["payload"], executor=FakeWorkflowExecutor()) for r in ROWS
    }


def test_dataset_shape_and_counts() -> None:
    assert [row["id"] for row in ROWS] == [f"P{n:02d}" for n in range(1, 41)]
    for row in ROWS:
        assert set(row) == {"id", "tags", "input", "expected", "note"}
        assert row["note"]
        assert row["expected"]["decision"] in {"approve", "manual_review", "rejected_input"}
    tags = Counter(tag for row in ROWS for tag in row["tags"])
    assert (tags["ok"], tags["error"], tags["correct"], tags["silent_wrong"]) == (28, 12, 24, 4)
    assert Counter(_kind(row) for row in ROWS if _kind(row)) == {
        "validate": 6,
        "poison": 3,
        "upstream": 3,
    }
    assert len({row["input"]["payload"] for row in ROWS}) == 40


def test_twelve_runs_fail_at_the_node_their_tags_name(v1: dict[str, Any]) -> None:
    failed = {row_id for row_id, run in v1.items() if run.status == "error"}
    assert failed == {row["id"] for row in ROWS if "error" in row["tags"]}
    for row in ROWS:
        if "error" in row["tags"]:
            assert _failing_node(v1[row["id"]].error) == NODE_FOR[_kind(row) or ""], row["id"]


def test_the_status_of_a_run_matches_its_tags(v1: dict[str, Any]) -> None:
    statuses = Counter(run.status for run in v1.values())
    assert statuses == {"completed": 28, "error": 12}


def test_four_ok_runs_give_the_wrong_answer(v1: dict[str, Any]) -> None:
    wrong = []
    for row in ROWS:
        run = v1[row["id"]]
        if run.status != "completed":
            continue
        out = json.loads(run.output)
        if (out["decision"], out["reason_code"]) != (
            row["expected"]["decision"],
            row["expected"]["reason_code"],
        ):
            wrong.append(row["id"])
    assert wrong == ["P25", "P26", "P27", "P28"]
    assert all("silent_wrong" in row["tags"] for row in ROWS if row["id"] in wrong)
    for row_id in wrong:  # what production did: manual review for a reference it could not verify
        assert json.loads(v1[row_id].output)["reason_code"] == "ORDER_REF_UNVERIFIED"


def test_the_correct_runs_match_the_gold_outcome(v1: dict[str, Any]) -> None:
    for row in (r for r in ROWS if "correct" in r["tags"]):
        out = json.loads(v1[row["id"]].output)
        assert (out["decision"], out["reason_code"]) == (
            row["expected"]["decision"],
            row["expected"]["reason_code"],
        ), row["id"]


def test_the_gold_outcome_of_each_error_is_what_a_corrected_workflow_gives(
    v2: dict[str, Any],
) -> None:
    """The gold for the 12 failures is not invented: cookbook 09's version 2 produces it."""
    for row in (r for r in ROWS if "error" in r["tags"]):
        run = v2[row["id"]]
        assert run.status == "completed", row["id"]
        out = json.loads(run.output)
        expected = row["expected"]
        assert (out["decision"], out["reason_code"]) == (
            expected["decision"],
            expected["reason_code"],
        ), row["id"]
        if "upstream_status" in expected:
            assert out["upstream_status"] == expected["upstream_status"]
        if "v2_errors_contain" in expected:
            assert any(expected["v2_errors_contain"] in e for e in out["errors"])


def test_the_question_schema_is_the_one_the_readme_describes() -> None:
    schema = json.loads((ASSETS / "review-queues/triage-review.review-questions.json").read_text())
    keys = [q["key"] for q in schema["questions"]]
    assert keys == [
        "root_cause_known",
        "failure_mode",
        "severity",
        "expected_output",
        "reviewer_notes",
    ]
    by_key = {q["key"]: q for q in schema["questions"]}
    assert by_key["failure_mode"]["options"] == ["prompt", "tool", "retrieval", "data", "infra"]
    assert by_key["expected_output"]["target"] == "expectation"
    assert {q["target"] for q in schema["questions"]} == {"feedback", "expectation"}


class _FakeCaliber:
    """Just enough of the SDK to run ``seed_traces.seed`` without a server."""

    def __init__(self) -> None:
        self.submitted: list[tuple[str, str]] = []
        self.workflows = self
        self.versions = self
        self.runs = self

    def import_workflow(self, *, manifest_yaml: str, name: str) -> Any:
        assert "refund-intake" in manifest_yaml and name == "prod-refund-intake-t"
        return type("W", (), {"workflow_id": "WF-1"})()

    def list(self, workflow_id: str) -> list[Any]:
        return [type("V", (), {"version_id": "WFV-1"})()]

    def publish(self, version_id: str) -> Any:
        return type("V", (), {"version_id": version_id})()

    def submit(self, *, workflow_version_id: str, input: str) -> Any:
        self.submitted.append((workflow_version_id, input))
        return type("R", (), {"workflow_run_id": f"WR-{len(self.submitted)}"})()

    def wait(self, run_id: str, *, timeout: float, raise_on_failure: bool) -> Any:
        assert raise_on_failure is False
        return type(
            "R", (), {"workflow_run_id": run_id, "trace_id": f"tr-{run_id}", "status": "completed"}
        )()


def test_seed_traces_submits_every_ticket_through_the_published_workflow() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("lab16_seed", ASSETS / "seed_traces.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    fake = _FakeCaliber()
    seeded = module.seed(fake, suffix="t")

    assert list(seeded) == [row["id"] for row in ROWS]
    assert [payload for _version, payload in fake.submitted] == [
        r["input"]["payload"] for r in ROWS
    ]
    assert {version for version, _ in fake.submitted} == {"WFV-1"}
    assert seeded["P01"] == {
        "run_id": "WR-1",
        "trace_id": "tr-WR-1",
        "status": "completed",
        "workflow_id": "WF-1",
    }
    # the script targets cookbook 09's version 1, which must exist where it says
    assert module.WORKFLOW.is_file()


def test_seed_script_is_valid_python_with_a_cli_entry_point() -> None:
    tree = ast.parse((ASSETS / "seed_traces.py").read_text())
    names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert {"seed", "load_rows"} <= names


def _fixed_plan() -> Any:
    """Cookbook 09's version 1 with the three nodes' code replaced by ``assets/fix/*.py``.

    That is exactly what step 12 of the lab's UI path does in the editor.
    """
    import yaml

    from caliber.egress import EgressPolicy
    from caliber.workflows.compiler import build_ir
    from caliber.workflows.manifest import parse_manifest
    from caliber.workflows.runtime import RuntimePlan
    from tests.test_cookbook_09_self_healing import _sender
    from tests.workflow_helpers import fake_resolver

    manifest = yaml.safe_load(
        (
            LAB.parent
            / "09-workflow-debugger-self-healing-lab/assets/workflow/ticket-intake.v1.workflow.yaml"
        ).read_text("utf-8")
    )
    for node in ("validate", "check_reference", "decide"):
        manifest["nodes"][node]["code"] = (ASSETS / f"fix/{node}.py").read_text("utf-8")
    resolver = fake_resolver()
    return RuntimePlan(
        ir=build_ir(parse_manifest(manifest), resolver, version="2"),
        resolver=resolver,
        webhook_sender=_sender,
        egress_policy=EgressPolicy(allowed_hosts=frozenset({"127.0.0.1"})),
    )


def test_the_fix_files_give_every_one_of_the_40_tickets_its_gold_outcome() -> None:
    """The lab's version 2 (step 12) fixes the 12 errors AND the 4 silent failures."""
    plan = _fixed_plan()
    for row in ROWS:
        run = execute(plan, row["input"]["payload"], executor=FakeWorkflowExecutor())
        assert run.status == "completed", row["id"]
        out = json.loads(run.output)
        expected = row["expected"]
        assert (out["decision"], out["reason_code"]) == (
            expected["decision"],
            expected["reason_code"],
        ), row["id"]
        if "upstream_status" in expected:
            assert out["upstream_status"] == expected["upstream_status"], row["id"]


def test_the_fix_files_are_lab_09s_patch_plus_the_order_reference_normalisation() -> None:
    patch = LAB.parent / "09-workflow-debugger-self-healing-lab/assets/workflow/patch"
    for name in ("validate.py", "decide.py"):
        assert (ASSETS / "fix" / name).read_text("utf-8") == (patch / name).read_text("utf-8")
    fixed = (ASSETS / "fix/check_reference.py").read_text("utf-8")
    assert ".strip().upper()" in fixed
    assert ".upper()" not in (patch / "check_reference.py").read_text("utf-8")
