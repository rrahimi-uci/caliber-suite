"""B07 public path: a run may only read context sources its project approved (B07.1b, #696).

The reference-profile app on PostgreSQL (the development profile on SQLite, where nothing is required). Real manifests
go through the real sealed bundle: a knowledge query that names its knowledge base, one that names only versions, one
whose versions arrive on an input port, and a parent workflow whose *child* queries a knowledge base. Each is refused
(``403``, ``reason_code: source_not_approved``, every offending ``kb@version`` named) until the sources are approved,
admitted after, and refused again at the next admission after a revocation; an idempotent retry of an already admitted
request still returns the original run.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select

from caliber.context.sources import approve_source, revoke_source
from caliber.db.models import (
    CaliberKnowledgeBase,
    CaliberKnowledgeBaseVersion,
    CaliberWorkflowRun,
)
from tests.egi_scenarios.conftest import (
    EDITOR_A,
    PROJECT_A,
    ReferenceApp,
    Workspaces,
    seed_release,
)
from tests.egi_scenarios.test_b02_admission import _call
from tests.workflow_helpers import make_manifest

KB = "KB-b07"


def _seed_knowledge(app: ReferenceApp) -> None:
    with app.session_factory() as session:
        session.add(
            CaliberKnowledgeBase(
                knowledge_base_id=KB,
                name="b07",
                owner=EDITOR_A,
                project_id=PROJECT_A,
                visibility="project",
                status="active",
                source_bucket="b",
                active_version_id="KBV-b07-1",
            )
        )
        session.flush()
        for number in (1, 2):
            session.add(
                CaliberKnowledgeBaseVersion(
                    knowledge_base_version_id=f"KBV-b07-{number}",
                    knowledge_base_id=KB,
                    version_number=number,
                    status="completed",
                    chunking_strategy="fixed",
                    embedding_model="m",
                    output_bucket="b",
                    output_prefix="p",
                )
            )
        session.commit()


def _query_manifest(
    workflow_id: str, node: dict[str, Any], *, wire_versions: bool = False
) -> dict[str, Any]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": manifest["nodes"]["start"],
        "knowledge": {"id": "knowledge", "type": "knowledge_query", **node},
        "final": manifest["nodes"]["final"],
    }
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "knowledge", "map": {"msg": "question"}},
        {"id": "e2", "from": "knowledge", "to": "final", "map": {"answer": "response"}},
    ]
    if wire_versions:  # a second query whose ``version_ids`` come from the first query's citations
        manifest["nodes"]["second"] = {**manifest["nodes"]["knowledge"], "id": "second"}
        manifest["edges"] = [
            {"id": "e1", "from": "start", "to": "knowledge", "map": {"msg": "question"}},
            {"id": "e2", "from": "start", "to": "second", "map": {"msg": "question"}},
            {"id": "e3", "from": "knowledge", "to": "second", "map": {"citations": "version_ids"}},
            {"id": "e4", "from": "second", "to": "final", "map": {"answer": "response"}},
        ]
    return manifest


def _publish(app: ReferenceApp, name: str, manifest_for: Any) -> tuple[str, str]:
    created = _call(app, "POST", EDITOR_A, PROJECT_A, "/workflows", {"name": name})
    assert created.status_code == 201, created.text
    workflow_id = created.json()["data"]["workflow_id"]
    draft = _call(
        app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        f"/workflows/{workflow_id}/versions",
        {"manifest": manifest_for(workflow_id)},
    )
    assert draft.status_code == 201, draft.text
    version_id = draft.json()["data"]["version_id"]
    published = _call(app, "POST", EDITOR_A, PROJECT_A, f"/workflow-versions/{version_id}/publish")
    assert published.status_code == 200, published.text
    app.client.app.state.config = app.client.app.state.config.model_copy(  # type: ignore[attr-defined]
        update={"workflow_run_queue_enabled": True, "admission_enabled": True}
    )
    return workflow_id, version_id


def _start(app: ReferenceApp, workflow_id: str, key: str) -> Any:
    return _call(
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


def _runs(app: ReferenceApp) -> int:
    with app.session_factory() as session:
        return int(
            session.execute(select(func.count()).select_from(CaliberWorkflowRun)).scalar_one()
        )


def _refused(response: Any, *named: str) -> None:
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["reason_code"] == "source_not_approved", body
    for item in named:
        assert item in body["detail"], (item, body)


@pytest.mark.parametrize(
    "shape",
    ["names_the_knowledge_base", "names_only_versions"],
)
def test_a_knowledge_query_is_refused_until_its_version_is_approved_and_again_after_revocation(
    shape: str, reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    _seed_knowledge(reference_app)
    node = (
        {"knowledge_base_id": KB, "retrieval_modes": ["dense"]}
        if shape == "names_the_knowledge_base"
        else {"version_ids": ["KBV-b07-2"], "retrieval_modes": ["dense"]}
    )
    version = "KBV-b07-1" if shape == "names_the_knowledge_base" else "KBV-b07-2"
    workflow_id, _ = _publish(reference_app, shape, lambda wf: _query_manifest(wf, node))
    refused = _start(reference_app, workflow_id, f"{shape}-1")
    if reference_app.profile != "reference":
        assert refused.status_code == 202, "development: no approval is required"
        return
    _refused(refused, f"{KB}@{version}")
    assert _runs(reference_app) == 0, "nothing was stored"

    with reference_app.session_factory() as session:
        approve_source(session, PROJECT_A, KB, version, EDITOR_A)
        session.commit()
    admitted = _start(reference_app, workflow_id, f"{shape}-2")
    assert admitted.status_code == 202, admitted.text
    run_id = admitted.json()["data"]["workflow_run_id"]

    with reference_app.session_factory() as session:
        revoke_source(session, PROJECT_A, KB, version, EDITOR_A)
        session.commit()
    replay = _start(reference_app, workflow_id, f"{shape}-2")
    assert replay.status_code == 202 and replay.json()["data"]["workflow_run_id"] == run_id, (
        "the same request returns the run it already admitted"
    )
    _refused(_start(reference_app, workflow_id, f"{shape}-3"), f"{KB}@{version}")
    retried = _call(
        reference_app,
        "POST",
        EDITOR_A,
        PROJECT_A,
        f"/workflow-runs/{run_id}/retry",
        {"reason": "again"},
    )
    # a retry is a new run: it needs the approval that is gone (it must be a failed run to be retried at all)
    assert retried.status_code in (403, 409), retried.text


def test_a_query_whose_versions_arrive_on_an_input_cannot_be_pinned_or_approved(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    _seed_knowledge(reference_app)
    workflow_id, _ = _publish(
        reference_app,
        "wired",
        lambda wf: _query_manifest(
            wf, {"knowledge_base_id": KB, "retrieval_modes": ["dense"]}, wire_versions=True
        ),
    )
    with reference_app.session_factory() as session:
        approve_source(session, PROJECT_A, KB, "KBV-b07-1", EDITOR_A)
        approve_source(session, PROJECT_A, KB, "KBV-b07-2", EDITOR_A)
        session.commit()
    response = _start(reference_app, workflow_id, "wired-1")
    if reference_app.profile != "reference":
        assert response.status_code == 202
        return
    _refused(response, "takes its versions from an input")
    assert _runs(reference_app) == 0


def test_a_parent_workflow_inherits_the_sources_its_child_reads(
    reference_app: ReferenceApp, two_workspaces: Workspaces
) -> None:
    _seed_knowledge(reference_app)
    child_workflow, child_version = _publish(
        reference_app,
        "child",
        lambda wf: _query_manifest(wf, {"knowledge_base_id": KB, "retrieval_modes": ["dense"]}),
    )

    def parent(workflow_id: str) -> dict[str, Any]:
        manifest = make_manifest(workflow_id)
        manifest["nodes"]["agent"] = {
            "id": "agent",
            "type": "subworkflow",
            "workflow_id": child_workflow,
            "alias": "manual",
            "version_id": child_version,
            "inputs": {"input": {"type": "string"}},
            "outputs": {"final_output": {"type": "string"}},
        }
        return manifest

    parent_workflow, _ = _publish(reference_app, "parent", parent)
    response = _start(reference_app, parent_workflow, "parent-1")
    if reference_app.profile != "reference":
        assert response.status_code == 202
        return
    _refused(response, f"{KB}@KBV-b07-1")
    with reference_app.session_factory() as session:
        approve_source(session, PROJECT_A, KB, "KBV-b07-1", EDITOR_A)
        session.commit()
    assert _start(reference_app, parent_workflow, "parent-2").status_code == 202
