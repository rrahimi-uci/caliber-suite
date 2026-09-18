"""P5-C HTTP journeys for project-scoped Workspace release operations."""

from __future__ import annotations

import hashlib
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberWorkspaceBreakGlassAuthorization,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRelease,
    CaliberWorkspaceRevision,
    CaliberWorkspaceRevisionResource,
)
from caliber.workspace_release_adapters import (
    FakeWorkspaceResourceAdapter,
    ProviderOutcome,
    WorkspaceResourceAdapterRegistry,
)
from caliber.workspace_release_operations import prepare_workspace_release_operation
from caliber.workspace_release_service import create_workspace_release

PREFIX = "/ajax-api/2.0/mlflow/caliber"
HEX = "a" * 64


def _create_project(client: TestClient, name: str) -> str:
    response = client.post(f"{PREFIX}/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["project_id"])


def _seed_release(
    db_session: Session,
    *,
    project_id: str,
    revision_id: str,
    release_key: str,
    environment_name: str = "dev",
) -> tuple[CaliberWorkspaceRelease, CaliberWorkspaceEnvironment]:
    digest = hashlib.sha256(release_key.encode()).hexdigest()
    environment = db_session.execute(
        select(CaliberWorkspaceEnvironment).where(
            CaliberWorkspaceEnvironment.project_id == project_id,
            CaliberWorkspaceEnvironment.name == environment_name,
        )
    ).scalar_one()
    revision = CaliberWorkspaceRevision(
        revision_id=revision_id,
        project_id=project_id,
        revision_number=int(release_key.rsplit("-", 1)[-1]),
        source_id=None,
        source_commit_sha=None,
        manifest={"apiVersion": "caliber/v1alpha1"},
        manifest_sha256=digest,
        source_bundle_sha256=digest,
        source_attestation="caller_attested",
        revision_sha256=digest,
        status="ready",
        created_by="@test",
    )
    db_session.add(revision)
    db_session.add(
        CaliberWorkspaceRevisionResource(
            resource_pin_id=f"WSPIN-{release_key}",
            revision_id=revision_id,
            resource_type="fake",
            logical_name="service",
            resource_id="service",
            version_ref="1",
            content_sha256=digest,
            provider_ref=f"fake:{release_key}",
            purpose="runtime",
            resolution={},
        )
    )
    db_session.flush()
    release = create_workspace_release(
        db_session,
        project_id=project_id,
        revision_id=revision_id,
        environment_id=environment.environment_id,
        environment_config_sha256=HEX,
        runtime_dependencies_sha256=HEX,
        policy_sha256=HEX,
        request_idempotency_key=release_key,
        requested_by="@test",
    )
    release.status = "approved"
    db_session.commit()
    return release, environment


def _operation_path(project_id: str, release_id: str) -> str:
    return f"{PREFIX}/projects/{project_id}/releases/{release_id}/operations"


def test_release_operation_routes_cover_apply_list_detail_and_rollback(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client, "P5-C operations API")
    first, environment = _seed_release(
        db_session, project_id=project_id, revision_id="WSR-route-1", release_key="route-1"
    )
    adapter = FakeWorkspaceResourceAdapter()
    client.app.state.workspace_resource_adapter_registry = WorkspaceResourceAdapterRegistry(
        {"fake": adapter}
    )

    path = _operation_path(project_id, first.release_id)
    prepared = client.post(
        path,
        json={
            "kind": "apply",
            "idempotency_key": "route-apply-1",
            "expected_environment_lock_version": 1,
        },
    )
    assert prepared.status_code == 201, prepared.text
    prepared_data = prepared.json()["data"]
    operation_id = prepared_data["operation"]["operation_id"]
    assert prepared_data["operation"]["status"] == "prepared"
    assert len(prepared_data["items"]) == 1

    listed = client.get(path)
    assert listed.status_code == 200, listed.text
    assert listed.json()["data"][0]["operation_id"] == operation_id
    detail = client.get(f"{path}/{operation_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["items"][0]["status"] == "prepared"

    applied = client.post(f"{path}/{operation_id}:apply")
    assert applied.status_code == 200, applied.text
    assert applied.json()["data"]["operation"]["status"] == "applied"
    assert len(adapter.apply_calls) == 1
    db_session.refresh(environment)
    assert environment.current_release_id == first.release_id

    second, _ = _seed_release(
        db_session, project_id=project_id, revision_id="WSR-route-2", release_key="route-2"
    )
    second_path = _operation_path(project_id, second.release_id)
    second_prepared = client.post(
        second_path,
        json={
            "kind": "apply",
            "idempotency_key": "route-apply-2",
            "expected_environment_lock_version": 2,
            "expected_current_release_id": first.release_id,
        },
    )
    assert second_prepared.status_code == 201, second_prepared.text
    second_operation_id = second_prepared.json()["data"]["operation"]["operation_id"]
    assert client.post(f"{second_path}/{second_operation_id}:apply").status_code == 200

    rollback = client.post(
        second_path,
        json={
            "kind": "rollback",
            "idempotency_key": "route-rollback-1",
            "expected_environment_lock_version": 3,
            "expected_current_release_id": second.release_id,
            "target_release_id": first.release_id,
        },
    )
    assert rollback.status_code == 201, rollback.text
    rollback_id = rollback.json()["data"]["operation"]["operation_id"]
    rolled_back = client.post(f"{second_path}/{rollback_id}:apply")
    assert rolled_back.status_code == 200, rolled_back.text
    assert rolled_back.json()["data"]["operation"]["status"] == "applied"
    assert len(adapter.rollback_calls) == 1


def test_release_operation_routes_fail_closed_without_adapter_and_on_degraded_environment(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client, "P5-C refusal API")
    release, environment = _seed_release(
        db_session, project_id=project_id, revision_id="WSR-route-3", release_key="route-3"
    )
    client.app.state.workspace_resource_adapter_registry = WorkspaceResourceAdapterRegistry()
    path = _operation_path(project_id, release.release_id)
    missing_adapter = client.post(
        path,
        json={
            "kind": "apply",
            "idempotency_key": "route-missing-adapter",
            "expected_environment_lock_version": 1,
        },
    )
    assert missing_adapter.status_code == 409, missing_adapter.text
    assert "adapter" in missing_adapter.json()["detail"]

    client.app.state.workspace_resource_adapter_registry = WorkspaceResourceAdapterRegistry(
        {"fake": FakeWorkspaceResourceAdapter()}
    )
    environment.status = "degraded"
    db_session.commit()
    degraded = client.post(
        path,
        json={
            "kind": "apply",
            "idempotency_key": "route-degraded",
            "expected_environment_lock_version": 1,
        },
    )
    assert degraded.status_code == 409, degraded.text
    assert "not active" in degraded.json()["detail"]


def test_release_operation_routes_cover_refusals_and_observation(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client, "P5-C operation refusal branches")
    release, _environment = _seed_release(
        db_session, project_id=project_id, revision_id="WSR-route-4", release_key="route-4"
    )
    path = _operation_path(project_id, release.release_id)

    mismatch = client.get(path, headers={"X-CALIBER-Project": "other-project"})
    assert mismatch.status_code == 400
    missing_release = client.get(_operation_path(project_id, "missing-release"))
    assert missing_release.status_code == 404
    missing_operation = client.get(f"{path}/missing-operation")
    assert missing_operation.status_code == 404

    # An app with no provider registry refuses the intent without creating a
    # durable operation.  The registry is then installed for the remaining
    # provider-effect journey.
    no_registry = client.post(
        path,
        json={
            "kind": "apply",
            "idempotency_key": "route-no-registry",
            "expected_environment_lock_version": 1,
        },
    )
    assert no_registry.status_code == 409

    adapter = FakeWorkspaceResourceAdapter()
    client.app.state.workspace_resource_adapter_registry = WorkspaceResourceAdapterRegistry(
        {"fake": adapter}
    )
    prepared = client.post(
        path,
        json={
            "kind": "apply",
            "idempotency_key": "route-observe",
            "expected_environment_lock_version": 1,
        },
    )
    assert prepared.status_code == 201, prepared.text
    operation_id = prepared.json()["data"]["operation"]["operation_id"]
    adapter.queue_apply(
        ProviderOutcome(
            "reconcile_required",
            "fake:ambiguous",
            error_code="unknown",
            error_summary="provider response was lost",
        )
    )
    client.app.state.workspace_resource_adapter_registry = WorkspaceResourceAdapterRegistry()
    refused_apply = client.post(f"{path}/{operation_id}:apply")
    assert refused_apply.status_code == 409

    client.app.state.workspace_resource_adapter_registry = WorkspaceResourceAdapterRegistry(
        {"fake": adapter}
    )
    applied = client.post(f"{path}/{operation_id}:apply")
    assert applied.status_code == 200, applied.text
    assert applied.json()["data"]["operation"]["status"] == "reconcile_required"
    adapter.queue_observe(ProviderOutcome("applied", "fake:observed"))
    observed = client.post(f"{path}/{operation_id}:observe")
    assert observed.status_code == 200, observed.text
    assert observed.json()["data"]["operation"]["status"] == "applied"

    client.app.state.workspace_resource_adapter_registry = object()
    with pytest.raises(RuntimeError, match="invalid type"):
        client.post(
            path,
            json={
                "kind": "apply",
                "idempotency_key": "route-invalid-registry",
                "expected_environment_lock_version": 2,
            },
        )


def test_release_operation_cancel_expired_route(client: TestClient, db_session: Session) -> None:
    project_id = _create_project(client, "P5-C cancel API")
    release, environment = _seed_release(
        db_session,
        project_id=project_id,
        revision_id="WSR-route-5",
        release_key="route-5",
        environment_name="prod",
    )
    release.status = "awaiting_quality_signoff"
    release.evaluation_evidence_sha256 = HEX
    environment.status = "active"
    authorization = CaliberWorkspaceBreakGlassAuthorization(
        authorization_id="WSBGA-route-5",
        project_id=project_id,
        workspace_release_id=release.release_id,
        environment_id=environment.environment_id,
        reason="incident",
        incident_ref="INC-route-5",
        authorization_ref="AUTH-route-5",
        authorized_by="@test",
        credential_kind="session",
        credential_id="session-route-5",
        revision_sha256=db_session.get(CaliberWorkspaceRevision, "WSR-route-5").revision_sha256,
        environment_config_sha256=HEX,
        runtime_dependencies_sha256=HEX,
        gate_evidence_sha256=HEX,
        policy_sha256=HEX,
        created_at=datetime(2019, 1, 1),
        expires_at=datetime(2099, 1, 1),
    )
    db_session.add(authorization)
    db_session.flush()
    adapter = FakeWorkspaceResourceAdapter()
    prepared = prepare_workspace_release_operation(
        db_session,
        project_id=project_id,
        workspace_release_id=release.release_id,
        environment_id=environment.environment_id,
        kind="apply",
        idempotency_key="route-expired",
        expected_environment_lock_version=1,
        requested_by="@test",
        adapters=WorkspaceResourceAdapterRegistry({"fake": adapter}),
        break_glass_authorization_id=authorization.authorization_id,
    )
    authorization.expires_at = datetime(2020, 1, 1)
    db_session.commit()
    path = _operation_path(project_id, release.release_id)
    cancelled = client.post(f"{path}/{prepared.operation.operation_id}:cancel-expired")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["data"]["operation"]["status"] == "cancelled"
    assert adapter.apply_calls == []
