"""The inventory of approved context sources (B07.1a, #692).

A project approves a knowledge-base *version* as a source of run context; nothing is loaded into a run unless its
version is approved for the run's project and the approval is not revoked (the admission and load checks are B07.1b and
B07.2; this module is the inventory they read). The rules:

* **Exact, versioned.** An approval names one ``(knowledge_base_id, version_id)``, never "the active version" or "the
  latest": a new build of the knowledge base is not approved by its predecessor's approval.
* **Only a project's own sources.** The knowledge base must belong to the project and the version to that knowledge
  base. Anything else (an unknown id, another project's knowledge base, a version of a different knowledge base) is the
  same :class:`SourceError`, with one message that names no identifier, so a foreign id cannot be probed.
* **Idempotent and race-safe.** Approving an approved source returns the existing row; two concurrent approvals
  create one (the unique key decides). Approving a revoked source reactivates the row (new approver and time).
* **Revoking keeps the row** (``revoked_at``, ``revoked_by``) as evidence; revoking a source that is not approved is
  :data:`NOT_APPROVED`. Both changes write an audit row; none of this commits (the caller owns the transaction).
* **No authorization here.** Who may approve is the caller's decision (a project editor or owner through a route);
  this module records the act and checks the source is real.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Final

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from caliber.audit import record as audit_record
from caliber.db.models import (
    CaliberApprovedContextSource,
    CaliberKnowledgeBase,
    CaliberKnowledgeBaseVersion,
)
from caliber.db.transactions import begin_sqlite_transaction

SOURCE_NOT_FOUND: Final = "source_not_found"
NOT_APPROVED: Final = "not_approved"
INVALID_ARGUMENT: Final = "invalid_argument"

_MAX = 64
_MAX_ACTOR = 256


class SourceError(ValueError):
    """The source cannot be approved or revoked; ``code`` is one of the module's constants."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _clean(name: str, value: object, limit: int) -> str:
    if type(value) is not str or not value.strip() or len(value) > limit or not value.isprintable():
        raise SourceError(
            INVALID_ARGUMENT,
            f"{name} must be a non-empty printable string of at most {limit} characters",
        )
    return value


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def new_source_id() -> str:
    return f"CAS-{uuid.uuid4().hex[:16]}"


def _find(
    session: Session, project_id: str, knowledge_base_id: str, version_id: str
) -> CaliberApprovedContextSource | None:
    return session.execute(
        select(CaliberApprovedContextSource).where(
            CaliberApprovedContextSource.project_id == project_id,
            CaliberApprovedContextSource.knowledge_base_id == knowledge_base_id,
            CaliberApprovedContextSource.version_id == version_id,
        )
    ).scalar_one_or_none()


def _require_real_source(
    session: Session, project_id: str, knowledge_base_id: str, version_id: str
) -> None:
    """The version exists, belongs to the knowledge base, and the knowledge base belongs to the project."""
    found = session.execute(
        select(CaliberKnowledgeBaseVersion.knowledge_base_version_id)
        .join(
            CaliberKnowledgeBase,
            CaliberKnowledgeBase.knowledge_base_id == CaliberKnowledgeBaseVersion.knowledge_base_id,
        )
        .where(
            CaliberKnowledgeBaseVersion.knowledge_base_version_id == version_id,
            CaliberKnowledgeBaseVersion.knowledge_base_id == knowledge_base_id,
            CaliberKnowledgeBase.project_id == project_id,
        )
    ).first()
    if found is None:
        raise SourceError(SOURCE_NOT_FOUND, "no such source in this project")


def approve_source(
    session: Session,
    project_id: str,
    knowledge_base_id: str,
    version_id: str,
    actor: str,
) -> CaliberApprovedContextSource:
    """Approve one knowledge-base version as a context source of ``project_id``. Flushes, never commits."""
    project = _clean("project_id", project_id, _MAX)
    kb = _clean("knowledge_base_id", knowledge_base_id, _MAX)
    version = _clean("version_id", version_id, _MAX)
    who = _clean("actor", actor, _MAX_ACTOR)
    _require_real_source(session, project, kb, version)
    begin_sqlite_transaction(session)
    row = _find(session, project, kb, version)
    if row is None:
        row = CaliberApprovedContextSource(
            id=new_source_id(),
            project_id=project,
            knowledge_base_id=kb,
            version_id=version,
            approved_by=who,
            approved_at=_now(),
        )
        try:
            with session.begin_nested():
                session.add(row)
        except IntegrityError:  # a concurrent approval created it between the read and the insert
            row = session.execute(
                select(CaliberApprovedContextSource)
                .where(
                    CaliberApprovedContextSource.project_id == project,
                    CaliberApprovedContextSource.knowledge_base_id == kb,
                    CaliberApprovedContextSource.version_id == version,
                )
                .execution_options(populate_existing=True)
            ).scalar_one()
        else:
            _audit(session, who, "approve_context_source", row)
            return row
    if row.revoked_at is not None:
        row.revoked_at, row.revoked_by = None, None
        row.approved_by, row.approved_at = who, _now()
        session.flush()
        _audit(session, who, "approve_context_source", row)
    return row


def revoke_source(
    session: Session,
    project_id: str,
    knowledge_base_id: str,
    version_id: str,
    actor: str,
) -> CaliberApprovedContextSource:
    """Revoke an approval; the row stays. Raises :data:`NOT_APPROVED` if it is not currently approved."""
    project = _clean("project_id", project_id, _MAX)
    kb = _clean("knowledge_base_id", knowledge_base_id, _MAX)
    version = _clean("version_id", version_id, _MAX)
    who = _clean("actor", actor, _MAX_ACTOR)
    row = _find(session, project, kb, version)
    if row is None or row.revoked_at is not None:
        raise SourceError(NOT_APPROVED, "this source is not approved")
    row.revoked_at, row.revoked_by = _now(), who
    session.flush()
    _audit(session, who, "revoke_context_source", row)
    return row


def is_approved(session: Session, project_id: str, knowledge_base_id: str, version_id: str) -> bool:
    """Whether the version is approved for the project right now (``False`` once revoked, and for anything unknown)."""
    row = _find(session, project_id, knowledge_base_id, version_id)
    return row is not None and row.revoked_at is None


def approved_sources(session: Session, project_id: str) -> list[CaliberApprovedContextSource]:
    """The project's currently approved sources, oldest approval first."""
    return list(
        session.execute(
            select(CaliberApprovedContextSource)
            .where(
                CaliberApprovedContextSource.project_id == project_id,
                CaliberApprovedContextSource.revoked_at.is_(None),
            )
            .order_by(CaliberApprovedContextSource.approved_at, CaliberApprovedContextSource.id)
        ).scalars()
    )


def _audit(session: Session, actor: str, action: str, row: CaliberApprovedContextSource) -> None:
    audit_record(
        session,
        actor=actor,
        action=action,
        entity_type="context_source",
        entity_id=row.id,
        details={
            "project_id": row.project_id,
            "knowledge_base_id": row.knowledge_base_id,
            "version_id": row.version_id,
        },
    )


__all__ = [
    "INVALID_ARGUMENT",
    "NOT_APPROVED",
    "SOURCE_NOT_FOUND",
    "SourceError",
    "approve_source",
    "approved_sources",
    "is_approved",
    "revoke_source",
]
