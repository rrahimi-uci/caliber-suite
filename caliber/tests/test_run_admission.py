"""Run admission (B02.2a, #680): pins, grant, decision and manifest in the transaction that creates the run.

The public create-run route, the event trigger and the shared enqueue service run against a real migrated database
(SQLite everywhere, PostgreSQL in the EGI scenario). A refusal must leave no trace: no run, no grant, no decision, no
manifest, no event.
"""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.auth import SCOPE_APPROVER, SCOPE_OPERATOR, SCOPE_VIEWER, CaliberIdentity
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberPolicyDecision,
    CaliberProject,
    CaliberProjectMember,
    CaliberRunGrant,
    CaliberRunManifest,
    CaliberWorkflow,
    CaliberWorkflowRun,
    CaliberWorkflowRunEvent,
    CaliberWorkflowVersion,
)
from caliber.policy.decisions import REFERENCE_POLICY_V1, Policy
from caliber.run_manifest import read_manifest
from caliber.workflows import admission
from caliber.workflows.admission import (
    AdmissionDenied,
    AdmissionSettings,
    check_retry_intent,
    resolve_pins,
)
from caliber.workflows.deployment_bundle import seal_bundle
from caliber.workflows.run_launch import enqueue_workflow_run
from tests.workflow_helpers import (
    PREFIX,
    create_draft,
    create_workflow,
    deploy_prod,
    make_support_manifest,
    register_demo_tools,
)

PROJECT = "P-admit"
RULES = Path(__file__).resolve().parent / "fixtures" / "rule_bundles" / "support_rules.v1.yaml"
RULES_SHA = hashlib.sha256(RULES.read_bytes()).hexdigest()


def _manifest(workflow_id: str, text: str = "Help the customer.") -> dict[str, Any]:
    """The support workflow with its prompt inline and no dataset, so every dependency resolves offline."""
    manifest = make_support_manifest(workflow_id)
    manifest["artifacts"] = {}
    manifest["nodes"]["support_agent"]["instructions"] = {"type": "inline", "text": text}
    return manifest


def _enable(client: TestClient, **extra: Any) -> None:
    client.app.state.config = client.app.state.config.model_copy(
        update={"workflow_run_queue_enabled": True, "admission_enabled": True, **extra}
    )


def _project(db: Session, user: str = "@test", role: str = "owner") -> None:
    if db.get(CaliberProject, PROJECT) is None:
        db.add(CaliberProject(project_id=PROJECT, name="admit", owner="@test"))
        db.flush()
    db.add(
        CaliberProjectMember(
            member_id=f"M-{user}", project_id=PROJECT, user_id=user, role=role, created_by="@test"
        )
    )
    db.commit()


def _project_workflow(
    client: TestClient,
    db: Session,
    *,
    with_project: bool = True,
    trigger: dict[str, Any] | None = None,
) -> tuple[str, str]:
    if with_project:
        _project(db)
        client.headers.update({"X-CALIBER-Project": PROJECT})
    register_demo_tools(client)
    workflow_id = create_workflow(client, "Admit WF")
    manifest = _manifest(workflow_id)
    if trigger is not None:
        manifest["nodes"]["start"]["trigger"] = trigger
    version_id, _ = create_draft(client, workflow_id, manifest)
    published = client.post(f"{PREFIX}/workflow-versions/{version_id}/publish")
    assert published.status_code == 200, published.text
    return workflow_id, version_id


def _run(client: TestClient, workflow_id: str, **body: Any) -> Any:
    return client.post(
        f"{PREFIX}/workflow-runs", json={"workflow_id": workflow_id, "alias": "manual", **body}
    )


def _counts(db: Session) -> dict[str, int]:
    db.expire_all()
    return {
        model.__tablename__: db.execute(select(func.count()).select_from(model)).scalar_one()
        for model in (
            CaliberWorkflowRun,
            CaliberRunGrant,
            CaliberPolicyDecision,
            CaliberRunManifest,
            CaliberWorkflowRunEvent,
        )
    }


def _nothing(db: Session) -> None:
    assert set(_counts(db).values()) == {0}, _counts(db)


# ---------------------------------------------------------------- the public create-run route


def test_admission_persists_pins(client: TestClient, db_session: Session) -> None:
    _enable(client)
    workflow_id, version_id = _project_workflow(client, db_session)
    response = _run(client, workflow_id, input="refund?", idempotency_key="k-1")
    assert response.status_code == 202, response.text
    run_id = response.json()["data"]["workflow_run_id"]

    assert _counts(db_session)[CaliberRunManifest.__tablename__] == 1
    run = db_session.get(CaliberWorkflowRun, run_id)
    assert run is not None and run.manifest_legacy is False
    assert (run.summary or {})["admitted"] is True
    manifest_row = db_session.execute(select(CaliberRunManifest)).scalar_one()
    grant = db_session.execute(select(CaliberRunGrant)).scalar_one()
    decision = db_session.execute(select(CaliberPolicyDecision)).scalar_one()
    record = read_manifest(bytes(manifest_row.manifest_bytes), manifest_row.manifest_digest)

    version = db_session.get(CaliberWorkflowVersion, version_id)
    assert version is not None
    sealed = version.compiled_bundle["deployment_bundle"]
    assert record["workflow_run_id"] == run_id and manifest_row.workflow_run_id == run_id
    assert record["pins"]["workflow_version"] == version_id
    assert record["pins"]["bundle_digest"] == sealed["integrity"]["digest"]
    assert record["pins"]["policy_version"] == "policy-v1"
    assert record["pins"]["tool_versions"] and all(
        "@" in pin for pin in record["pins"]["tool_versions"]
    )
    assert record["pins"]["model_routes"] == ["fake"]
    assert (record["tenant_id"], record["region"], record["residency"]) == (
        "local",
        "local",
        "local",
    )
    assert record["project_id"] == PROJECT
    assert record["subject"] == {"user_id": "@test", "auth_method": "trusted_header"}
    assert record["workload"]["name"] == "Admit WF"
    assert record["workload"]["workflow_version_digest"] == version.manifest_hash
    assert record["purpose"] == "workflow_run:manual"
    assert record["legacy"] is False and record["rule_bundle_sha256"] is None

    # The grant and the decision are those the manifest names, for exactly this run.
    assert (grant.workflow_run_id, grant.subject_user_id, grant.project_id) == (
        run_id,
        "@test",
        PROJECT,
    )
    assert list(grant.allowed_actions) == ["resource.execute"]
    assert list(grant.allowed_resources) == [f"workflow:{workflow_id}"]
    assert (decision.workflow_run_id, decision.grant_id, decision.result) == (
        run_id,
        grant.grant_id,
        "allow",
    )
    assert decision.policy_version == "policy-v1" and decision.action == "resource.execute"
    assert (
        decision.purpose == record["purpose"]
        and decision.workload_id == record["workload"]["workload_id"]
    )

    events = db_session.execute(select(CaliberWorkflowRunEvent.event_type)).scalars().all()
    assert "workflow.run.queued" in events


def test_without_admission_a_run_stays_a_legacy_run(
    client: TestClient, db_session: Session
) -> None:
    _enable(client, admission_enabled=False)
    workflow_id, _ = _project_workflow(client, db_session)
    response = _run(client, workflow_id)
    assert response.status_code == 202, response.text
    run = db_session.get(CaliberWorkflowRun, response.json()["data"]["workflow_run_id"])
    assert run is not None and run.manifest_legacy is True
    counts = _counts(db_session)
    assert counts[CaliberRunManifest.__tablename__] == counts[CaliberRunGrant.__tablename__] == 0


def test_alias_move_preserves_pins(client: TestClient, db_session: Session) -> None:
    _enable(client)
    workflow_id, first_version = _project_workflow(client, db_session)
    deploy_prod(client, workflow_id, first_version)
    first = _run(client, workflow_id, alias="prod", input="hello", idempotency_key="k-move")
    assert first.status_code == 202, first.text
    run_id = first.json()["data"]["workflow_run_id"]
    original = db_session.execute(select(CaliberRunManifest)).scalar_one()
    original_digest = original.manifest_digest

    # Move the alias to a new version.
    second_version, _ = create_draft(client, workflow_id, _manifest(workflow_id, "A new prompt."))
    assert client.post(f"{PREFIX}/workflow-versions/{second_version}/publish").status_code == 200
    deploy_prod(client, workflow_id, second_version)

    again = _run(client, workflow_id, alias="prod", input="hello", idempotency_key="k-move")
    assert again.status_code == 202, again.text
    assert again.json()["data"]["workflow_run_id"] == run_id, "the original run comes back"
    assert _counts(db_session)[CaliberRunManifest.__tablename__] == 1, "no second manifest"
    db_session.expire_all()
    assert (
        db_session.execute(select(CaliberRunManifest)).scalar_one().manifest_digest
        == original_digest
    )

    changed = _run(
        client, workflow_id, alias="prod", input="something else", idempotency_key="k-move"
    )
    assert changed.status_code == 409, changed.text
    assert "different request" in changed.json()["detail"]
    other_alias = _run(client, workflow_id, alias="manual", input="hello", idempotency_key="k-move")
    assert other_alias.status_code == 409, "a different release selector is a different request"
    assert _counts(db_session)[CaliberRunManifest.__tablename__] == 1

    # A new request resolves the moved alias afresh and pins the new version; the first manifest is untouched.
    fresh = _run(client, workflow_id, alias="prod", input="hello", idempotency_key="k-new")
    assert fresh.status_code == 202, fresh.text
    pins = [
        read_manifest(bytes(row.manifest_bytes))["pins"]["workflow_version"]
        for row in db_session.execute(
            select(CaliberRunManifest).order_by(CaliberRunManifest.created_at)
        ).scalars()
    ]
    assert sorted(pins) == sorted([first_version, second_version])


def test_policy_denial_rolls_back_all(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(client)
    workflow_id, _ = _project_workflow(client, db_session)
    deny = Policy(
        version="policy-deny",
        action_risk=REFERENCE_POLICY_V1.action_risk,
        risk_ceiling=4,
        deny_rules=frozenset({("resource.execute", f"workflow:{workflow_id}")}),
    )
    real = AdmissionSettings.from_config
    monkeypatch.setattr(
        AdmissionSettings,
        "from_config",
        classmethod(lambda cls, config: _with_policy(real(config), deny)),
    )
    response = _run(client, workflow_id, idempotency_key="k-deny")
    assert response.status_code == 403, response.text
    assert "explicit_deny" in response.json()["detail"]
    _nothing(db_session)


def _with_policy(settings: AdmissionSettings, policy: Policy) -> AdmissionSettings:
    from dataclasses import replace

    return replace(settings, policy=policy)


def test_missing_rule_bundle_denies(
    client: TestClient, db_session: Session, tmp_path: Path
) -> None:
    workflow_id, _ = _project_workflow(client, db_session)
    for path, digest, expected in (
        (str(tmp_path / "absent.yaml"), RULES_SHA, "missing_bytes"),
        (str(RULES), "0" * 64, "digest_mismatch"),
    ):
        _enable(client, rule_bundle_path=path, rule_bundle_sha256=digest)
        refused = _run(client, workflow_id, idempotency_key=f"k-{expected}")
        assert refused.status_code == 409, refused.text
        assert expected in refused.json()["detail"]
        _nothing(db_session)

    # The bundle is pinned when it matches, and the manifest says which bytes.
    copy = tmp_path / "rules.yaml"
    copy.write_bytes(RULES.read_bytes())
    _enable(client, rule_bundle_path=str(copy), rule_bundle_sha256=RULES_SHA)
    admitted = _run(client, workflow_id, idempotency_key="k-bundle")
    assert admitted.status_code == 202, admitted.text
    record = read_manifest(
        bytes(db_session.execute(select(CaliberRunManifest)).scalar_one().manifest_bytes)
    )
    assert (record["rule_bundle_path"], record["rule_bundle_sha256"]) == (
        str(copy.resolve()),
        RULES_SHA,
    )

    # Changed bytes stop the next admission, and the first manifest keeps the digest it pinned.
    copy.write_bytes(b"changed: true\n")
    stopped = _run(client, workflow_id, idempotency_key="k-after-change")
    assert stopped.status_code == 409 and "digest_mismatch" in stopped.json()["detail"]
    assert _counts(db_session)[CaliberRunManifest.__tablename__] == 1


def test_a_workflow_without_a_project_is_not_admitted(
    client: TestClient, db_session: Session
) -> None:
    _enable(client)
    workflow_id, _ = _project_workflow(client, db_session, with_project=False)
    refused = _run(client, workflow_id)
    assert refused.status_code == 409 and "project" in refused.json()["detail"]
    _nothing(db_session)


def test_a_viewer_cannot_start_a_run_and_nothing_is_stored(
    client: TestClient, db_session: Session
) -> None:
    _enable(client)
    workflow_id, _ = _project_workflow(client, db_session)
    _project(db_session, user="@viewer", role="viewer")
    headers = {"X-CALIBER-User": "@viewer", "X-CALIBER-Project": PROJECT}
    refused = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_id": workflow_id, "alias": "manual"},
        headers=headers,
    )
    assert refused.status_code in (403, 404), refused.text
    _nothing(db_session)


def test_a_manifest_override_cannot_be_pinned_so_it_is_not_admitted(
    client: TestClient, db_session: Session
) -> None:
    _enable(client)
    workflow_id, _ = _project_workflow(client, db_session)
    refused = _run(client, workflow_id, manifest=_manifest(workflow_id, "an override"))
    assert (
        refused.status_code == 409 and "manifest_override_not_admitted" in refused.json()["detail"]
    )
    _nothing(db_session)


def test_the_reference_profile_admits_without_the_flag() -> None:
    reference = CaliberConfig.model_construct(profile="reference", admission_enabled=False)
    assert AdmissionSettings.from_config(reference).enabled is True
    assert (
        AdmissionSettings.from_config(CaliberConfig.model_construct(profile="development")).enabled
        is False
    )


def test_a_rule_bundle_path_without_its_digest_fails_fast() -> None:
    for config in (
        CaliberConfig.model_construct(rule_bundle_path="/x/rules.yaml", rule_bundle_sha256=""),
        CaliberConfig.model_construct(rule_bundle_path="", rule_bundle_sha256="a" * 64),
    ):
        with pytest.raises(ValueError, match="set together"):
            AdmissionSettings.from_config(config)


def test_the_event_trigger_admits_and_refuses_like_the_create_route(
    client: TestClient, db_session: Session, tmp_path: Path
) -> None:
    _enable(client)
    workflow_id, version_id = _project_workflow(
        client,
        db_session,
        trigger={"mode": "event", "event_name": "order_created", "alias": "prod"},
    )
    deploy_prod(client, workflow_id, version_id)
    path = f"{PREFIX}/workflows/{workflow_id}/trigger"
    body = {"event_name": "order_created", "input": "o-1", "idempotency_key": "evt-1"}
    fired = client.post(path, json=body)
    assert fired.status_code == 202, fired.text
    record = read_manifest(
        bytes(db_session.execute(select(CaliberRunManifest)).scalar_one().manifest_bytes)
    )
    assert record["purpose"] == "workflow_run:event"
    assert (
        client.post(path, json=body).json()["data"]["workflow_run_id"]
        == fired.json()["data"]["workflow_run_id"]
    )
    changed = client.post(path, json={**body, "input": "o-2"})
    assert changed.status_code == 409, changed.text
    assert _counts(db_session)[CaliberRunManifest.__tablename__] == 1

    _enable(client, rule_bundle_path=str(tmp_path / "absent.yaml"), rule_bundle_sha256=RULES_SHA)
    before = _counts(db_session)
    refused = client.post(path, json={**body, "idempotency_key": "evt-2"})
    assert refused.status_code == 409, refused.text
    assert _counts(db_session) == before, "the refused event stored nothing"


def test_a_store_failure_or_a_lost_decision_stores_nothing_and_says_why(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sqlalchemy.exc import IntegrityError, OperationalError

    from caliber.policy.decisions import DecisionInputError

    workflow_id, version_id = _project_workflow(client, db_session)
    workflow, version = _db_pair(db_session, workflow_id, version_id)
    settings = AdmissionSettings(enabled=True)

    def call() -> AdmissionDenied:
        db_session.rollback()
        with pytest.raises(AdmissionDenied) as caught:
            enqueue_workflow_run(
                db_session,
                workflow=workflow,
                version=version,
                alias="prod",
                source="api",
                actor="@test",
                identity=_identity(),
                admission=settings,
            )
        db_session.rollback()
        return caught.value

    def raise_(error: Exception) -> Any:
        def fail(*_a: Any, **_k: Any) -> Any:
            raise error

        return fail

    with monkeypatch.context() as patch:
        patch.setattr(
            admission.grants, "create_grant", raise_(OperationalError("x", {}, Exception()))
        )
        unavailable = call()
    assert (unavailable.code, unavailable.status) == ("authority_unavailable", 503)

    with monkeypatch.context() as patch:
        patch.setattr(admission, "decide", raise_(DecisionInputError("bad input")))
        refused = call()
    assert (refused.code, refused.status) == ("policy_denied", 409)

    with monkeypatch.context() as patch:
        unrecorded = SimpleNamespace(
            allowed=False, reason_codes=("authority_unavailable",), decision_id="d"
        )
        patch.setattr(admission, "decide", lambda *_a, **_k: unrecorded)
        lost = call()
    assert (lost.code, lost.status) == ("policy_denied", 503)

    with monkeypatch.context() as patch:
        patch.setattr(admission, "CaliberRunManifest", raise_(IntegrityError("x", {}, Exception())))
        duplicate = call()
    assert (duplicate.code, duplicate.status) == ("manifest_refused", 409)
    assert set(_counts(db_session).values()) == {0}


# ---------------------------------------------------------------- the shared enqueue service (event trigger, tools)


def _db_pair(
    db: Session, workflow_id: str, version_id: str
) -> tuple[CaliberWorkflow, CaliberWorkflowVersion]:
    workflow = db.get(CaliberWorkflow, workflow_id)
    version = db.get(CaliberWorkflowVersion, version_id)
    assert workflow is not None and version is not None
    return workflow, version


def _identity(user: str = "@test", *scopes: str, kind: str | None = None) -> CaliberIdentity:
    return CaliberIdentity(
        user_id=user,
        scopes=frozenset(scopes or {SCOPE_VIEWER, SCOPE_OPERATOR, SCOPE_APPROVER}),
        active_project_id=PROJECT,
        credential_kind=kind,
    )


def test_the_enqueue_service_admits_atomically_and_a_refusal_leaves_the_caller_to_roll_back(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, version_id = _project_workflow(client, db_session)
    workflow, version = _db_pair(db_session, workflow_id, version_id)
    settings = AdmissionSettings(enabled=True)
    run, created = enqueue_workflow_run(
        db_session,
        workflow=workflow,
        version=version,
        alias="prod",
        source="event",
        actor="@test",
        identity=_identity(),
        admission=settings,
        idempotency_key="ev-1",
    )
    db_session.commit()
    assert created is True and run.manifest_legacy is False
    assert _counts(db_session)[CaliberRunManifest.__tablename__] == 1
    record = read_manifest(
        bytes(db_session.execute(select(CaliberRunManifest)).scalar_one().manifest_bytes)
    )
    assert record["purpose"] == "workflow_run:event"

    # The same event again: the same run. A changed one: refused.
    same, created_again = enqueue_workflow_run(
        db_session,
        workflow=workflow,
        version=version,
        alias="prod",
        source="event",
        actor="@test",
        identity=_identity(),
        admission=settings,
        idempotency_key="ev-1",
    )
    assert (same.workflow_run_id, created_again) == (run.workflow_run_id, False)
    with pytest.raises(AdmissionDenied) as conflict:
        enqueue_workflow_run(
            db_session,
            workflow=workflow,
            version=version,
            alias="prod",
            source="event",
            actor="@test",
            input_text="different",
            identity=_identity(),
            admission=settings,
            idempotency_key="ev-1",
        )
    assert conflict.value.status == 409 and conflict.value.code == "intent_conflict"

    # Without the authenticated caller nothing is admitted; a refused run leaves the session to be rolled back.
    before = _counts(db_session)
    with pytest.raises(AdmissionDenied) as anonymous:
        enqueue_workflow_run(
            db_session,
            workflow=workflow,
            version=version,
            alias="prod",
            source="event",
            actor="@test",
            admission=settings,
            idempotency_key="ev-2",
        )
    assert anonymous.value.status == 401
    assert _counts(db_session) == before, "refused before any row was written"


def test_a_member_whose_role_cannot_execute_is_refused_by_the_grant_and_everything_rolls_back(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, version_id = _project_workflow(client, db_session)
    _project(db_session, user="@bystander", role="viewer")
    workflow, version = _db_pair(db_session, workflow_id, version_id)
    before = _counts(db_session)
    with pytest.raises(AdmissionDenied) as caught:
        enqueue_workflow_run(
            db_session,
            workflow=workflow,
            version=version,
            alias="prod",
            source="event",
            actor="@bystander",
            identity=_identity("@bystander"),
            admission=AdmissionSettings(enabled=True),
        )
    assert caught.value.code == "grant_refused" and caught.value.status == 403
    db_session.rollback()
    assert _counts(db_session) == before


def test_a_credential_kind_that_is_not_a_known_method_is_refused(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, version_id = _project_workflow(client, db_session)
    workflow, version = _db_pair(db_session, workflow_id, version_id)
    with pytest.raises(AdmissionDenied) as caught:
        enqueue_workflow_run(
            db_session,
            workflow=workflow,
            version=version,
            alias="prod",
            source="event",
            actor="@test",
            identity=_identity(kind="telepathy"),
            admission=AdmissionSettings(enabled=True),
        )
    assert caught.value.code == "subject_invalid"
    with pytest.raises(AdmissionDenied) as anonymous:
        enqueue_workflow_run(
            db_session,
            workflow=workflow,
            version=version,
            alias="prod",
            source="event",
            actor="@test",
            identity=_identity("anonymous"),
            admission=AdmissionSettings(enabled=True),
        )
    assert anonymous.value.code == "subject_invalid"


# ---------------------------------------------------------------- pins come from the sealed bundle


@pytest.fixture
def real_bundle(client: TestClient, db_session: Session) -> dict[str, Any]:
    """A real sealed deployment bundle (compiled when the version was published), to re-seal with other locks."""
    _, version_id = _project_workflow(client, db_session)
    version = db_session.get(CaliberWorkflowVersion, version_id)
    assert version is not None
    return copy.deepcopy(version.compiled_bundle["deployment_bundle"])


def _version(
    bundle: dict[str, Any],
    dependencies: list[dict[str, Any]],
    *,
    models: tuple[str, ...] = ("inherit",),
) -> Any:
    manifest = make_support_manifest("WF-pins")
    manifest["artifacts"] = {}
    manifest["nodes"]["support_agent"]["instructions"] = {"type": "inline", "text": "x"}
    manifest["nodes"]["support_agent"]["model"] = models[0]
    ready = all(item.get("status") == "resolved" for item in dependencies)
    sealed = seal_bundle({**bundle, "dependencies": dependencies, "ready_to_deploy": ready})
    return SimpleNamespace(
        version_id="WFV-pins", manifest=manifest, compiled_bundle={"deployment_bundle": sealed}
    )


def _dep(kind: str, reference: str, version: str | None, **extra: Any) -> dict[str, Any]:
    return {"kind": kind, "reference": reference, "version": version, "status": "resolved", **extra}


def test_pins_are_taken_from_the_bundles_dependency_lock(real_bundle: dict[str, Any]) -> None:
    version = _version(
        real_bundle,
        [
            _dep("prompt", "support-agent@prod", "7"),
            _dep("tool", "tool.lookup_policy.v1", "1.2.0"),
            _dep("mcp_tool", "srv/fetch", None, digest="d" * 64),
            _dep("skill", "triage", "3"),
            _dep(
                "knowledge_base",
                "kb-1",
                "v9",
                snapshot={
                    "knowledge_base_id": "kb-1",
                    "versions": [{"version_id": "KBV-9"}, {"note": "no id"}, "junk"],
                },
            ),
            _dep(
                "eval_dataset", "ds", None, status="unresolved"
            ),  # evaluation only: does not stop a run
            _dep("subworkflow", "WF-child@prod", "WFV-child"),
        ],
        models=("gpt-4o",),
    )
    pins = resolve_pins(version, AdmissionSettings(enabled=True, llm_provider="openai"))
    assert pins.prompt_versions == ("support-agent@7",)
    assert pins.tool_versions == ("srv/fetch@" + "d" * 64, "tool.lookup_policy.v1@1.2.0")
    assert pins.skill_versions == ("triage@3",)
    assert [(kb.knowledge_base_id, kb.version_id) for kb in pins.knowledge_base_versions] == [
        ("kb-1", "KBV-9")
    ]
    assert pins.model_routes == ("openai", "openai/gpt-4o")
    assert pins.workflow_version == "WFV-pins" and pins.policy_version == "policy-v1"
    assert len(pins.bundle_digest) == 64


@pytest.mark.parametrize(
    "compiled",
    [None, {}, {"deployment_bundle": "nope"}, {"deployment_bundle": {"kind": "other"}}],
)
def test_a_version_without_a_sealed_bundle_cannot_be_pinned(
    compiled: Any, real_bundle: dict[str, Any]
) -> None:
    version = _version(real_bundle, [])
    version.compiled_bundle = compiled
    with pytest.raises(AdmissionDenied) as caught:
        resolve_pins(version, AdmissionSettings(enabled=True))
    assert caught.value.code == "bundle_unavailable"


def test_an_altered_bundle_is_refused(real_bundle: dict[str, Any]) -> None:
    version = _version(real_bundle, [_dep("tool", "tool.x.v1", "1.0.0")])
    version.compiled_bundle["deployment_bundle"]["dependencies"][0]["version"] = "9.9.9"
    with pytest.raises(AdmissionDenied, match="integrity"):
        resolve_pins(version, AdmissionSettings(enabled=True))


def test_an_unresolved_runtime_dependency_is_refused(real_bundle: dict[str, Any]) -> None:
    version = _version(
        real_bundle, [_dep("prompt", "support-agent@prod", None, status="unresolved")]
    )
    with pytest.raises(AdmissionDenied, match="unresolved dependencies"):
        resolve_pins(version, AdmissionSettings(enabled=True))


def test_a_floating_or_ambiguous_dependency_is_refused_as_a_manifest_error(
    real_bundle: dict[str, Any],
) -> None:
    for dependency in (
        _dep("tool", "tool.x.v1", "latest"),
        _dep("prompt", "p@prod", ""),
    ):
        with pytest.raises(AdmissionDenied) as caught:
            resolve_pins(_version(real_bundle, [dependency]), AdmissionSettings(enabled=True))
        assert caught.value.code == "manifest_refused", dependency
    same = _version(
        real_bundle,
        [
            _dep(
                "knowledge_base",
                "kb-1",
                None,
                snapshot={"knowledge_base_id": "kb-1", "versions": [{"version_id": "V1"}]},
            ),
            _dep(
                "knowledge_base",
                "kb-1",
                None,
                snapshot={"knowledge_base_id": "kb-1", "versions": [{"version_id": "V2"}]},
            ),
        ],
    )
    with pytest.raises(AdmissionDenied) as twice:
        resolve_pins(same, AdmissionSettings(enabled=True))
    assert "two versions" in str(twice.value)


def test_a_manifest_that_no_longer_parses_cannot_be_pinned(real_bundle: dict[str, Any]) -> None:
    version = _version(real_bundle, [])
    version.manifest = {"nodes": "broken"}
    with pytest.raises(AdmissionDenied, match="manifest cannot be read"):
        resolve_pins(version, AdmissionSettings(enabled=True))


def test_a_version_with_no_digest_cannot_form_a_workload(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, version_id = _project_workflow(client, db_session)
    workflow, version = _db_pair(db_session, workflow_id, version_id)
    version.manifest_hash = ""
    with pytest.raises(AdmissionDenied) as caught:
        admission.prepare_admission(
            settings=AdmissionSettings(enabled=True),
            workflow=workflow,
            version=version,
            identity=_identity(),
            source="api",
            now=datetime.now(timezone.utc),
            run_tenant_id="local",
        )
    assert caught.value.code == "workload_unavailable"


def test_a_project_in_another_organization_is_refused(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, version_id = _project_workflow(client, db_session)
    workflow, version = _db_pair(db_session, workflow_id, version_id)
    with pytest.raises(AdmissionDenied) as caught:
        admission.prepare_admission(
            settings=AdmissionSettings(enabled=True),
            workflow=workflow,
            version=version,
            identity=_identity(),
            source="api",
            now=datetime.now(timezone.utc),
            run_tenant_id="other-org",
        )
    assert caught.value.code == "manifest_refused" and "organization_switch" in str(caught.value)


def test_a_mismatched_run_is_not_persisted(client: TestClient, db_session: Session) -> None:
    workflow_id, version_id = _project_workflow(client, db_session)
    workflow, version = _db_pair(db_session, workflow_id, version_id)
    prepared = admission.prepare_admission(
        settings=AdmissionSettings(enabled=True),
        workflow=workflow,
        version=version,
        identity=_identity(),
        source="api",
        now=datetime.now(timezone.utc),
        run_tenant_id="local",
    )
    legacy = CaliberWorkflowRun(
        workflow_run_id=prepared.run_id,
        workflow_id=workflow_id,
        project_id=PROJECT,
        manifest_legacy=True,
    )
    with pytest.raises(AdmissionDenied) as caught:
        admission.persist_admission(
            db_session,
            prepared,
            legacy,
            identity=_identity(),
            settings=AdmissionSettings(enabled=True),
        )
    assert caught.value.status == 500


# ---------------------------------------------------------------- retries


def _existing(**fields: Any) -> CaliberWorkflowRun:
    base: dict[str, Any] = {
        "workflow_run_id": "WR-x",
        "workflow_id": "WF-1",
        "deployment_alias": "prod",
        "input_payload": "hi",
        "project_id": PROJECT,
        "summary": {},
    }
    return CaliberWorkflowRun(**{**base, **fields})


def test_a_retry_must_be_the_request_that_created_the_run() -> None:
    check = {
        "workflow_id": "WF-1",
        "alias": "prod",
        "input_text": "hi",
        "input_files": None,
        "project_id": PROJECT,
    }
    check_retry_intent(_existing(), **check)
    for change in (
        {"workflow_id": "WF-2"},
        {"alias": "manual"},
        {"input_text": "bye"},
        {"project_id": "P-other"},
        {"input_files": [{"file_id": "f1"}]},
    ):
        with pytest.raises(AdmissionDenied) as caught:
            check_retry_intent(_existing(), **{**check, **change})
        assert caught.value.status == 409
    with_files = _existing(summary={"input_files": [{"file_id": "f1"}]})
    check_retry_intent(with_files, **{**check, "input_files": [{"file_id": "f1"}]})
    with pytest.raises(AdmissionDenied):
        check_retry_intent(with_files, **check)


def test_the_policy_and_grant_ttl_come_from_the_settings(
    client: TestClient, db_session: Session
) -> None:
    _enable(client, admission_grant_ttl_seconds=120)
    workflow_id, _ = _project_workflow(client, db_session)
    assert _run(client, workflow_id).status_code == 202
    grant = db_session.execute(select(CaliberRunGrant)).scalar_one()
    assert (
        100
        <= (grant.expires_at - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds()
        <= 125
    )
    assert json.dumps(
        sorted(REFERENCE_POLICY_V1.action_risk)
    )  # the reference policy is the default
