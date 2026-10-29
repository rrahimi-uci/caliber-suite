"""B02.6: cron and service-token ingress use the typed governed admission path."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberPolicyDecision,
    CaliberProject,
    CaliberRunGrant,
    CaliberRunManifest,
    CaliberWorkflowRun,
    CaliberWorkspaceRelease,
    CaliberWorkspaceRuntimeLineage,
)
from caliber.orchestrator.scheduler import WorkflowSchedulerTask
from caliber.policy.grants import GrantError, authorize, create_grant
from caliber.policy.principal import (
    SERVICE_PRINCIPAL_ACTION_NOT_ALLOWED,
    SERVICE_PRINCIPAL_AUTH_METHOD,
    SERVICE_PRINCIPAL_EXPIRED,
    SERVICE_PRINCIPAL_ORGANIZATION_MISMATCH,
    SERVICE_PRINCIPAL_PROJECT_MISMATCH,
    SERVICE_PRINCIPAL_PURPOSE_MISMATCH,
    SERVICE_PRINCIPAL_RESOURCE_NOT_ALLOWED,
    SERVICE_PRINCIPAL_REVOKED,
    SERVICE_PRINCIPAL_WORKLOAD_MISMATCH,
    PrincipalError,
    ServicePrincipal,
    ServicePrincipalError,
    Subject,
    Workload,
)
from caliber.run_manifest import read_manifest
from caliber.workflows.admission import AdmissionSettings
from caliber.workflows.run_launch import current_runtime_lineage_coordinates
from tests.test_run_admission import _project_workflow
from tests.workflow_helpers import PREFIX, deploy_prod, seed_workspace_release

PROJECT = "P-admit"


def _principal(
    *,
    project_id: str = PROJECT,
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> tuple[ServicePrincipal, Workload]:
    workload = Workload.for_workflow("service-workflow", "a" * 64)
    return (
        ServicePrincipal(
            principal_id="service_token:svt-test",
            tenant_id="local",
            project_id=project_id,
            workflow_id="service-workflow",
            workload_id=workload.workload_id,
            purpose="workflow_run:service",
            allowed_actions=frozenset({"resource.execute"}),
            allowed_resources=frozenset({"workflow:service-workflow"}),
            expires_at=expires_at,
            revoked_at=revoked_at,
        ),
        workload,
    )


def test_service_principal_is_typed_and_binding_is_exact() -> None:
    principal, workload = _principal()
    identity = principal.to_identity()
    assert identity.user_id == principal.principal_id
    assert identity.credential_kind == SERVICE_PRINCIPAL_AUTH_METHOD
    assert identity.scopes == frozenset()
    assert principal.to_subject() == Subject(
        user_id=principal.principal_id,
        auth_method=SERVICE_PRINCIPAL_AUTH_METHOD,
        project_id=PROJECT,
    )

    principal.validate_binding(
        tenant_id="local",
        project_id=PROJECT,
        workflow_id="service-workflow",
        workload=workload,
        purpose="workflow_run:service",
        action="resource.execute",
        resource="workflow:service-workflow",
    )
    with pytest.raises(ServicePrincipalError) as mismatch:
        principal.validate_binding(
            tenant_id="local",
            project_id="other-project",
            workflow_id="service-workflow",
            workload=workload,
            purpose="workflow_run:service",
            action="resource.execute",
            resource="workflow:service-workflow",
        )
    assert mismatch.value.code == SERVICE_PRINCIPAL_PROJECT_MISMATCH


def test_service_principal_refusals_are_named_and_fail_closed() -> None:
    principal, workload = _principal()
    binding = {
        "tenant_id": "local",
        "project_id": PROJECT,
        "workflow_id": "service-workflow",
        "workload": workload,
        "purpose": "workflow_run:service",
        "action": "resource.execute",
        "resource": "workflow:service-workflow",
    }
    cases = [
        (replace(principal, revoked_at=datetime.now(timezone.utc)), {}, SERVICE_PRINCIPAL_REVOKED),
        (
            replace(principal, expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)),
            {"now": datetime.now(timezone.utc)},
            SERVICE_PRINCIPAL_EXPIRED,
        ),
        (principal, {"tenant_id": "other-tenant"}, SERVICE_PRINCIPAL_ORGANIZATION_MISMATCH),
        (principal, {"project_id": "other-project"}, SERVICE_PRINCIPAL_PROJECT_MISMATCH),
        (principal, {"workflow_id": "other-workflow"}, SERVICE_PRINCIPAL_WORKLOAD_MISMATCH),
        (
            principal,
            {"workload": Workload.for_workflow("service-workflow", "b" * 64)},
            SERVICE_PRINCIPAL_WORKLOAD_MISMATCH,
        ),
        (principal, {"purpose": "workflow_run:cron"}, SERVICE_PRINCIPAL_PURPOSE_MISMATCH),
        (principal, {"action": "context.read"}, SERVICE_PRINCIPAL_ACTION_NOT_ALLOWED),
        (principal, {"resource": "workflow:other"}, SERVICE_PRINCIPAL_RESOURCE_NOT_ALLOWED),
    ]
    for candidate, changes, code in cases:
        with pytest.raises(ServicePrincipalError) as refusal:
            candidate.validate_binding(**(binding | changes))
        assert refusal.value.code == code

    with pytest.raises(PrincipalError, match="server-created credential_id"):
        principal.to_subject().to_identity(frozenset())

    assert principal.auth_method == SERVICE_PRINCIPAL_AUTH_METHOD
    with pytest.raises(PrincipalError, match="allowed_actions"):
        replace(principal, allowed_actions=frozenset())
    with pytest.raises(PrincipalError, match="allowed_resources"):
        replace(principal, allowed_resources=frozenset())
    with pytest.raises(PrincipalError, match="allowed_actions"):
        replace(principal, allowed_actions=frozenset({""}))
    with pytest.raises(PrincipalError, match="allowed_resources"):
        replace(principal, allowed_resources=frozenset({""}))
    with pytest.raises(PrincipalError, match="workload_id"):
        replace(principal, workload_id="not-derived")


def test_expired_service_principal_cannot_issue_a_grant(db_session: Session) -> None:
    project = CaliberProject(project_id=PROJECT, name="service principal", owner="@owner")
    run = CaliberWorkflowRun(
        workflow_run_id="run-service-principal",
        workflow_id="service-workflow",
        project_id=PROJECT,
        tenant_id="local",
        status="queued",
    )
    db_session.add_all([project, run])
    db_session.flush()
    expired, workload = _principal(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    with pytest.raises(GrantError, match=SERVICE_PRINCIPAL_EXPIRED):
        create_grant(
            db_session,
            grant_id="grant-service-principal",
            run_id=run.workflow_run_id,
            subject=expired.to_subject(),
            workload=workload,
            project_id=PROJECT,
            allowed_actions=["resource.execute"],
            allowed_resources=["workflow:service-workflow"],
            policy_version="policy-v1",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            service_principal=expired,
        )
    db_session.rollback()


def test_service_principal_grant_binding_and_allowed_sets_are_required(
    db_session: Session,
) -> None:
    project = CaliberProject(project_id=PROJECT, name="service principal", owner="@owner")
    run = CaliberWorkflowRun(
        workflow_run_id="run-service-principal-binding",
        workflow_id="service-workflow",
        project_id=PROJECT,
        tenant_id="local",
        status="queued",
    )
    db_session.add_all([project, run])
    db_session.flush()
    principal, workload = _principal()
    grant_args = {
        "session": db_session,
        "grant_id": "grant-service-principal-binding",
        "run_id": run.workflow_run_id,
        "subject": principal.to_subject(),
        "workload": workload,
        "project_id": PROJECT,
        "allowed_actions": ["resource.execute"],
        "allowed_resources": ["workflow:service-workflow"],
        "policy_version": "policy-v1",
        "expires_at": datetime.now(timezone.utc) + timedelta(minutes=5),
        "service_principal": principal,
    }
    with pytest.raises(GrantError, match="server-created principal binding"):
        create_grant(**{**grant_args, "service_principal": None})
    with pytest.raises(GrantError, match="does not match the service principal"):
        create_grant(
            **{
                **grant_args,
                "subject": replace(principal, principal_id="service_token:other").to_subject(),
            }
        )
    with pytest.raises(GrantError, match="only valid for a service_principal subject"):
        create_grant(
            **{
                **grant_args,
                "subject": Subject("human", "session", PROJECT),
            }
        )
    with pytest.raises(GrantError, match="action outside"):
        create_grant(
            **{
                **grant_args,
                "allowed_actions": ["resource.execute", "context.read"],
            }
        )
    with pytest.raises(GrantError, match="resource outside"):
        create_grant(
            **{
                **grant_args,
                "allowed_resources": ["workflow:service-workflow", "workflow:other"],
            }
        )
    db_session.rollback()


def _enable_reference_admission(client: TestClient) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={
            "profile": "reference",
            "workflow_run_queue_enabled": True,
            "admission_enabled": True,
        }
    )


def _publish_service_and_token(client: TestClient, db_session: Session) -> tuple[str, str]:
    workflow_id, version_id = _project_workflow(client, db_session)
    deploy_prod(client, workflow_id, version_id)
    published = client.post(f"{PREFIX}/workflows/{workflow_id}/service", json={})
    assert published.status_code == 200, published.text
    minted = client.post(
        f"{PREFIX}/workflows/{workflow_id}/service/tokens", json={"name": "scheduler"}
    )
    assert minted.status_code == 201, minted.text
    return workflow_id, minted.json()["data"]["token"]


def test_service_token_run_is_admitted_with_manifest_grant_decision_and_lineage(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, token = _publish_service_and_token(client, db_session)
    seed_workspace_release(db_session, PROJECT, name="service-dev")
    production = seed_workspace_release(
        db_session, PROJECT, name="service-prod", environment_class="production"
    )
    db_session.commit()
    _enable_reference_admission(client)

    response = client.post(
        f"{PREFIX}/services/{workflow_id}/invoke",
        json={"input": {"user_message": "hello"}, "idempotency_key": "sp-1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["data"]["run_id"]

    with client.app.state.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        grant = session.execute(select(CaliberRunGrant)).scalar_one()
        decision = session.execute(select(CaliberPolicyDecision)).scalar_one()
        manifest = session.execute(select(CaliberRunManifest)).scalar_one()
        assert run is not None and run.manifest_legacy is False
        assert run.runtime_lineage_id
        lineage = session.get(CaliberWorkspaceRuntimeLineage, run.runtime_lineage_id)
        assert lineage is not None
        assert lineage.environment_id == production["environment_id"]
        assert run.created_by == grant.subject_user_id
        record = read_manifest(bytes(manifest.manifest_bytes), manifest.manifest_digest)
        assert record["subject"]["auth_method"] == SERVICE_PRINCIPAL_AUTH_METHOD
        assert record["subject"]["user_id"] == grant.subject_user_id
        assert record["purpose"] == "workflow_run:service"
        assert record["grant_id"] == grant.grant_id
        assert (decision.workflow_run_id, decision.grant_id, decision.result) == (
            run_id,
            grant.grant_id,
            "allow",
        )
        assert (
            authorize(
                session,
                run_id=run_id,
                grant_id=grant.grant_id,
                action="resource.execute",
                resource=f"workflow:{workflow_id}",
                scopes=frozenset(),
            ).subject.auth_method
            == SERVICE_PRINCIPAL_AUTH_METHOD
        )


def test_reference_service_token_without_current_lineage_stores_nothing(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, token = _publish_service_and_token(client, db_session)
    seed_workspace_release(db_session, PROJECT, name="service-dev")
    db_session.commit()
    _enable_reference_admission(client)

    response = client.post(
        f"{PREFIX}/services/{workflow_id}/invoke",
        json={"input": {"user_message": "no lineage"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422, response.text
    assert "lineage_required" in response.text
    with client.app.state.session_factory() as session:
        for model in (
            CaliberWorkflowRun,
            CaliberRunGrant,
            CaliberPolicyDecision,
            CaliberRunManifest,
        ):
            assert session.execute(select(func.count()).select_from(model)).scalar_one() == 0


def test_reference_service_token_refuses_ambiguous_deployment_class(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, token = _publish_service_and_token(client, db_session)
    seed_workspace_release(
        db_session, PROJECT, name="service-prod-a", environment_class="production"
    )
    seed_workspace_release(
        db_session, PROJECT, name="service-prod-b", environment_class="production"
    )
    db_session.commit()
    _enable_reference_admission(client)

    response = client.post(
        f"{PREFIX}/services/{workflow_id}/invoke",
        json={"input": {"user_message": "ambiguous"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422, response.text
    assert "lineage_required" in response.text
    with client.app.state.session_factory() as session:
        assert (
            session.execute(select(func.count()).select_from(CaliberWorkflowRun)).scalar_one() == 0
        )


def test_reference_service_token_refuses_ineligible_mapped_release(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, token = _publish_service_and_token(client, db_session)
    coordinates = seed_workspace_release(
        db_session, PROJECT, name="service-revoked", environment_class="production"
    )
    release = db_session.get(CaliberWorkspaceRelease, coordinates["workspace_release_id"])
    assert release is not None
    release.status = "rejected"
    db_session.commit()
    _enable_reference_admission(client)

    response = client.post(
        f"{PREFIX}/services/{workflow_id}/invoke",
        json={"input": {"user_message": "revoked"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 409, response.text
    assert "release_status=rejected" in response.text
    with client.app.state.session_factory() as session:
        assert (
            session.execute(select(func.count()).select_from(CaliberWorkflowRun)).scalar_one() == 0
        )


def test_lineage_resolver_fails_closed_for_invalid_or_broken_mapping(
    client: TestClient, db_session: Session
) -> None:
    assert current_runtime_lineage_coordinates(
        db_session, None, deployment_environment="production"
    ) == (None, None)
    assert current_runtime_lineage_coordinates(
        db_session, PROJECT, deployment_environment=None
    ) == (None, None)
    assert current_runtime_lineage_coordinates(
        db_session, PROJECT, deployment_environment="unknown"
    ) == (None, None)

    workflow_id, _ = _project_workflow(client, db_session)
    coordinates = seed_workspace_release(
        db_session, PROJECT, name="service-broken", environment_class="production"
    )
    environment_id = coordinates["environment_id"]
    from caliber.db.models import CaliberWorkspaceEnvironment

    environment = db_session.get(CaliberWorkspaceEnvironment, environment_id)
    assert environment is not None
    environment.current_release_id = "missing-release"
    db_session.flush()
    assert current_runtime_lineage_coordinates(
        db_session, PROJECT, deployment_environment="production"
    ) == (None, None)
    assert workflow_id


def test_reference_public_service_is_refused_without_a_service_principal(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, _ = _publish_service_and_token(client, db_session)
    client.delete(f"{PREFIX}/workflows/{workflow_id}/service")
    published = client.post(
        f"{PREFIX}/workflows/{workflow_id}/service",
        json={"auth_required": False},
    )
    assert published.status_code == 200, published.text
    _enable_reference_admission(client)

    response = client.post(
        f"{PREFIX}/services/{workflow_id}/invoke",
        json={"input": {"user_message": "public"}},
    )
    assert response.status_code == 403, response.text
    assert "service_principal_required" in response.text
    with client.app.state.session_factory() as session:
        assert (
            session.execute(select(func.count()).select_from(CaliberWorkflowRun)).scalar_one() == 0
        )


def test_scheduler_admits_with_server_owned_principal_and_lineage(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, version_id = _project_workflow(
        client,
        db_session,
        trigger={"mode": "cron", "cron": "* * * * *", "timezone": "UTC", "alias": "prod"},
    )
    deploy_prod(client, workflow_id, version_id)
    seed_workspace_release(db_session, PROJECT, name="cron", environment_class="production")
    db_session.commit()

    scheduler = WorkflowSchedulerTask(
        session_factory=client.app.state.session_factory,
        admission=AdmissionSettings(enabled=True, lineage_required=True),
    )
    assert scheduler._tick_inner(datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)) == 1
    with client.app.state.session_factory() as session:
        run = session.execute(select(CaliberWorkflowRun)).scalar_one()
        grant = session.execute(select(CaliberRunGrant)).scalar_one()
        assert run.manifest_legacy is False and run.runtime_lineage_id
        assert grant.subject_auth_method == SERVICE_PRINCIPAL_AUTH_METHOD
        assert grant.subject_user_id == f"service_principal:cron:{workflow_id}"


def test_scheduler_refuses_unconfigured_project_before_dispatch(client: TestClient) -> None:
    from tests.test_workflow_triggers import _seed_deployed

    with client.app.state.session_factory() as session:
        _seed_deployed(
            session,
            workflow_id="unconfigured-cron",
            trigger={"mode": "cron", "cron": "* * * * *", "timezone": "UTC", "alias": "prod"},
        )
    published: list[dict[str, object]] = []
    scheduler = WorkflowSchedulerTask(
        session_factory=client.app.state.session_factory,
        publish=published.append,
        admission=AdmissionSettings(enabled=True, lineage_required=True),
    )
    assert scheduler._tick_inner(datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)) == 0
    assert published == []
    with client.app.state.session_factory() as session:
        assert (
            session.execute(select(func.count()).select_from(CaliberWorkflowRun)).scalar_one() == 0
        )


def test_scheduler_refuses_missing_lineage_before_dispatch(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, version_id = _project_workflow(
        client,
        db_session,
        trigger={"mode": "cron", "cron": "* * * * *", "timezone": "UTC", "alias": "prod"},
    )
    deploy_prod(client, workflow_id, version_id)
    db_session.commit()
    scheduler = WorkflowSchedulerTask(
        session_factory=client.app.state.session_factory,
        admission=AdmissionSettings(enabled=True, lineage_required=True),
    )
    assert scheduler._tick_inner(datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)) == 0
    with client.app.state.session_factory() as session:
        assert (
            session.execute(select(func.count()).select_from(CaliberWorkflowRun)).scalar_one() == 0
        )
