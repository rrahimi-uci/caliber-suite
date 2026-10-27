"""B02 public path: a run is admitted in one transaction, or not at all (B02.2a, #680).

The reference-profile app on PostgreSQL (SQLite runs the same logic in the development profile with admission
switched on). A project editor creates a workflow, publishes it and starts a run through the public API. The run, its
grant, the policy decision and the immutable manifest exist together or not at all; a retry returns the original; a
second project cannot see or reuse it; a refusal (a rule bundle that is missing) leaves no trace; and two requests
racing on one idempotency key admit exactly one run.
"""

from __future__ import annotations

import threading
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberPolicyDecision,
    CaliberRunGrant,
    CaliberRunManifest,
    CaliberWorkflowRun,
    CaliberWorkflowRunEvent,
)
from caliber.run_manifest import read_manifest
from tests.egi_scenarios.conftest import (
    EDITOR_A,
    EDITOR_B,
    PROJECT_A,
    ReferenceApp,
    Workspaces,
    seed_release,
)
from tests.workflow_helpers import make_manifest

PREFIX = "/ajax-api/2.0/mlflow/caliber"


def _call(
    app: ReferenceApp, method: str, user: str, project: str, path: str, body: Any = None
) -> Any:
    headers = app.headers(user, project)
    token = app.client.get(f"{PREFIX}/csrf", headers=headers).json()["data"]["token"]
    return app.client.request(
        method, f"{PREFIX}{path}", json=body, headers={**headers, "X-CALIBER-CSRF": token}
    )


def _counts(session: Session) -> dict[str, int]:
    return {
        model.__tablename__: session.execute(select(func.count()).select_from(model)).scalar_one()
        for model in (
            CaliberWorkflowRun,
            CaliberRunGrant,
            CaliberPolicyDecision,
            CaliberRunManifest,
            CaliberWorkflowRunEvent,
        )
    }


def _published_workflow(app: ReferenceApp) -> str:
    created = _call(app, "POST", EDITOR_A, PROJECT_A, "/workflows", {"name": "EGI admission"})
    assert created.status_code == 201, created.text
    workflow_id = created.json()["data"]["workflow_id"]
    draft = _call(
        app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        f"/workflows/{workflow_id}/versions",
        {"manifest": make_manifest(workflow_id)},
    )
    assert draft.status_code == 201, draft.text
    version_id = draft.json()["data"]["version_id"]
    published = _call(app, "POST", EDITOR_A, PROJECT_A, f"/workflow-versions/{version_id}/publish")
    assert published.status_code == 200, published.text
    app.client.app.state.config = app.client.app.state.config.model_copy(  # type: ignore[attr-defined]
        update={"workflow_run_queue_enabled": True, "admission_enabled": True}
    )
    return workflow_id


def test_a_run_is_admitted_whole_or_not_at_all(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    workflow_id = _published_workflow(reference_app)
    body = {
        "workflow_id": workflow_id,
        "alias": "manual",
        "input": "refund?",
        "idempotency_key": "egi-1",
        **seed_release(reference_app, PROJECT_A),
    }

    admitted = _call(reference_app, "POST", EDITOR_A, PROJECT_A, "/workflow-runs", body)
    assert admitted.status_code == 202, admitted.text
    run_id = admitted.json()["data"]["workflow_run_id"]
    with reference_app.session_factory() as session:
        counts = _counts(session)
        assert (
            counts[CaliberRunManifest.__tablename__] == 1 == counts[CaliberRunGrant.__tablename__]
        )
        assert counts[CaliberPolicyDecision.__tablename__] == 1
        manifest = session.execute(select(CaliberRunManifest)).scalar_one()
        grant = session.execute(select(CaliberRunGrant)).scalar_one()
        decision = session.execute(select(CaliberPolicyDecision)).scalar_one()
        run = session.get(CaliberWorkflowRun, run_id)
        record = read_manifest(bytes(manifest.manifest_bytes), manifest.manifest_digest)
        assert run is not None and run.manifest_legacy is False
        assert record["subject"]["user_id"] == EDITOR_A and record["project_id"] == PROJECT_A
        assert (grant.workflow_run_id, decision.workflow_run_id) == (run_id, run_id)
        assert (decision.grant_id, decision.result) == (grant.grant_id, "allow")

    # The same request again returns the original run; a changed one is a conflict; nothing new is stored.
    again = _call(reference_app, "POST", EDITOR_A, PROJECT_A, "/workflow-runs", body)
    assert again.status_code == 202 and again.json()["data"]["workflow_run_id"] == run_id
    changed = _call(
        reference_app, "POST", EDITOR_A, PROJECT_A, "/workflow-runs", {**body, "input": "other"}
    )
    assert changed.status_code == 409, changed.text
    with reference_app.session_factory() as session:
        assert _counts(session) == counts

    # Another project's editor cannot start (or reuse the key of) this workflow's runs.
    foreign = _call(reference_app, "POST", EDITOR_B, "egi-project-b", "/workflow-runs", body)
    assert foreign.status_code in (403, 404), foreign.text
    with reference_app.session_factory() as session:
        assert _counts(session) == counts


def test_a_refused_admission_leaves_no_trace(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    workflow_id = _published_workflow(reference_app)
    reference_app.client.app.state.config = reference_app.client.app.state.config.model_copy(  # type: ignore[attr-defined]
        update={"rule_bundle_path": "/nonexistent/rules.yaml", "rule_bundle_sha256": "a" * 64}
    )
    refused = _call(
        reference_app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        "/workflow-runs",
        {
            "workflow_id": workflow_id,
            "alias": "manual",
            "idempotency_key": "egi-refused",
            **seed_release(reference_app, PROJECT_A),
        },
    )
    assert refused.status_code == 409, refused.text
    with reference_app.session_factory() as session:
        assert set(_counts(session).values()) == {0}, _counts(session)


def test_two_requests_racing_on_one_key_admit_exactly_one_run(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    workflow_id = _published_workflow(reference_app)
    body = {
        "workflow_id": workflow_id,
        "alias": "manual",
        "input": "race",
        "idempotency_key": "egi-race",
        **seed_release(reference_app, PROJECT_A),
    }
    barrier = threading.Barrier(4)
    results: list[tuple[int, str]] = []
    lock = threading.Lock()

    def send() -> None:
        barrier.wait(10)
        response = _call(reference_app, "POST", EDITOR_A, PROJECT_A, "/workflow-runs", body)
        run_id = ""
        if response.status_code == 202:
            run_id = response.json()["data"]["workflow_run_id"]
        with lock:
            results.append((response.status_code, run_id))

    threads = [threading.Thread(target=send, daemon=True) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert len(results) == 4, results
    statuses = {status for status, _ in results}
    assert statuses == {202}, results
    admitted = {run_id for status, run_id in results if status == 202}
    assert len(admitted) == 1, results
    with reference_app.session_factory() as session:
        counts = _counts(session)
        assert counts[CaliberWorkflowRun.__tablename__] == 1, counts
        assert (
            counts[CaliberRunManifest.__tablename__] == 1 == counts[CaliberRunGrant.__tablename__]
        )
        assert counts[CaliberPolicyDecision.__tablename__] == 1, "a loser leaves no decision behind"


def test_a_run_without_lineage_is_refused_in_the_reference_profile_only(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    workflow_id = _published_workflow(reference_app)
    body = {
        "workflow_id": workflow_id,
        "alias": "manual",
        "input": "hi",
        "idempotency_key": "egi-lineage",
    }
    bare = _call(reference_app, "POST", EDITOR_A, PROJECT_A, "/workflow-runs", body)
    trigger = _call(
        reference_app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        f"/workflows/{workflow_id}/trigger",
        {"input": "hi"},
    )
    with reference_app.session_factory() as session:
        runs = session.execute(select(func.count()).select_from(CaliberWorkflowRun)).scalar_one()
    if reference_app.profile == "reference":
        assert bare.status_code == 422 and bare.json()["error"] == "lineage_required", bare.text
        assert trigger.status_code == 422 and trigger.json()["error"] == "lineage_required", (
            trigger.text
        )
        assert runs == 0, "refused before anything was stored"
        lineage = seed_release(reference_app, PROJECT_A)
        ok = _call(
            reference_app, "POST", EDITOR_A, PROJECT_A, "/workflow-runs", {**body, **lineage}
        )
        assert ok.status_code == 202, ok.text
        with reference_app.session_factory() as session:
            run = session.get(CaliberWorkflowRun, ok.json()["data"]["workflow_run_id"])
            assert run is not None and run.runtime_lineage_id, "the run carries its lineage"
    else:
        assert bare.status_code == 202, "development profile: lineage is optional"


def test_a_run_over_an_unapproved_knowledge_base_is_refused_until_the_source_is_approved(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    from caliber.context.sources import approve_source, revoke_source
    from caliber.db.models import CaliberKnowledgeBase, CaliberKnowledgeBaseVersion

    workflow_id = _published_workflow(reference_app)
    with reference_app.session_factory() as session:
        session.add(
            CaliberKnowledgeBase(
                knowledge_base_id="KB-b02",
                name="b02",
                owner=EDITOR_A,
                project_id=PROJECT_A,
                visibility="project",
                status="active",
                source_bucket="b",
            )
        )
        session.flush()
        session.add(
            CaliberKnowledgeBaseVersion(
                knowledge_base_version_id="KBV-b02",
                knowledge_base_id="KB-b02",
                version_number=1,
                status="completed",
                chunking_strategy="fixed",
                embedding_model="m",
                output_bucket="b",
                output_prefix="p",
            )
        )
        session.commit()
    from dataclasses import replace

    from caliber.run_manifest import KnowledgeBasePin
    from caliber.workflows import admission

    real = admission._resolve

    def resolve(version: Any, settings: Any) -> Any:
        pins, execution_manifest = real(version, settings)
        return replace(
            pins, knowledge_base_versions=(KnowledgeBasePin("KB-b02", "KBV-b02"),)
        ), execution_manifest

    import pytest as _pytest

    with _pytest.MonkeyPatch.context() as patch:
        patch.setattr(admission, "_resolve", resolve)
        lineage = seed_release(reference_app, PROJECT_A)
        body = {"workflow_id": workflow_id, "alias": "manual", **lineage}
        refused = _call(
            reference_app,
            "POST",
            EDITOR_A,
            PROJECT_A,
            "/workflow-runs",
            {**body, "idempotency_key": "src-1"},
        )
        if reference_app.profile == "reference":
            assert refused.status_code == 409, refused.text
            assert "source_not_approved" in refused.text and "KB-b02@KBV-b02" in refused.text
            with reference_app.session_factory() as session:
                assert (
                    session.execute(
                        select(func.count()).select_from(CaliberWorkflowRun)
                    ).scalar_one()
                    == 0
                )
                approve_source(session, PROJECT_A, "KB-b02", "KBV-b02", EDITOR_A)
                session.commit()
            ok = _call(
                reference_app,
                "POST",
                EDITOR_A,
                PROJECT_A,
                "/workflow-runs",
                {**body, "idempotency_key": "src-2"},
            )
            assert ok.status_code == 202, ok.text
            with reference_app.session_factory() as session:
                revoke_source(session, PROJECT_A, "KB-b02", "KBV-b02", EDITOR_A)
                session.commit()
            again = _call(
                reference_app,
                "POST",
                EDITOR_A,
                PROJECT_A,
                "/workflow-runs",
                {**body, "idempotency_key": "src-3"},
            )
            assert again.status_code == 409, "a revoked source is refused at once"
        else:
            assert refused.status_code == 202, "development profile: sources need no approval"
