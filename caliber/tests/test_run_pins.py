"""The worker executes what a run was pinned to, or fails it by name (B02.2b, #683)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import (
    CaliberProject,
    CaliberProjectMember,
    CaliberRunManifest,
    CaliberWorkflowRun,
    CaliberWorkflowVersion,
)
from caliber.orchestrator.workflow_run_worker import WorkflowRunWorker
from caliber.workflows import run_pins
from caliber.workflows.run_pins import PinsError, load_run_manifest, verify_run_pins
from tests.workflow_helpers import (
    PREFIX,
    create_draft,
    create_workflow,
    make_support_manifest,
    register_demo_tools,
)

PROJECT = "P-pins"


def _manifest(workflow_id: str, text: str = "Help the customer.") -> dict[str, Any]:
    manifest = make_support_manifest(workflow_id)
    manifest["artifacts"] = {}
    manifest["nodes"]["support_agent"]["instructions"] = {"type": "inline", "text": text}
    return manifest


def _setup(client: TestClient, db: Session, *, admission: bool = True) -> tuple[str, str]:
    client.app.state.config = client.app.state.config.model_copy(
        update={"workflow_run_queue_enabled": True, "admission_enabled": admission}
    )
    db.add(CaliberProject(project_id=PROJECT, name="pins", owner="@test"))
    db.flush()
    db.add(
        CaliberProjectMember(
            member_id="M-pins",
            project_id=PROJECT,
            user_id="@test",
            role="owner",
            created_by="@test",
        )
    )
    db.commit()
    client.headers.update({"X-CALIBER-Project": PROJECT})
    register_demo_tools(client)
    workflow_id = create_workflow(client, "Pins WF")
    version_id, _ = create_draft(client, workflow_id, _manifest(workflow_id))
    assert client.post(f"{PREFIX}/workflow-versions/{version_id}/publish").status_code == 200
    return workflow_id, version_id


def _worker(client: TestClient) -> WorkflowRunWorker:
    return WorkflowRunWorker(
        session_factory=client.app.state.session_factory,
        config=client.app.state.config,
        event_bus=getattr(client.app.state, "event_bus", None),
    )


def _submit(client: TestClient, workflow_id: str, key: str = "k") -> str:
    created = client.post(
        f"{PREFIX}/workflow-runs",
        json={"workflow_id": workflow_id, "alias": "manual", "input": "hi", "idempotency_key": key},
    )
    assert created.status_code == 202, created.text
    return str(created.json()["data"]["workflow_run_id"])


def _run(db: Session, run_id: str) -> CaliberWorkflowRun:
    db.expire_all()
    run = db.get(CaliberWorkflowRun, run_id)
    assert run is not None
    return run


def test_an_admitted_run_executes_and_keeps_what_admission_recorded(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, _ = _setup(client, db_session)
    run_id = _submit(client, workflow_id)
    _worker(client)._tick()
    run = _run(db_session, run_id)
    assert run.status == "completed", (run.error_code, run.error_summary)
    summary = run.summary or {}
    assert summary["admitted"] is True and summary["manifest_id"]
    assert summary["admission_intent"]["workflow_id"] == workflow_id, (
        "kept through the worker's summary rebuild"
    )
    assert summary["authoring_manifest_hash"]


def test_a_legacy_run_keeps_todays_path(client: TestClient, db_session: Session) -> None:
    workflow_id, _ = _setup(client, db_session, admission=False)
    run_id = _submit(client, workflow_id)
    _worker(client)._tick()
    run = _run(db_session, run_id)
    assert run.manifest_legacy is True and run.status == "completed"


def test_a_version_recompiled_after_admission_fails_the_run_by_name(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, version_id = _setup(client, db_session)
    run_id = _submit(client, workflow_id)
    version = db_session.get(CaliberWorkflowVersion, version_id)
    assert version is not None
    from caliber.workflows.deployment_bundle import seal_bundle

    bundle = dict(version.compiled_bundle["deployment_bundle"])
    bundle["portability"] = {"changed": True}  # any change re-seals to another digest
    version.compiled_bundle = {**version.compiled_bundle, "deployment_bundle": seal_bundle(bundle)}
    db_session.commit()
    _worker(client)._tick()
    run = _run(db_session, run_id)
    assert (run.status, run.error_code) == ("failed", "pins_changed"), run.error_summary
    assert "recompiled" in (run.error_summary or "")


def test_a_version_that_no_longer_exists_fails_the_run(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, version_id = _setup(client, db_session)
    run_id = _submit(client, workflow_id)
    version = db_session.get(CaliberWorkflowVersion, version_id)
    assert version is not None
    version.compiled_bundle = None
    db_session.commit()
    _worker(client)._tick()
    assert _run(db_session, run_id).error_code == "pins_changed"


def test_a_snapshot_that_is_not_the_pinned_bundles_fails_the_run(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, _ = _setup(client, db_session)
    run_id = _submit(client, workflow_id)
    run = _run(db_session, run_id)
    snapshot = dict(run.manifest_snapshot)
    snapshot["name"] = "swapped after admission"
    run.manifest_snapshot = snapshot
    db_session.commit()
    _worker(client)._tick()
    assert _run(db_session, run_id).error_code == "pins_mismatch"


def test_another_provider_is_not_a_pinned_model_route(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, _ = _setup(client, db_session)
    run_id = _submit(client, workflow_id)
    client.app.state.config = client.app.state.config.model_copy(update={"llm_provider": "openai"})
    _worker(client)._tick()
    run = _run(db_session, run_id)
    assert (run.status, run.error_code) == ("failed", "model_route_changed"), run.error_summary


def test_a_different_workflow_version_than_the_pinned_one_fails(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, _ = _setup(client, db_session)
    run_id = _submit(client, workflow_id)
    second, _ = create_draft(client, workflow_id, _manifest(workflow_id, "another"))
    assert client.post(f"{PREFIX}/workflow-versions/{second}/publish").status_code == 200
    run = _run(db_session, run_id)
    run.workflow_version_id = second
    db_session.commit()
    _worker(client)._tick()
    assert _run(db_session, run_id).error_code == "pins_mismatch"


def test_an_admitted_run_without_a_manifest_is_never_dispatched(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, version_id = _setup(client, db_session)
    db_session.add(
        CaliberWorkflowRun(
            workflow_run_id="WR-partial",
            workflow_id=workflow_id,
            project_id=PROJECT,
            workflow_version_id=version_id,
            status="queued",
            manifest_legacy=False,
            input_payload="x",
        )
    )
    db_session.commit()
    _worker(client)._tick()
    run = _run(db_session, "WR-partial")
    assert (run.status, run.error_code) == ("failed", "manifest_missing")


def test_a_manifest_that_does_not_read_back_or_names_another_run_fails(
    client: TestClient, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    workflow_id, _ = _setup(client, db_session)
    run_id = _submit(client, workflow_id)
    assert load_run_manifest(db_session, run_id) is not None
    assert load_run_manifest(db_session, "WR-none") is None

    from caliber.run_manifest import ManifestError

    def unreadable(*_a: Any, **_k: Any) -> Any:
        raise ManifestError("invalid_manifest", "bytes changed")

    with monkeypatch.context() as patch:
        patch.setattr(run_pins, "read_manifest", unreadable)
        with pytest.raises(PinsError) as caught:
            load_run_manifest(db_session, run_id)
    assert caught.value.code == "manifest_corrupt"

    real = run_pins.read_manifest
    with monkeypatch.context() as patch:
        patch.setattr(
            run_pins,
            "read_manifest",
            lambda raw, digest=None: {**real(raw, digest), "workflow_run_id": "WR-other"},
        )
        with pytest.raises(PinsError, match="another run"):
            load_run_manifest(db_session, run_id)


def test_verify_returns_the_pins_for_an_intact_run_and_none_for_a_legacy_one(
    client: TestClient, db_session: Session
) -> None:
    workflow_id, version_id = _setup(client, db_session)
    run_id = _submit(client, workflow_id)
    run = _run(db_session, run_id)
    version = db_session.get(CaliberWorkflowVersion, version_id)
    pins = verify_run_pins(db_session, run, version, run.manifest_snapshot, llm_provider="fake")
    assert pins is not None and pins.pins["workflow_version"] == version_id
    assert pins.manifest_id == (run.summary or {})["manifest_id"]
    legacy = CaliberWorkflowRun(
        workflow_run_id="WR-leg", workflow_id=workflow_id, manifest_legacy=True
    )
    assert verify_run_pins(db_session, legacy, version, None, llm_provider="fake") is None
    assert db_session.query(CaliberRunManifest).count() == 1
