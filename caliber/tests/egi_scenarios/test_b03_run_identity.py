"""B03 public path: a run executes only while the authority it was admitted under still stands (B03.4, #684).

The reference-profile app on PostgreSQL (the same logic on SQLite in the development profile with admission on). A
project editor starts a run through the public API. Before the worker picks it up the project removes the editor, or
the run's grant is revoked: the worker fails the run closed, by name, builds nothing and executes nothing, and the
manifest, the grant and the earlier decisions are unchanged. A run whose authority stands executes, and its dispatch
decision is recorded.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select

from caliber.db.models import (
    CaliberPolicyDecision,
    CaliberProjectMember,
    CaliberRunGrant,
    CaliberRunManifest,
    CaliberWorkflowRun,
)
from caliber.orchestrator import workflow_run_worker as worker_module
from caliber.orchestrator.workflow_run_worker import WorkflowRunWorker
from caliber.policy import grants
from tests.egi_scenarios.conftest import (
    EDITOR_A,
    PROJECT_A,
    ReferenceApp,
    Workspaces,
    seed_release,
)
from tests.egi_scenarios.test_b02_admission import _call, _published_workflow


def _start(app: ReferenceApp, workflow_id: str, key: str) -> str:
    response = _call(
        app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        "/workflow-runs",
        {
            "workflow_id": workflow_id,
            "alias": "manual",
            "input": "hi",
            "idempotency_key": key,
            **seed_release(app, PROJECT_A),
        },
    )
    assert response.status_code == 202, response.text
    return str(response.json()["data"]["workflow_run_id"])


def _work(app: ReferenceApp) -> None:
    WorkflowRunWorker(
        session_factory=app.session_factory,
        config=app.client.app.state.config,  # type: ignore[attr-defined]
        event_bus=getattr(app.client.app.state, "event_bus", None),
    )._tick()


def _run(app: ReferenceApp, run_id: str) -> tuple[str, str | None, str | None]:
    with app.session_factory() as session:
        run = session.get(CaliberWorkflowRun, run_id)
        assert run is not None
        return run.status, run.error_code, run.error_summary


def _admission_records(app: ReferenceApp) -> list[Any]:
    with app.session_factory() as session:
        return [
            (row.manifest_id, row.manifest_digest, bytes(row.manifest_bytes))
            for row in session.execute(select(CaliberRunManifest)).scalars()
        ] + [
            (row.grant_id, row.expires_at, row.revoked_at is None)
            for row in session.execute(select(CaliberRunGrant)).scalars()
        ]


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    seen: list[Any] = []
    real = worker_module.build_plan

    def capture(*args: Any, **kwargs: Any) -> Any:
        plan = real(*args, **kwargs)
        seen.append(plan)
        return plan

    monkeypatch.setattr(worker_module, "build_plan", capture)
    return seen


def test_a_run_whose_authority_stands_executes_and_records_its_dispatch_decision(
    reference_app: ReferenceApp, two_workspaces: Workspaces, built: list[Any]
) -> None:
    run_id = _start(reference_app, _published_workflow(reference_app), "b03-ok")
    _work(reference_app)
    assert _run(reference_app, run_id)[0] == "completed" and len(built) == 1
    with reference_app.session_factory() as session:
        results = [
            d.result
            for d in session.execute(
                select(CaliberPolicyDecision).where(CaliberPolicyDecision.workflow_run_id == run_id)
            ).scalars()
        ]
    assert results == ["allow", "allow"], "the admission decision and the dispatch decision"


def test_a_removed_member_or_a_revoked_grant_dispatches_nothing(
    reference_app: ReferenceApp, two_workspaces: Workspaces, built: list[Any]
) -> None:
    workflow_id = _published_workflow(reference_app)
    removed = _start(reference_app, workflow_id, "b03-removed")
    revoked = _start(reference_app, workflow_id, "b03-revoked")
    before = _admission_records(reference_app)

    with reference_app.session_factory() as session:
        member = session.execute(
            select(CaliberProjectMember).where(
                CaliberProjectMember.user_id == EDITOR_A,
                CaliberProjectMember.project_id == PROJECT_A,
            )
        ).scalar_one()
        member.status = "inactive"
        session.commit()
    _work(reference_app)
    status, code, summary = _run(reference_app, removed)
    assert (status, code) == ("failed", "authority_denied") and "not_a_member" in str(summary)
    assert built == [], "nothing was built for it"

    with reference_app.session_factory() as session:
        member = session.execute(
            select(CaliberProjectMember).where(CaliberProjectMember.user_id == EDITOR_A)
        ).scalar_one()
        member.status = "active"
        grant = session.execute(
            select(CaliberRunGrant).where(CaliberRunGrant.workflow_run_id == revoked)
        ).scalar_one()
        grants.revoke_grant(
            session, run_id=revoked, grant_id=grant.grant_id, by="@egi", reason="offboarded"
        )
        session.commit()
    _work(reference_app)
    status, code, summary = _run(reference_app, revoked)
    assert (status, code) == ("failed", "authority_denied") and "grant_revoked" in str(summary)
    assert built == [], "nothing was built or executed for either"

    after = [
        r for r in _admission_records(reference_app) if len(r) == 3 and isinstance(r[2], bytes)
    ]
    assert after == [r for r in before if len(r) == 3 and isinstance(r[2], bytes)], (
        "manifests unchanged"
    )
    with reference_app.session_factory() as session:
        assert (
            session.execute(select(func.count()).select_from(CaliberRunManifest)).scalar_one() == 2
        )
        denials = {
            d.workflow_run_id: d.reason_codes
            for d in session.execute(
                select(CaliberPolicyDecision).where(CaliberPolicyDecision.result == "deny")
            ).scalars()
        }
    assert denials == {removed: ["not_a_member"], revoked: ["grant_revoked"]}


def test_an_authority_store_error_fails_the_run_and_does_not_strand_it(
    reference_app: ReferenceApp,
    two_workspaces: Workspaces,
    built: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On PostgreSQL a failing statement aborts the transaction; the worker rolls back and still records the failure."""
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError

    from caliber.workflows import run_identity

    def broken(session: Any, **_kwargs: Any) -> Any:
        if reference_app.dialect == "postgresql":
            session.execute(text("SELECT 1/0"))  # DataError: the transaction is now aborted
        raise OperationalError("select", {}, Exception("the authority store is down"))

    run_id = _start(reference_app, _published_workflow(reference_app), "b03-down")
    monkeypatch.setattr(run_identity, "decide", broken)
    _work(reference_app)
    status, code, _summary = _run(reference_app, run_id)
    assert (status, code) == ("failed", "authority_unavailable")
    assert built == []


def test_the_check_does_not_hold_its_row_locks_through_pre_flight(
    reference_app: ReferenceApp,
    two_workspaces: Workspaces,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While the plan is built the grant and membership rows can be changed by someone else at once (a lock held
    there would time out the change)."""
    from sqlalchemy import text

    run_id = _start(reference_app, _published_workflow(reference_app), "b03-locks")
    outcome: dict[str, Any] = {}
    real = worker_module.build_plan

    def build_while_someone_revokes(*args: Any, **kwargs: Any) -> Any:
        with reference_app.session_factory() as other:
            if reference_app.dialect == "postgresql":
                other.execute(text("SET LOCAL lock_timeout = '500ms'"))
            grant = other.execute(
                select(CaliberRunGrant).where(CaliberRunGrant.workflow_run_id == run_id)
            ).scalar_one()
            grants.revoke_grant(
                other, run_id=run_id, grant_id=grant.grant_id, by="@egi", reason="mid-flight"
            )
            member = other.execute(
                select(CaliberProjectMember).where(CaliberProjectMember.user_id == EDITOR_A)
            ).scalar_one()
            member.role = "viewer"
            other.commit()
            outcome["changed"] = True
        return real(*args, **kwargs)

    monkeypatch.setattr(worker_module, "build_plan", build_while_someone_revokes)
    _work(reference_app)
    assert outcome == {"changed": True}, (
        "the revocation and the demotion were not blocked by the check"
    )
