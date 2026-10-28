"""Authorize a run's read of a context source *before* its bytes are loaded (B07.2, #699).

:func:`authorize_source` answers one question for one source: may *this run*, acting as the subject its manifest
recorded, read *this* knowledge-base version, now? It is called by the knowledge-query runner before the retrieval
service is touched, so a refusal means nothing was fetched. The answer comes from:

1. **The run's own identity.** The caller supplies the run's subject (the worker takes it from
   :func:`caliber.workflows.run_identity.identity_for_run`); it must equal the subject the manifest recorded. The
   subject, workload, project and purpose used below are the manifest's, never the caller's.
2. **The exact grant.** ``decide`` (B03.3) is called with the manifest's run id, the grant id the manifest names, the
   typed subject and workload, the action ``context.read``, the resource :func:`~caliber.context.sources.source_resource`
   of the exact ``(knowledge_base_id, version_id)``, and the manifest's purpose. No "latest grant" and no ambient grant
   is ever looked up. The grant carries ``context.read`` on each source admission approved (reference profile); a
   source it does not list is ``resource_not_allowed``, a member who left is ``not_a_member``, a revoked or expired
   grant is refused, and the decision (allow or deny) is recorded.
3. **The source's current approval** (:func:`~caliber.context.sources.is_approved`), checked first: revoked after
   admission means refused now, with an audit row (no policy decision is asked or recorded for a withdrawn source).

Every refusal is a :class:`ContextDenied` with one fixed message that names no source and carries no payload; the stable
``code`` and the policy's ``reasons`` say why. An authority store that cannot answer is a refusal, never an allow.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from caliber.audit import record as audit_record
from caliber.context.sources import is_approved, source_resource
from caliber.db.models import CaliberKnowledgeBaseVersion
from caliber.policy.decisions import (
    REFERENCE_POLICY_V1,
    DecisionInputError,
    Policy,
    PolicyDecision,
    decide,
)
from caliber.policy.grants import AUTHORITY_UNAVAILABLE
from caliber.policy.principal import PrincipalError, Subject, Workload

CONTEXT_READ: Final = "context.read"
IDENTITY_MISMATCH: Final = "identity_mismatch"
MANIFEST_INVALID: Final = "manifest_invalid"
AUTHORITY_DENIED: Final = "authority_denied"
SOURCE_NOT_APPROVED: Final = "source_not_approved"
_MESSAGE: Final = "this run may not read that context source"


class ContextDenied(PermissionError):  # noqa: N818 - a refusal, named for what it says
    """The run may not read the source; nothing was loaded. ``code`` is stable, ``reasons`` the policy's."""

    def __init__(
        self, code: str, reasons: tuple[str, ...] = (), *, message: str = _MESSAGE
    ) -> None:
        detail = f" ({', '.join(reasons)})" if reasons else ""
        super().__init__(f"{code}{detail}: {message}")
        self.code = code
        self.reasons = reasons


@dataclass(frozen=True)
class ContextAuthorization:
    """A recorded allow for one source."""

    knowledge_base_id: str
    version_id: str
    decision: PolicyDecision


def authorize_source(
    session: Session,
    identity: Subject,
    run_manifest: Mapping[str, Any],
    grant_id: str,
    knowledge_base_id: str,
    version_id: str,
    *,
    policy: Policy = REFERENCE_POLICY_V1,
    now: datetime | None = None,
) -> ContextAuthorization:
    """Return the authorization if the run may read ``(knowledge_base_id, version_id)`` now; else raise
    :class:`ContextDenied`. Flushes the recorded decision and never commits: the caller owns the transaction (and
    should commit so a denial stays on record)."""
    try:
        saved = Subject.from_manifest(
            run_manifest["subject"], project_id=run_manifest.get("project_id")
        )
        workload = Workload.from_manifest(run_manifest["workload"])
        run_id = str(run_manifest["workflow_run_id"])
        purpose = str(run_manifest["purpose"])
        project_id = run_manifest.get("project_id")
    except (KeyError, PrincipalError) as exc:
        raise ContextDenied(MANIFEST_INVALID) from exc
    if (identity.user_id, identity.auth_method) != (saved.user_id, saved.auth_method):
        raise ContextDenied(IDENTITY_MISMATCH)
    try:
        approved = isinstance(project_id, str) and is_approved(
            session, project_id, knowledge_base_id, version_id
        )
    except SQLAlchemyError as exc:
        raise ContextDenied(AUTHORITY_UNAVAILABLE) from exc
    if not approved:
        # Withdrawn (or never granted) approval is not a policy decision, so it leaves an audit row instead.
        audit_record(
            session,
            actor=saved.user_id,
            action="context_read_denied",
            entity_type="context_source",
            entity_id=run_id,
            details={
                "run_id": run_id,
                "grant_id": grant_id,
                "knowledge_base_id": knowledge_base_id,
                "version_id": version_id,
                "reason": SOURCE_NOT_APPROVED,
            },
        )
        raise ContextDenied(SOURCE_NOT_APPROVED)
    try:
        decision = decide(
            session,
            run_id=run_id,
            grant_id=grant_id,
            subject=saved,
            workload=workload,
            action=CONTEXT_READ,
            resource=source_resource(knowledge_base_id, version_id),
            purpose=purpose,
            policy=policy,
            now=now,
        )
    except DecisionInputError as exc:
        raise ContextDenied(MANIFEST_INVALID) from exc
    except SQLAlchemyError as exc:
        raise ContextDenied(AUTHORITY_UNAVAILABLE) from exc
    if not decision.allowed:
        code = (
            AUTHORITY_UNAVAILABLE
            if AUTHORITY_UNAVAILABLE in decision.reason_codes
            else AUTHORITY_DENIED
        )
        raise ContextDenied(code, decision.reason_codes)
    return ContextAuthorization(knowledge_base_id, version_id, decision)


def _commit_quietly(session: Session) -> None:
    try:
        session.commit()
    except SQLAlchemyError:
        session.rollback()


class SourceGuard:
    """The callable ``promoter.build_plan(source_guard=...)`` takes, remembering the decision that allowed each version.

    Called with a knowledge-base ``version_id`` before it is read, it authorizes that exact source for this run in its
    own short transaction (committed whether it allows or denies, so the decision is on record) and raises
    :class:`ContextDenied` to refuse. A version that does not exist has no knowledge base to name and is refused like
    any source the grant does not list. ``decisions`` maps each allowed version to its decision id: the context
    manifest cites them (B07.3b).
    """

    def __init__(
        self,
        session_factory: Callable[[], Session],
        identity: Subject,
        run_manifest: Mapping[str, Any],
        grant_id: str,
        *,
        policy: Policy = REFERENCE_POLICY_V1,
    ) -> None:
        self._session_factory = session_factory
        self._identity = identity
        self._run_manifest = run_manifest
        self._grant_id = grant_id
        self._policy = policy
        self.decisions: dict[str, str] = {}

    def __call__(self, version_id: str) -> None:
        try:
            with self._session_factory() as session:
                try:
                    row = session.get(CaliberKnowledgeBaseVersion, version_id)
                    knowledge_base_id = row.knowledge_base_id if row is not None else ""
                    authorization = authorize_source(
                        session,
                        self._identity,
                        self._run_manifest,
                        self._grant_id,
                        knowledge_base_id,
                        version_id,
                        policy=self._policy,
                    )
                except ContextDenied:
                    _commit_quietly(
                        session
                    )  # the decision (or audit row) of a refusal stays on record
                    raise
                except SQLAlchemyError:
                    # never let a database message (SQL and bound parameters) reach the node's error text
                    session.rollback()
                    raise ContextDenied(AUTHORITY_UNAVAILABLE) from None
                session.commit()
        except SQLAlchemyError:
            raise ContextDenied(AUTHORITY_UNAVAILABLE) from None
        self.decisions[version_id] = authorization.decision.decision_id


def source_guard_for(
    session_factory: Callable[[], Session],
    identity: Subject,
    run_manifest: Mapping[str, Any],
    grant_id: str,
    *,
    policy: Policy = REFERENCE_POLICY_V1,
) -> SourceGuard:
    """A :class:`SourceGuard` for one admitted run."""
    return SourceGuard(session_factory, identity, run_manifest, grant_id, policy=policy)


__all__ = [
    "AUTHORITY_DENIED",
    "CONTEXT_READ",
    "IDENTITY_MISMATCH",
    "MANIFEST_INVALID",
    "SOURCE_NOT_APPROVED",
    "ContextAuthorization",
    "ContextDenied",
    "SourceGuard",
    "authorize_source",
    "source_guard_for",
]
