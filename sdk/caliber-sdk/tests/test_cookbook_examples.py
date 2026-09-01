"""The full SDK cookbook examples are executable and stay tied to the docs."""

from __future__ import annotations

import csv
import io
import json as jsonlib
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from caliber_sdk import CaliberClient
from examples.cookbooks.cookbook_01_trustworthy_intake_classifier import run as run_01
from examples.cookbooks.cookbook_02_precision_skills import run as run_02
from examples.cookbooks.cookbook_03_policy_safe_decision_tool import run as run_03
from examples.cookbooks.cookbook_04_document_to_json_pipeline import run as run_04
from examples.cookbooks.cookbook_05_governed_tool_connectivity import run as run_05
from examples.cookbooks.cookbook_06_grounded_knowledge_assistant import run as run_06
from examples.cookbooks.cookbook_07_support_triage_copilot import run as run_07
from examples.cookbooks.cookbook_08_incident_response_copilot import run as run_08
from examples.cookbooks.cookbook_09_self_healing_workflows import run as run_09
from examples.cookbooks.cookbook_10_trustworthy_evaluation import run as run_10
from examples.cookbooks.cookbook_11_release_signoff_factory import run as run_11
from examples.cookbooks.cookbook_12_aria_evaluation_harness import run as run_12
from examples.cookbooks.cookbook_13_aria_review_queue import run as run_13
from examples.cookbooks.cookbook_14_aria_governance_starter_kit import run as run_14
from examples.cookbooks.cookbook_15_aria_triage_recalibrate_loop import run as run_15
from examples.cookbooks.cookbook_16_observability_triage import run as run_16
from examples.cookbooks.cookbook_17_financial_analysis import (
    CSV_FIELDS,
    MONTHLY_FINANCIALS,
    PROMPT_ALIAS,
    PROMPT_TEMPLATE,
    build_financial_csv,
    expected_statistics,
    parse_analysis,
    parse_turn_analysis,
)
from examples.cookbooks.cookbook_17_financial_analysis import run as run_17

BASE = "https://caliber.test"


def stub_server(routes: dict[str, Any]) -> CaliberClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.rsplit("/caliber", 1)[-1]
        key = f"{request.method} {path}"
        if key not in routes:  # pragma: no cover
            raise AssertionError(f"example called an unstubbed route: {key}")
        body = routes[key]
        payload = body(request) if callable(body) else body
        if isinstance(payload, httpx.Response):
            return payload
        return httpx.Response(200, json={"data": payload})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CaliberClient(BASE, token="calpat_test", http_client=http)


def recipe_payload(
    cookbook_id: str,
    title: str,
    *,
    prerequisites: list[str] | None = None,
) -> dict[str, Any]:
    prerequisites = prerequisites or []
    checks = [{"label": item, "status": "operator_confirmation_required"} for item in prerequisites]
    return {
        "recipes": [
            {
                "id": cookbook_id,
                "title": title,
                "summary": title,
                "prerequisites": prerequisites,
                "readiness": {
                    "status": "configuration_required" if checks else "ready",
                    "checks": checks,
                },
            }
        ]
    }


def cookbook_listing(
    cookbook_id: str,
    title: str,
    *,
    prerequisite: str | None = None,
) -> dict[str, Any]:
    return recipe_payload(
        cookbook_id,
        title,
        prerequisites=[prerequisite] if prerequisite else None,
    )


def installed_payload(cookbook_id: str, workflow_id: str, version_id: str) -> dict[str, Any]:
    return {
        "recipe": {"id": cookbook_id},
        "workflow": {"workflow_id": workflow_id, "status": "paused"},
        "version": {"version_id": version_id, "status": "draft"},
        "activation_requires_review": True,
    }


def plan_detail(
    plan_id: str, status: str, *, steps: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "plan": {"plan_id": plan_id, "goal": "g", "status": status},
        "steps": steps
        if steps is not None
        else [{"step_id": f"{plan_id}-1", "plan_id": plan_id, "title": "step"}],
    }


def plan_step(plan_id: str, index: int, capability_key: str) -> dict[str, Any]:
    return {
        "step_id": f"{plan_id}-{index}",
        "plan_id": plan_id,
        "title": capability_key,
        "capability_key": capability_key,
    }


def plan_interaction(plan_id: str, step_id: str, interaction_id: str) -> dict[str, Any]:
    return {
        "interaction_id": interaction_id,
        "plan_id": plan_id,
        "step_id": step_id,
        "kind": "confirm",
    }


def trace_items(*trace_ids: str) -> dict[str, Any]:
    return {"items": [{"trace_id": trace_id} for trace_id in trace_ids]}


def test_cookbook_01_runs_the_prompt_workspace_baseline_diff_loop() -> None:
    """Regression is scored on the prompt workspace's Test Sets -> Runs loop
    (pin a baseline, then read the Vs. baseline diff), not a generic
    dataset+judge+evaluation flow -- see the recipe's own README."""

    test_run_ids = iter(["PTR-01-baseline", "PTR-01-comparison"])

    def create_test_run(request: httpx.Request) -> dict[str, Any]:
        body = jsonlib.loads(request.content)
        assert body["agent_id"] == "cookbook-01-intake-classifier"
        assert len(body["results"]) == 2
        return {"test_run_id": next(test_run_ids)}

    caliber = stub_server(
        {
            "GET /cookbooks": cookbook_listing(
                "01",
                "Trustworthy Intake Classifier",
                prerequisite="Configured model provider",
            ),
            "POST /cookbooks/01/install": installed_payload("01", "WF-01", "WFV-01"),
            "POST /prompts": {"name": "cookbook-01-intake-classifier"},
            "POST /prompts/cookbook-01-intake-classifier/aliases/prod": {"version": 1},
            "GET /me": {"user_id": "@alice"},
            "POST /eval-datasets": {"dataset_id": "ED-01"},
            "POST /eval-datasets/ED-01/examples": {"example_id": "EX-01"},
            "POST /prompts/test-runs": create_test_run,
            "POST /prompts/cookbook-01-intake-classifier/baseline": {
                "baseline_run_id": "PTR-01-baseline"
            },
            "POST /prompts/cookbook-01-intake-classifier/versions": {"version": 2},
            "GET /prompts/test-runs/PTR-01-baseline": {
                "test_run_id": "PTR-01-baseline",
                "results": [
                    {"testCaseId": "P01", "verdict": "pass"},
                    {"testCaseId": "P02", "verdict": "pass"},
                ],
            },
            "GET /prompts/test-runs/PTR-01-comparison": {
                "test_run_id": "PTR-01-comparison",
                "results": [
                    {"testCaseId": "P01", "verdict": "fail"},
                    {"testCaseId": "P02", "verdict": "fail"},
                ],
            },
            "POST /judges": {"judge_id": "J-01"},
            "POST /prompts/calibration/runs": {
                "job": {"job_id": "CAL-01"},
                "item": {"item_id": "VI-01"},
            },
        }
    )
    with caliber:
        result = run_01(caliber)
    assert result["prompt_name"] == "cookbook-01-intake-classifier"
    assert result["dataset_id"] == "ED-01"
    assert result["baseline_run_id"] == "PTR-01-baseline"
    assert result["comparison_run_id"] == "PTR-01-comparison"
    assert result["regressions"] == ["P01", "P02"]
    assert result["judge_id"] == "J-01"
    assert result["calibration_job_id"] == "CAL-01"


def test_cookbook_02_creates_and_calibrates_skill() -> None:
    caliber = stub_server(
        {
            "GET /cookbooks": recipe_payload("02", "Precision Skills"),
            "POST /cookbooks/02/install": installed_payload("02", "WF-02", "WFV-02"),
            "GET /me": {"user_id": "@alice"},
            "POST /skills": {"skill_id": "SK-02"},
            "POST /skills/SK-02/test-render": {"word_count": 8},
            "POST /skills/SK-02/test-selection": {"selection_score": 0.82},
            "POST /skills/SK-02/calibrate": {"job_id": "JOB-02", "status": "queued"},
        }
    )
    with caliber:
        result = run_02(caliber)
    assert result["skill_id"] == "SK-02"
    assert result["selection_score"] == 0.82


def test_cookbook_03_registers_tool_and_saves_hardening_cases() -> None:
    caliber = stub_server(
        {
            "GET /cookbooks": cookbook_listing(
                "03",
                "Policy-Safe Decision Tool",
                prerequisite="Runtime approvals",
            ),
            "POST /cookbooks/03/install": installed_payload("03", "WF-03", "WFV-03"),
            "POST /tools": {"tool_id": "TOOL-03"},
            "PUT /tools/TOOL-03/test-cases": {"saved": 2},
            "POST /tools/TOOL-03/calibrate": {"job_id": "CAL-03", "status": "queued"},
        }
    )
    with caliber:
        result = run_03(caliber)
    assert result["tool_id"] == "TOOL-03"
    assert result["calibration_job_id"] == "CAL-03"


def test_cookbook_04_uploads_managed_file_and_preview_runs_workflow() -> None:
    caliber = stub_server(
        {
            "GET /cookbooks": cookbook_listing(
                "04",
                "Document-to-JSON Pipeline",
                prerequisite="A supported document uploaded to a CALIBER project",
            ),
            "POST /projects": {"project_id": "PRJ-04"},
            "POST /projects/PRJ-04/files": {"file_id": "FILE-04"},
            "POST /cookbooks/04/install": installed_payload("04", "WF-04", "WFV-04"),
            "POST /workflow-versions/WFV-04/preview-run": {"status": "succeeded"},
        }
    )
    with caliber:
        result = run_04(caliber)
    assert result["project_id"] == "PRJ-04"
    assert result["file_id"] == "FILE-04"
    assert result["preview"] == {"status": "succeeded"}


def test_cookbook_05_proves_the_governed_tool_block_before_and_after() -> None:
    invoke_calls: Iterator[dict[str, Any]] = iter(
        [
            {"success": True, "error": None, "result": {"items": []}},
            {"success": False, "error": "tool 'issue_write' is not allowed", "result": None},
        ]
    )

    def create_server(request: httpx.Request) -> dict[str, Any]:
        body = jsonlib.loads(request.content)
        # `transport` is hyphenated and the URL field is `uri`, not `url`.
        assert body["transport"] == "streamable-http"
        assert body["uri"] == "https://api.githubcopilot.com/mcp/"
        return {"server_id": "MCP-05"}

    def invoke_tool(request: httpx.Request) -> dict[str, Any]:
        return next(invoke_calls)

    caliber = stub_server(
        {
            "GET /cookbooks": cookbook_listing(
                "05",
                "Governed Tool Connectivity",
                prerequisite="Reachable MCP server",
            ),
            "POST /cookbooks/05/install": installed_payload("05", "WF-05", "WFV-05"),
            "POST /mcp-servers": create_server,
            "POST /mcp-servers/MCP-05/test-connection": {"ok": True},
            "POST /mcp-servers/MCP-05/discover-tools": {"discovered": 2},
            "POST /mcp-servers/MCP-05/invoke-tool": invoke_tool,
            "PATCH /mcp-servers/MCP-05/tools/issue_write/policy": {"allowed": False},
            "PUT /mcp-servers/MCP-05/tools/search_repositories/test-cases": {"saved": 1},
            "POST /mcp-servers/MCP-05/tools/search_repositories/calibrate": {"status": "passed"},
        }
    )
    with caliber:
        result = run_05(caliber)
    assert result["server_id"] == "MCP-05"
    assert result["connection"] == {"ok": True}
    allowed_call, blocked_call = result["allowed_call"], result["blocked_call"]
    assert isinstance(allowed_call, dict) and allowed_call["success"] is True
    assert isinstance(blocked_call, dict) and blocked_call["success"] is False


def test_cookbook_06_builds_and_queries_knowledge_base() -> None:
    def create_kb(request: httpx.Request) -> dict[str, Any]:
        body = jsonlib.loads(request.content)
        # Real catalog choices from `options()`, not free-form guesses.
        assert body["chunking_strategy"] == "fixed_size"
        assert body["embedding_model"] == "text-embedding-3-small"
        assert body["source_bucket"]
        return {"knowledge_base_id": "KB-06"}

    def query(request: httpx.Request) -> dict[str, Any]:
        body = jsonlib.loads(request.content)
        assert body["version_ids"] == ["KBV-06"]
        assert "knowledge_base_id" not in body
        return {"answer": "Escalate to billing review."}

    caliber = stub_server(
        {
            "GET /cookbooks": cookbook_listing(
                "06",
                "Grounded Knowledge Assistant",
                prerequisite="Embedding provider",
            ),
            "POST /cookbooks/06/install": installed_payload("06", "WF-06", "WFV-06"),
            "GET /knowledge-bases/options": {
                "chunking_strategies": [{"id": "fixed_size", "name": "Fixed size"}],
                "embedding_models": [{"id": "text-embedding-3-small", "name": "Small"}],
            },
            "POST /knowledge-bases": create_kb,
            "POST /knowledge-bases/KB-06/versions": {"version_id": "KBV-06"},
            "POST /knowledge/query": query,
            "GET /me": {"user_id": "@alice"},
            "POST /eval-datasets": {"dataset_id": "ED-06"},
            "POST /eval-datasets/ED-06/examples": {"example_id": "EX-06"},
            "POST /knowledge-bases/KB-06/calibrate": {"score": 0.91},
        }
    )
    with caliber:
        result = run_06(caliber)
    assert result["knowledge_base_id"] == "KB-06"
    assert result["version_id"] == "KBV-06"
    assert result["eval_dataset_id"] == "ED-06"


def test_cookbook_07_drives_the_approval_gate_both_ways() -> None:
    """One run is approved and resumes to completion; a matching second run
    is rejected and must never reach a terminal success -- the two safety
    branches the recipe's own README calls out as its demo evidence."""

    run_status: dict[str, str] = {}

    def submit_run(request: httpx.Request) -> dict[str, Any]:
        body = jsonlib.loads(request.content)
        run_id = "WR-07-approve" if body["idempotency_key"].endswith("approve") else "WR-07-reject"
        run_status[run_id] = "waiting_approval"
        return {"workflow_run_id": run_id, "status": "queued"}

    def get_run(run_id: str) -> Any:
        return lambda _request: {"workflow_run_id": run_id, "status": run_status[run_id]}

    def resume_run(request: httpx.Request) -> dict[str, Any]:
        run_status["WR-07-approve"] = "succeeded"
        return {"workflow_run_id": "WR-07-approve", "status": "succeeded"}

    def reject_run(request: httpx.Request) -> dict[str, Any]:
        run_status["WR-07-reject"] = "failed"
        return {"rejected": True}

    caliber = stub_server(
        {
            "GET /cookbooks": cookbook_listing(
                "07",
                "Support Triage Copilot",
                prerequisite="Issue tracker connector",
            ),
            "POST /cookbooks/07/install": installed_payload("07", "WF-07", "WFV-07"),
            "GET /me": {"user_id": "@alice"},
            "POST /review-queues": {"queue_id": "RQ-07"},
            "POST /eval-datasets": {"dataset_id": "ED-07"},
            "POST /judges": {"judge_id": "J-07"},
            "POST /evaluations": {"evaluation_id": "EV-07"},
            "POST /workflow-versions/WFV-07/publish": {
                "version_id": "WFV-07",
                "status": "published",
            },
            "POST /workflow-runs": submit_run,
            "GET /workflow-runs/WR-07-approve": get_run("WR-07-approve"),
            "GET /workflow-runs/WR-07-reject": get_run("WR-07-reject"),
            "POST /workflow-runs/WR-07-approve/approval/approve": {"approved": True},
            "POST /workflow-runs/WR-07-approve/resume": resume_run,
            "POST /workflow-runs/WR-07-reject/approval/reject": reject_run,
        }
    )
    with caliber:
        result = run_07(caliber)
    assert result["queue_id"] == "RQ-07"
    assert result["evaluation_id"] == "EV-07"
    assert result["approved_status"] == "succeeded"
    assert result["rejected_status"] == "failed"


def test_cookbook_08_collects_observability_and_enqueues_reviews() -> None:
    caliber = stub_server(
        {
            "GET /cookbooks": cookbook_listing(
                "08",
                "Incident Response Copilot",
                prerequisite="Deployment health data source",
            ),
            "POST /cookbooks/08/install": installed_payload("08", "WF-08", "WFV-08"),
            "GET /observability/traces": trace_items("TR-08A", "TR-08B"),
            "GET /observability/metrics": {"error_rate": 0.4},
            "POST /review-queues": {"queue_id": "RQ-08"},
            "POST /review-queues/RQ-08/items": {"queued": 2},
        }
    )
    with caliber:
        result = run_08(caliber)
    assert result["queue_id"] == "RQ-08"
    assert result["trace_ids"] == ["TR-08A", "TR-08B"]


def test_cookbook_09_reproduces_retries_and_recovers_a_failing_run() -> None:
    run_status: dict[str, str] = {}

    def submit_run(request: httpx.Request) -> dict[str, Any]:
        run_status["WR-09"] = "waiting_approval"
        return {"workflow_run_id": "WR-09", "status": "queued"}

    def get_run(run_id: str) -> Any:
        return lambda _request: {"workflow_run_id": run_id, "status": run_status[run_id]}

    def reject_run(request: httpx.Request) -> dict[str, Any]:
        run_status["WR-09"] = "failed"
        return {"rejected": True}

    def retry_run(request: httpx.Request) -> dict[str, Any]:
        run_status["WR-09-retry"] = "waiting_approval"
        return {"workflow_run_id": "WR-09-retry", "status": "queued"}

    def resume_run(request: httpx.Request) -> dict[str, Any]:
        run_status["WR-09-retry"] = "succeeded"
        return {"workflow_run_id": "WR-09-retry", "status": "succeeded"}

    caliber = stub_server(
        {
            "GET /cookbooks": cookbook_listing(
                "09",
                "Self-Healing Workflows",
                prerequisite="Workflow worker",
            ),
            "POST /cookbooks/09/install": installed_payload("09", "WF-09", "WFV-09"),
            "POST /workflow-versions/WFV-09/publish": {
                "version_id": "WFV-09",
                "status": "published",
            },
            "POST /workflow-runs": submit_run,
            "GET /workflow-runs/WR-09": get_run("WR-09"),
            "POST /workflow-runs/WR-09/approval/reject": reject_run,
            "GET /workflow-runs/WR-09/checkpoints": [{"checkpoint_id": "CKPT-09"}],
            "GET /workflow-runs/WR-09/trace": {"spans": []},
            "POST /workflow-runs/WR-09/retry": retry_run,
            "GET /workflow-runs/WR-09-retry/lineage": {"parent_run_id": "WR-09"},
            "GET /workflow-runs/WR-09-retry": get_run("WR-09-retry"),
            "POST /workflow-runs/WR-09-retry/approval/approve": {"approved": True},
            "POST /workflow-runs/WR-09-retry/resume": resume_run,
            "POST /workflow-versions/WFV-09/propose-patch": {"patch_id": "PATCH-09"},
        }
    )
    with caliber:
        result = run_09(caliber)
    assert result["failing_run_id"] == "WR-09"
    assert result["failing_status"] == "failed"
    assert result["checkpoint_count"] == 1
    assert result["has_trace"] is True
    assert result["retried_run_id"] == "WR-09-retry"
    assert result["recovered_run_id"] == "WR-09-retry"
    assert result["recovered_status"] == "succeeded"
    assert result["patch_id"] == "PATCH-09"


def test_cookbook_10_computes_judge_human_alignment() -> None:
    """The recipe's defining mechanic is judge/human alignment (Cohen's
    kappa), not just that a judge and a queue exist side by side."""

    def submit_answer(request: httpx.Request) -> dict[str, Any]:
        body = jsonlib.loads(request.content)
        assert body == {"answers": {"judge_is_correct": True}}
        return {"item_id": "RI-10", "status": "completed"}

    def alignment(request: httpx.Request) -> dict[str, Any]:
        body = jsonlib.loads(request.content)
        # `provenance` must be stripped before this call -- the request
        # schema forbids it as an extra field.
        assert set(body["examples"][0]) == {"inputs", "outputs", "expectations", "label"}
        return {"judge_id": "J-10", "kappa": 1.0, "agreement": 1.0, "sample_size": 1}

    caliber = stub_server(
        {
            "GET /cookbooks": cookbook_listing(
                "10",
                "Trustworthy Evaluation",
                prerequisite="Judge provider",
            ),
            "POST /cookbooks/10/install": installed_payload("10", "WF-10", "WFV-10"),
            "GET /me": {"user_id": "@alice"},
            "POST /eval-datasets": {"dataset_id": "ED-10"},
            "POST /judges": {"judge_id": "J-10"},
            "POST /evaluations": {"evaluation_id": "EV-10"},
            "POST /review-queues": {"queue_id": "RQ-10"},
            "GET /observability/traces": trace_items("TR-10"),
            "POST /review-queues/RQ-10/items": [{"item_id": "RI-10"}],
            "POST /review-queues/RQ-10/items/RI-10/submit": submit_answer,
            "GET /review-queues/RQ-10/alignment-examples": {
                "examples": [
                    {
                        "inputs": {"trace_id": "TR-10"},
                        "outputs": "billing dispute",
                        "expectations": {},
                        "label": True,
                        "provenance": {"queue_id": "RQ-10", "item_id": "RI-10"},
                    }
                ],
                "skipped": [],
            },
            "POST /judges/J-10/alignment": alignment,
        }
    )
    with caliber:
        result = run_10(caliber)
    assert result["queue_id"] == "RQ-10"
    assert result["judge_id"] == "J-10"
    assert result["alignment_kappa"] == 1.0


def test_cookbook_11_drives_release_signoff_end_to_end() -> None:
    caliber = stub_server(
        {
            "GET /cookbooks": cookbook_listing(
                "11",
                "Release Signoff Factory",
                prerequisite="Evaluation evidence",
            ),
            "POST /cookbooks/11/install": installed_payload("11", "WF-11", "WFV-11"),
            "POST /releases/candidates": {"candidate_id": "RC-11", "weighted_score": 0.91},
            "POST /releases/candidates/RC-11/evaluate": {
                "candidate_id": "RC-11",
                "weighted_score": 0.94,
            },
            "POST /releases/candidates/RC-11/reports": {"job_id": "REP-11"},
            "POST /releases/candidates/RC-11/signoffs": {"signoff_id": "SIG-11", "decision": "go"},
        }
    )
    with caliber:
        result = run_11(caliber)
    assert result["candidate_id"] == "RC-11"
    assert result["score"] == 0.94


def test_cookbook_12_creates_the_judge_and_dataset_past_the_execution_gap() -> None:
    """The shipped planner leaves step inputs empty (the recipe's own
    verified "Execution gap"): approving the plan alone creates nothing, so
    the real judge/dataset must come from their own typed calls."""

    steps = [
        plan_step("PLAN-12", 1, "judge.create"),
        plan_step("PLAN-12", 2, "eval_dataset.create"),
    ]
    plan_states = iter(["paused"])
    answers = iter(["running", "completed"])
    caliber = stub_server(
        {
            "GET /cookbooks": recipe_payload("12", "Aria Evaluation Harness from Intent"),
            "POST /cookbooks/12/install": installed_payload("12", "WF-12", "WFV-12"),
            "GET /me": {"user_id": "@alice"},
            "POST /aria/plans": plan_detail("PLAN-12", "planning", steps=steps),
            "GET /aria/plans/PLAN-12": lambda _r: plan_detail(
                "PLAN-12", next(plan_states), steps=steps
            ),
            "POST /aria/plans/PLAN-12/approve": plan_detail("PLAN-12", "approved", steps=steps),
            "POST /aria/plans/PLAN-12/execute": plan_detail("PLAN-12", "running", steps=steps),
            "GET /aria/plans/PLAN-12/interactions": [
                plan_interaction("PLAN-12", "PLAN-12-1", "INT-12-1"),
                plan_interaction("PLAN-12", "PLAN-12-2", "INT-12-2"),
            ],
            "POST /judges": {"judge_id": "J-12"},
            "POST /eval-datasets": {"dataset_id": "ED-12"},
            "POST /aria/interactions/INT-12-1/answer": lambda _r: plan_detail(
                "PLAN-12", next(answers), steps=steps
            ),
            "POST /aria/interactions/INT-12-2/answer": lambda _r: plan_detail(
                "PLAN-12", next(answers), steps=steps
            ),
        }
    )
    with caliber:
        result = run_12(caliber)
    assert result["plan_id"] == "PLAN-12"
    assert result["status"] == "completed"
    assert result["judge_id"] == "J-12"
    assert result["dataset_id"] == "ED-12"


def test_cookbook_13_creates_the_review_queue_past_the_execution_gap() -> None:
    steps = [
        plan_step("PLAN-13", 1, "review_queue.create"),
        plan_step("PLAN-13", 2, "review_queue.add_items"),
    ]
    plan_states = iter(["paused"])
    answers = iter(["running", "completed"])
    caliber = stub_server(
        {
            "GET /cookbooks": cookbook_listing(
                "13",
                "Aria Review Queue",
                prerequisite="Trace ids",
            ),
            "POST /cookbooks/13/install": installed_payload("13", "WF-13", "WFV-13"),
            "POST /aria/plans": plan_detail("PLAN-13", "planning", steps=steps),
            "GET /aria/plans/PLAN-13": lambda _r: plan_detail(
                "PLAN-13", next(plan_states), steps=steps
            ),
            "POST /aria/plans/PLAN-13/approve": plan_detail("PLAN-13", "approved", steps=steps),
            "POST /aria/plans/PLAN-13/execute": plan_detail("PLAN-13", "running", steps=steps),
            "GET /aria/plans/PLAN-13/interactions": [
                plan_interaction("PLAN-13", "PLAN-13-1", "INT-13-1"),
                plan_interaction("PLAN-13", "PLAN-13-2", "INT-13-2"),
            ],
            "POST /review-queues": {"queue_id": "RQ-13"},
            "POST /aria/interactions/INT-13-1/answer": lambda _r: plan_detail(
                "PLAN-13", next(answers), steps=steps
            ),
            "POST /aria/interactions/INT-13-2/answer": lambda _r: plan_detail(
                "PLAN-13", next(answers), steps=steps
            ),
        }
    )
    with caliber:
        result = run_13(caliber)
    assert result["plan_id"] == "PLAN-13"
    assert result["status"] == "completed"
    assert result["queue_id"] == "RQ-13"


def test_cookbook_14_creates_all_three_governance_assets_past_the_execution_gap() -> None:
    steps = [
        plan_step("PLAN-14", 1, "judge.create"),
        plan_step("PLAN-14", 2, "eval_dataset.create"),
        plan_step("PLAN-14", 3, "review_queue.create"),
        plan_step("PLAN-14", 4, "review_queue.add_items"),
    ]
    plan_states = iter(["paused"])
    answers = iter(["running", "running", "running", "completed"])
    caliber = stub_server(
        {
            "GET /cookbooks": recipe_payload("14", "Aria Governance Starter Kit"),
            "POST /cookbooks/14/install": installed_payload("14", "WF-14", "WFV-14"),
            "GET /me": {"user_id": "@alice"},
            "POST /aria/plans": plan_detail("PLAN-14", "planning", steps=steps),
            "GET /aria/plans/PLAN-14": lambda _r: plan_detail(
                "PLAN-14", next(plan_states), steps=steps
            ),
            "POST /aria/plans/PLAN-14/approve": plan_detail("PLAN-14", "approved", steps=steps),
            "POST /aria/plans/PLAN-14/execute": plan_detail("PLAN-14", "running", steps=steps),
            "GET /aria/plans/PLAN-14/interactions": [
                plan_interaction("PLAN-14", "PLAN-14-1", "INT-14-1"),
                plan_interaction("PLAN-14", "PLAN-14-2", "INT-14-2"),
                plan_interaction("PLAN-14", "PLAN-14-3", "INT-14-3"),
                plan_interaction("PLAN-14", "PLAN-14-4", "INT-14-4"),
            ],
            "POST /judges": {"judge_id": "J-14"},
            "POST /eval-datasets": {"dataset_id": "ED-14"},
            "POST /review-queues": {"queue_id": "RQ-14"},
            "POST /aria/interactions/INT-14-1/answer": lambda _r: plan_detail(
                "PLAN-14", next(answers), steps=steps
            ),
            "POST /aria/interactions/INT-14-2/answer": lambda _r: plan_detail(
                "PLAN-14", next(answers), steps=steps
            ),
            "POST /aria/interactions/INT-14-3/answer": lambda _r: plan_detail(
                "PLAN-14", next(answers), steps=steps
            ),
            "POST /aria/interactions/INT-14-4/answer": lambda _r: plan_detail(
                "PLAN-14", next(answers), steps=steps
            ),
        }
    )
    with caliber:
        result = run_14(caliber)
    assert result["plan_id"] == "PLAN-14"
    assert result["status"] == "completed"
    assert result["judge_id"] == "J-14"
    assert result["dataset_id"] == "ED-14"
    assert result["queue_id"] == "RQ-14"


def test_cookbook_15_drives_the_triage_then_recalibrate_loop() -> None:
    """`workflow.calibrate` is Aria's first async capability: the step parks
    in `waiting_job` and the plan stays `running` until polled to
    resolution, rather than pausing for a human or finishing inline."""

    steps = [
        plan_step("PLAN-15", 1, "review_queue.create"),
        plan_step("PLAN-15", 2, "review_queue.add_items"),
        plan_step("PLAN-15", 3, "workflow.calibrate"),
    ]
    plan_states = iter(["paused"])
    # The first two interactions still advance the plan; the calibrate
    # interaction parks it in `running` rather than completing inline.
    answers = iter(["running", "running", "running"])
    caliber = stub_server(
        {
            "GET /cookbooks": cookbook_listing(
                "15",
                "Aria Triage & Recalibrate Loop",
                prerequisite="Existing traces",
            ),
            "POST /cookbooks/15/install": installed_payload("15", "WF-15", "WFV-15"),
            "GET /observability/traces": trace_items("TR-15"),
            "POST /aria/plans": plan_detail("PLAN-15", "planning", steps=steps),
            "GET /aria/plans/PLAN-15": lambda _r: plan_detail(
                "PLAN-15", next(plan_states), steps=steps
            ),
            "POST /aria/plans/PLAN-15/approve": plan_detail("PLAN-15", "approved", steps=steps),
            "POST /aria/plans/PLAN-15/execute": plan_detail("PLAN-15", "running", steps=steps),
            "GET /aria/plans/PLAN-15/interactions": [
                plan_interaction("PLAN-15", "PLAN-15-1", "INT-15-1"),
                plan_interaction("PLAN-15", "PLAN-15-2", "INT-15-2"),
                plan_interaction("PLAN-15", "PLAN-15-3", "INT-15-3"),
            ],
            "POST /review-queues": {"queue_id": "RQ-15"},
            "POST /review-queues/RQ-15/items": [{"item_id": "RI-15"}],
            "POST /workflows/WF-15/calibration/runs": {"job_id": "JOB-15"},
            "POST /aria/interactions/INT-15-1/answer": lambda _r: plan_detail(
                "PLAN-15", next(answers), steps=steps
            ),
            "POST /aria/interactions/INT-15-2/answer": lambda _r: plan_detail(
                "PLAN-15", next(answers), steps=steps
            ),
            "POST /aria/interactions/INT-15-3/answer": lambda _r: plan_detail(
                "PLAN-15", next(answers), steps=steps
            ),
            "POST /aria/plans/PLAN-15/poll": plan_detail("PLAN-15", "completed", steps=steps),
        }
    )
    with caliber:
        result = run_15(caliber)
    assert result["plan_id"] == "PLAN-15"
    assert result["status"] == "completed"
    assert result["queue_id"] == "RQ-15"
    assert result["enqueued_trace_ids"] == ["TR-15"]
    assert result["calibration_job_id"] == "JOB-15"


def test_cookbook_16_promotes_a_trace_into_a_dataset_and_queue() -> None:
    caliber = stub_server(
        {
            "GET /cookbooks": cookbook_listing(
                "16",
                "Production Observability & Triage",
                prerequisite="Existing traces",
            ),
            "POST /cookbooks/16/install": installed_payload("16", "WF-16", "WFV-16"),
            "GET /observability/traces": trace_items("TR-16A", "TR-16B"),
            "GET /me": {"user_id": "@alice"},
            "POST /eval-datasets": {"dataset_id": "ED-16"},
            "POST /eval-datasets/ED-16/examples/from-trace": {"example_id": "EX-16"},
            "POST /review-queues": {"queue_id": "RQ-16"},
            "POST /review-queues/RQ-16/items": {"queued": 2},
        }
    )
    with caliber:
        result = run_16(caliber)
    assert result["dataset_id"] == "ED-16"
    assert result["queue_id"] == "RQ-16"
    assert result["trace_ids"] == ["TR-16A", "TR-16B"]


def _financial_analysis_fixture() -> dict[str, Any]:
    return {
        "currency": "USD",
        "periods": 12,
        "percentile_method": "linear_interpolation_rank_(n-1)*p",
        "statistics": expected_statistics(),
        "observations": ["Revenue and operating income increased over the year."],
    }


def test_cookbook_17_csv_has_realistic_balanced_monthly_financials() -> None:
    rows = list(csv.DictReader(io.StringIO(build_financial_csv().decode())))

    assert tuple(rows[0]) == CSV_FIELDS
    assert len(rows) == 12 == len(MONTHLY_FINANCIALS)
    assert [row["month"] for row in rows] == [f"2025-{month:02d}" for month in range(1, 13)]
    for row in rows:
        components = sum(
            int(row[column])
            for column in (
                "operating_expenses",
                "payroll",
                "cloud_compute_costs",
                "other_expenses",
            )
        )
        assert int(row["total_expenses"]) == components
        assert int(row["operating_income"]) == int(row["revenue"]) - components


def test_cookbook_17_rejects_incomplete_model_statistics() -> None:
    incomplete = _financial_analysis_fixture()
    del incomplete["statistics"]["revenue"]["p90"]

    with pytest.raises(ValueError, match="revenue"):
        parse_analysis(jsonlib.dumps(incomplete))


def test_cookbook_17_surfaces_provider_errors_before_persisting_output() -> None:
    with pytest.raises(RuntimeError, match="AuthenticationError"):
        parse_turn_analysis(
            {
                "assistant_message": {
                    "content": "I encountered an error: AuthenticationError",
                    "metadata_": {"error": True},
                }
            }
        )


def test_cookbook_17_creates_runs_and_persists_everything_through_typed_sdk() -> None:
    csv_bytes = build_financial_csv()
    analysis = _financial_analysis_fixture()
    scoped_headers: list[str | None] = []

    expected_result = (
        jsonlib.dumps(
            {
                "scenario": "monthly_company_financial_analysis",
                "project_id": "PRJ-17",
                "blob_bucket": "sdk-financial-unit-test",
                "input_object_key": "projects/PRJ-17/inputs/monthly-financials.csv",
                "managed_file_id": "FILE-17-IN",
                "prompt_name": "sdk-financial-analysis-unit-test",
                "prompt_version": 1,
                "prompt_alias": "prod",
                "assistant_session_id": "SESSION-17",
                "analysis": analysis,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode()

    def project(request: httpx.Request) -> dict[str, Any]:
        assert "x-caliber-project" not in request.headers
        body = jsonlib.loads(request.content)
        assert body["name"] == "SDK Financial Analysis unit-test"
        return {"project_id": "PRJ-17", "name": body["name"]}

    def create_bucket(request: httpx.Request) -> dict[str, Any]:
        scoped_headers.append(request.headers.get("x-caliber-project"))
        assert jsonlib.loads(request.content) == {"name": "sdk-financial-unit-test"}
        return {"name": "sdk-financial-unit-test"}

    def upload(request: httpx.Request) -> dict[str, Any]:
        scoped_headers.append(request.headers.get("x-caliber-project"))
        if b'filename="monthly-financials.csv"' in request.content:
            assert b"monthly-financials.csv" in request.content
            assert CSV_FIELDS[0].encode() in request.content
            assert b"projects/PRJ-17/inputs/monthly-financials.csv" in request.content
            return {"key": "projects/PRJ-17/inputs/monthly-financials.csv"}
        assert b"monthly-financial-analysis.json" in request.content
        assert b"projects/PRJ-17/outputs/monthly-financial-analysis.json" in request.content
        assert expected_result in request.content
        return {"key": "projects/PRJ-17/outputs/monthly-financial-analysis.json"}

    def download(request: httpx.Request) -> httpx.Response:
        scoped_headers.append(request.headers.get("x-caliber-project"))
        key = request.url.params["key"]
        if key.endswith("monthly-financials.csv"):
            return httpx.Response(200, content=csv_bytes, headers={"content-type": "text/csv"})
        assert key.endswith("monthly-financial-analysis.json")
        return httpx.Response(
            200, content=expected_result, headers={"content-type": "application/json"}
        )

    def import_object(request: httpx.Request) -> dict[str, Any]:
        scoped_headers.append(request.headers.get("x-caliber-project"))
        assert jsonlib.loads(request.content) == {
            "key": "projects/PRJ-17/inputs/monthly-financials.csv",
            "path": "inputs/monthly-financials.csv",
        }
        return {"file_id": "FILE-17-IN", "project_id": "PRJ-17"}

    def create_prompt(request: httpx.Request) -> dict[str, Any]:
        scoped_headers.append(request.headers.get("x-caliber-project"))
        body = jsonlib.loads(request.content)
        assert body == {
            "name": "sdk-financial-analysis-unit-test",
            "template": PROMPT_TEMPLATE,
            "commit_message": "Create SDK financial-analysis cookbook prompt",
        }
        return {"name": body["name"], "version": 1}

    def create_session(request: httpx.Request) -> dict[str, Any]:
        scoped_headers.append(request.headers.get("x-caliber-project"))
        body = jsonlib.loads(request.content)
        assert PROMPT_TEMPLATE in body["goal"]
        assert body["metadata_"]["project_id"] == "PRJ-17"
        assert body["metadata_"]["blob_bucket"] == "sdk-financial-unit-test"
        assert body["metadata_"]["blob_key"].endswith("monthly-financials.csv")
        assert body["metadata_"]["managed_file_id"] == "FILE-17-IN"
        assert body["metadata_"]["prompt_ref"] == ("prompts:/sdk-financial-analysis-unit-test@prod")
        assert body["skill_mode"] == "off"
        return {"session_id": "SESSION-17"}

    def send_message(request: httpx.Request) -> dict[str, Any]:
        scoped_headers.append(request.headers.get("x-caliber-project"))
        body = jsonlib.loads(request.content)
        assert csv_bytes.decode() in body["content"]
        assert body["artifact_type"] == "prompt"
        assert body["skill_mode"] == "off"
        return {"assistant_message": {"content": jsonlib.dumps(analysis)}}

    caliber = stub_server(
        {
            "GET /me": {"user_id": "@alice"},
            "POST /projects": project,
            "GET /object-store/status": {"connected": True},
            "POST /object-store/buckets": create_bucket,
            "POST /object-store/buckets/sdk-financial-unit-test/objects": upload,
            "GET /object-store/buckets/sdk-financial-unit-test/object": download,
            "POST /object-store/buckets/sdk-financial-unit-test/object/import": import_object,
            "POST /prompts": create_prompt,
            "GET /prompts/sdk-financial-analysis-unit-test/versions/1": {
                "name": "sdk-financial-analysis-unit-test",
                "version": 1,
                "template": PROMPT_TEMPLATE,
            },
            "POST /prompts/sdk-financial-analysis-unit-test/aliases/prod": {
                "name": "sdk-financial-analysis-unit-test",
                "alias": PROMPT_ALIAS,
                "version": 1,
                "release_status": "applied",
            },
            "GET /prompts": [
                {
                    "agent_id": "sdk-financial-analysis-unit-test",
                    "prompt_name": "sdk-financial-analysis-unit-test",
                    "version": 1,
                    "alias": PROMPT_ALIAS,
                    "has_prompt": True,
                }
            ],
            "POST /assistant/sessions": create_session,
            "POST /assistant/sessions/SESSION-17/messages": send_message,
        }
    )
    with caliber:
        result = run_17(caliber, run_key="unit-test")
        caliber.whoami()

    assert result == {
        "project_id": "PRJ-17",
        "blob_bucket": "sdk-financial-unit-test",
        "input_object_key": "projects/PRJ-17/inputs/monthly-financials.csv",
        "managed_file_id": "FILE-17-IN",
        "prompt_name": "sdk-financial-analysis-unit-test",
        "prompt_version": 1,
        "prompt_alias": "prod",
        "assistant_session_id": "SESSION-17",
        "output_object_key": "projects/PRJ-17/outputs/monthly-financial-analysis.json",
        "analysis": analysis,
    }
    assert scoped_headers and set(scoped_headers) == {"PRJ-17"}
