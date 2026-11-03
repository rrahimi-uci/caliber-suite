"""Durable, scoped model/read observations for offline replay (B09.3, #799).

Observation bytes are stored in the configured :class:`StorageBackend`; SQL stores only the immutable index and
the digests needed to find and verify them. The API deliberately performs the project/principal scope lookup before
calling ``read_bytes``. A caller that names a foreign observation therefore cannot learn whether its object exists,
and a changed object is never returned as replay input.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.db.models import CaliberStepObservation, CaliberWorkflowRun
from caliber.ids import new_step_observation_id
from caliber.storage.base import StorageBackend, StorageConflictError, StorageError

ObservationKind = Literal["model", "read"]
_CONTROL_CHAR_MIN = 0x20


class ObservationError(RuntimeError):
    """Base class for durable-observation failures."""


class ObservationValidationError(ObservationError, ValueError):
    """The observation identity or payload is not a supported v1 value."""


class ObservationConflictError(ObservationError):
    """The same durable occurrence was recorded with different content or metadata."""


class ObservationNotFoundError(ObservationError):
    """No observation visible to the supplied scope exists."""


class ObservationScopeError(ObservationNotFoundError):
    """The observation exists outside the caller's project/principal scope."""


class ObservationIntegrityError(ObservationError):
    """The storage object no longer matches its immutable SQL metadata."""


@dataclass(frozen=True)
class ObservationRecord:
    """A scoped observation index row, optionally carrying the verified bytes."""

    observation_id: str
    workflow_run_id: str
    project_id: str | None
    principal: str
    node_id: str
    occurrence_id: str
    attempt: int
    observation_kind: ObservationKind
    request_digest: str
    content_hash: str
    content_size: int
    storage_backend: str
    content_ref: str
    object_version_id: str | None
    provider: str
    provider_version: str
    created_at: Any
    content: bytes | None = None


def _canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ObservationValidationError("observation request must be finite JSON") from exc


def request_digest(request: object) -> str:
    """Return the lowercase SHA-256 of a canonical request value."""

    return hashlib.sha256(_canonical_bytes(request)).hexdigest()


def _content_bytes(content: object) -> bytes:
    if isinstance(content, bytes):
        return content
    if isinstance(content, bytearray):
        return bytes(content)
    if isinstance(content, str):
        try:
            return content.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ObservationValidationError("observation content must be valid UTF-8") from exc
    if isinstance(content, (Mapping, Sequence)) and not isinstance(
        content, (str, bytes, bytearray)
    ):
        return _canonical_bytes(content)
    raise ObservationValidationError("observation content must be bytes, text, or JSON")


def _text(name: str, value: object, limit: int) -> str:
    if type(value) is not str or not value or value != value.strip() or len(value) > limit:
        raise ObservationValidationError(
            f"{name} must be a non-empty trimmed string of at most {limit} characters"
        )
    if any(ord(char) < _CONTROL_CHAR_MIN for char in value):
        raise ObservationValidationError(f"{name} must not contain control characters")
    return value


def _kind(value: object) -> ObservationKind:
    if value not in {"model", "read"}:
        raise ObservationValidationError("observation_kind must be 'model' or 'read'")
    return value


def _storage_key(
    *,
    project_id: str | None,
    workflow_run_id: str,
    node_id: str,
    occurrence_id: str,
    attempt: int,
    digest: str,
) -> str:
    def component(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]

    scope = component(project_id or "global")
    return f"replay/observations/{scope}/{component(workflow_run_id)}/{component(node_id)}/{component(occurrence_id)}/{attempt}/{digest}.bin"


def _record(row: CaliberStepObservation, *, content: bytes | None = None) -> ObservationRecord:
    return ObservationRecord(
        observation_id=row.observation_id,
        workflow_run_id=row.workflow_run_id,
        project_id=row.project_id,
        principal=row.principal,
        node_id=row.node_id,
        occurrence_id=row.occurrence_id,
        attempt=row.attempt,
        observation_kind=cast(ObservationKind, row.observation_kind),
        request_digest=row.request_digest,
        content_hash=row.content_hash,
        content_size=row.content_size,
        storage_backend=row.storage_backend,
        content_ref=row.content_ref,
        object_version_id=row.object_version_id,
        provider=row.provider,
        provider_version=row.provider_version,
        created_at=row.created_at,
        content=content,
    )


def _existing_row(
    session: Session,
    *,
    workflow_run_id: str,
    node_id: str,
    occurrence_id: str,
    attempt: int,
    observation_kind: ObservationKind,
) -> CaliberStepObservation | None:
    return session.scalar(
        select(CaliberStepObservation).where(
            CaliberStepObservation.workflow_run_id == workflow_run_id,
            CaliberStepObservation.node_id == node_id,
            CaliberStepObservation.occurrence_id == occurrence_id,
            CaliberStepObservation.attempt == attempt,
            CaliberStepObservation.observation_kind == observation_kind,
        )
    )


def record_observation(
    session: Session,
    backend: StorageBackend,
    *,
    workflow_run_id: str,
    project_id: str | None,
    principal: str,
    node_id: str,
    occurrence_id: str,
    attempt: int,
    observation_kind: ObservationKind,
    request: object,
    content: object,
    provider: str,
    provider_version: str,
) -> ObservationRecord:
    """Persist one observation in the caller's transaction without committing it.

    Repeating the exact durable occurrence/request is idempotent. A changed request, content, scope or provider
    under that occurrence is a conflict and never overwrites the original bytes.
    """

    run_id = _text("workflow_run_id", workflow_run_id, 64)
    node = _text("node_id", node_id, 128)
    occurrence = _text("occurrence_id", occurrence_id, 128)
    actor = _text("principal", principal, 256)
    if project_id is not None:
        project_id = _text("project_id", project_id, 64)
    run = session.get(CaliberWorkflowRun, run_id)
    if run is None:
        raise ObservationNotFoundError(f"workflow run {run_id!r} does not exist")
    if run.project_id != project_id:
        raise ObservationScopeError("observation project scope does not match its workflow run")
    if type(attempt) is not int or attempt < 1:
        raise ObservationValidationError("attempt must be a positive integer")
    kind = _kind(observation_kind)
    provider_name = _text("provider", provider, 128)
    provider_release = _text("provider_version", provider_version, 128)
    digest = request_digest(request)
    payload = _content_bytes(content)
    content_hash = hashlib.sha256(payload).hexdigest()
    existing = _existing_row(
        session,
        workflow_run_id=run_id,
        node_id=node,
        occurrence_id=occurrence,
        attempt=attempt,
        observation_kind=kind,
    )
    if existing is not None:
        if (
            existing.request_digest != digest
            or existing.project_id != project_id
            or existing.principal != actor
            or existing.content_hash != content_hash
            or existing.content_size != len(payload)
            or existing.provider != provider_name
            or existing.provider_version != provider_release
        ):
            raise ObservationConflictError(
                "durable observation already exists with different content or provenance"
            )
        return _record(existing, content=payload)

    key = _storage_key(
        project_id=project_id,
        workflow_run_id=run_id,
        node_id=node,
        occurrence_id=occurrence,
        attempt=attempt,
        digest=digest,
    )
    try:
        metadata = backend.write_bytes(
            key,
            payload,
            media_type="application/octet-stream",
            metadata={"sha256": content_hash, "kind": kind},
            overwrite=False,
        )
    except StorageConflictError as exc:
        raise ObservationConflictError(
            "observation object already exists without a matching index row"
        ) from exc
    except StorageError as exc:
        raise ObservationError("observation bytes could not be stored") from exc

    row = CaliberStepObservation(
        observation_id=new_step_observation_id(),
        workflow_run_id=run_id,
        project_id=project_id,
        principal=actor,
        node_id=node,
        occurrence_id=occurrence,
        attempt=attempt,
        observation_kind=kind,
        request_digest=digest,
        content_hash=content_hash,
        content_size=len(payload),
        storage_backend=backend.name,
        content_ref=key,
        object_version_id=metadata.object_version_id,
        provider=provider_name,
        provider_version=provider_release,
    )
    session.add(row)
    session.flush()
    return _record(row, content=payload)


def _scoped_row(
    session: Session,
    *,
    observation_id: str,
    project_id: str | None,
    principal: str,
) -> CaliberStepObservation:
    row = session.scalar(
        select(CaliberStepObservation).where(
            CaliberStepObservation.observation_id == observation_id,
            CaliberStepObservation.project_id.is_(project_id)
            if project_id is None
            else CaliberStepObservation.project_id == project_id,
            CaliberStepObservation.principal == principal,
        )
    )
    if row is None:
        raise ObservationScopeError(
            "observation is not visible in the supplied project/principal scope"
        )
    return row


def read_observation(
    session: Session,
    backend: StorageBackend,
    *,
    observation_id: str,
    project_id: str | None,
    principal: str,
) -> ObservationRecord:
    """Read and verify one observation, checking scope before touching storage."""

    actor = _text("principal", principal, 256)
    row = _scoped_row(
        session, observation_id=observation_id, project_id=project_id, principal=actor
    )
    if backend.name != row.storage_backend:
        raise ObservationIntegrityError("observation is bound to a different storage backend")
    try:
        payload = backend.read_bytes(row.content_ref)
    except StorageError as exc:
        raise ObservationIntegrityError("observation bytes are unavailable") from exc
    if len(payload) != row.content_size or hashlib.sha256(payload).hexdigest() != row.content_hash:
        raise ObservationIntegrityError("observation bytes do not match the recorded content hash")
    return _record(row, content=payload)


def list_observation_rows(
    session: Session,
    *,
    workflow_run_id: str,
    project_id: str | None,
    principal: str,
    observation_ids: Sequence[str] | None = None,
    limit: int | None = None,
) -> list[ObservationRecord]:
    """List only index rows visible to the supplied scope; no object bytes are fetched.

    ``observation_ids`` and ``limit`` are metadata-query bounds for consumers such as historical replay. They do not
    broaden the scope predicate and deliberately happen before any object bytes are read.
    """

    actor = _text("principal", principal, 256)
    run_id = _text("workflow_run_id", workflow_run_id, 64)
    if observation_ids is not None:
        ids = tuple(observation_ids)
        if not ids:
            raise ObservationValidationError("observation_ids must not be empty")
        if any(type(item) is not str or not item for item in ids):
            raise ObservationValidationError("observation_ids must contain non-empty strings")
    else:
        ids = ()
    if limit is not None and (type(limit) is not int or limit < 1):
        raise ObservationValidationError("limit must be a positive integer")
    scope = (
        CaliberStepObservation.project_id.is_(project_id)
        if project_id is None
        else CaliberStepObservation.project_id == project_id
    )
    statement = (
        select(CaliberStepObservation)
        .where(
            CaliberStepObservation.workflow_run_id == run_id,
            scope,
            CaliberStepObservation.principal == actor,
        )
        .order_by(
            CaliberStepObservation.node_id,
            CaliberStepObservation.occurrence_id,
            CaliberStepObservation.attempt,
            CaliberStepObservation.observation_id,
        )
    )
    if ids:
        statement = statement.where(CaliberStepObservation.observation_id.in_(ids))
    if limit is not None:
        statement = statement.limit(limit)
    rows = session.scalars(statement).all()
    return [_record(row) for row in rows]


@dataclass
class ObservationRecorder:
    """Runtime callback bound to one run, scope and storage backend."""

    session: Session
    backend: StorageBackend
    workflow_run_id: str
    project_id: str | None
    principal: str

    def __call__(self, event: Mapping[str, Any]) -> ObservationRecord:
        required = ("node_id", "occurrence_id", "attempt", "observation_kind", "request", "content")
        missing = [key for key in required if key not in event]
        if missing:
            raise ObservationValidationError(f"observation event is missing {', '.join(missing)}")
        return record_observation(
            self.session,
            self.backend,
            workflow_run_id=self.workflow_run_id,
            project_id=self.project_id,
            principal=self.principal,
            node_id=event["node_id"],
            occurrence_id=event["occurrence_id"],
            attempt=event["attempt"],
            observation_kind=event["observation_kind"],
            request=event["request"],
            content=event["content"],
            provider=event.get("provider", "unknown"),
            provider_version=event.get("provider_version", "unknown"),
        )


__all__ = [
    "ObservationConflictError",
    "ObservationError",
    "ObservationIntegrityError",
    "ObservationKind",
    "ObservationNotFoundError",
    "ObservationRecord",
    "ObservationRecorder",
    "ObservationScopeError",
    "ObservationValidationError",
    "list_observation_rows",
    "read_observation",
    "record_observation",
    "request_digest",
]
