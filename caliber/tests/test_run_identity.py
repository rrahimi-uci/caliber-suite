"""A run acts as the subject and workload its manifest recorded, under the exact grant it names, and only while that
authority still stands (B03.4, #684)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberPolicyDecision,
    CaliberProjectMember,
    CaliberRunGrant,
    CaliberRunManifest,
    CaliberWorkflowRun,
)
from caliber.policy import grants
from caliber.policy.principal import Subject, Workload
from caliber.workflows import run_identity
from caliber.workflows.run_identity import (
    IdentityDenied,
    RunIdentity,
    identity_for_run,
)
from caliber.workflows.run_pins import load_run_manifest
from tests.test_run_pins import PROJECT, _run, _setup, _submit, _worker


def _admitted(client: TestClient, db: Session, key: str = "k") -> tuple[str, CaliberWorkflowRun]:
    workflow_id, _ = _setup(client, db)
    run_id = _submit(client, workflow_id, key)
    return workflow_id, _run(db, run_id)


def _denied(db: Session, run: CaliberWorkflowRun, **kwargs: Any) -> IdentityDenied:
    with pytest.raises(IdentityDenied) as caught:
        identity_for_run(db, run, **kwargs)
    return caught.value


def _state(db: Session, run_id: str) -> tuple[Any, ...]:
    """Everything admission wrote that a refusal must leave alone."""
    db.expire_all()
    manifest = db.execute(
        select(CaliberRunManifest).where(CaliberRunManifest.workflow_run_id == run_id)
    ).scalar_one()
    grant = db.execute(
        select(CaliberRunGrant).where(CaliberRunGrant.workflow_run_id == run_id)
    ).scalar_one()
    return (
        manifest.manifest_bytes,
        manifest.manifest_digest,
        grant.grant_id,
        grant.expires_at,
        grant.allowed_actions,
        grant.allowed_resources,
    )


def test_worker_identity_from_exact_grant(client: TestClient, db_session: Session) -> None:
    _, run = _admitted(client, db_session)
    record = load_run_manifest(db_session, run.workflow_run_id).record  # type: ignore[union-attr]
    identity = identity_for_run(db_session, run)
    assert isinstance(identity, RunIdentity)
    assert identity.grant_id == record["grant_id"], "the grant the manifest names, not the newest"
    assert identity.subject == Subject.from_manifest(record["subject"], project_id=PROJECT)
    assert identity.workload == Workload.from_manifest(record["workload"])
    assert identity.purpose == record["purpose"] == "workflow_run:api"
    assert identity.decision.allowed and identity.decision.persisted
    stored = db_session.get(CaliberPolicyDecision, identity.decision.decision_id)
    assert stored is not None and stored.result == "allow" and stored.grant_id == identity.grant_id


def test_legacy_profile_behavior(client: TestClient, db_session: Session) -> None:
    workflow_id, _ = _setup(client, db_session, admission=False)
    run = _run(db_session, _submit(client, workflow_id))
    assert run.manifest_legacy is True
    before = db_session.execute(select(func.count()).select_from(CaliberRunGrant)).scalar_one()
    assert identity_for_run(db_session, run) is None, "no identity, and none is manufactured"
    assert (
        db_session.execute(select(func.count()).select_from(CaliberRunGrant)).scalar_one()
        == before
        == 0
    )
    _worker(client)._tick()
    assert _run(db_session, run.workflow_run_id).status == "completed"


def test_foreign_grant_denied(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manifest that names another run's grant, another subject or another workload is refused by name."""
    workflow_id, first = _admitted(client, db_session, "k1")
    second = _run(db_session, _submit(client, workflow_id, "k2"))
    foreign = load_run_manifest(db_session, second.workflow_run_id).record["grant_id"]  # type: ignore[union-attr]
    real = run_identity.load_run_manifest

    def tampered(**changes: Any) -> Any:
        def load(session: Session, run_id: str) -> Any:
            loaded = real(session, run_id)
            return type(loaded)(
                manifest_id=loaded.manifest_id,
                digest=loaded.digest,
                record={**loaded.record, **changes},
                pins=loaded.pins,
            )

        return load

    with monkeypatch.context() as patch:
        patch.setattr(run_identity, "load_run_manifest", tampered(grant_id=foreign))
        assert _denied(db_session, first).reasons == ("grant_not_found",), "another run's grant"
    with monkeypatch.context() as patch:
        patch.setattr(
            run_identity,
            "load_run_manifest",
            tampered(subject={"user_id": "@mallory", "auth_method": "session"}),
        )
        assert _denied(db_session, first).reasons == ("subject_mismatch",)
    with monkeypatch.context() as patch:
        other = Workload.for_workflow("Other", "f" * 64).to_manifest()
        patch.setattr(run_identity, "load_run_manifest", tampered(workload=other))
        assert _denied(db_session, first).reasons == ("workload_mismatch",)
    assert identity_for_run(db_session, first) is not None, "the real manifest still stands"


def test_the_manifest_must_name_a_grant_and_valid_principals(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, run = _admitted(client, db_session)
    real = run_identity.load_run_manifest

    def without(*drop: str, **replace: Any) -> Any:
        def load(session: Session, run_id: str) -> Any:
            loaded = real(session, run_id)
            record = {k: v for k, v in loaded.record.items() if k not in drop}
            return type(loaded)(
                manifest_id=loaded.manifest_id,
                digest=loaded.digest,
                record={**record, **replace},
                pins=loaded.pins,
            )

        return load

    with monkeypatch.context() as patch:
        patch.setattr(run_identity, "load_run_manifest", without("grant_id"))
        assert _denied(db_session, run).code == "grant_unbound"
    with monkeypatch.context() as patch:
        patch.setattr(
            run_identity, "load_run_manifest", without(subject={"user_id": "x", "extra": 1})
        )
        assert _denied(db_session, run).code == "identity_invalid"
    with monkeypatch.context() as patch:
        patch.setattr(run_identity, "load_run_manifest", without("subject"))
        assert _denied(db_session, run).code == "identity_invalid"
    with monkeypatch.context() as patch:
        patch.setattr(run_identity, "load_run_manifest", lambda *_a: None)
        assert _denied(db_session, run).code == "manifest_missing"
    with monkeypatch.context() as patch:
        from caliber.workflows.run_pins import PinsError

        def corrupt(*_a: Any) -> Any:
            raise PinsError("manifest_corrupt", "the bytes changed")

        patch.setattr(run_identity, "load_run_manifest", corrupt)
        assert _denied(db_session, run).code == "manifest_corrupt"


def test_revoked_resume_denied(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose grant was revoked while it waited is failed at dispatch, with nothing built or executed, and the
    admission records are untouched."""
    from caliber.orchestrator import workflow_run_worker as worker_module

    built: list[Any] = []
    monkeypatch.setattr(worker_module, "build_plan", lambda *a, **k: built.append(1))
    _, run = _admitted(client, db_session)
    before = _state(db_session, run.workflow_run_id)
    grant = db_session.execute(select(CaliberRunGrant)).scalar_one()
    grants.revoke_grant(
        db_session,
        run_id=run.workflow_run_id,
        grant_id=grant.grant_id,
        by="@owner",
        reason="offboarded",
    )
    db_session.commit()
    _worker(client)._tick()
    failed = _run(db_session, run.workflow_run_id)
    assert (failed.status, failed.error_code) == ("failed", "authority_denied")
    assert "grant_revoked" in (failed.error_summary or "")
    assert built == [], "zero dispatch"
    assert _state(db_session, run.workflow_run_id) == before, "no admission record was changed"
    decisions = (
        db_session.execute(
            select(CaliberPolicyDecision).order_by(CaliberPolicyDecision.evaluated_at)
        )
        .scalars()
        .all()
    )
    assert [d.result for d in decisions] == ["allow", "deny"] and decisions[-1].reason_codes == [
        "grant_revoked"
    ]


def test_a_removed_member_and_an_expired_grant_dispatch_nothing(
    client: TestClient, db_session: Session
) -> None:
    _, run = _admitted(client, db_session, "k-member")
    member = db_session.execute(
        select(CaliberProjectMember).where(CaliberProjectMember.user_id == "@test")
    ).scalar_one()
    member.status = "inactive"
    db_session.commit()
    # the project owner is "@test" too; take ownership away so only membership could grant the role
    from caliber.db.models import CaliberProject

    project = db_session.get(CaliberProject, PROJECT)
    assert project is not None
    project.owner = "@someone-else"
    db_session.commit()
    assert _denied(db_session, run).reasons == ("not_a_member",)
    db_session.rollback()

    member.status = "active"
    project.owner = "@test"
    db_session.commit()
    assert identity_for_run(db_session, run) is not None
    expired = _denied(db_session, run, now=datetime.now(timezone.utc) + timedelta(days=30))
    assert expired.code == "authority_denied" and expired.reasons == ("grant_expired",)


def test_an_unavailable_authority_is_a_refusal_never_an_allow(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, run = _admitted(client, db_session)

    def down(*_a: Any, **_k: Any) -> Any:
        raise OperationalError("select", {}, Exception("database is down"))

    with monkeypatch.context() as patch:
        patch.setattr(run_identity, "decide", down)
        assert _denied(db_session, run).code == "authority_unavailable"
    with monkeypatch.context() as patch:
        patch.setattr(run_identity, "load_run_manifest", down)
        assert _denied(db_session, run).code == "authority_unavailable"
    with monkeypatch.context() as patch:
        from caliber.policy import decisions

        real = decisions.decide

        def unrecorded(*a: Any, **k: Any) -> Any:
            decision = real(*a, **k)
            return type(decision)(
                **{
                    **decision.__dict__,
                    "result": "deny",
                    "reason_codes": ("authority_unavailable",),
                    "persisted": False,
                }
            )

        patch.setattr(run_identity, "decide", unrecorded)
        assert _denied(db_session, run).code == "authority_unavailable"
    with monkeypatch.context() as patch:
        from caliber.policy.decisions import DecisionInputError

        def malformed(*_a: Any, **_k: Any) -> Any:
            raise DecisionInputError("blank")

        patch.setattr(run_identity, "decide", malformed)
        assert _denied(db_session, run).code == "identity_invalid"


def test_no_manifest_mutation(client: TestClient, db_session: Session) -> None:
    _, run = _admitted(client, db_session)
    before = _state(db_session, run.workflow_run_id)
    for _ in range(3):
        identity_for_run(db_session, run)
    _denied(db_session, run, now=datetime.now(timezone.utc) + timedelta(days=30))
    db_session.commit()
    assert _state(db_session, run.workflow_run_id) == before
    assert (
        db_session.execute(select(func.count()).select_from(CaliberRunManifest)).scalar_one() == 1
    )
    assert db_session.execute(select(func.count()).select_from(CaliberRunGrant)).scalar_one() == 1


def test_an_admitted_run_executes_when_its_authority_stands(
    client: TestClient, db_session: Session
) -> None:
    _, run = _admitted(client, db_session)
    _worker(client)._tick()
    done = _run(db_session, run.workflow_run_id)
    assert done.status == "completed", (done.error_code, done.error_summary)


def test_a_grant_that_expired_while_the_run_waited_fails_the_run_at_dispatch(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from caliber.orchestrator import workflow_run_worker as worker_module

    _, run = _admitted(client, db_session)
    real = worker_module.identity_for_run
    later = datetime.now(timezone.utc) + timedelta(days=30)
    monkeypatch.setattr(
        worker_module,
        "identity_for_run",
        lambda session, row, *, policy: real(session, row, policy=policy, now=later),
    )
    _worker(client)._tick()
    failed = _run(db_session, run.workflow_run_id)
    assert (failed.status, failed.error_code) == ("failed", "authority_denied")
    assert "grant_expired" in (failed.error_summary or "")


def test_a_run_resumed_after_its_grant_was_revoked_is_failed_not_continued(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The run is parked waiting for approval; the approval is given and the run resumed through the public routes;
    the worker then refuses to continue it because the grant it was admitted under is gone."""
    from caliber.orchestrator import workflow_run_worker as worker_module
    from tests.test_routes_workflow_runs_async import (
        PREFIX as ASYNC_PREFIX,
    )
    from tests.test_routes_workflow_runs_async import (
        _enable_runtime_approvals,
        _seed_waiting_approval,
    )

    built: list[Any] = []
    monkeypatch.setattr(worker_module, "build_plan", lambda *a, **k: built.append(1))
    _, run = _admitted(client, db_session)
    _enable_runtime_approvals(client)
    _seed_waiting_approval(client, run.workflow_run_id, checkpoint_input_by_port={"input": "hi"})
    approved = client.post(
        f"{ASYNC_PREFIX}/workflow-runs/{run.workflow_run_id}/approval/approve",
        json={"reason": "ok"},
    )
    assert approved.status_code == 200, approved.text
    grant = db_session.execute(select(CaliberRunGrant)).scalar_one()
    grants.revoke_grant(
        db_session,
        run_id=run.workflow_run_id,
        grant_id=grant.grant_id,
        by="@owner",
        reason="left the project",
    )
    db_session.commit()
    resumed = client.post(f"{ASYNC_PREFIX}/workflow-runs/{run.workflow_run_id}/resume")
    assert resumed.status_code == 202, resumed.text
    _worker(client)._tick()
    failed = _run(db_session, run.workflow_run_id)
    assert (failed.status, failed.error_code) == ("failed", "authority_denied")
    assert "grant_revoked" in (failed.error_summary or "")
    assert built == [], "no node ran after the resume"
