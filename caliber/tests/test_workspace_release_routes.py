"""P5-F HTTP journeys for the Workspace release lifecycle: create, list, get,
evidence, evaluate, quality-signoff, approve, and break-glass apply.

Every route here is a thin wrapper: the state machine, decision rules, and
break-glass preconditions are already exhaustively tested at the service
layer (test_workspace_release_foundation.py, test_workspace_release_governance.py).
These tests instead pin what only the route layer can get wrong: request
parsing, status codes, authorization boundaries, and exception-to-HTTP-status
mapping.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException
from starlette.testclient import TestClient

from caliber.auth import SCOPE_ADMIN, SCOPE_APPROVER, SCOPE_OPERATOR, SCOPE_VIEWER, CaliberIdentity
from caliber.db.models import (
    CaliberProject,
    CaliberProjectMember,
    CaliberWorkspaceChangeRequest,
    CaliberWorkspaceChangeRequestHead,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceRelease,
    CaliberWorkspaceRevision,
)
from caliber.routes.workspace_releases import _break_glass_apply_sync
from caliber.schemas import WorkspaceBreakGlassApplyRequest
from caliber.workspace_release_governance import record_workspace_release_decision
from caliber.workspace_release_operation_service import (
    OPERATION_APPLIED,
    OPERATION_APPLYING,
    create_workspace_release_operation,
    transition_workspace_release_operation,
)
from caliber.workspace_release_service import RELEASE_APPROVED, create_workspace_release

_ADMIN_SCOPES = frozenset({SCOPE_ADMIN, SCOPE_APPROVER, SCOPE_OPERATOR, SCOPE_VIEWER})

PREFIX = "/ajax-api/2.0/mlflow/caliber"
HEX = "a" * 64
GATE = "b" * 64


def _create_project(client: TestClient, name: str = "P5-F release routes") -> str:
    response = client.post(f"{PREFIX}/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return str(response.json()["data"]["project_id"])


def _environment_id(db_session: Session, project_id: str, name: str) -> str:
    environment = db_session.execute(
        select(CaliberWorkspaceEnvironment).where(
            CaliberWorkspaceEnvironment.project_id == project_id,
            CaliberWorkspaceEnvironment.name == name,
        )
    ).scalar_one()
    return environment.environment_id


def _seed_revision(
    db_session: Session,
    project_id: str,
    revision_id: str,
    *,
    digest: str = HEX,
    actor: str = "@dev",
) -> None:
    db_session.add(
        CaliberWorkspaceRevision(
            revision_id=revision_id,
            project_id=project_id,
            revision_number=1,
            manifest={"apiVersion": "caliber/v1alpha1"},
            manifest_sha256=digest,
            source_bundle_sha256=digest,
            source_attestation="caller_attested",
            revision_sha256=digest,
            status="ready",
            created_by=actor,
        )
    )
    db_session.commit()


def _releases_path(project_id: str) -> str:
    return f"{PREFIX}/projects/{project_id}/releases"


def test_create_get_and_list_a_release_without_a_change_request(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_revision(db_session, project_id, "WSR-f1")
    dev_environment = _environment_id(db_session, project_id, "dev")

    created = client.post(
        _releases_path(project_id),
        json={
            "revision_id": "WSR-f1",
            "environment_id": dev_environment,
            "environment_config_sha256": HEX,
            "runtime_dependencies_sha256": HEX,
            "policy_sha256": HEX,
            "request_idempotency_key": "release-f1",
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()["data"]
    assert body["status"] == "draft"
    assert body["revision_id"] == "WSR-f1"
    release_id = body["release_id"]

    fetched = client.get(f"{_releases_path(project_id)}/{release_id}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json()["data"]["release_id"] == release_id

    listed = client.get(_releases_path(project_id))
    assert listed.status_code == 200, listed.text
    assert [row["release_id"] for row in listed.json()["data"]] == [release_id]

    filtered = client.get(_releases_path(project_id), params={"environment_id": "nonexistent"})
    assert filtered.status_code == 200, filtered.text
    assert filtered.json()["data"] == []


def test_create_replaying_the_same_idempotency_key_and_content_returns_the_same_release(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_revision(db_session, project_id, "WSR-f2")
    dev_environment = _environment_id(db_session, project_id, "dev")
    body = {
        "revision_id": "WSR-f2",
        "environment_id": dev_environment,
        "environment_config_sha256": HEX,
        "runtime_dependencies_sha256": HEX,
        "policy_sha256": HEX,
        "request_idempotency_key": "release-f2",
    }

    first = client.post(_releases_path(project_id), json=body)
    second = client.post(_releases_path(project_id), json=body)
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["data"]["release_id"] == second.json()["data"]["release_id"]


def test_get_release_404s_outside_its_project(client: TestClient, db_session: Session) -> None:
    project_id = _create_project(client)
    other_project_id = _create_project(client, "P5-F other project")
    _seed_revision(db_session, project_id, "WSR-f3")
    dev_environment = _environment_id(db_session, project_id, "dev")
    created = client.post(
        _releases_path(project_id),
        json={
            "revision_id": "WSR-f3",
            "environment_id": dev_environment,
            "environment_config_sha256": HEX,
            "runtime_dependencies_sha256": HEX,
            "policy_sha256": HEX,
            "request_idempotency_key": "release-f3",
        },
    )
    release_id = created.json()["data"]["release_id"]

    response = client.get(f"{_releases_path(other_project_id)}/{release_id}")
    assert response.status_code == 404, response.text


def test_evaluate_starts_the_release_then_creates_an_attempt_and_lists_it(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_revision(db_session, project_id, "WSR-f4")
    dev_environment = _environment_id(db_session, project_id, "dev")
    created = client.post(
        _releases_path(project_id),
        json={
            "revision_id": "WSR-f4",
            "environment_id": dev_environment,
            "environment_config_sha256": HEX,
            "runtime_dependencies_sha256": HEX,
            "policy_sha256": HEX,
            "request_idempotency_key": "release-f4",
        },
    )
    release_id = created.json()["data"]["release_id"]
    assert created.json()["data"]["status"] == "draft"

    evaluated = client.post(
        f"{_releases_path(project_id)}/{release_id}/evaluate",
        json={
            "idempotency_key": "eval-f4",
            "evaluation_plan_sha256": HEX,
            "input_sha256": HEX,
        },
    )
    assert evaluated.status_code == 202, evaluated.text
    evaluation_id = evaluated.json()["data"]["evaluation_id"]
    assert evaluated.json()["data"]["status"] == "queued"

    release_after = client.get(f"{_releases_path(project_id)}/{release_id}")
    assert release_after.json()["data"]["status"] == "evaluating"

    listed = client.get(f"{_releases_path(project_id)}/{release_id}/evaluations")
    assert listed.status_code == 200, listed.text
    assert [row["evaluation_id"] for row in listed.json()["data"]] == [evaluation_id]

    detail = client.get(f"{_releases_path(project_id)}/{release_id}/evaluations/{evaluation_id}")
    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["idempotency_key"] == "eval-f4"

    # Same idempotency key and content: a true replay, not a second attempt --
    # it must not try (and fail) to re-run the already-completed
    # draft->evaluating transition, and must return the *same* evaluation.
    replay = client.post(
        f"{_releases_path(project_id)}/{release_id}/evaluate",
        json={"idempotency_key": "eval-f4", "evaluation_plan_sha256": HEX, "input_sha256": HEX},
    )
    assert replay.status_code == 202, replay.text
    assert replay.json()["data"]["evaluation_id"] == evaluation_id

    # A genuinely new attempt (a different key) while one is still active is
    # refused rather than silently running two evaluations concurrently.
    concurrent = client.post(
        f"{_releases_path(project_id)}/{release_id}/evaluate",
        json={
            "idempotency_key": "eval-second-attempt",
            "evaluation_plan_sha256": HEX,
            "input_sha256": HEX,
        },
    )
    assert concurrent.status_code == 409, concurrent.text


def test_evaluate_404s_for_a_release_in_another_project(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    other_project_id = _create_project(client, "P5-F other project 2")
    _seed_revision(db_session, project_id, "WSR-f5")
    dev_environment = _environment_id(db_session, project_id, "dev")
    created = client.post(
        _releases_path(project_id),
        json={
            "revision_id": "WSR-f5",
            "environment_id": dev_environment,
            "environment_config_sha256": HEX,
            "runtime_dependencies_sha256": HEX,
            "policy_sha256": HEX,
            "request_idempotency_key": "release-f5",
        },
    )
    release_id = created.json()["data"]["release_id"]

    response = client.post(
        f"{_releases_path(other_project_id)}/{release_id}/evaluate",
        json={"idempotency_key": "x", "evaluation_plan_sha256": HEX, "input_sha256": HEX},
    )
    assert response.status_code == 404, response.text


def test_list_evidence_is_empty_for_a_fresh_release(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    _seed_revision(db_session, project_id, "WSR-f6")
    dev_environment = _environment_id(db_session, project_id, "dev")
    created = client.post(
        _releases_path(project_id),
        json={
            "revision_id": "WSR-f6",
            "environment_id": dev_environment,
            "environment_config_sha256": HEX,
            "runtime_dependencies_sha256": HEX,
            "policy_sha256": HEX,
            "request_idempotency_key": "release-f6",
        },
    )
    release_id = created.json()["data"]["release_id"]

    response = client.get(f"{_releases_path(project_id)}/{release_id}/evidence")
    assert response.status_code == 200, response.text
    assert response.json()["data"] == []


# --- decisions: quality-signoff / approve -----------------------------------


def _seed_governed_release(
    db_session: Session,
    *,
    project_id: str,
    environment_name: str,
    revision_id: str,
    key: str,
) -> tuple[CaliberWorkspaceRelease, str]:
    """A release bound to an accepted Change Request, ready for a decision.

    Mirrors test_workspace_release_governance.py's own ``_seed``/``_release``
    fixtures -- three distinct actors (developer/QA/owner) matter here because
    ``_require_distinct_actor`` refuses a decision maker who originated the
    change.
    """
    environment_id = _environment_id(db_session, project_id, environment_name)
    digest = hashlib.sha256(key.encode()).hexdigest()
    db_session.add(
        CaliberWorkspaceRevision(
            revision_id=revision_id,
            project_id=project_id,
            revision_number=1,
            manifest={"apiVersion": "caliber/v1alpha1"},
            manifest_sha256=digest,
            source_bundle_sha256=digest,
            source_attestation="caller_attested",
            revision_sha256=digest,
            status="ready",
            created_by="@dev",
        )
    )
    cr_id = f"WCR-{key}"
    head_id = f"WCH-{key}"
    db_session.add(
        CaliberWorkspaceChangeRequest(
            change_request_id=cr_id,
            project_id=project_id,
            current_head_revision_id=revision_id,
            created_by="@dev",
            title="P5-F candidate",
            status="accepted",
            review_backend="caliber",
        )
    )
    db_session.add(
        CaliberWorkspaceChangeRequestHead(
            head_id=head_id,
            change_request_id=cr_id,
            generation=1,
            revision_id=revision_id,
            revision_sha256=digest,
            review_policy_version="v1",
            review_policy_sha256=digest,
            changed_by="@dev",
        )
    )
    db_session.flush()
    release = create_workspace_release(
        db_session,
        project_id=project_id,
        revision_id=revision_id,
        environment_id=environment_id,
        environment_config_sha256=digest,
        runtime_dependencies_sha256=digest,
        policy_sha256=digest,
        request_idempotency_key=key,
        requested_by="@dev",
        change_request_id=cr_id,
        change_request_head_id=head_id,
    )
    release.status = "awaiting_quality_signoff"
    release.evaluation_evidence_sha256 = GATE
    db_session.commit()
    return release, head_id


def test_quality_signoff_go_advances_a_qa_release_to_approved(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    db_session.add(
        CaliberProjectMember(
            member_id="PRJM-qa",
            project_id=project_id,
            user_id="@qa",
            role="reviewer",
            status="active",
            created_by="@test",
        )
    )
    db_session.commit()
    client.app.state.config = client.app.state.config.model_copy(
        update={"admin_users": "@test,@qa"}
    )
    release, head_id = _seed_governed_release(
        db_session, project_id=project_id, environment_name="qa", revision_id="WSR-f7", key="f7"
    )

    response = client.post(
        f"{_releases_path(project_id)}/{release.release_id}/quality-signoff",
        headers={"X-CALIBER-User": "@qa"},
        json={
            "decision": "go",
            "rationale": "looks good",
            "gate_evidence_sha256": GATE,
            "change_request_head_id": head_id,
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["data"]["decision"] == "go"
    assert response.json()["data"]["decided_by"] == "@qa"

    db_session.refresh(release)
    assert release.status == RELEASE_APPROVED


def test_quality_signoff_by_the_developer_who_requested_it_is_refused(
    client: TestClient, db_session: Session
) -> None:
    """``_require_distinct_actor``, exercised through the route."""
    project_id = _create_project(client)
    client.app.state.config = client.app.state.config.model_copy(
        update={"admin_users": "@test,@dev"}
    )
    release, head_id = _seed_governed_release(
        db_session, project_id=project_id, environment_name="qa", revision_id="WSR-f8", key="f8"
    )

    response = client.post(
        f"{_releases_path(project_id)}/{release.release_id}/quality-signoff",
        headers={"X-CALIBER-User": "@dev"},
        json={"decision": "go", "gate_evidence_sha256": GATE, "change_request_head_id": head_id},
    )
    assert response.status_code == 403, response.text


def test_approve_requires_a_fresh_quality_signoff_first(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    release, head_id = _seed_governed_release(
        db_session,
        project_id=project_id,
        environment_name="prod",
        revision_id="WSR-f9",
        key="f9",
    )
    release.status = "awaiting_approval"
    db_session.commit()

    response = client.post(
        f"{_releases_path(project_id)}/{release.release_id}/approve",
        json={"decision": "go", "gate_evidence_sha256": GATE},
    )
    assert response.status_code == 409, response.text
    assert "quality signoff" in response.json()["detail"]


def test_full_production_decision_chain_uses_distinct_qa_and_owner_actors(
    client: TestClient, db_session: Session
) -> None:
    project_id = _create_project(client)
    db_session.add(
        CaliberProjectMember(
            member_id="PRJM-qa2",
            project_id=project_id,
            user_id="@qa",
            role="reviewer",
            status="active",
            created_by="@test",
        )
    )
    db_session.commit()
    client.app.state.config = client.app.state.config.model_copy(
        update={"admin_users": "@test,@qa"}
    )
    release, head_id = _seed_governed_release(
        db_session,
        project_id=project_id,
        environment_name="prod",
        revision_id="WSR-f10",
        key="f10",
    )

    quality = client.post(
        f"{_releases_path(project_id)}/{release.release_id}/quality-signoff",
        headers={"X-CALIBER-User": "@qa"},
        json={"decision": "go", "gate_evidence_sha256": GATE, "change_request_head_id": head_id},
    )
    assert quality.status_code == 201, quality.text
    db_session.refresh(release)
    assert release.status == "awaiting_approval"

    approved = client.post(
        f"{_releases_path(project_id)}/{release.release_id}/approve",
        json={"decision": "go", "gate_evidence_sha256": GATE},
    )
    assert approved.status_code == 201, approved.text
    assert approved.json()["data"]["decided_by"] == "@test"
    db_session.refresh(release)
    assert release.status == RELEASE_APPROVED


# --- break-glass apply -------------------------------------------------------
#
# `create_workspace_break_glass_apply` requires a genuine "session"-kind
# credential (`identity.credential_kind == "session"`, set only by a real
# cookie-based login -- never by this test suite's default trusted-header
# auth). test_workspace_release_operation_routes.py hit the identical
# constraint and resolved it the same way: construct the break-glass call
# directly with a session-credentialed identity to prove this module's own
# wiring, and use a genuine HTTP request only to prove the credential-kind
# gate itself holds for an ordinary API caller.


def test_break_glass_apply_over_http_is_refused_without_a_real_session_credential(
    client: TestClient, db_session: Session
) -> None:
    """An ordinary (trusted-header) API caller can never break-glass-apply,
    session or no -- this is the actual security boundary a real attacker
    would hit, and it is only reachable by an HTTP-level test."""
    project_id = _create_project(client)
    release, _head_id = _seed_governed_release(
        db_session,
        project_id=project_id,
        environment_name="prod",
        revision_id="WSR-f11",
        key="f11",
    )
    environment = db_session.get(CaliberWorkspaceEnvironment, release.environment_id)
    assert environment is not None
    environment.recovery_policy_enabled = True
    db_session.commit()

    response = client.post(
        f"{_releases_path(project_id)}/{release.release_id}/break-glass-apply",
        json={
            "reason": "prod incident",
            "incident_ref": "INC-1",
            "authorization_ref": "AUTH-1",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
            "gate_evidence_sha256": GATE,
            "expected_current_release_id": release.release_id,
            "expected_environment_lock_version": 1,
            "idempotency_key": "break-glass-f11",
        },
    )
    assert response.status_code == 409, response.text
    assert "session" in response.json()["detail"]


def _seed_verified_qa_staging_prod_chain(
    db_session: Session, *, project_id: str, key: str
) -> CaliberWorkspaceRelease:
    """Build a full QA(applied)->staging(applied)->prod(awaiting quality
    signoff) chain eligible for break-glass apply, and return the prod
    release. Shared by every break-glass-apply route test below -- the
    break-glass preconditions require the *entire* chain, not just the
    terminal release, so each test needs this same fixture setup rather than
    duplicating it per test.
    """
    db_session.add(CaliberProject(project_id=project_id, name="P5-F break-glass", owner="@test"))
    db_session.add(
        CaliberProjectMember(
            member_id=f"PRJM-{key}-qa",
            project_id=project_id,
            user_id="@qa",
            role="reviewer",
            status="active",
            created_by="@test",
        )
    )
    db_session.flush()
    for name, environment_class, order in (
        ("dev", "development", 10),
        ("qa", "qa", 20),
        ("staging", "staging", 30),
        ("prod", "production", 40),
    ):
        db_session.add(
            CaliberWorkspaceEnvironment(
                environment_id=f"WSE-{key}-{name}",
                project_id=project_id,
                name=name,
                environment_class=environment_class,
                promotion_order=order,
                status="active",
                recovery_policy_enabled=(name == "prod"),
                created_by="@test",
            )
        )
    db_session.flush()

    digest = "d" * 64
    db_session.add(
        CaliberWorkspaceRevision(
            revision_id=f"WSR-{key}",
            project_id=project_id,
            revision_number=1,
            manifest={"apiVersion": "caliber/v1alpha1"},
            manifest_sha256=digest,
            source_bundle_sha256=digest,
            source_attestation="caller_attested",
            revision_sha256=digest,
            status="ready",
            created_by="@dev",
        )
    )
    db_session.add(
        CaliberWorkspaceChangeRequest(
            change_request_id=f"WCR-{key}",
            project_id=project_id,
            current_head_revision_id=f"WSR-{key}",
            created_by="@dev",
            title="P5-F break-glass candidate",
            status="accepted",
            review_backend="caliber",
        )
    )
    db_session.add(
        CaliberWorkspaceChangeRequestHead(
            head_id=f"WCH-{key}",
            change_request_id=f"WCR-{key}",
            generation=1,
            revision_id=f"WSR-{key}",
            revision_sha256=digest,
            review_policy_version="v1",
            review_policy_sha256=digest,
            changed_by="@dev",
        )
    )
    db_session.flush()

    qa_release = create_workspace_release(
        db_session,
        project_id=project_id,
        revision_id=f"WSR-{key}",
        environment_id=f"WSE-{key}-qa",
        environment_config_sha256=digest,
        runtime_dependencies_sha256=digest,
        policy_sha256=digest,
        request_idempotency_key=f"{key}-qa",
        requested_by="@dev",
        change_request_id=f"WCR-{key}",
        change_request_head_id=f"WCH-{key}",
    )
    qa_release.status = "awaiting_quality_signoff"
    qa_release.evaluation_evidence_sha256 = GATE
    db_session.flush()
    record_workspace_release_decision(
        db_session,
        project_id=project_id,
        workspace_release_id=qa_release.release_id,
        identity=CaliberIdentity(user_id="@qa", scopes=_ADMIN_SCOPES),
        kind="quality",
        decision="go",
        gate_evidence_sha256=GATE,
        change_request_head_id=f"WCH-{key}",
    )
    qa_apply = create_workspace_release_operation(
        db_session,
        project_id=project_id,
        workspace_release_id=qa_release.release_id,
        environment_id=f"WSE-{key}-qa",
        kind="apply",
        idempotency_key=f"{key}-qa-apply",
        expected_environment_lock_version=1,
        requested_by="@test",
    )
    transition_workspace_release_operation(
        db_session, qa_apply, OPERATION_APPLYING, actor="@test", expected_lock_version=1
    )
    transition_workspace_release_operation(
        db_session, qa_apply, OPERATION_APPLIED, actor="@test", expected_lock_version=2
    )

    staging_release = create_workspace_release(
        db_session,
        project_id=project_id,
        revision_id=f"WSR-{key}",
        environment_id=f"WSE-{key}-staging",
        environment_config_sha256=digest,
        runtime_dependencies_sha256=digest,
        policy_sha256=digest,
        request_idempotency_key=f"{key}-staging",
        requested_by="@dev",
        change_request_id=f"WCR-{key}",
        change_request_head_id=f"WCH-{key}",
        predecessor_release_id=qa_release.release_id,
    )
    staging_release.status = RELEASE_APPROVED
    db_session.flush()
    staging_apply = create_workspace_release_operation(
        db_session,
        project_id=project_id,
        workspace_release_id=staging_release.release_id,
        environment_id=f"WSE-{key}-staging",
        kind="apply",
        idempotency_key=f"{key}-staging-apply",
        expected_environment_lock_version=1,
        requested_by="@test",
    )
    transition_workspace_release_operation(
        db_session, staging_apply, OPERATION_APPLYING, actor="@test", expected_lock_version=1
    )
    transition_workspace_release_operation(
        db_session, staging_apply, OPERATION_APPLIED, actor="@test", expected_lock_version=2
    )

    prod_release = create_workspace_release(
        db_session,
        project_id=project_id,
        revision_id=f"WSR-{key}",
        environment_id=f"WSE-{key}-prod",
        environment_config_sha256=digest,
        runtime_dependencies_sha256=digest,
        policy_sha256=digest,
        request_idempotency_key=f"{key}-prod",
        requested_by="@dev",
        change_request_id=f"WCR-{key}",
        change_request_head_id=f"WCH-{key}",
        predecessor_release_id=staging_release.release_id,
    )
    prod_release.status = "awaiting_quality_signoff"
    prod_release.evaluation_evidence_sha256 = GATE
    db_session.commit()
    return prod_release


def test_break_glass_apply_succeeds_through_a_verified_qa_staging_prod_chain(
    db_session: Session, session_factory: sessionmaker[Session]
) -> None:
    """Calls this module's own `_break_glass_apply_sync` directly with a
    session-credentialed identity -- see the module comment above for why
    this isn't a genuine HTTP call. The QA->staging->prod chain itself is
    exhaustively covered by test_workspace_release_governance.py; this proves
    both that this route module maps a valid request into that call
    correctly, shaping the response as `{authorization_id, operation_id}`,
    and -- since the route no longer hardcodes `machine_gates_passed`/
    `integrity_checks_passed` -- that a genuinely passed release still
    derives `True`/`True` live and the apply still succeeds exactly as
    before (the no-regression case for the P5-F gap closure).
    """
    project_id = "PRJ-f12"
    prod_release = _seed_verified_qa_staging_prod_chain(
        db_session, project_id=project_id, key="f12"
    )

    payload = WorkspaceBreakGlassApplyRequest(
        reason="prod incident",
        incident_ref="INC-2",
        authorization_ref="AUTH-2",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        gate_evidence_sha256=GATE,
        expected_current_release_id=prod_release.release_id,
        expected_environment_lock_version=1,
        idempotency_key="f12-break-glass",
    )
    data = _break_glass_apply_sync(
        session_factory,
        project_id=project_id,
        release_id=prod_release.release_id,
        identity=CaliberIdentity(
            user_id="@test",
            scopes=_ADMIN_SCOPES,
            credential_kind="session",
            credential_id="session-f12",
        ),
        payload=payload,
    )

    assert data["authorization_id"]
    assert data["operation_id"]


def test_break_glass_apply_denies_when_persisted_evidence_has_gone_stale_since_earlier_checks(
    db_session: Session, session_factory: sessionmaker[Session]
) -> None:
    """Proves the actual production wiring, not just the governance-layer
    helper in isolation: build the identical verified QA->staging->prod
    chain the success test above uses, then mutate the prod release's own
    persisted evidence digest right before calling `_break_glass_apply_sync`
    -- simulating evidence that went stale between an earlier read and the
    apply itself. The route must re-derive live from the release row at call
    time and deny, proving this is a real check and not a relocated
    constant (the old hardcoded `True, True` would have let this through).
    """
    project_id = "PRJ-f13"
    prod_release = _seed_verified_qa_staging_prod_chain(
        db_session, project_id=project_id, key="f13"
    )
    prod_release.evaluation_evidence_sha256 = (
        "e" * 64
    )  # evidence moved on after the chain was built
    db_session.commit()

    payload = WorkspaceBreakGlassApplyRequest(
        reason="prod incident",
        incident_ref="INC-3",
        authorization_ref="AUTH-3",
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        gate_evidence_sha256=GATE,  # the caller's earlier-captured (now-stale) digest
        expected_current_release_id=prod_release.release_id,
        expected_environment_lock_version=1,
        idempotency_key="f13-break-glass",
    )

    with pytest.raises(HTTPException) as exc_info:
        _break_glass_apply_sync(
            session_factory,
            project_id=project_id,
            release_id=prod_release.release_id,
            identity=CaliberIdentity(
                user_id="@test",
                scopes=_ADMIN_SCOPES,
                credential_kind="session",
                credential_id="session-f13",
            ),
            payload=payload,
        )
    assert exc_info.value.status_code == 409
    assert "machine and integrity gates must pass" in str(exc_info.value.detail)
