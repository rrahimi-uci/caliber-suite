"""B15.4's reconciled run-scoped proposal, approval and replay contract."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from caliber_sdk import CaliberClient
from caliber_sdk.models import (
    ApprovalProposal,
    RuntimeApprovalAck,
    WorkflowApprovalView,
    WorkflowRun,
    WorkflowRunReplayReport,
    WorkflowRuntimeApproval,
)

BASE = "https://caliber.test"


def client_with(handler: Any) -> CaliberClient:
    return CaliberClient(
        BASE,
        token="calpat_test",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def envelope(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data})


def body(request: httpx.Request) -> Any:
    return json.loads(request.content) if request.content else None


def test_submit_approval_proposal_decide_wait_export_replay_and_identity_chain() -> None:
    seen: list[tuple[str, str, Any]] = []
    replay_payload = {
        "schema_version": "v1",
        "workflow_run_id": "RUN-1",
        "manifest_id": "MF-1",
        "manifest_digest": "d" * 64,
        "observation_count": 1,
        "observation_ids": ["OBS-1"],
        "criteria_version": "harborlight-v1",
        "decisions": [
            {
                "candidate_id": "PROP-1",
                "criteria_version": "harborlight-v1",
                "outcome": "accepted",
                "observation_digest": "o" * 64,
                "failed_criteria": [],
                "original_decision_id": "DEC-1",
                "semantic_match": True,
                "observation_match": True,
            }
        ],
        "matches_original": True,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.rsplit("/caliber", 1)[-1]
        seen.append((request.method, path, body(request)))
        if (request.method, path) == ("POST", "/workflow-runs"):
            return envelope({"workflow_run_id": "RUN-1", "status": "queued"}, 202)
        if (request.method, path) == ("GET", "/workflow-runs/RUN-1/approvals"):
            return envelope(
                [
                    {
                        "runtime_approval_id": "RAP-1",
                        "workflow_run_id": "RUN-1",
                        "node_id": "review",
                        "status": "pending",
                        "proposal_id": "PROP-1",
                    }
                ]
            )
        if (request.method, path) == ("GET", "/workflow-runs/RUN-1/approvals/RAP-1"):
            return envelope(
                {
                    "approval": {"runtime_approval_id": "RAP-1", "workflow_run_id": "RUN-1"},
                    "binding": {
                        "proposal_id": "PROP-1",
                        "intent_hash": "i",
                        "evidence_hash": "e",
                        "policy_digest": "p",
                        "expires_at": "2026-11-04T00:00:00Z",
                    },
                    "proposal": {
                        "proposal_id": "PROP-1",
                        "connector": "document-request",
                        "connector_version": "1",
                        "effect_class": "irreversible_write",
                        "resource": "cases/MORT-2026-000184/document-requests",
                        "state": "proposed",
                        "args_fields": [{"name": "document_type", "kind": "string"}],
                    },
                    "policy": {"required_role": "caliber.approver", "approval_count": 1},
                    "quorum": {"approvers": [], "required": 1, "remaining": 1},
                    "state": {"code": "open"},
                    "allowed_responses": ["approve", "reject", "request_changes"],
                    "caller": {
                        "principal": "@approver",
                        "approve": {"allowed": True},
                        "reject": {"allowed": True},
                    },
                }
            )
        if (request.method, path) == ("POST", "/workflow-runs/RUN-1/approval/approve"):
            assert body(request) == {"runtime_approval_id": "RAP-1", "reason": "looks right"}
            return envelope(
                {
                    "workflow_run_id": "RUN-1",
                    "status": "waiting_approval",
                }
            )
        if (request.method, path) == ("POST", "/workflow-runs/RUN-1/approval/reject"):
            assert body(request) == {"runtime_approval_id": "RAP-1", "reason": "no"}
            return envelope(
                {
                    "workflow_run_id": "RUN-1",
                    "status": "failed",
                }
            )
        if (request.method, path) == ("GET", "/workflow-runs/RUN-1"):
            return envelope({"workflow_run_id": "RUN-1", "status": "completed"})
        if (request.method, path) == ("POST", "/workflow-runs/RUN-1/replay"):
            return envelope(replay_payload)
        if (request.method, path) == ("GET", "/workflow-runs/RUN-1/evidence"):
            return httpx.Response(200, content=b"zip")
        raise AssertionError(f"unstubbed route {request.method} {path}")

    with client_with(handler) as caliber:
        submitted = caliber.workflows.runs.submit(workflow_version_id="WFV-1", input="review")
        approvals = caliber.workflows.runs.approvals(submitted.workflow_run_id)
        view = caliber.workflows.runs.approval("RUN-1", approvals[0].runtime_approval_id)
        proposal = caliber.workflows.runs.approval_proposal("RUN-1", "RAP-1")
        decided = caliber.workflows.runs.decide(
            "RUN-1", runtime_approval_id="RAP-1", decision="approve", reason="looks right"
        )
        run = caliber.workflows.runs.wait("RUN-1", timeout=1, interval=0.001)
        archive = caliber.workflows.runs.evidence("RUN-1")
        replay = caliber.workflows.runs.replay("RUN-1", observation_ids=["OBS-1"])

    assert isinstance(submitted, WorkflowRun)
    assert isinstance(approvals[0], WorkflowRuntimeApproval)
    assert approvals[0]["status"] == "pending"
    assert isinstance(view, WorkflowApprovalView)
    assert isinstance(proposal, ApprovalProposal)
    assert isinstance(decided, WorkflowRun)
    assert isinstance(run, WorkflowRun)
    assert archive == b"zip"
    assert isinstance(replay, WorkflowRunReplayReport)
    assert replay.matches_original is True
    assert seen[-1] == ("POST", "/workflow-runs/RUN-1/replay", {"observation_ids": ["OBS-1"]})


def test_typed_compatibility_reads_preserve_unknown_fields_and_report_equality() -> None:
    from caliber_sdk.models import WorkflowRunReplayReport

    approval = WorkflowRuntimeApproval(extra={"future_field": "kept"})
    assert approval["future_field"] == "kept"
    with pytest.raises(KeyError):
        _ = approval["missing_field"]

    report = WorkflowRunReplayReport(workflow_run_id="RUN-1")
    assert report == {"workflow_run_id": "RUN-1"}
    assert report != {"workflow_run_id": "RUN-2"}
    assert report == WorkflowRunReplayReport(workflow_run_id="RUN-1")
    assert report != object()


def test_approve_decodes_a_partial_quorum_ack() -> None:
    with client_with(
        lambda request: envelope(
            {
                "workflow_run_id": "RUN-1",
                "runtime_approval_id": "RAP-1",
                "status": "pending",
                "approvals": 1,
                "required_approvals": 2,
                "remaining_approvals": 1,
            }
        )
    ) as caliber:
        ack = caliber.workflows.runs.approve(
            "RUN-1", runtime_approval_id="RAP-1", reason="first reviewer"
        )

    assert isinstance(ack, RuntimeApprovalAck)
    assert ack.remaining_approvals == 1


def test_decide_omits_optional_reason_and_legacy_reject_stays_compatible() -> None:
    seen: list[tuple[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, body(request)))
        return envelope({"workflow_run_id": "RUN-1", "status": "failed"})

    with client_with(handler) as caliber:
        caliber.workflows.runs.decide("RUN-1", runtime_approval_id="RAP-1", decision="reject")
        caliber.workflows.runs.reject("RUN-1")

    assert seen == [
        (
            "/ajax-api/2.0/mlflow/caliber/workflow-runs/RUN-1/approval/reject",
            {"runtime_approval_id": "RAP-1"},
        ),
        ("/ajax-api/2.0/mlflow/caliber/workflow-runs/RUN-1/approval/reject", {}),
    ]


def test_decide_rejects_unsupported_request_changes_before_http() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return envelope({})

    with client_with(handler) as caliber:
        with pytest.raises(ValueError, match=r"approve.*reject"):
            caliber.workflows.runs.decide(
                "RUN-1",
                runtime_approval_id="RAP-1",
                decision="request_changes",  # type: ignore[arg-type]
                reason="needs more evidence",
            )
        with pytest.raises(ValueError, match="must not be empty"):
            caliber.workflows.runs.decide("RUN-1", runtime_approval_id=" ", decision="approve")

    assert calls == 0
