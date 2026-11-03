"""B09.3 durable model/read observations and replay integrity tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy.orm import Session

from caliber.db.models import CaliberStepObservation, CaliberWorkflowRun
from caliber.replay import (
    ObservationConflictError,
    ObservationError,
    ObservationIntegrityError,
    ObservationNotFoundError,
    ObservationRecord,
    ObservationRecorder,
    ObservationScopeError,
    ObservationValidationError,
    list_observation_rows,
    read_observation,
    record_observation,
    request_digest,
)
from caliber.storage import LocalStorageBackend
from caliber.storage.base import StorageConflictError, StorageError
from caliber.workflows.compiler import build_ir
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, execute
from tests.workflow_helpers import fake_resolver, make_manifest


def _backend(tmp_path: Path) -> LocalStorageBackend:
    return LocalStorageBackend(f"file://{tmp_path}")


def _seed_run(db_session: Session, run_id: str = "WR-OBS") -> None:
    db_session.add(CaliberWorkflowRun(workflow_run_id=run_id, workflow_id="workflow-obs"))
    db_session.flush()


def _record(
    db_session: Session,
    backend: LocalStorageBackend,
    *,
    occurrence_id: str = "agent:0",
    project_id: str | None = None,
    request: object = None,
    content: object = b"answer",
) -> ObservationRecord:
    return record_observation(
        db_session,
        backend,
        workflow_run_id="WR-OBS",
        project_id=project_id,
        principal="@operator",
        node_id="agent",
        occurrence_id=occurrence_id,
        attempt=1,
        observation_kind="model",
        request={"prompt": "hello"} if request is None else request,
        content=content,
        provider="test-provider",
        provider_version="test-v1",
    )


def test_every_call_recorded(db_session: Session, tmp_path: Path) -> None:
    """The runtime emits one model observation and each observable tool read."""

    _seed_run(db_session)
    backend = _backend(tmp_path)
    recorder = ObservationRecorder(
        session=db_session,
        backend=backend,
        workflow_run_id="WR-OBS",
        project_id=None,
        principal="@operator",
    )
    manifest = make_manifest()
    manifest["nodes"]["agent"]["tools"] = ["lookup_policy"]
    manifest["tools"] = {
        "lookup_policy": {
            "registry_ref": "tool.lookup_policy.v1",
            "version_constraint": ">=1.0,<2.0",
        }
    }
    resolver = fake_resolver()
    plan = RuntimePlan(
        ir=build_ir(parse_manifest(manifest), resolver, version="7"),
        resolver=resolver,
        observation_recorder=recorder,
    )

    result = execute(plan, "hello", executor=FakeWorkflowExecutor())
    db_session.commit()

    assert result.status == "completed"
    rows = list_observation_rows(
        db_session,
        workflow_run_id="WR-OBS",
        project_id=None,
        principal="@operator",
    )
    assert [row.observation_kind for row in rows].count("model") == 1
    reads = [row for row in rows if row.observation_kind == "read"]
    assert len(reads) == 1
    assert reads[0].occurrence_id.endswith(":read:0")
    assert all(row.content is None for row in rows)


def test_identical_requests_at_distinct_occurrences_are_distinct(
    db_session: Session, tmp_path: Path
) -> None:
    _seed_run(db_session)
    backend = _backend(tmp_path)
    first = _record(db_session, backend, occurrence_id="loop:0")
    second = _record(db_session, backend, occurrence_id="loop:1")
    db_session.commit()

    assert first.observation_id != second.observation_id
    rows = db_session.query(CaliberStepObservation).all()
    assert len(rows) == 2
    assert {row.occurrence_id for row in rows} == {"loop:0", "loop:1"}


def test_repeating_the_same_observation_is_idempotent(db_session: Session, tmp_path: Path) -> None:
    _seed_run(db_session)
    backend = _backend(tmp_path)
    first = _record(db_session, backend)
    second = _record(db_session, backend)
    db_session.commit()

    assert first.observation_id == second.observation_id
    assert db_session.query(CaliberStepObservation).count() == 1


def test_changed_request_at_an_occurrence_is_rejected(db_session: Session, tmp_path: Path) -> None:
    _seed_run(db_session)
    backend = _backend(tmp_path)
    _record(db_session, backend)

    with pytest.raises(ObservationConflictError):
        _record(db_session, backend, request={"prompt": "changed"})


def test_observation_requires_an_existing_run_and_matching_project_scope(
    db_session: Session, tmp_path: Path
) -> None:
    backend = _backend(tmp_path)
    with pytest.raises(ObservationNotFoundError):
        record_observation(
            db_session,
            backend,
            workflow_run_id="WR-MISSING",
            project_id=None,
            principal="@operator",
            node_id="agent",
            occurrence_id="agent:0",
            attempt=1,
            observation_kind="model",
            request={"prompt": "hello"},
            content=b"answer",
            provider="test-provider",
            provider_version="test-v1",
        )

    _seed_run(db_session)
    db_session.get(CaliberWorkflowRun, "WR-OBS").project_id = "PRJ-1"
    with pytest.raises(ObservationScopeError):
        _record(db_session, backend, occurrence_id="wrong-project", request={"project": "other"})


def test_tampering_is_detected_before_replay(db_session: Session, tmp_path: Path) -> None:
    _seed_run(db_session)
    backend = _backend(tmp_path)
    record = _record(db_session, backend)
    db_session.commit()
    object_path = tmp_path / record.content_ref
    object_path.write_bytes(b"tampered")

    with pytest.raises(ObservationIntegrityError):
        read_observation(
            db_session,
            backend,
            observation_id=record.observation_id,
            project_id=None,
            principal="@operator",
        )


def test_foreign_scope_is_rejected_before_storage_fetch(
    db_session: Session, tmp_path: Path
) -> None:
    _seed_run(db_session)
    backend = _backend(tmp_path)
    record = _record(db_session, backend)
    db_session.commit()
    original_read = backend.read_bytes
    backend.read_bytes = Mock(wraps=original_read)  # type: ignore[method-assign]

    with pytest.raises(ObservationScopeError):
        read_observation(
            db_session,
            backend,
            observation_id=record.observation_id,
            project_id="PRJ-foreign",
            principal="@operator",
        )

    backend.read_bytes.assert_not_called()  # type: ignore[attr-defined]


def test_read_returns_verified_bytes_and_list_is_metadata_only(
    db_session: Session, tmp_path: Path
) -> None:
    _seed_run(db_session)
    backend = _backend(tmp_path)
    record = _record(db_session, backend, content={"answer": "ok"})
    db_session.commit()

    restored = read_observation(
        db_session,
        backend,
        observation_id=record.observation_id,
        project_id=None,
        principal="@operator",
    )
    listed = list_observation_rows(
        db_session,
        workflow_run_id="WR-OBS",
        project_id=None,
        principal="@operator",
    )

    assert restored.content == b'{"answer":"ok"}'
    assert listed[0].content is None


def test_list_observation_rows_bounds_and_selects_metadata(
    db_session: Session, tmp_path: Path
) -> None:
    _seed_run(db_session)
    backend = _backend(tmp_path)
    record = _record(db_session, backend)
    db_session.commit()

    selected = list_observation_rows(
        db_session,
        workflow_run_id="WR-OBS",
        project_id=None,
        principal="@operator",
        observation_ids=[record.observation_id],
        limit=1,
    )
    assert [item.observation_id for item in selected] == [record.observation_id]
    assert selected[0].content is None

    with pytest.raises(ObservationValidationError):
        list_observation_rows(
            db_session,
            workflow_run_id="WR-OBS",
            project_id=None,
            principal="@operator",
            observation_ids=[],
        )
    with pytest.raises(ObservationValidationError):
        list_observation_rows(
            db_session,
            workflow_run_id="WR-OBS",
            project_id=None,
            principal="@operator",
            observation_ids=[1],  # type: ignore[list-item]
        )
    with pytest.raises(ObservationValidationError):
        list_observation_rows(
            db_session,
            workflow_run_id="WR-OBS",
            project_id=None,
            principal="@operator",
            limit=0,
        )


@pytest.mark.parametrize(
    ("request_value", "content_kind"),
    [
        (object(), "bytes"),
        ({"value": float("nan")}, "bytes"),
        ({"prompt": "hello"}, "object"),
        ({"prompt": "hello"}, "surrogate"),
    ],
)
def test_invalid_json_and_content_are_rejected(
    db_session: Session, tmp_path: Path, request_value: object, content_kind: str
) -> None:
    _seed_run(db_session)
    content: object = {
        "bytes": b"answer",
        "object": object(),
        "surrogate": "\ud800",
    }[content_kind]
    with pytest.raises(ObservationValidationError):
        _record(db_session, _backend(tmp_path), request=request_value, content=content)


def test_validation_covers_scope_attempt_kind_and_text_guards(
    db_session: Session, tmp_path: Path
) -> None:
    _seed_run(db_session)
    backend = _backend(tmp_path)
    base = {
        "session": db_session,
        "backend": backend,
        "workflow_run_id": "WR-OBS",
        "project_id": None,
        "principal": "@operator",
        "node_id": "agent",
        "occurrence_id": "invalid",
        "attempt": 1,
        "observation_kind": "model",
        "request": {"prompt": "hello"},
        "content": b"answer",
        "provider": "test-provider",
        "provider_version": "test-v1",
    }
    for field, value in (
        ("project_id", " "),
        ("attempt", 0),
        ("observation_kind", "other"),
        ("provider", ""),
        ("principal", "operator\nname"),
    ):
        values = dict(base)
        values[field] = value
        with pytest.raises(ObservationValidationError):
            record_observation(**values)

    assert request_digest({"b": 2, "a": 1}) == request_digest({"a": 1, "b": 2})
    byte_record = _record(
        db_session, backend, occurrence_id="bytearray", content=bytearray(b"bytes")
    )
    text_record = _record(db_session, backend, occurrence_id="text", content="text")
    assert byte_record.content == b"bytes"
    assert text_record.content == b"text"


@pytest.mark.parametrize("error", [StorageConflictError("conflict"), StorageError("down")])
def test_storage_write_failures_are_not_silenced(
    db_session: Session, tmp_path: Path, error: StorageError
) -> None:
    _seed_run(db_session)
    backend = Mock()
    backend.name = "local"
    backend.write_bytes.side_effect = error
    with pytest.raises(
        ObservationConflictError if isinstance(error, StorageConflictError) else ObservationError
    ):
        _record(db_session, backend, occurrence_id=f"write-{type(error).__name__}")


def test_read_refuses_wrong_backend_and_missing_object(db_session: Session, tmp_path: Path) -> None:
    _seed_run(db_session)
    backend = _backend(tmp_path)
    record = _record(db_session, backend)
    db_session.commit()

    backend.name = "s3"  # type: ignore[misc]
    with pytest.raises(ObservationIntegrityError):
        read_observation(
            db_session,
            backend,
            observation_id=record.observation_id,
            project_id=None,
            principal="@operator",
        )
    backend.name = "local"  # type: ignore[misc]
    backend.read_bytes = Mock(side_effect=StorageError("unavailable"))  # type: ignore[method-assign]
    with pytest.raises(ObservationIntegrityError):
        read_observation(
            db_session,
            backend,
            observation_id=record.observation_id,
            project_id=None,
            principal="@operator",
        )


def test_recorder_rejects_incomplete_events(db_session: Session, tmp_path: Path) -> None:
    _seed_run(db_session)
    recorder = ObservationRecorder(
        session=db_session,
        backend=_backend(tmp_path),
        workflow_run_id="WR-OBS",
        project_id=None,
        principal="@operator",
    )
    with pytest.raises(ObservationValidationError):
        recorder({"node_id": "agent"})
