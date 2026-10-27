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
  create one (the unique key decides). Approving a revoked source reactivates the row (new approver and time). The
  row is read fresh and locked (``FOR UPDATE``, PostgreSQL) before a change, so two concurrent revocations or
  reactivations change it once and audit once, never overwriting who revoked it.
* **Revoking keeps the row** (``revoked_at``, ``revoked_by``) as evidence; revoking a source that is not approved is
  :data:`NOT_APPROVED`. Both changes write an audit row; none of this commits (the caller owns the transaction).
* **Only a completed build of a live knowledge base** can be approved (the same ``completed`` rule that gates
  activating a version), and an approval whose version or knowledge base has since been deleted no longer counts:
  :func:`is_approved` and :func:`approved_sources` join the version, so the inventory fails closed.
* **No authorization here.** Who may approve is the caller's decision (a project editor or owner through a route);
  this module records the act and checks the source is real.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any, Final

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
SOURCE_NOT_APPROVED: Final = "source_not_approved"

_MAX = 64
_MAX_ACTOR = 256


class SourceError(ValueError):
    """The source cannot be approved or revoked; ``code`` is one of the module's constants."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def source_resource(knowledge_base_id: str, version_id: str) -> str:
    """The resource a grant and its decisions name for one context source: the exact knowledge-base version."""
    return f"knowledge_base:{knowledge_base_id}@{version_id}"


def memory_resource(reference: str) -> str:
    """The resource a grant and its decisions name for one memory a run reads."""
    return f"memory:{reference}"


class SourceNotApproved(SourceError):  # noqa: N818 - a refusal, named for what it says
    """One or more knowledge-base versions are not approved for the project; ``sources`` lists them (sorted)."""

    def __init__(self, sources: tuple[tuple[str, str], ...]) -> None:
        listed = ", ".join(f"{kb}@{version}" for kb, version in sources)
        super().__init__(
            SOURCE_NOT_APPROVED,
            f"{SOURCE_NOT_APPROVED}: not approved as a context source for this project: {listed}",
        )
        self.sources = sources


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
    session: Session,
    project_id: str,
    knowledge_base_id: str,
    version_id: str,
) -> CaliberApprovedContextSource | None:
    """The row, read from the database (never the session's identity map: a revocation committed elsewhere must
    be seen) and locked for the caller's transaction, so concurrent changes take turns."""
    statement = (
        select(CaliberApprovedContextSource)
        .where(
            CaliberApprovedContextSource.project_id == project_id,
            CaliberApprovedContextSource.knowledge_base_id == knowledge_base_id,
            CaliberApprovedContextSource.version_id == version_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    return session.execute(statement).scalar_one_or_none()


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
            CaliberKnowledgeBaseVersion.status == "completed",
            CaliberKnowledgeBase.status != "archived",
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
        except (
            IntegrityError
        ) as exc:  # a concurrent approval created it between the read and the insert
            existing = _find(session, project, kb, version)
            if existing is None:  # not the unique key: the project vanished, say so without detail
                raise SourceError(SOURCE_NOT_FOUND, "no such source in this project") from exc
            row = existing
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


def _live(statement: Any) -> Any:
    """Restrict to approvals whose version and knowledge base still exist (a deleted source no longer counts)."""
    return statement.join(
        CaliberKnowledgeBaseVersion,
        CaliberKnowledgeBaseVersion.knowledge_base_version_id
        == CaliberApprovedContextSource.version_id,
    ).where(
        CaliberKnowledgeBaseVersion.knowledge_base_id
        == CaliberApprovedContextSource.knowledge_base_id
    )


def is_approved(session: Session, project_id: str, knowledge_base_id: str, version_id: str) -> bool:
    """Whether the version is approved for the project right now: ``False`` once revoked, for anything never
    approved, and for an approval whose version or knowledge base has since been deleted."""
    found = session.execute(
        _live(select(CaliberApprovedContextSource.id)).where(
            CaliberApprovedContextSource.project_id == project_id,
            CaliberApprovedContextSource.knowledge_base_id == knowledge_base_id,
            CaliberApprovedContextSource.version_id == version_id,
            CaliberApprovedContextSource.revoked_at.is_(None),
        )
    ).first()
    return found is not None


def require_approved(session: Session, project_id: str, sources: Iterable[tuple[str, str]]) -> None:
    """Raise :class:`SourceNotApproved` naming every ``(knowledge_base_id, version_id)`` that is not approved for the
    project (all of them at once, sorted, so a caller fixes them in one pass). Nothing is raised for an empty list."""
    missing = tuple(
        sorted(
            {
                (kb, version)
                for kb, version in sources
                if not is_approved(session, project_id, kb, version)
            }
        )
    )
    if missing:
        raise SourceNotApproved(missing)


def approved_sources(session: Session, project_id: str) -> list[CaliberApprovedContextSource]:
    """The project's currently approved, still existing sources, oldest approval first."""
    return list(
        session.execute(
            _live(select(CaliberApprovedContextSource))
            .where(
                CaliberApprovedContextSource.project_id == project_id,
                CaliberApprovedContextSource.revoked_at.is_(None),
            )
            .order_by(CaliberApprovedContextSource.approved_at, CaliberApprovedContextSource.id)
            .execution_options(populate_existing=True)
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
    "SOURCE_NOT_APPROVED",
    "SOURCE_NOT_FOUND",
    "SourceError",
    "SourceNotApproved",
    "approve_source",
    "approved_sources",
    "is_approved",
    "memory_resource",
    "require_approved",
    "revoke_source",
    "source_resource",
]
