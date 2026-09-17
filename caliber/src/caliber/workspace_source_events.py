"""Durable source-provider webhook and identity-link operations.

This module is the persistence half of the P4-E boundary.  Adapters verify
signatures and normalize provider values; this service stores only the
normalized delivery envelope and provider-to-CALIBER identity link.  It never
stores raw webhook bodies or credentials and does not call a provider.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.db.models import (
    CaliberWorkspaceSource,
    CaliberWorkspaceSourceActorLink,
    CaliberWorkspaceSourceEvent,
)
from caliber.ids import new_workspace_source_actor_link_id, new_workspace_source_event_id
from caliber.workspace_source_control import SourceControlWebhook

SOURCE_EVENT_RECEIVED = "received"
SOURCE_EVENT_PROCESSED = "processed"
SOURCE_EVENT_FAILED = "failed"
SOURCE_EVENT_TERMINAL = frozenset({SOURCE_EVENT_PROCESSED, SOURCE_EVENT_FAILED})
ACTOR_LINK_ACTIVE = "active"
ACTOR_LINK_REVOKED = "revoked"
MAX_DELIVERY_ID_CHARS = 256
MAX_EVENT_TYPE_CHARS = 128
MAX_PROVIDER_SUBJECT_CHARS = 256
MAX_USER_ID_CHARS = 256
MAX_ERROR_CODE_CHARS = 64
MAX_ERROR_SUMMARY_CHARS = 2048


class WorkspaceSourceEventError(RuntimeError):
    """Base error for source-event persistence operations."""

    code = "workspace_source_event_error"


class WorkspaceSourceEventNotFoundError(WorkspaceSourceEventError):
    """The requested source event does not exist."""

    code = "workspace_source_event_not_found"


class WorkspaceSourceEventConflictError(WorkspaceSourceEventError):
    """A delivery identity was reused with different normalized evidence."""

    code = "workspace_source_event_conflict"


class WorkspaceSourceEventTransitionError(WorkspaceSourceEventError):
    """The source event cannot enter the requested processing state."""

    code = "invalid_workspace_source_event_transition"


class WorkspaceSourceActorLinkError(RuntimeError):
    """Base error for provider actor-link operations."""

    code = "workspace_source_actor_link_error"


class WorkspaceSourceActorLinkNotFoundError(WorkspaceSourceActorLinkError):
    """The requested actor link does not exist."""

    code = "workspace_source_actor_link_not_found"


class WorkspaceSourceActorLinkConflictError(WorkspaceSourceActorLinkError):
    """A provider subject is already linked to another active user."""

    code = "workspace_source_actor_link_conflict"


def _utc_naive(now: datetime | None) -> datetime:
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone(timezone.utc).replace(tzinfo=None)
    return stamp


def _bounded(value: str, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    result = value.strip()
    if len(result) > maximum:
        raise ValueError(f"{field} must be at most {maximum} characters")
    return result


def _optional_bounded(value: str | None, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    return _bounded(value, field, maximum)


def _source(
    session: Session, source_id: str, project_id: str | None = None
) -> CaliberWorkspaceSource:
    source = session.get(CaliberWorkspaceSource, source_id)
    if source is None or (project_id is not None and source.project_id != project_id):
        raise WorkspaceSourceEventNotFoundError(f"source {source_id!r} was not found")
    return source


def _assert_event_matches_source(
    source: CaliberWorkspaceSource, event: SourceControlWebhook
) -> None:
    if not event.signature_verified:
        raise WorkspaceSourceEventConflictError(
            "only signature-verified source events may enter the durable inbox"
        )
    if event.repository_id != source.canonical_repository_id:
        raise WorkspaceSourceEventConflictError(
            "source event repository does not match the configured source binding"
        )


def _same_event(row: CaliberWorkspaceSourceEvent, event: SourceControlWebhook) -> bool:
    return (
        row.event_type == event.event_type
        and row.repository_id == event.repository_id
        and row.payload_sha256 == event.payload_sha256
    )


def record_source_event(
    session: Session,
    *,
    source_id: str,
    event: SourceControlWebhook,
    now: datetime | None = None,
) -> tuple[CaliberWorkspaceSourceEvent, bool]:
    """Insert one verified delivery or return its identical prior row.

    The second return value is ``True`` only when this call inserted the row.
    A unique ``(source_id, provider_delivery_id)`` constraint protects the
    read-then-insert sequence when two webhook workers race.
    """

    source = _source(session, source_id)
    _assert_event_matches_source(source, event)
    delivery_id = _bounded(event.delivery_id, "delivery_id", MAX_DELIVERY_ID_CHARS)
    existing = session.execute(
        select(CaliberWorkspaceSourceEvent).where(
            CaliberWorkspaceSourceEvent.source_id == source_id,
            CaliberWorkspaceSourceEvent.provider_delivery_id == delivery_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if not _same_event(existing, event):
            raise WorkspaceSourceEventConflictError(
                "provider delivery id was reused with different event evidence"
            )
        return existing, False

    row = CaliberWorkspaceSourceEvent(
        source_event_id=new_workspace_source_event_id(),
        source_id=source_id,
        provider_delivery_id=delivery_id,
        event_type=_bounded(event.event_type, "event_type", MAX_EVENT_TYPE_CHARS),
        repository_id=event.repository_id,
        payload_sha256=event.payload_sha256,
        status=SOURCE_EVENT_RECEIVED,
        received_at=_utc_naive(now),
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        raced = session.execute(
            select(CaliberWorkspaceSourceEvent).where(
                CaliberWorkspaceSourceEvent.source_id == source_id,
                CaliberWorkspaceSourceEvent.provider_delivery_id == delivery_id,
            )
        ).scalar_one_or_none()
        if raced is None:
            raise
        if not _same_event(raced, event):
            raise WorkspaceSourceEventConflictError(
                "provider delivery id was reused with different event evidence"
            ) from None
        return raced, False
    return row, True


def transition_source_event(
    session: Session,
    source_event_id: str,
    *,
    status: str,
    now: datetime | None = None,
    error_code: str | None = None,
    error_summary: str | None = None,
) -> CaliberWorkspaceSourceEvent:
    """Advance an inbox row while preserving terminal processing history."""

    row = session.get(CaliberWorkspaceSourceEvent, source_event_id)
    if row is None:
        raise WorkspaceSourceEventNotFoundError(f"source event {source_event_id!r} was not found")
    if status not in {SOURCE_EVENT_RECEIVED, *SOURCE_EVENT_TERMINAL}:
        raise WorkspaceSourceEventTransitionError(f"unsupported source event status: {status}")
    allowed = {
        SOURCE_EVENT_RECEIVED: {SOURCE_EVENT_RECEIVED, SOURCE_EVENT_PROCESSED, SOURCE_EVENT_FAILED},
        SOURCE_EVENT_PROCESSED: {SOURCE_EVENT_PROCESSED},
        SOURCE_EVENT_FAILED: {SOURCE_EVENT_FAILED},
    }
    if status not in allowed[row.status]:
        raise WorkspaceSourceEventTransitionError(
            f"source event cannot transition from {row.status!r} to {status!r}"
        )
    if (
        row.status == SOURCE_EVENT_FAILED
        and status == SOURCE_EVENT_FAILED
        and error_code is None
        and error_summary is None
    ):
        return row
    if status == SOURCE_EVENT_FAILED:
        if error_code is None:
            raise ValueError("error_code is required for failed source events")
        row.error_code = _bounded(error_code, "error_code", MAX_ERROR_CODE_CHARS)
        row.error_summary = _optional_bounded(
            error_summary, "error_summary", MAX_ERROR_SUMMARY_CHARS
        )
    elif status == SOURCE_EVENT_PROCESSED:
        row.error_code = None
        row.error_summary = None
    else:
        row.error_code = None
        row.error_summary = None
    row.status = status
    row.processed_at = None if status == SOURCE_EVENT_RECEIVED else _utc_naive(now)
    session.flush()
    return row


def requeue_failed_source_event(
    session: Session, source_event_id: str
) -> CaliberWorkspaceSourceEvent:
    """Explicitly reopen a failed delivery for a later reconciliation pass."""

    row = session.get(CaliberWorkspaceSourceEvent, source_event_id)
    if row is None:
        raise WorkspaceSourceEventNotFoundError(f"source event {source_event_id!r} was not found")
    if row.status != SOURCE_EVENT_FAILED:
        raise WorkspaceSourceEventTransitionError(
            "only failed source events may be explicitly requeued"
        )
    row.status = SOURCE_EVENT_RECEIVED
    row.processed_at = None
    row.error_code = None
    row.error_summary = None
    session.flush()
    return row


def _source_for_actor_link(
    session: Session, source_id: str, project_id: str
) -> CaliberWorkspaceSource:
    source = session.get(CaliberWorkspaceSource, source_id)
    if source is None or source.project_id != project_id:
        raise WorkspaceSourceActorLinkNotFoundError(f"source {source_id!r} was not found")
    return source


def link_source_actor(
    session: Session,
    *,
    project_id: str,
    source_id: str,
    provider_subject_id: str,
    caliber_user_id: str,
    verified_method: str,
    now: datetime | None = None,
) -> tuple[CaliberWorkspaceSourceActorLink, bool]:
    """Create an active provider identity link idempotently.

    A provider subject may have one active CALIBER principal per source.  A
    revoked historical row is retained and a later re-link gets a new row.
    """

    source = _source_for_actor_link(session, source_id, project_id)
    subject = _bounded(provider_subject_id, "provider_subject_id", MAX_PROVIDER_SUBJECT_CHARS)
    user_id = _bounded(caliber_user_id, "caliber_user_id", MAX_USER_ID_CHARS)
    method = _bounded(verified_method, "verified_method", 64)
    existing = session.execute(
        select(CaliberWorkspaceSourceActorLink).where(
            CaliberWorkspaceSourceActorLink.source_id == source_id,
            CaliberWorkspaceSourceActorLink.provider_subject_id == subject,
            CaliberWorkspaceSourceActorLink.status == ACTOR_LINK_ACTIVE,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.caliber_user_id != user_id:
            raise WorkspaceSourceActorLinkConflictError(
                "provider subject is already linked to another active CALIBER user"
            )
        return existing, False

    row = CaliberWorkspaceSourceActorLink(
        source_actor_link_id=new_workspace_source_actor_link_id(),
        project_id=project_id,
        source_id=source_id,
        provider=source.provider,
        provider_host=source.provider_host,
        provider_subject_id=subject,
        caliber_user_id=user_id,
        status=ACTOR_LINK_ACTIVE,
        verified_method=method,
        verified_at=_utc_naive(now),
        created_at=_utc_naive(now),
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        raced = session.execute(
            select(CaliberWorkspaceSourceActorLink).where(
                CaliberWorkspaceSourceActorLink.source_id == source_id,
                CaliberWorkspaceSourceActorLink.provider_subject_id == subject,
                CaliberWorkspaceSourceActorLink.status == ACTOR_LINK_ACTIVE,
            )
        ).scalar_one_or_none()
        if raced is None:
            raise
        if raced.caliber_user_id != user_id:
            raise WorkspaceSourceActorLinkConflictError(
                "provider subject is already linked to another active CALIBER user"
            ) from None
        return raced, False
    return row, True


def get_active_source_actor_link(
    session: Session, *, source_id: str, provider_subject_id: str
) -> CaliberWorkspaceSourceActorLink | None:
    """Return only an active link; revoked subjects never satisfy review policy."""

    subject = _bounded(provider_subject_id, "provider_subject_id", MAX_PROVIDER_SUBJECT_CHARS)
    return session.execute(
        select(CaliberWorkspaceSourceActorLink).where(
            CaliberWorkspaceSourceActorLink.source_id == source_id,
            CaliberWorkspaceSourceActorLink.provider_subject_id == subject,
            CaliberWorkspaceSourceActorLink.status == ACTOR_LINK_ACTIVE,
        )
    ).scalar_one_or_none()


def revoke_source_actor_link(
    session: Session,
    source_actor_link_id: str,
    *,
    revoked_by: str,
    reason: str,
    now: datetime | None = None,
) -> tuple[CaliberWorkspaceSourceActorLink, bool]:
    """Revoke a link without deleting its provenance."""

    row = session.get(CaliberWorkspaceSourceActorLink, source_actor_link_id)
    if row is None:
        raise WorkspaceSourceActorLinkNotFoundError(
            f"source actor link {source_actor_link_id!r} was not found"
        )
    if row.status == ACTOR_LINK_REVOKED:
        return row, False
    row.status = ACTOR_LINK_REVOKED
    row.revoked_at = _utc_naive(now)
    row.revoked_by = _bounded(revoked_by, "revoked_by", MAX_USER_ID_CHARS)
    row.revocation_reason = _bounded(reason, "reason", MAX_ERROR_SUMMARY_CHARS)
    session.flush()
    return row, True


__all__ = [
    "ACTOR_LINK_ACTIVE",
    "ACTOR_LINK_REVOKED",
    "SOURCE_EVENT_FAILED",
    "SOURCE_EVENT_PROCESSED",
    "SOURCE_EVENT_RECEIVED",
    "WorkspaceSourceActorLinkConflictError",
    "WorkspaceSourceActorLinkError",
    "WorkspaceSourceActorLinkNotFoundError",
    "WorkspaceSourceEventConflictError",
    "WorkspaceSourceEventError",
    "WorkspaceSourceEventNotFoundError",
    "WorkspaceSourceEventTransitionError",
    "get_active_source_actor_link",
    "link_source_actor",
    "record_source_event",
    "requeue_failed_source_event",
    "revoke_source_actor_link",
    "transition_source_event",
]
