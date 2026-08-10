"""The full SDK cookbook examples are executable and stay tied to the docs."""

from __future__ import annotations

from typing import Any

import httpx

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

BASE = "https://caliber.test"


def stub_server(routes: dict[str, Any]) -> CaliberClient:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.rsplit("/caliber", 1)[-1]
        key = f"{request.method} {path}"
        if key not in routes:  # pragma: no cover
            raise AssertionError(f"example called an unstubbed route: {key}")
        body = routes[key]
        payload = body(request) if callable(body) else body
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


def plan_detail(plan_id: str, status: str) -> dict[str, Any]:
    return {
        "plan": {"plan_id": plan_id, "goal": "g", "status": status},
        "steps": [{"step_id": f"{plan_id}-1", "plan_id": plan_id, "title": "step"}],
    }


def trace_items(*trace_ids: str) -> dict[str, Any]:
    return {"items": [{"trace_id": trace_id} for trace_id in trace_ids]}


def test_cookbook_01_builds_dataset_judge_and_evaluation() -> None:
    caliber = stub_server(
        {
            "GET /cookbooks": cookbook_listing(
                "01",
                "Trustworthy Intake Classifier",
                prerequisite="Configured model provider",
            ),
            "POST /cookbooks/01/install": installed_payload("01", "WF-01", "WFV-01"),
            "GET /me": {"user_id": "@alice"},
            "POST /eval-datasets": {"dataset_id": "ED-01"},
            "POST /eval-datasets/ED-01/examples": {"example_id": "EX-01"},
            "POST /judges": {"judge_id": "J-01"},
            "POST /evaluations": {"evaluation_id": "EV-01", "status": "queued"},
        }
    )
    with caliber:
        result = run_01(caliber)
    assert result["dataset_id"] == "ED-01"
    assert result["judge_id"] == "J-01"
    assert result["evaluation_id"] == "EV-01"


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


def test_cookbook_04_uploads_managed_file_and_validates_workflow() -> None:
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
            "POST /workflow-versions/WFV-04/validate": {"valid": True},
        }
    )
    with caliber:
        result = run_04(caliber)
    assert result["project_id"] == "PRJ-04"
    assert result["file_id"] == "FILE-04"
    assert result["validation"] == {"valid": True}


def test_cookbook_05_connects_and_calibrates_mcp_server() -> None:
    caliber = stub_server(
        {
            "GET /cookbooks": cookbook_listing(
                "05",
                "Governed Tool Connectivity",
                prerequisite="Reachable MCP server",
            ),
            "POST /cookbooks/05/install": installed_payload("05", "WF-05", "WFV-05"),
            "POST /mcp-servers": {"server_id": "MCP-05"},
            "POST /mcp-servers/MCP-05/test-connection": {"ok": True},
            "POST /mcp-servers/MCP-05/discover-tools": {"discovered": 2},
            "PATCH /mcp-servers/MCP-05/tools/issue_write/policy": {"allowed": False},
            "PUT /mcp-servers/MCP-05/tools/search_repositories/test-cases": {"saved": 1},
            "POST /mcp-servers/MCP-05/tools/search_repositories/calibrate": {"status": "passed"},
        }
    )
    with caliber:
        result = run_05(caliber)
    assert result["server_id"] == "MCP-05"
    assert result["connection"] == {"ok": True}


def test_cookbook_06_builds_and_queries_knowledge_base() -> None:
    caliber = stub_server(
        {
            "GET /cookbooks": cookbook_listing(
                "06",
                "Grounded Knowledge Assistant",
                prerequisite="Embedding provider",
            ),
            "POST /cookbooks/06/install": installed_payload("06", "WF-06", "WFV-06"),
            "POST /knowledge-bases": {"knowledge_base_id": "KB-06"},
            "POST /knowledge-bases/KB-06/versions": {"version_id": "KBV-06"},
            "POST /knowledge/query": {"answer": "Escalate to billing review."},
            "POST /knowledge-bases/KB-06/calibrate": {"score": 0.91},
        }
    )
    with caliber:
        result = run_06(caliber)
    assert result["knowledge_base_id"] == "KB-06"
    assert result["version_id"] == "KBV-06"


def test_cookbook_07_creates_review_and_evaluation_assets() -> None:
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
        }
    )
    with caliber:
        result = run_07(caliber)
    assert result["queue_id"] == "RQ-07"
    assert result["evaluation_id"] == "EV-07"


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


def test_cookbook_09_publishes_and_waits_for_failure_state() -> None:
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
            "POST /workflow-runs": {"workflow_run_id": "WR-09", "status": "queued"},
            "GET /workflow-runs/WR-09": {"workflow_run_id": "WR-09", "status": "failed"},
        }
    )
    with caliber:
        result = run_09(caliber)
    assert result["run_id"] == "WR-09"
    assert result["status"] == "failed"


def test_cookbook_10_creates_queue_for_disagreement_review() -> None:
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
        }
    )
    with caliber:
        result = run_10(caliber)
    assert result["queue_id"] == "RQ-10"
    assert result["judge_id"] == "J-10"


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


def test_cookbook_12_executes_the_aria_plan_after_approval() -> None:
    states = iter(["paused"])
    caliber = stub_server(
        {
            "GET /cookbooks": recipe_payload("12", "Aria Evaluation Harness from Intent"),
            "POST /cookbooks/12/install": installed_payload("12", "WF-12", "WFV-12"),
            "POST /aria/plans": plan_detail("PLAN-12", "planning"),
            "GET /aria/plans/PLAN-12": lambda _r: plan_detail("PLAN-12", next(states)),
            "POST /aria/plans/PLAN-12/approve": plan_detail("PLAN-12", "approved"),
            "POST /aria/plans/PLAN-12/execute": plan_detail("PLAN-12", "completed"),
        }
    )
    with caliber:
        result = run_12(caliber)
    assert result["plan_id"] == "PLAN-12"
    assert result["status"] == "completed"


def test_cookbook_13_reads_back_the_created_review_queues() -> None:
    states = iter(["paused"])
    caliber = stub_server(
        {
            "GET /cookbooks": cookbook_listing(
                "13",
                "Aria Review Queue",
                prerequisite="Trace ids",
            ),
            "POST /cookbooks/13/install": installed_payload("13", "WF-13", "WFV-13"),
            "POST /aria/plans": plan_detail("PLAN-13", "planning"),
            "GET /aria/plans/PLAN-13": lambda _r: plan_detail("PLAN-13", next(states)),
            "POST /aria/plans/PLAN-13/approve": plan_detail("PLAN-13", "approved"),
            "POST /aria/plans/PLAN-13/execute": plan_detail("PLAN-13", "completed"),
            "GET /review-queues": [{"queue_id": "RQ-13"}],
        }
    )
    with caliber:
        result = run_13(caliber)
    assert result["queues"] == ["RQ-13"]


def test_cookbook_14_reads_back_all_governance_assets() -> None:
    states = iter(["paused"])
    caliber = stub_server(
        {
            "GET /cookbooks": recipe_payload("14", "Aria Governance Starter Kit"),
            "POST /cookbooks/14/install": installed_payload("14", "WF-14", "WFV-14"),
            "POST /aria/plans": plan_detail("PLAN-14", "planning"),
            "GET /aria/plans/PLAN-14": lambda _r: plan_detail("PLAN-14", next(states)),
            "POST /aria/plans/PLAN-14/approve": plan_detail("PLAN-14", "approved"),
            "POST /aria/plans/PLAN-14/execute": plan_detail("PLAN-14", "completed"),
            "GET /judges": [{"judge_id": "J-14"}],
            "GET /eval-datasets": [{"dataset_id": "ED-14"}],
            "GET /review-queues": [{"queue_id": "RQ-14"}],
        }
    )
    with caliber:
        result = run_14(caliber)
    assert result["judges"] == ["J-14"]
    assert result["datasets"] == ["ED-14"]
    assert result["queues"] == ["RQ-14"]


def test_cookbook_15_lists_review_queues_and_jobs() -> None:
    states = iter(["paused"])
    caliber = stub_server(
        {
            "GET /cookbooks": cookbook_listing(
                "15",
                "Aria Triage & Recalibrate Loop",
                prerequisite="Existing traces",
            ),
            "POST /cookbooks/15/install": installed_payload("15", "WF-15", "WFV-15"),
            "POST /aria/plans": plan_detail("PLAN-15", "planning"),
            "GET /aria/plans/PLAN-15": lambda _r: plan_detail("PLAN-15", next(states)),
            "POST /aria/plans/PLAN-15/approve": plan_detail("PLAN-15", "approved"),
            "POST /aria/plans/PLAN-15/execute": plan_detail("PLAN-15", "completed"),
            "GET /review-queues": [{"queue_id": "RQ-15"}],
            "GET /jobs": [{"job_id": "JOB-15", "status": "queued"}],
        }
    )
    with caliber:
        result = run_15(caliber)
    assert result["queue_ids"] == ["RQ-15"]
    assert result["job_ids"] == ["JOB-15"]


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
