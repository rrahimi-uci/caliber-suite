"""Workspace release evaluation/decision/approval and operation lifecycle:
``ProjectReleasesAPI``/``ProjectReleaseOperationsAPI`` (`P6-B`).

Both resources use plain ``limit``/``offset`` pagination -- unlike the
cursor-paginated import/revision/Change-Request families -- matching this
release family's own server routes
(:mod:`caliber.routes.workspace_releases`/``workspace_release_operations``).
A release-operation payload nests its items under ``{"operation": ...,
"items": [...]}``; every other single-object response here is the resource
directly under ``data``, and every list response is a bare array under
``data`` (no ``{"items": [...]}`` wrapper, unlike the cursor families).
"""

from __future__ import annotations

import json as jsonlib
from typing import Any

import httpx

from caliber_sdk import CaliberClient
from caliber_sdk.models.workspace import (
    WorkspaceBreakGlassApplyResult,
    WorkspaceRelease,
    WorkspaceReleaseDecision,
    WorkspaceReleaseEvaluation,
    WorkspaceReleaseEvidence,
    WorkspaceReleaseOperation,
    WorkspaceReleaseOperationResult,
)

BASE = "https://caliber.test"


def client_with(handler: Any) -> CaliberClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CaliberClient(BASE, token="calpat_test", http_client=http)


def envelope(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"data": data})


_RELEASE: dict[str, Any] = {
    "release_id": "WRL-1",
    "project_id": "PRJ-1",
    "revision_id": "WSR-1",
    "environment_id": "development",
    "environment_config_sha256": "a" * 64,
    "runtime_dependencies_sha256": "b" * 64,
    "policy_sha256": "c" * 64,
    "request_idempotency_key": "req-1",
    "status": "draft",
    "requested_by": "user-1",
    "lock_version": 1,
}

_EVALUATION: dict[str, Any] = {
    "evaluation_id": "WRE-1",
    "project_id": "PRJ-1",
    "workspace_release_id": "WRL-1",
    "idempotency_key": "eval-1",
    "evaluation_plan_sha256": "d" * 64,
    "input_sha256": "e" * 64,
    "status": "queued",
    "attempt_number": 1,
    "requested_by": "user-1",
}

_EVIDENCE: dict[str, Any] = {
    "evidence_id": "WEV-1",
    "workspace_release_id": "WRL-1",
    "kind": "evaluation_run",
    "evidence_ref": "run-1",
    "evidence_sha256": "f" * 64,
    "required": True,
    "recorded_by": "user-1",
}

_DECISION: dict[str, Any] = {
    "decision_id": "WRD-1",
    "workspace_release_id": "WRL-1",
    "kind": "quality",
    "decision": "go",
    "rationale": "looks good",
    "decided_by": "qa-1",
    "revision_sha256": "g" * 64,
    "environment_config_sha256": "a" * 64,
    "runtime_dependencies_sha256": "b" * 64,
    "gate_evidence_sha256": "h" * 64,
    "policy_sha256": "c" * 64,
}

_OPERATION: dict[str, Any] = {
    "operation_id": "WRO-1",
    "project_id": "PRJ-1",
    "workspace_release_id": "WRL-1",
    "environment_id": "production",
    "kind": "apply",
    "idempotency_key": "op-1",
    "expected_environment_lock_version": 1,
    "status": "prepared",
    "lock_version": 1,
    "requested_by": "user-1",
    "observation_count": 0,
}

_OPERATION_ITEM: dict[str, Any] = {
    "operation_item_id": "WROI-1",
    "workspace_release_operation_id": "WRO-1",
    "revision_resource_id": "WRP-1",
    "action": "promote",
    "target_ref": "ref-1",
    "status": "prepared",
}


# --- releases: list / create / get ------------------------------------------


def test_list_releases_sends_status_environment_and_paging_filters() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["params"] = dict(request.url.params)
        return envelope([_RELEASE])

    with client_with(handler) as caliber:
        releases = caliber.workspaces.releases.list(
            "PRJ-1", status="draft", environment_id="development", limit=10, offset=5
        )

    assert seen["path"] == "/projects/PRJ-1/releases"
    assert seen["params"] == {
        "status": "draft",
        "environment_id": "development",
        "limit": "10",
        "offset": "5",
    }
    assert releases == [WorkspaceRelease(**_RELEASE)]


def test_list_releases_omits_filters_when_unset() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return envelope([])

    with client_with(handler) as caliber:
        caliber.workspaces.releases.list("PRJ-1")

    assert seen["params"] == {}


def test_create_release_sends_required_and_optional_fields() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["json"] = jsonlib.loads(request.content)
        return httpx.Response(201, json={"data": _RELEASE})

    with client_with(handler) as caliber:
        release = caliber.workspaces.releases.create(
            "PRJ-1",
            revision_id="WSR-1",
            environment_id="development",
            environment_config_sha256="a" * 64,
            runtime_dependencies_sha256="b" * 64,
            policy_sha256="c" * 64,
            request_idempotency_key="req-1",
            change_request_id="WCR-1",
            predecessor_release_id="WRL-0",
        )

    assert seen["path"] == "/projects/PRJ-1/releases"
    assert seen["json"]["change_request_id"] == "WCR-1"
    assert seen["json"]["predecessor_release_id"] == "WRL-0"
    assert "change_request_head_id" not in seen["json"]
    assert "version_tag_id" not in seen["json"]
    assert release == WorkspaceRelease(**_RELEASE)


def test_get_release() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.rsplit("/caliber", 1)[-1] == "/projects/PRJ-1/releases/WRL-1"
        return envelope(_RELEASE)

    with client_with(handler) as caliber:
        release = caliber.workspaces.releases.get("PRJ-1", "WRL-1")

    assert release == WorkspaceRelease(**_RELEASE)


# --- evidence / evaluate / evaluations ---------------------------------------


def test_list_evidence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert (
            request.url.path.rsplit("/caliber", 1)[-1] == "/projects/PRJ-1/releases/WRL-1/evidence"
        )
        return envelope([_EVIDENCE])

    with client_with(handler) as caliber:
        evidence = caliber.workspaces.releases.list_evidence("PRJ-1", "WRL-1")

    assert evidence == [WorkspaceReleaseEvidence(**_EVIDENCE)]


def test_evaluate_sends_idempotency_key_and_digests() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["json"] = jsonlib.loads(request.content)
        return httpx.Response(202, json={"data": _EVALUATION})

    with client_with(handler) as caliber:
        evaluation = caliber.workspaces.releases.evaluate(
            "PRJ-1",
            "WRL-1",
            idempotency_key="eval-1",
            evaluation_plan_sha256="d" * 64,
            input_sha256="e" * 64,
        )

    assert seen["path"] == "/projects/PRJ-1/releases/WRL-1/evaluate"
    assert seen["json"] == {
        "idempotency_key": "eval-1",
        "evaluation_plan_sha256": "d" * 64,
        "input_sha256": "e" * 64,
    }
    assert evaluation == WorkspaceReleaseEvaluation(**_EVALUATION)


def test_list_evaluations() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert (
            request.url.path.rsplit("/caliber", 1)[-1]
            == "/projects/PRJ-1/releases/WRL-1/evaluations"
        )
        return envelope([_EVALUATION])

    with client_with(handler) as caliber:
        evaluations = caliber.workspaces.releases.list_evaluations("PRJ-1", "WRL-1")

    assert evaluations == [WorkspaceReleaseEvaluation(**_EVALUATION)]


def test_get_evaluation() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert (
            request.url.path.rsplit("/caliber", 1)[-1]
            == "/projects/PRJ-1/releases/WRL-1/evaluations/WRE-1"
        )
        return envelope(_EVALUATION)

    with client_with(handler) as caliber:
        evaluation = caliber.workspaces.releases.get_evaluation("PRJ-1", "WRL-1", "WRE-1")

    assert evaluation == WorkspaceReleaseEvaluation(**_EVALUATION)


# --- quality-signoff / approve / break-glass-apply --------------------------


def test_quality_signoff_sends_decision_body() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["json"] = jsonlib.loads(request.content)
        return httpx.Response(201, json={"data": _DECISION})

    with client_with(handler) as caliber:
        decision = caliber.workspaces.releases.quality_signoff(
            "PRJ-1",
            "WRL-1",
            decision="go",
            gate_evidence_sha256="h" * 64,
            rationale="looks good",
            change_request_head_id="WCH-1",
        )

    assert seen["path"] == "/projects/PRJ-1/releases/WRL-1/quality-signoff"
    assert seen["json"] == {
        "decision": "go",
        "gate_evidence_sha256": "h" * 64,
        "rationale": "looks good",
        "change_request_head_id": "WCH-1",
    }
    assert decision == WorkspaceReleaseDecision(**_DECISION)


def test_approve_sends_decision_body_without_optional_head() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["json"] = jsonlib.loads(request.content)
        return httpx.Response(201, json={"data": {**_DECISION, "kind": "release"}})

    with client_with(handler) as caliber:
        decision = caliber.workspaces.releases.approve(
            "PRJ-1", "WRL-1", decision="go", gate_evidence_sha256="h" * 64
        )

    assert seen["path"] == "/projects/PRJ-1/releases/WRL-1/approve"
    assert seen["json"] == {
        "decision": "go",
        "gate_evidence_sha256": "h" * 64,
        "rationale": "",
    }
    assert decision.kind == "release"


def test_break_glass_apply_sends_every_field() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["json"] = jsonlib.loads(request.content)
        return httpx.Response(
            201, json={"data": {"authorization_id": "WBG-1", "operation_id": "WRO-1"}}
        )

    with client_with(handler) as caliber:
        result = caliber.workspaces.releases.break_glass_apply(
            "PRJ-1",
            "WRL-1",
            reason="prod incident",
            incident_ref="INC-1",
            authorization_ref="AUTH-1",
            expires_at="2026-01-01T00:00:00Z",
            gate_evidence_sha256="h" * 64,
            expected_current_release_id="WRL-1",
            expected_environment_lock_version=3,
            idempotency_key="bg-1",
        )

    assert seen["path"] == "/projects/PRJ-1/releases/WRL-1/break-glass-apply"
    assert seen["json"] == {
        "reason": "prod incident",
        "incident_ref": "INC-1",
        "authorization_ref": "AUTH-1",
        "expires_at": "2026-01-01T00:00:00Z",
        "gate_evidence_sha256": "h" * 64,
        "expected_current_release_id": "WRL-1",
        "expected_environment_lock_version": 3,
        "idempotency_key": "bg-1",
    }
    assert result == WorkspaceBreakGlassApplyResult(authorization_id="WBG-1", operation_id="WRO-1")


# --- release operations: list / get / create / apply / observe / cancel ----


def test_list_operations_sends_paging_params() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["params"] = dict(request.url.params)
        return envelope([_OPERATION])

    with client_with(handler) as caliber:
        operations = caliber.workspaces.release_operations.list(
            "PRJ-1", "WRL-1", limit=25, offset=0
        )

    assert seen["path"] == "/projects/PRJ-1/releases/WRL-1/operations"
    assert seen["params"] == {"limit": "25", "offset": "0"}
    assert operations == [WorkspaceReleaseOperation(**_OPERATION)]


def test_get_operation_decodes_nested_operation_and_items() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert (
            request.url.path.rsplit("/caliber", 1)[-1]
            == "/projects/PRJ-1/releases/WRL-1/operations/WRO-1"
        )
        return envelope({"operation": _OPERATION, "items": [_OPERATION_ITEM]})

    with client_with(handler) as caliber:
        result = caliber.workspaces.release_operations.get("PRJ-1", "WRL-1", "WRO-1")

    assert isinstance(result, WorkspaceReleaseOperationResult)
    assert result.operation == WorkspaceReleaseOperation(**_OPERATION)
    assert len(result.items) == 1
    assert result.items[0].operation_item_id == "WROI-1"


def test_create_operation_sends_kind_and_cas_fields() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path.rsplit("/caliber", 1)[-1]
        seen["json"] = jsonlib.loads(request.content)
        return httpx.Response(201, json={"data": {"operation": _OPERATION, "items": []}})

    with client_with(handler) as caliber:
        result = caliber.workspaces.release_operations.create(
            "PRJ-1",
            "WRL-1",
            kind="apply",
            idempotency_key="op-1",
            expected_environment_lock_version=1,
            target_release_id="WRL-1",
        )

    assert seen["path"] == "/projects/PRJ-1/releases/WRL-1/operations"
    assert seen["json"] == {
        "kind": "apply",
        "idempotency_key": "op-1",
        "expected_environment_lock_version": 1,
        "target_release_id": "WRL-1",
    }
    assert result.operation == WorkspaceReleaseOperation(**_OPERATION)
    assert result.items == []


def test_apply_observe_cancel_expired_hit_their_own_literal_paths() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path.rsplit("/caliber", 1)[-1])
        return envelope({"operation": _OPERATION, "items": []})

    with client_with(handler) as caliber:
        caliber.workspaces.release_operations.apply("PRJ-1", "WRL-1", "WRO-1")
        caliber.workspaces.release_operations.observe("PRJ-1", "WRL-1", "WRO-1")
        caliber.workspaces.release_operations.cancel_expired("PRJ-1", "WRL-1", "WRO-1")

    assert seen_paths == [
        "/projects/PRJ-1/releases/WRL-1/operations/WRO-1:apply",
        "/projects/PRJ-1/releases/WRL-1/operations/WRO-1:observe",
        "/projects/PRJ-1/releases/WRL-1/operations/WRO-1:cancel-expired",
    ]


# --- waiters -----------------------------------------------------------------


def test_wait_for_evaluation_polls_past_non_terminal_states() -> None:
    states = iter(["queued", "running", "succeeded"])

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({**_EVALUATION, "status": next(states)})

    with client_with(handler) as caliber:
        evaluation = caliber.workspaces.releases.wait_for_evaluation(
            "PRJ-1", "WRL-1", "WRE-1", interval=0.001, max_interval=0.001, timeout=5
        )

    assert evaluation.status == "succeeded"
    assert evaluation.is_terminal


def test_operations_wait_polls_past_non_terminal_states() -> None:
    states = iter(["prepared", "applying", "applied"])

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"operation": {**_OPERATION, "status": next(states)}, "items": []})

    with client_with(handler) as caliber:
        result = caliber.workspaces.release_operations.wait(
            "PRJ-1", "WRL-1", "WRO-1", interval=0.001, max_interval=0.001, timeout=5
        )

    assert result.operation.status == "applied"
    assert result.is_terminal


def test_operations_wait_treats_reconcile_required_as_terminal() -> None:
    """It will never advance on its own -- a waiter must not block past it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return envelope({"operation": {**_OPERATION, "status": "reconcile_required"}, "items": []})

    with client_with(handler) as caliber:
        result = caliber.workspaces.release_operations.wait(
            "PRJ-1", "WRL-1", "WRO-1", interval=0.001, max_interval=0.001, timeout=5
        )

    assert result.operation.status == "reconcile_required"
    assert result.is_terminal
