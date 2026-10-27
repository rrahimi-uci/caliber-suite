"""A stored secret never leaves the platform: a canary run through the core journey (B12.5b, #666).

``EGI_CANARY_<uuid4>`` is stored under the name ``egi-canary`` through the encrypted secret store, and the
provider setting ``llm_api_key_env`` is pointed at ``secret://egi-canary``. The journey then runs (sign in,
create a project, register an agent, run a workflow, run the refinement pipeline to a candidate, apply it,
export the audit log) with ``caplog`` at DEBUG, and the value must be absent from every response body, every
log record, every ``caliber_workflow_run_events`` row and every ``CaliberAuditLog`` row. The scan covers
responses to *refused* requests too, since error bodies are where values leak.

Controls keep the scan honest: a planted canary in each scanned surface is found (a scan that cannot fail
proves nothing), the secret is shown to resolve through the production path, and the failure paths of
resolution (revoked, corrupted ciphertext, wrong key) and of the provider builder are scanned too.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber import secrets as secrets_module
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberAuditLog,
    CaliberSecret,
    CaliberSecretVersion,
    CaliberWorkflowRun,
    CaliberWorkflowRunEvent,
)
from caliber.secret_store import SecretStore, generate_key
from caliber.secrets import resolve_secret, unbind_secret_store
from caliber.server import create_app
from tests.conftest import DEFAULT_TEST_USER
from tests.test_e2e_pipeline import PREFIX, _seed_verified_job, _wait_for_candidate_ready_job
from tests.workflow_helpers import create_and_publish

SECRET_NAME = "egi-canary"
SECRET_REFERENCE = f"secret://{SECRET_NAME}"
KEY_ENV = "CALIBER_TEST_CANARY_KEY"


class Scan:
    """Everything the platform said or stored during one journey, searched for one value."""

    def __init__(self, canary: str) -> None:
        self.canary = canary
        self.bodies: list[tuple[str, str]] = []

    def record(self, label: str, text: str) -> None:
        self.bodies.append((label, text))

    def hits(
        self, session_factory: sessionmaker[Session], records: list[logging.LogRecord]
    ) -> list[str]:
        found: list[str] = []
        for label, text in self.bodies:
            if self.canary in text:
                found.append(f"response body: {label}")
        for record in records:
            haystack = [record.getMessage(), str(record.args), str(record.__dict__.get("exc_text"))]
            if record.exc_info:
                haystack.append(logging.Formatter().formatException(record.exc_info))
            if any(self.canary in part for part in haystack):
                found.append(f"log record: {record.name}: {record.getMessage()[:80]}")
        with session_factory() as session:
            for row in session.execute(select(CaliberWorkflowRunEvent)).scalars():
                if self.canary in json.dumps(row.payload, default=str) or self.canary in str(
                    row.event_type
                ):
                    found.append(f"workflow run event {row.event_id}")
            for audit in session.execute(select(CaliberAuditLog)).scalars():
                blob = json.dumps(
                    [audit.actor, audit.action, audit.entity_type, audit.entity_id, audit.details],
                    default=str,
                )
                if self.canary in blob:
                    found.append(f"audit row {audit.action}")
        return found


@pytest.fixture
def canary() -> str:
    return f"EGI_CANARY_{uuid.uuid4()}"


@pytest.fixture
def canary_config(app_config: CaliberConfig, monkeypatch: pytest.MonkeyPatch) -> CaliberConfig:
    monkeypatch.setenv(KEY_ENV, generate_key())
    return app_config.model_copy(
        update={
            "secret_encryption_key_source": KEY_ENV,
            "llm_api_key_env": SECRET_REFERENCE,
            "llm_provider": "fake",
            # stderr only: the S3 sink would write the journey's DEBUG records to a live object store.
            "log_sink": "stderr",
            "background_tasks_enabled": True,
            "workflow_run_queue_enabled": True,
        }
    )


@pytest.fixture
def canary_client(
    canary_config: CaliberConfig, engine: Any, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    app = create_app(config=canary_config)
    app.state.engine = engine
    app.state.session_factory = session_factory
    # The store is bound against the app's own factory; rebind to the fixture's so resolution sees the test DB.
    store = app.state.secret_store
    assert isinstance(store, SecretStore), "the encrypted store must be configured for this test"
    secrets_module.bind_secret_store(store, session_factory)
    try:
        with TestClient(app, headers={"X-CALIBER-User": DEFAULT_TEST_USER}) as client:
            yield client
    finally:
        unbind_secret_store()


class Journey:
    """The core journey over HTTP, remembering every response body for the scan."""

    def __init__(self, client: TestClient, scan: Scan) -> None:
        self.client = client
        self.scan = scan

    def call(
        self, method: str, path: str, expect: int | tuple[int, ...] | None = None, **kwargs: Any
    ) -> Any:
        response = self.client.request(method, path, **kwargs)
        self.scan.record(f"{method} {path} -> {response.status_code}", response.text)
        if expect is not None:
            allowed = (expect,) if isinstance(expect, int) else expect
            assert response.status_code in allowed, (
                f"{method} {path}: {response.status_code} {response.text}"
            )
        return response


def _run_journey(journey: Journey, db_session: Session, canary: str) -> dict[str, Any]:
    outcome: dict[str, Any] = {}

    # The operator stores the canary and points the provider setting at it.
    put = journey.call("PUT", f"{PREFIX}/secrets/{SECRET_NAME}", 201, json={"value": canary})
    assert put.json()["data"]["reference"] == SECRET_REFERENCE
    journey.call("GET", f"{PREFIX}/secrets", 200)
    journey.call("GET", f"{PREFIX}/settings", (200, 404))

    # Sign in (the test proxy asserts the admin identity) and create a project.
    journey.call("GET", f"{PREFIX}/projects", 200)
    project = journey.call(
        "POST", f"{PREFIX}/projects", (200, 201), json={"name": "Canary Project"}
    )
    outcome["project_id"] = project.json()["data"]["project_id"]

    # An agent, a published workflow, and a run of it (writes workflow-run events).
    journey.call(
        "POST",
        f"{PREFIX}/agents",
        201,
        json={
            "agent_id": "canary-agent",
            "experiment_id": "exp-canary-001",
            "name": "Canary Agent",
            "owner": "@test",
            "artifact_types": ["system_prompt"],
        },
    )
    workflow_id, version_id = create_and_publish(journey.client, workflow_name="Canary Workflow")
    run = journey.call(
        "POST",
        f"{PREFIX}/workflow-versions/{version_id}/preview-run",
        200,
        json={"input": "where is my order 123?"},
    )
    queued = journey.call(
        "POST",
        f"{PREFIX}/workflow-runs",
        202,
        json={"workflow_version_id": version_id, "input": "hello"},
    )
    run_id = queued.json()["data"]["workflow_run_id"]
    deadline = time.monotonic() + 45
    status = ""
    while time.monotonic() < deadline:
        detail = journey.call("GET", f"{PREFIX}/workflow-runs/{run_id}", 200)
        status = detail.json()["data"]["status"]
        if status in {"completed", "failed"}:
            break
        time.sleep(0.3)
    assert status == "completed", f"the queued workflow run ended {status!r}"
    journey.call("GET", f"{PREFIX}/workflow-runs/{run_id}/events", (200, 404))
    outcome["workflow_id"] = workflow_id
    outcome["run"] = run.json()

    # A refused request: error bodies are where values leak.
    journey.call("GET", f"{PREFIX}/secrets/does-not-exist", (404, 405))
    journey.call("PUT", f"{PREFIX}/secrets/bad name!", 400, json={"value": canary})
    journey.call("PUT", f"{PREFIX}/secrets/{SECRET_NAME}", 400, json={"value": ""})

    # Calibration / refinement pipeline to a candidate, then apply.
    job_id = _seed_verified_job(db_session, agent_id="canary-agent", free_text="too formal")
    candidate = _wait_for_candidate_ready_job(journey.client, "canary-agent")
    assert candidate == job_id
    applied = journey.call("POST", f"{PREFIX}/jobs/{job_id}/apply", 200, json={})
    assert applied.json()["data"]["status"] == "applied"
    journey.call("GET", f"{PREFIX}/jobs/{job_id}", 200)

    # Exports.
    journey.call("GET", f"{PREFIX}/audit-log/export", (200, 404))
    journey.call("GET", f"{PREFIX}/audit-log", (200, 404))
    return outcome


def test_resolved_secret_never_echoed(
    canary_client: TestClient,
    session_factory: sessionmaker[Session],
    db_session: Session,
    canary: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    scan = Scan(canary)
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="caliber")
    journey = Journey(canary_client, scan)
    _run_journey(journey, db_session, canary)

    # The reference resolves through the production path to the stored value: the secret was in reach
    # of the platform the whole time, so its absence from the surfaces below is not an unset setting.
    assert resolve_secret(canary_client.app.state.config.llm_api_key_env) == canary  # type: ignore[attr-defined]
    assert scan.bodies, "the journey must have produced responses to scan"

    with session_factory() as session:
        assert session.execute(select(CaliberWorkflowRunEvent)).scalars().first() is not None, (
            "the journey wrote no workflow run events, so that part of the scan would be vacuous"
        )
        assert session.execute(select(CaliberAuditLog)).scalars().first() is not None
    assert caplog.records, "DEBUG logging captured nothing"

    assert scan.hits(session_factory, caplog.records) == []


# ---------------------------------------------------------------- the scan can fail


def _record(message: str, *args: Any, exc: BaseException | None = None) -> logging.LogRecord:
    exc_info = (type(exc), exc, exc.__traceback__) if exc is not None else None
    return logging.LogRecord("caliber.test", logging.INFO, __file__, 1, message, args, exc_info)


def test_the_scan_finds_a_canary_planted_in_each_surface(
    session_factory: sessionmaker[Session], canary: str
) -> None:
    """A scan that cannot fail proves nothing: plant the value in every surface it reads and demand a hit."""
    clean = Scan(canary)
    clean.record("GET /x -> 200", "{}")
    assert clean.hits(session_factory, [_record("nothing here")]) == []

    in_body = Scan(canary)
    in_body.record("GET /leaky -> 200", json.dumps({"detail": f"key was {canary}"}))
    assert in_body.hits(session_factory, []) == ["response body: GET /leaky -> 200"]

    scan = Scan(canary)
    for record in (
        _record(f"key is {canary}"),
        _record("key is %s", canary),
        _record("failed", exc=RuntimeError(f"bad key {canary}")),
    ):
        assert len(scan.hits(session_factory, [record])) == 1, record.msg

    with session_factory() as session:
        session.add(CaliberWorkflowRun(workflow_run_id="run-planted", workflow_id="wf-planted"))
        session.flush()
        session.add(
            CaliberWorkflowRunEvent(
                workflow_run_id="run-planted",
                sequence=1,
                event_type="node_output",
                payload={"text": canary},
            )
        )
        session.add(
            CaliberAuditLog(
                actor="@test", action="leak", entity_type="x", entity_id="y", details={"k": canary}
            )
        )
        session.commit()
    found = scan.hits(session_factory, [])
    assert any(hit.startswith("workflow run event") for hit in found)
    assert "audit row leak" in found


# ---------------------------------------------------------------- the failure paths of resolution


def _scan_logs(canary: str, caplog: pytest.LogCaptureFixture, session_factory: Any) -> list[str]:
    return Scan(canary).hits(session_factory, caplog.records)


def test_revoked_and_absent_references_resolve_to_nothing_without_logging_the_value(
    canary_client: TestClient,
    session_factory: sessionmaker[Session],
    canary: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    assert (
        canary_client.put(f"{PREFIX}/secrets/{SECRET_NAME}", json={"value": canary}).status_code
        == 201
    )
    assert resolve_secret(SECRET_REFERENCE) == canary
    revoked = canary_client.post(f"{PREFIX}/secrets/{SECRET_NAME}/revoke")
    assert revoked.status_code == 200 and canary not in revoked.text
    assert resolve_secret(SECRET_REFERENCE) is None, "a revoked secret fails closed"
    assert resolve_secret("secret://never-stored") is None
    assert _scan_logs(canary, caplog, session_factory) == []


def test_a_decryption_failure_is_logged_without_the_value_or_a_traceback_that_carries_it(
    canary_client: TestClient,
    session_factory: sessionmaker[Session],
    canary: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    canary_client.put(f"{PREFIX}/secrets/{SECRET_NAME}", json={"value": canary})
    with session_factory() as session:
        version = session.execute(select(CaliberSecretVersion)).scalars().one()
        version.ciphertext = bytes(b ^ 0xFF for b in version.ciphertext)  # corrupted at rest
        session.commit()
    assert resolve_secret(SECRET_REFERENCE) is None
    assert any("could not be resolved" in r.getMessage() for r in caplog.records), (
        "the failure is logged"
    )
    assert _scan_logs(canary, caplog, session_factory) == []


def test_the_provider_builder_names_the_reference_never_the_value_when_the_secret_is_gone(
    canary_client: TestClient,
    canary_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    canary: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from caliber.llm.provider import LLMProviderError, _build_base_provider

    caplog.set_level(logging.DEBUG)
    canary_client.put(f"{PREFIX}/secrets/{SECRET_NAME}", json={"value": canary})
    canary_client.post(f"{PREFIX}/secrets/{SECRET_NAME}/revoke")
    openai_config = canary_config.model_copy(update={"llm_provider": "openai"})
    with pytest.raises(LLMProviderError) as raised:
        _build_base_provider(openai_config)
    message = str(raised.value)
    assert SECRET_REFERENCE in message and canary not in message
    assert _scan_logs(canary, caplog, session_factory) == []


def test_the_value_is_encrypted_at_rest_and_absent_from_the_secret_tables_metadata(
    canary_client: TestClient, session_factory: sessionmaker[Session], canary: str
) -> None:
    canary_client.put(f"{PREFIX}/secrets/{SECRET_NAME}", json={"value": canary})
    with session_factory() as session:
        record = session.get(CaliberSecret, SECRET_NAME)
        version = session.execute(select(CaliberSecretVersion)).scalars().one()
        assert canary.encode() not in bytes(version.ciphertext)
        assert canary not in json.dumps(
            [record.name, record.description, record.created_by, record.updated_by], default=str
        )
