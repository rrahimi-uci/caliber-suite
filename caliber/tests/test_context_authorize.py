"""A run reads a context source only if its own grant, its subject's current membership and the source's current
approval all say so, decided before the source is loaded (B07.2, #699)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.context import authorize as authorize_module
from caliber.context.authorize import ContextDenied, authorize_source, source_guard_for
from caliber.context.sources import approve_source, revoke_source, source_resource
from caliber.db.models import (
    CaliberKnowledgeBase,
    CaliberKnowledgeBaseVersion,
    CaliberPolicyDecision,
    CaliberProjectMember,
    CaliberRunGrant,
    CaliberWorkflowRun,
)
from caliber.knowledge.service import KnowledgeBaseService
from caliber.orchestrator.workflow_run_worker import WorkflowRunWorker
from caliber.policy.principal import Subject, Workload
from caliber.workflows.admission import AdmissionSettings
from caliber.workflows.run_pins import load_run_manifest
from tests.test_run_admission import PROJECT, _enable, _project
from tests.workflow_helpers import PREFIX, create_draft, create_workflow, make_manifest

KB = "KB-ctx"
V1, V2 = "KBV-ctx-1", "KBV-ctx-2"


def _query_manifest(workflow_id: str) -> dict[str, Any]:
    manifest = make_manifest(workflow_id)
    manifest["nodes"] = {
        "start": manifest["nodes"]["start"],
        "knowledge": {
            "id": "knowledge",
            "type": "knowledge_query",
            "knowledge_base_id": KB,
            "version_ids": [V1],
            "retrieval_modes": ["dense"],
        },
        "final": manifest["nodes"]["final"],
    }
    manifest["edges"] = [
        {"id": "e1", "from": "start", "to": "knowledge", "map": {"msg": "question"}},
        {"id": "e2", "from": "knowledge", "to": "final", "map": {"answer": "response"}},
    ]
    return manifest


class World:
    def __init__(self, client: TestClient, db: Session, run_id: str) -> None:
        self.client, self.db, self.run_id = client, db, run_id
        loaded = load_run_manifest(db, run_id)
        assert loaded is not None
        self.record = loaded.record
        self.grant_id = str(self.record["grant_id"])
        self.subject = Subject.from_manifest(self.record["subject"], project_id=PROJECT)

    def authorize(self, kb: str = KB, version: str = V1, **overrides: Any) -> Any:
        arguments: dict[str, Any] = {
            "identity": self.subject,
            "run_manifest": self.record,
            "grant_id": self.grant_id,
            "knowledge_base_id": kb,
            "version_id": version,
            **overrides,
        }
        return authorize_source(self.db, **arguments)


@pytest.fixture
def world(client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch) -> World:
    _enable(client)
    real = AdmissionSettings.from_config
    monkeypatch.setattr(
        AdmissionSettings,
        "from_config",
        classmethod(lambda cls, config: replace(real(config), approved_sources_required=True)),
    )
    _project(db_session)
    client.headers.update({"X-CALIBER-Project": PROJECT})
    db_session.add(
        CaliberKnowledgeBase(
            knowledge_base_id=KB,
            name="ctx",
            owner="@test",
            project_id=PROJECT,
            visibility="project",
            status="active",
            source_bucket="b",
            active_version_id=V1,
        )
    )
    db_session.flush()
    for version in (V1, V2):
        db_session.add(
            CaliberKnowledgeBaseVersion(
                knowledge_base_version_id=version,
                knowledge_base_id=KB,
                version_number=int(version[-1]),
                status="completed",
                chunking_strategy="fixed",
                embedding_model="m",
                output_bucket="b",
                output_prefix="p",
            )
        )
    db_session.flush()
    approve_source(db_session, PROJECT, KB, V1, "@test")
    approve_source(db_session, PROJECT, KB, V2, "@test")
    db_session.commit()
    workflow_id = create_workflow(client, "Ctx WF")
    version_id, _ = create_draft(client, workflow_id, _query_manifest(workflow_id))
    assert client.post(f"{PREFIX}/workflow-versions/{version_id}/publish").status_code == 200
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={
            "workflow_id": workflow_id,
            "alias": "manual",
            "input": "q",
            "idempotency_key": "ctx-1",
        },
    )
    assert created.status_code == 202, created.text
    return World(client, db_session, created.json()["data"]["workflow_run_id"])


def _worker(client: TestClient) -> WorkflowRunWorker:
    return WorkflowRunWorker(
        session_factory=client.app.state.session_factory,
        config=client.app.state.config,
        event_bus=getattr(client.app.state, "event_bus", None),
    )


def _decisions(db: Session, action: str) -> list[CaliberPolicyDecision]:
    db.expire_all()
    return list(
        db.execute(
            select(CaliberPolicyDecision).where(CaliberPolicyDecision.action == action)
        ).scalars()
    )


def test_the_grant_lists_exactly_the_sources_admission_approved(world: World) -> None:
    grant = world.db.execute(select(CaliberRunGrant)).scalar_one()
    assert set(grant.allowed_actions) == {"resource.execute", "context.read"}
    assert source_resource(KB, V1) in grant.allowed_resources
    assert source_resource(KB, V2) not in grant.allowed_resources, (
        "approved, but this run does not read it"
    )


def test_exact_policy_arguments(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    real = authorize_module.decide

    def spy(session: Session, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return real(session, **kwargs)

    monkeypatch.setattr(authorize_module, "decide", spy)
    authorization = world.authorize()
    (call,) = seen
    assert call["run_id"] == world.run_id and call["grant_id"] == world.grant_id
    assert call["subject"] == world.subject
    assert call["workload"] == Workload.from_manifest(world.record["workload"])
    assert call["action"] == "context.read"
    assert call["resource"] == f"knowledge_base:{KB}@{V1}"
    assert call["purpose"] == world.record["purpose"]
    assert authorization.decision.allowed and authorization.knowledge_base_id == KB


def test_wrong_grant_denied(world: World) -> None:
    for grant_id in ("gr-nope", "", "gr-" + "0" * 32):
        with pytest.raises(ContextDenied) as caught:
            world.authorize(grant_id=grant_id or "gr-blank")
        assert caught.value.code == "authority_denied" and "grant_not_found" in caught.value.reasons
        assert KB not in str(caught.value) and V1 not in str(caught.value), "no source named"
    other = {**world.record, "subject": {"user_id": "@mallory", "auth_method": "session"}}
    with pytest.raises(ContextDenied) as mismatch:
        world.authorize(run_manifest=other)
    assert mismatch.value.code == "identity_mismatch", (
        "the manifest's subject must be the run's identity"
    )
    with pytest.raises(ContextDenied) as forged:
        world.authorize(identity=Subject("@mallory", "session", PROJECT))
    assert forged.value.code == "identity_mismatch"
    with pytest.raises(ContextDenied) as broken:
        world.authorize(run_manifest={"purpose": "x"})
    assert broken.value.code == "manifest_invalid"


def test_revoked_membership(world: World) -> None:
    member = world.db.execute(
        select(CaliberProjectMember).where(CaliberProjectMember.user_id == "@test")
    ).scalar_one()
    from caliber.db.models import CaliberProject

    project = world.db.get(CaliberProject, PROJECT)
    assert project is not None
    project.owner = "@someone-else"
    member.status = "inactive"
    world.db.commit()
    with pytest.raises(ContextDenied) as caught:
        world.authorize()
    assert caught.value.code == "authority_denied" and caught.value.reasons == ("not_a_member",)


def test_a_source_that_is_not_approved_now_or_not_in_the_grant_is_refused(world: World) -> None:
    revoke_source(world.db, PROJECT, KB, V1, "@owner")
    world.db.commit()
    with pytest.raises(ContextDenied) as revoked:
        world.authorize()
    assert revoked.value.code == "source_not_approved"
    with pytest.raises(ContextDenied) as unlisted:
        world.authorize(
            version=V2
        )  # approved for the project, but the run's grant does not list it
    assert (
        unlisted.value.code == "authority_denied"
        and "resource_not_allowed" in unlisted.value.reasons
    )


def test_foreign_or_unapproved_source_never_reaches_query(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    queries: list[Any] = []
    monkeypatch.setattr(
        KnowledgeBaseService, "query", lambda self, request, **kw: queries.append(request)
    )
    revoke_source(world.db, PROJECT, KB, V1, "@owner")  # revoked after the run was admitted
    world.db.commit()
    _worker(world.client)._tick()
    db = world.db
    db.expire_all()
    run = db.get(CaliberWorkflowRun, world.run_id)
    assert run is not None and run.status == "failed", (run.status, run.error_code)
    assert queries == [], "zero retrieval calls"
    assert _decisions(db, "context.read") == [], "a withdrawn source asks no policy question"
    from caliber.db.models import CaliberAuditLog

    (audit,) = db.execute(
        select(CaliberAuditLog).where(CaliberAuditLog.action == "context_read_denied")
    ).scalars()
    assert audit.entity_id == world.run_id and audit.details["reason"] == "source_not_approved"
    assert audit.details["knowledge_base_id"] == KB and audit.details["version_id"] == V1

    guard = source_guard_for(
        world.client.app.state.session_factory,
        world.subject,
        world.record,
        world.grant_id,
    )
    for version in (
        "KBV-unknown",
        V2,
    ):  # a source that does not exist, and one the grant does not list
        with pytest.raises(ContextDenied):
            guard(version)
    assert queries == []


def test_allowed_source_query_once(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    queries: list[Any] = []

    class Answer:
        def model_dump(self, mode: str = "python") -> dict[str, Any]:
            return {
                "answer": "ok",
                "text": "ok",
                "result": {},
                "citations": [],
                "chunks": [],
                "graph_context": {},
            }

    def query(self: Any, request: Any, **kwargs: Any) -> Answer:
        queries.append(list(request.version_ids))
        return Answer()

    monkeypatch.setattr(KnowledgeBaseService, "query", query)
    _worker(world.client)._tick()
    run = world.db.get(CaliberWorkflowRun, world.run_id)
    world.db.expire_all()
    run = world.db.get(CaliberWorkflowRun, world.run_id)
    assert run is not None and run.status == "completed", (
        run.status,
        run.error_code,
        run.error_summary,
    )
    assert queries == [[V1]], "the pinned source, queried once"
    allowed = [d for d in _decisions(world.db, "context.read") if d.result == "allow"]
    assert [d.resource for d in allowed] == [source_resource(KB, V1)]


def test_an_unavailable_or_malformed_authority_is_a_refusal_never_an_allow(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import OperationalError

    from caliber.policy.decisions import DecisionInputError

    def down(*_a: Any, **_k: Any) -> Any:
        raise OperationalError("select", {}, Exception("store down"))

    def malformed(*_a: Any, **_k: Any) -> Any:
        raise DecisionInputError("blank")

    real = authorize_module.decide

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

    for replacement, code in (
        (down, "authority_unavailable"),
        (malformed, "manifest_invalid"),
        (unrecorded, "authority_unavailable"),
    ):
        with monkeypatch.context() as patch:
            patch.setattr(authorize_module, "decide", replacement)
            with pytest.raises(ContextDenied) as caught:
                world.authorize()
            assert caught.value.code == code
    with monkeypatch.context() as patch:
        patch.setattr(authorize_module, "is_approved", down)
        with pytest.raises(ContextDenied) as caught:
            world.authorize()
        assert caught.value.code == "authority_unavailable"
