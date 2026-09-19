"""Transactional Workspace Change Request lifecycle services (`P4-D`).

The service deliberately works with immutable revision IDs and append-only
history. A head update never edits the reviewed package; it appends a new head,
so old comments, checks, reviews, and provider attestations remain auditable
but cannot satisfy the current-head policy.

Public routes in :mod:`caliber.routes.workspace_change_requests` use the
service for all database work. The acceptance primitive (``accept_change_request``)
is also reachable from that module's ``POST .../change-requests/{id}:accept``
route (`P4-D`'s public acceptance route): the route derives ``qa_evidence``
itself from a durably recorded, server-verified `CaliberWorkspaceReleaseDecision`
row (Phase 5's QA release governance) bound to the exact Change Request head
being accepted -- never from a caller-supplied claim -- before calling this
function. See that route's own docstring for the full evidence-derivation
reasoning.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import total_ordering
from typing import Any, Literal

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_ADMIN, SCOPE_OPERATOR, CaliberIdentity, scopes_for_user
from caliber.db.models import (
    CaliberProject,
    CaliberProjectMember,
    CaliberWorkspaceChangeRequest,
    CaliberWorkspaceChangeRequestCheck,
    CaliberWorkspaceChangeRequestComment,
    CaliberWorkspaceChangeRequestHead,
    CaliberWorkspaceChangeRequestReview,
    CaliberWorkspaceChangeRequestReviewer,
    CaliberWorkspaceExternalReviewAttestation,
    CaliberWorkspaceRevision,
    CaliberWorkspaceSource,
    CaliberWorkspaceVersionClaim,
    CaliberWorkspaceVersionTag,
)
from caliber.ids import (
    new_workspace_change_request_check_id,
    new_workspace_change_request_comment_id,
    new_workspace_change_request_head_id,
    new_workspace_change_request_id,
    new_workspace_change_request_review_id,
    new_workspace_change_request_reviewer_id,
    new_workspace_external_review_attestation_id,
    new_workspace_version_claim_id,
    new_workspace_version_tag_id,
)
from caliber.resource_access import ROLE_EDITOR, ROLE_OWNER, require_project_access

_Factory = sessionmaker[Session]

CR_DRAFT = "draft"
CR_OPEN = "open"
CR_CHANGES_REQUESTED = "changes_requested"
CR_TECHNICALLY_APPROVED = "technically_approved"
CR_QA_IN_PROGRESS = "qa_in_progress"
CR_OUT_OF_DATE = "out_of_date"
CR_ACCEPTED = "accepted"
CR_CLOSED = "closed"

_MUTABLE_HEAD_STATES = frozenset({CR_OPEN, CR_CHANGES_REQUESTED})
_CLOSEABLE_STATES = frozenset(
    {CR_DRAFT, CR_OPEN, CR_CHANGES_REQUESTED, CR_TECHNICALLY_APPROVED, CR_OUT_OF_DATE}
)
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


@total_ordering
@dataclass(frozen=True)
class SemanticVersion:
    """Small SemVer 2.0 precedence value without a runtime dependency."""

    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, SemanticVersion):
            return NotImplemented
        stable = (self.major, self.minor, self.patch)
        other_stable = (other.major, other.minor, other.patch)
        if stable != other_stable:
            return stable < other_stable
        return _compare_prerelease(self.prerelease, other.prerelease) < 0


def _compare_prerelease(left: tuple[str, ...], right: tuple[str, ...]) -> int:  # noqa: PLR0911
    if not left and right:
        return 1
    if left and not right:
        return -1
    for left_part, right_part in zip(left, right, strict=False):
        if left_part == right_part:
            continue
        left_numeric = left_part.isdigit()
        right_numeric = right_part.isdigit()
        if left_numeric and right_numeric:
            return -1 if int(left_part) < int(right_part) else 1
        if left_numeric != right_numeric:
            return -1 if left_numeric else 1
        return -1 if left_part < right_part else 1
    if len(left) == len(right):
        return 0
    return -1 if len(left) < len(right) else 1


def canonical_semantic_version(value: str) -> tuple[str, SemanticVersion]:
    """Validate and normalize a SemVer string; reject presentation ``v`` prefixes."""
    candidate = value.strip()
    match = _SEMVER_RE.fullmatch(candidate)
    if match is None or candidate.startswith("v"):
        raise HTTPException(status_code=400, detail="invalid_semantic_version")
    major, minor, patch, prerelease, _build = match.groups()
    identifiers = tuple(prerelease.split(".")) if prerelease else ()
    if any(part.isdigit() and len(part) > 1 and part.startswith("0") for part in identifiers):
        raise HTTPException(status_code=400, detail="invalid_semantic_version")
    return candidate, SemanticVersion(int(major), int(minor), int(patch), identifiers)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _fail(status: int, reason: str) -> HTTPException:
    return HTTPException(status_code=status, detail=reason)


def _project(
    session: Session, project_id: str, identity: CaliberIdentity, action: str
) -> CaliberProject:
    project, _decision = require_project_access(session, identity, project_id, action)
    return project


def _revision(session: Session, project_id: str, revision_id: str) -> CaliberWorkspaceRevision:
    row = session.execute(
        select(CaliberWorkspaceRevision).where(
            CaliberWorkspaceRevision.project_id == project_id,
            CaliberWorkspaceRevision.revision_id == revision_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise _fail(404, "revision_not_found")
    if row.status != "ready":
        raise _fail(409, "revision_not_ready")
    return row


def _request(
    session: Session, project_id: str, change_request_id: str
) -> CaliberWorkspaceChangeRequest:
    row = session.execute(
        select(CaliberWorkspaceChangeRequest).where(
            CaliberWorkspaceChangeRequest.project_id == project_id,
            CaliberWorkspaceChangeRequest.change_request_id == change_request_id,
        )
    ).scalar_one_or_none()
    if row is None:
        raise _fail(404, "change_request_not_found")
    return row


def _head(
    session: Session, request: CaliberWorkspaceChangeRequest
) -> CaliberWorkspaceChangeRequestHead:
    row = session.execute(
        select(CaliberWorkspaceChangeRequestHead).where(
            CaliberWorkspaceChangeRequestHead.change_request_id == request.change_request_id,
            CaliberWorkspaceChangeRequestHead.generation == request.head_generation,
        )
    ).scalar_one_or_none()
    if row is None:
        raise RuntimeError("change request head pointer has no history row")
    return row


def _active_source(session: Session, project_id: str) -> CaliberWorkspaceSource:
    source = session.execute(
        select(CaliberWorkspaceSource).where(
            CaliberWorkspaceSource.project_id == project_id,
            CaliberWorkspaceSource.status == "active",
        )
    ).scalar_one_or_none()
    if source is None:
        raise _fail(409, "source_provider_unavailable")
    return source


def _source_policy(session: Session, project_id: str) -> tuple[str, str, list[str]]:
    source = _active_source(session, project_id)
    policy = (
        source.external_review_policy if isinstance(source.external_review_policy, dict) else {}
    )
    raw_checks = policy.get("required_checks", [])
    checks = [item.strip() for item in raw_checks if isinstance(item, str) and item.strip()]
    return source.external_review_policy_version, source.external_review_policy_sha256, checks


def _head_policy(
    session: Session, project_id: str, review_backend: str
) -> tuple[str, str, list[str]]:
    if review_backend == "source_provider":
        return _source_policy(session, project_id)
    return "caliber-v1", "", []


def _reviewer_role(session: Session, project: CaliberProject, user_id: str) -> str | None:
    if project.owner == user_id:
        return ROLE_OWNER
    member = session.execute(
        select(CaliberProjectMember).where(
            CaliberProjectMember.project_id == project.project_id,
            CaliberProjectMember.user_id == user_id,
            CaliberProjectMember.status == "active",
        )
    ).scalar_one_or_none()
    return member.role if member is not None else None


def _require_reviewer_eligible(
    session: Session, project: CaliberProject, user_id: str, config: Any
) -> str:
    role = _reviewer_role(session, project, user_id)
    if role not in {ROLE_OWNER, ROLE_EDITOR}:
        raise _fail(403, "reviewer_not_eligible")
    scopes = scopes_for_user(config, user_id)
    if SCOPE_OPERATOR not in scopes and SCOPE_ADMIN not in scopes:
        raise _fail(403, "reviewer_not_eligible")
    return role


def _active_claim(session: Session, change_request_id: str) -> CaliberWorkspaceVersionClaim | None:
    return session.execute(
        select(CaliberWorkspaceVersionClaim).where(
            CaliberWorkspaceVersionClaim.change_request_id == change_request_id,
            CaliberWorkspaceVersionClaim.status == "reserved",
        )
    ).scalar_one_or_none()


def _highest_accepted_version(session: Session, project_id: str) -> SemanticVersion | None:
    versions = session.execute(
        select(CaliberWorkspaceVersionClaim.semantic_version).where(
            CaliberWorkspaceVersionClaim.project_id == project_id,
            CaliberWorkspaceVersionClaim.status == "accepted",
        )
    ).scalars()
    parsed = [canonical_semantic_version(value)[1] for value in versions]
    return max(parsed) if parsed else None


def _ensure_version_is_new(session: Session, project_id: str, version: SemanticVersion) -> None:
    highest = _highest_accepted_version(session, project_id)
    if highest is not None and version <= highest:
        raise _fail(409, "semantic_version_not_greater_than_accepted")


def _reserve_claim(
    session: Session,
    *,
    project: CaliberProject,
    change_request_id: str,
    semantic_version: str,
    actor: str,
) -> CaliberWorkspaceVersionClaim:
    canonical, parsed = canonical_semantic_version(semantic_version)
    _ensure_version_is_new(session, project.project_id, parsed)
    existing = session.execute(
        select(CaliberWorkspaceVersionClaim).where(
            CaliberWorkspaceVersionClaim.project_id == project.project_id,
            CaliberWorkspaceVersionClaim.semantic_version == canonical,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise _fail(409, "semantic_version_unavailable")
    claim = CaliberWorkspaceVersionClaim(
        claim_id=new_workspace_version_claim_id(),
        project_id=project.project_id,
        change_request_id=change_request_id,
        semantic_version=canonical,
        status="reserved",
        claimed_by=actor,
    )
    session.add(claim)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _fail(409, "semantic_version_unavailable") from exc
    return claim


def _abandon_claim(claim: CaliberWorkspaceVersionClaim) -> None:
    claim.status = "abandoned"
    claim.abandoned_at = _now()


def _claim_version(claim: CaliberWorkspaceVersionClaim) -> SemanticVersion:
    return canonical_semantic_version(claim.semantic_version)[1]


def _create_head(
    session: Session,
    *,
    request: CaliberWorkspaceChangeRequest,
    revision: CaliberWorkspaceRevision,
    actor: str,
    summary: str,
    policy_version: str,
    policy_sha256: str,
) -> CaliberWorkspaceChangeRequestHead:
    head = CaliberWorkspaceChangeRequestHead(
        head_id=new_workspace_change_request_head_id(),
        change_request_id=request.change_request_id,
        generation=request.head_generation,
        revision_id=revision.revision_id,
        revision_sha256=revision.revision_sha256,
        review_policy_version=policy_version,
        review_policy_sha256=policy_sha256,
        changed_by=actor,
        change_summary=summary,
    )
    session.add(head)
    return head


def create_change_request(
    session: Session,
    *,
    project_id: str,
    identity: CaliberIdentity,
    config: Any,
    title: str,
    description: str,
    head_revision_id: str,
    base_revision_id: str | None,
    semantic_version: str,
    review_backend: Literal["caliber", "source_provider"],
    reviewer_user_ids: list[str],
) -> CaliberWorkspaceChangeRequest:
    """Create a draft, its first immutable head, and exactly one version claim."""
    project = _project(session, project_id, identity, "change_request.create")
    head_revision = _revision(session, project_id, head_revision_id)
    accepted_id = project.accepted_revision_id
    if accepted_id is None:
        if base_revision_id is not None:
            raise _fail(409, "base_revision_must_be_null_for_first_acceptance")
    elif base_revision_id != accepted_id:
        raise _fail(409, "base_revision_not_current")
    if base_revision_id is not None:
        base_revision = _revision(session, project_id, base_revision_id)
        if base_revision.revision_id == head_revision.revision_id:
            raise _fail(400, "base_and_head_must_differ")
    if review_backend == "source_provider" and reviewer_user_ids:
        raise _fail(400, "review_backends_cannot_be_combined")
    policy_version, policy_sha256, _checks = _head_policy(session, project_id, review_backend)
    request = CaliberWorkspaceChangeRequest(
        change_request_id=new_workspace_change_request_id(),
        project_id=project_id,
        base_revision_id=base_revision_id,
        current_head_revision_id=head_revision.revision_id,
        created_by=identity.user_id,
        title=title,
        description=description,
        head_generation=1,
        status=CR_DRAFT,
        review_backend=review_backend,
        lock_version=1,
    )
    session.add(request)
    session.flush()
    _create_head(
        session,
        request=request,
        revision=head_revision,
        actor=identity.user_id,
        summary="initial head",
        policy_version=policy_version,
        policy_sha256=policy_sha256,
    )
    _reserve_claim(
        session,
        project=project,
        change_request_id=request.change_request_id,
        semantic_version=semantic_version,
        actor=identity.user_id,
    )
    seen: set[str] = set()
    for user_id in reviewer_user_ids:
        if user_id in seen:
            continue
        seen.add(user_id)
        _require_reviewer_eligible(session, project, user_id, config)
        session.add(
            CaliberWorkspaceChangeRequestReviewer(
                reviewer_id=new_workspace_change_request_reviewer_id(),
                change_request_id=request.change_request_id,
                user_id=user_id,
                assigned_by=identity.user_id,
                active=True,
            )
        )
    audit_record(
        session,
        actor=identity.user_id,
        action="create_workspace_change_request",
        entity_type="workspace_change_request",
        entity_id=request.change_request_id,
        details={"project_id": project_id, "head_revision_id": head_revision_id},
    )
    session.commit()
    return request


def _ensure_current_base(
    session: Session, project: CaliberProject, request: CaliberWorkspaceChangeRequest
) -> None:
    if project.accepted_revision_id != request.base_revision_id:
        request.status = CR_OUT_OF_DATE
        request.lock_version += 1
        session.commit()
        raise _fail(409, "change_request_out_of_date")


def _queue_required_checks(
    session: Session,
    request: CaliberWorkspaceChangeRequest,
    head: CaliberWorkspaceChangeRequestHead,
) -> None:
    _version, _digest, names = _head_policy(session, request.project_id, request.review_backend)
    for name in names:
        existing = session.execute(
            select(CaliberWorkspaceChangeRequestCheck).where(
                CaliberWorkspaceChangeRequestCheck.head_id == head.head_id,
                CaliberWorkspaceChangeRequestCheck.check_name == name,
                CaliberWorkspaceChangeRequestCheck.status.in_(("queued", "running")),
            )
        ).scalar_one_or_none()
        if existing is not None:
            continue
        session.add(
            CaliberWorkspaceChangeRequestCheck(
                check_id=new_workspace_change_request_check_id(),
                head_id=head.head_id,
                check_name=name,
                attempt_number=1,
                implementation_version="caliber-change-request-v1",
                input_digest=head.revision_sha256,
                status="queued",
            )
        )


def submit_change_request(
    session: Session,
    *,
    project_id: str,
    change_request_id: str,
    identity: CaliberIdentity,
    config: Any,
) -> CaliberWorkspaceChangeRequest:
    project = _project(session, project_id, identity, "change_request.update")
    request = _request(session, project_id, change_request_id)
    if request.status != CR_DRAFT:
        raise _fail(409, "change_request_not_draft")
    head = _head(session, request)
    revision = _revision(session, project_id, head.revision_id)
    _ensure_current_base(session, project, request)
    claim = _active_claim(session, request.change_request_id)
    if claim is None:
        raise _fail(409, "version_claim_required")
    _ensure_version_is_new(session, project_id, _claim_version(claim))
    if request.review_backend == "caliber":
        reviewers = (
            session.execute(
                select(CaliberWorkspaceChangeRequestReviewer).where(
                    CaliberWorkspaceChangeRequestReviewer.change_request_id
                    == request.change_request_id,
                    CaliberWorkspaceChangeRequestReviewer.active.is_(True),
                )
            )
            .scalars()
            .all()
        )
        if not reviewers:
            raise _fail(409, "reviewer_assignment_required")
        for reviewer in reviewers:
            _require_reviewer_eligible(session, project, reviewer.user_id, config)
    elif not _verified_external_attestation(session, request, head, revision):
        raise _fail(409, "external_review_attestation_required")
    request.status = CR_OPEN
    request.lock_version += 1
    _queue_required_checks(session, request, head)
    audit_record(
        session,
        actor=identity.user_id,
        action="submit_workspace_change_request",
        entity_type="workspace_change_request",
        entity_id=request.change_request_id,
        details={"head_id": head.head_id, "revision_id": revision.revision_id},
    )
    session.commit()
    return request


def _check_expected_lock(request: CaliberWorkspaceChangeRequest, expected: int) -> None:
    if request.lock_version != expected:
        raise _fail(409, "change_request_lock_version_conflict")


def update_change_request_head(
    session: Session,
    *,
    project_id: str,
    change_request_id: str,
    identity: CaliberIdentity,
    revision_id: str,
    change_summary: str,
    expected_lock_version: int,
) -> CaliberWorkspaceChangeRequest:
    project = _project(session, project_id, identity, "change_request.update")
    request = _request(session, project_id, change_request_id)
    _check_expected_lock(request, expected_lock_version)
    if request.status not in _MUTABLE_HEAD_STATES:
        raise _fail(409, "change_request_head_not_mutable")
    _ensure_current_base(session, project, request)
    revision = _revision(session, project_id, revision_id)
    if revision.revision_id == request.base_revision_id:
        raise _fail(400, "base_and_head_must_differ")
    request.head_generation += 1
    request.current_head_revision_id = revision.revision_id
    request.status = CR_OPEN
    request.lock_version += 1
    policy_version, policy_sha256, _checks = _head_policy(
        session, project_id, request.review_backend
    )
    new_head = _create_head(
        session,
        request=request,
        revision=revision,
        actor=identity.user_id,
        summary=change_summary,
        policy_version=policy_version,
        policy_sha256=policy_sha256,
    )
    _queue_required_checks(session, request, new_head)
    audit_record(
        session,
        actor=identity.user_id,
        action="update_workspace_change_request_head",
        entity_type="workspace_change_request",
        entity_id=request.change_request_id,
        details={"generation": request.head_generation, "revision_id": revision.revision_id},
    )
    session.commit()
    return request


def rebase_change_request(
    session: Session,
    *,
    project_id: str,
    change_request_id: str,
    identity: CaliberIdentity,
    revision_id: str,
    change_summary: str,
    expected_lock_version: int,
    semantic_version: str | None,
) -> CaliberWorkspaceChangeRequest:
    project = _project(session, project_id, identity, "change_request.update")
    request = _request(session, project_id, change_request_id)
    _check_expected_lock(request, expected_lock_version)
    if request.status != CR_OUT_OF_DATE:
        raise _fail(409, "change_request_not_out_of_date")
    revision = _revision(session, project_id, revision_id)
    new_base = project.accepted_revision_id
    if new_base is not None and new_base == revision.revision_id:
        raise _fail(400, "base_and_head_must_differ")
    claim = _active_claim(session, request.change_request_id)
    if claim is None:
        raise _fail(409, "version_claim_required")
    if _claim_version(claim) <= (
        _highest_accepted_version(session, project_id) or SemanticVersion(0, 0, 0)
    ):
        if semantic_version is None:
            raise _fail(409, "new_semantic_version_required")
        canonical, parsed = canonical_semantic_version(semantic_version)
        if parsed <= (_highest_accepted_version(session, project_id) or SemanticVersion(0, 0, 0)):
            raise _fail(409, "semantic_version_not_greater_than_accepted")
        if canonical == claim.semantic_version:
            raise _fail(409, "new_semantic_version_required")
        existing = session.execute(
            select(CaliberWorkspaceVersionClaim).where(
                CaliberWorkspaceVersionClaim.project_id == project_id,
                CaliberWorkspaceVersionClaim.semantic_version == canonical,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise _fail(409, "semantic_version_unavailable")
        _abandon_claim(claim)
        claim = _reserve_claim(
            session,
            project=project,
            change_request_id=request.change_request_id,
            semantic_version=canonical,
            actor=identity.user_id,
        )
    request.base_revision_id = new_base
    request.head_generation += 1
    request.current_head_revision_id = revision.revision_id
    request.status = CR_OPEN
    request.lock_version += 1
    policy_version, policy_sha256, _checks = _head_policy(
        session, project_id, request.review_backend
    )
    new_head = _create_head(
        session,
        request=request,
        revision=revision,
        actor=identity.user_id,
        summary=change_summary,
        policy_version=policy_version,
        policy_sha256=policy_sha256,
    )
    _queue_required_checks(session, request, new_head)
    audit_record(
        session,
        actor=identity.user_id,
        action="rebase_workspace_change_request",
        entity_type="workspace_change_request",
        entity_id=request.change_request_id,
        details={"base_revision_id": new_base, "head_revision_id": revision.revision_id},
    )
    session.commit()
    return request


def close_change_request(
    session: Session,
    *,
    project_id: str,
    change_request_id: str,
    identity: CaliberIdentity,
    reason: str,
    expected_lock_version: int,
) -> CaliberWorkspaceChangeRequest:
    _project(session, project_id, identity, "change_request.update")
    request = _request(session, project_id, change_request_id)
    _check_expected_lock(request, expected_lock_version)
    if request.status not in _CLOSEABLE_STATES:
        raise _fail(409, "change_request_not_closeable")
    if request.created_by != identity.user_id:
        _project(session, project_id, identity, "change_request.manage")
    claim = _active_claim(session, request.change_request_id)
    if claim is not None:
        _abandon_claim(claim)
    request.status = CR_CLOSED
    request.closed_reason = reason
    request.lock_version += 1
    audit_record(
        session,
        actor=identity.user_id,
        action="close_workspace_change_request",
        entity_type="workspace_change_request",
        entity_id=request.change_request_id,
        details={"reason": reason},
    )
    session.commit()
    return request


def add_comment(
    session: Session,
    *,
    project_id: str,
    change_request_id: str,
    identity: CaliberIdentity,
    body: str,
    head_id: str | None,
    resource_type: str | None,
    resource_name: str | None,
    source_path: str | None,
) -> CaliberWorkspaceChangeRequestComment:
    _project(session, project_id, identity, "change_request.comment")
    request = _request(session, project_id, change_request_id)
    if head_id is not None:
        head = session.get(CaliberWorkspaceChangeRequestHead, head_id)
        if head is None or head.change_request_id != request.change_request_id:
            raise _fail(404, "change_request_head_not_found")
    comment = CaliberWorkspaceChangeRequestComment(
        comment_id=new_workspace_change_request_comment_id(),
        change_request_id=request.change_request_id,
        head_id=head_id,
        resource_type=resource_type,
        resource_name=resource_name,
        source_path=source_path,
        body=body,
        author=identity.user_id,
    )
    session.add(comment)
    session.commit()
    return comment


def assign_reviewer(
    session: Session,
    *,
    project_id: str,
    change_request_id: str,
    identity: CaliberIdentity,
    config: Any,
    user_id: str,
    expected_lock_version: int,
) -> CaliberWorkspaceChangeRequestReviewer:
    project = _project(session, project_id, identity, "change_request.manage")
    request = _request(session, project_id, change_request_id)
    _check_expected_lock(request, expected_lock_version)
    if request.review_backend != "caliber":
        raise _fail(409, "review_backends_cannot_be_combined")
    if request.status not in _MUTABLE_HEAD_STATES | {CR_DRAFT, CR_TECHNICALLY_APPROVED}:
        raise _fail(409, "reviewer_assignment_frozen")
    _require_reviewer_eligible(session, project, user_id, config)
    active = session.execute(
        select(CaliberWorkspaceChangeRequestReviewer).where(
            CaliberWorkspaceChangeRequestReviewer.change_request_id == request.change_request_id,
            CaliberWorkspaceChangeRequestReviewer.user_id == user_id,
            CaliberWorkspaceChangeRequestReviewer.active.is_(True),
        )
    ).scalar_one_or_none()
    if active is not None:
        raise _fail(409, "reviewer_already_assigned")
    row = CaliberWorkspaceChangeRequestReviewer(
        reviewer_id=new_workspace_change_request_reviewer_id(),
        change_request_id=request.change_request_id,
        user_id=user_id,
        assigned_by=identity.user_id,
        active=True,
    )
    session.add(row)
    session.flush()
    if request.status == CR_TECHNICALLY_APPROVED:
        # A newly assigned reviewer has no current-head decision, so the
        # previously derived approval is no longer complete.
        request.status = CR_OPEN
    request.lock_version += 1
    session.commit()
    return row


def remove_reviewer(
    session: Session,
    *,
    project_id: str,
    change_request_id: str,
    identity: CaliberIdentity,
    user_id: str,
    expected_lock_version: int,
) -> CaliberWorkspaceChangeRequest:
    _project(session, project_id, identity, "change_request.manage")
    request = _request(session, project_id, change_request_id)
    _check_expected_lock(request, expected_lock_version)
    if request.status not in _MUTABLE_HEAD_STATES | {CR_DRAFT, CR_TECHNICALLY_APPROVED}:
        raise _fail(409, "reviewer_assignment_frozen")
    reviewer = session.execute(
        select(CaliberWorkspaceChangeRequestReviewer).where(
            CaliberWorkspaceChangeRequestReviewer.change_request_id == request.change_request_id,
            CaliberWorkspaceChangeRequestReviewer.user_id == user_id,
            CaliberWorkspaceChangeRequestReviewer.active.is_(True),
        )
    ).scalar_one_or_none()
    if reviewer is None:
        raise _fail(404, "reviewer_not_assigned")
    reviewer.active = False
    reviewer.removed_by = identity.user_id
    reviewer.removed_at = _now()
    request.lock_version += 1
    if request.status in {CR_OPEN, CR_TECHNICALLY_APPROVED}:
        session.flush()
        request.status = (
            CR_TECHNICALLY_APPROVED
            if _native_approval_complete(session, request, _head(session, request))
            else CR_OPEN
        )
    session.commit()
    return request


def _native_checks_pass(
    session: Session,
    request: CaliberWorkspaceChangeRequest,
    head: CaliberWorkspaceChangeRequestHead,
) -> bool:
    _version, _digest, required = _head_policy(session, request.project_id, request.review_backend)
    if not required:
        return True
    rows = (
        session.execute(
            select(CaliberWorkspaceChangeRequestCheck).where(
                CaliberWorkspaceChangeRequestCheck.head_id == head.head_id,
                CaliberWorkspaceChangeRequestCheck.check_name.in_(required),
            )
        )
        .scalars()
        .all()
    )
    latest: dict[str, CaliberWorkspaceChangeRequestCheck] = {}
    for row in rows:
        previous = latest.get(row.check_name)
        if previous is None or row.attempt_number > previous.attempt_number:
            latest[row.check_name] = row
    return all(
        latest.get(name) is not None and latest[name].status == "passed" for name in required
    )


def _native_approval_complete(
    session: Session,
    request: CaliberWorkspaceChangeRequest,
    head: CaliberWorkspaceChangeRequestHead,
) -> bool:
    assignments = (
        session.execute(
            select(CaliberWorkspaceChangeRequestReviewer).where(
                CaliberWorkspaceChangeRequestReviewer.change_request_id
                == request.change_request_id,
                CaliberWorkspaceChangeRequestReviewer.active.is_(True),
            )
        )
        .scalars()
        .all()
    )
    if not assignments:
        return False
    reviews = (
        session.execute(
            select(CaliberWorkspaceChangeRequestReview).where(
                CaliberWorkspaceChangeRequestReview.change_request_id == request.change_request_id,
                CaliberWorkspaceChangeRequestReview.head_id == head.head_id,
            )
        )
        .scalars()
        .all()
    )
    latest: dict[str, CaliberWorkspaceChangeRequestReview] = {}
    for row in reviews:
        latest[row.reviewer_id] = row
    return all(
        latest.get(assignment.reviewer_id) is not None
        and latest[assignment.reviewer_id].decision == "approve"
        for assignment in assignments
    ) and _native_checks_pass(session, request, head)


def submit_review(
    session: Session,
    *,
    project_id: str,
    change_request_id: str,
    identity: CaliberIdentity,
    config: Any,
    head_id: str,
    decision: Literal["approve", "request_changes"],
    rationale: str,
) -> CaliberWorkspaceChangeRequestReview:
    project = _project(session, project_id, identity, "change_request.review")
    request = _request(session, project_id, change_request_id)
    if request.review_backend != "caliber":
        raise _fail(409, "review_backends_cannot_be_combined")
    if request.status not in {CR_OPEN, CR_TECHNICALLY_APPROVED}:
        raise _fail(409, "change_request_not_reviewable")
    head = _head(session, request)
    if head.head_id != head_id:
        raise _fail(409, "review_head_is_stale")
    revision = _revision(session, project_id, head.revision_id)
    reviewer = session.execute(
        select(CaliberWorkspaceChangeRequestReviewer).where(
            CaliberWorkspaceChangeRequestReviewer.change_request_id == request.change_request_id,
            CaliberWorkspaceChangeRequestReviewer.user_id == identity.user_id,
            CaliberWorkspaceChangeRequestReviewer.active.is_(True),
        )
    ).scalar_one_or_none()
    if reviewer is None:
        raise _fail(403, "reviewer_assignment_required")
    role = _require_reviewer_eligible(session, project, identity.user_id, config)
    if revision.created_by == identity.user_id:
        raise _fail(403, "self_review_forbidden")
    review = CaliberWorkspaceChangeRequestReview(
        review_id=new_workspace_change_request_review_id(),
        change_request_id=request.change_request_id,
        head_id=head.head_id,
        reviewer_id=reviewer.reviewer_id,
        decision=decision,
        rationale=rationale,
        actor_role=role,
        actor_scopes=sorted(identity.scopes),
    )
    session.add(review)
    # The test/application session factory deliberately disables autoflush so
    # route reads do not unexpectedly flush unrelated work. Approval
    # aggregation must nevertheless include the review being submitted.
    session.flush()
    if decision == "request_changes":
        request.status = CR_CHANGES_REQUESTED
    elif _native_approval_complete(session, request, head):
        request.status = CR_TECHNICALLY_APPROVED
    request.lock_version += 1
    session.commit()
    return review


def record_check(
    session: Session,
    *,
    change_request_id: str,
    head_id: str,
    check_name: str,
    status: Literal["queued", "running", "passed", "failed", "cancelled"],
    implementation_version: str,
    input_digest: str,
    evidence_ref: str | None = None,
    evidence_digest: str | None = None,
    claimed_by: str | None = None,
) -> CaliberWorkspaceChangeRequestCheck:
    """Internal worker hook for durable check attempts; no public write route."""
    head = session.get(CaliberWorkspaceChangeRequestHead, head_id)
    if head is None or head.change_request_id != change_request_id:
        raise _fail(404, "change_request_head_not_found")
    active = session.execute(
        select(CaliberWorkspaceChangeRequestCheck).where(
            CaliberWorkspaceChangeRequestCheck.head_id == head_id,
            CaliberWorkspaceChangeRequestCheck.check_name == check_name,
            CaliberWorkspaceChangeRequestCheck.status.in_(("queued", "running")),
        )
    ).scalar_one_or_none()
    if active is not None:
        active.status = status
        active.implementation_version = implementation_version
        active.input_digest = input_digest
        active.evidence_ref = evidence_ref
        active.evidence_digest = evidence_digest
        active.claimed_by = claimed_by
        active.completed_at = _now() if status in {"passed", "failed", "cancelled"} else None
        session.commit()
        return active
    latest = session.execute(
        select(CaliberWorkspaceChangeRequestCheck.attempt_number)
        .where(
            CaliberWorkspaceChangeRequestCheck.head_id == head_id,
            CaliberWorkspaceChangeRequestCheck.check_name == check_name,
        )
        .order_by(CaliberWorkspaceChangeRequestCheck.attempt_number.desc())
        .limit(1)
    ).scalar_one_or_none()
    row = CaliberWorkspaceChangeRequestCheck(
        check_id=new_workspace_change_request_check_id(),
        head_id=head_id,
        check_name=check_name,
        attempt_number=(int(latest) + 1) if latest is not None else 1,
        implementation_version=implementation_version,
        input_digest=input_digest,
        evidence_ref=evidence_ref,
        evidence_digest=evidence_digest,
        status=status,
        claimed_by=claimed_by,
        completed_at=_now() if status in {"passed", "failed", "cancelled"} else None,
    )
    session.add(row)
    session.commit()
    return row


def _verified_external_attestation(
    session: Session,
    request: CaliberWorkspaceChangeRequest,
    head: CaliberWorkspaceChangeRequestHead,
    revision: CaliberWorkspaceRevision,
) -> bool:
    rows = (
        session.execute(
            select(CaliberWorkspaceExternalReviewAttestation).where(
                CaliberWorkspaceExternalReviewAttestation.change_request_id
                == request.change_request_id,
                CaliberWorkspaceExternalReviewAttestation.head_id == head.head_id,
                CaliberWorkspaceExternalReviewAttestation.status == "verified",
                CaliberWorkspaceExternalReviewAttestation.workspace_revision_sha256
                == revision.revision_sha256,
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return False
    source = _active_source(session, request.project_id)
    policy_version, policy_sha, required = _source_policy(session, request.project_id)
    for row in rows:
        conclusions = row.check_conclusions if isinstance(row.check_conclusions, dict) else {}
        if (
            row.policy_version != policy_version
            or row.policy_sha256 != policy_sha
            or row.source_id != source.source_id
            or head.review_policy_version != policy_version
            or head.review_policy_sha256 != policy_sha
            or row.required_checks != required
            or row.uncovered_commits
            or row.uncovered_paths
        ):
            continue
        trusted_sources = (
            row.trusted_check_sources if isinstance(row.trusted_check_sources, list) else []
        )
        if required and not trusted_sources:
            continue
        if any(conclusions.get(name) not in {"success", "passed", True} for name in required):
            continue
        actors = row.review_actors if isinstance(row.review_actors, list) else []
        normalized_actors = [item for item in actors if isinstance(item, dict)]
        if (
            not normalized_actors
            or any(item.get("user_id") == request.created_by for item in normalized_actors)
            or not any(
                item.get("decision") in {"approve", "approved"} for item in normalized_actors
            )
        ):
            continue
        return True
    return False


def record_external_attestation(
    session: Session,
    *,
    change_request_id: str,
    head_id: str,
    source_id: str,
    provider_change_request_id: str,
    provider_url: str | None,
    provider_head_commit: str,
    provider_resulting_commit: str,
    source_tree_sha256: str,
    workspace_revision_sha256: str,
    policy_version: str,
    policy_sha256: str,
    required_checks: list[str],
    trusted_check_sources: list[str],
    check_conclusions: dict[str, object],
    review_actors: list[dict[str, object]],
    adapter_version: str,
    verification_input_digest: str,
    status: Literal["verified", "insufficient", "stale", "revoked"],
    reason: str = "",
    provider_ruleset_sha256: str | None = None,
    merge_method: str | None = None,
    merge_actor: str | None = None,
    merged_at: datetime | None = None,
    provider_event_ids: list[str] | None = None,
    coverage_digest: str | None = None,
    uncovered_commits: list[str] | None = None,
    uncovered_paths: list[str] | None = None,
    trusted_adapter: bool = False,
) -> CaliberWorkspaceExternalReviewAttestation:
    """Store normalized provider evidence; verified rows require an adapter trust boundary."""
    if status == "verified" and not trusted_adapter:
        raise _fail(403, "verified_attestation_requires_trusted_adapter")
    request = session.get(CaliberWorkspaceChangeRequest, change_request_id)
    head = session.get(CaliberWorkspaceChangeRequestHead, head_id)
    if request is None or head is None or head.change_request_id != change_request_id:
        raise _fail(404, "change_request_head_not_found")
    if request.review_backend != "source_provider":
        raise _fail(409, "review_backends_cannot_be_combined")
    source = session.get(CaliberWorkspaceSource, source_id)
    if source is None or source.project_id != request.project_id:
        raise _fail(404, "source_not_found")
    revision = session.get(CaliberWorkspaceRevision, head.revision_id)
    if revision is None or revision.revision_sha256 != workspace_revision_sha256:
        raise _fail(409, "attestation_revision_digest_mismatch")
    row = CaliberWorkspaceExternalReviewAttestation(
        attestation_id=new_workspace_external_review_attestation_id(),
        change_request_id=change_request_id,
        head_id=head_id,
        source_id=source_id,
        provider_change_request_id=provider_change_request_id,
        provider_url=provider_url,
        provider_head_commit=provider_head_commit,
        provider_resulting_commit=provider_resulting_commit,
        source_tree_sha256=source_tree_sha256,
        workspace_revision_sha256=workspace_revision_sha256,
        policy_version=policy_version,
        policy_sha256=policy_sha256,
        provider_ruleset_sha256=provider_ruleset_sha256,
        required_checks=list(required_checks),
        trusted_check_sources=list(trusted_check_sources),
        check_conclusions=dict(check_conclusions),
        review_actors=list(review_actors),
        merge_method=merge_method,
        merge_actor=merge_actor,
        merged_at=merged_at,
        provider_event_ids=list(provider_event_ids or []),
        adapter_version=adapter_version,
        status=status,
        reason=reason,
        verification_input_digest=verification_input_digest,
        coverage_digest=coverage_digest,
        uncovered_commits=list(uncovered_commits or []),
        uncovered_paths=list(uncovered_paths or []),
    )
    session.add(row)
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise _fail(409, "duplicate_external_attestation") from exc
    return row


def create_version_tag(
    session: Session,
    *,
    project_id: str,
    change_request_id: str,
    revision_id: str,
    tag: str,
    kind: Literal["qa_candidate", "accepted"],
    actor: str,
) -> CaliberWorkspaceVersionTag:
    """Internal immutable tag repository used by Phase 5 release services."""
    project = session.get(CaliberProject, project_id)
    if project is None:
        raise _fail(404, "project_not_found")
    revision = _revision(session, project_id, revision_id)
    request = _request(session, project_id, change_request_id)
    claim = _active_claim(session, change_request_id)
    if claim is None:
        raise _fail(409, "version_claim_required")
    canonical, _parsed = canonical_semantic_version(claim.semantic_version)
    expected = f"{canonical}-rc.{request.head_generation}" if kind == "qa_candidate" else canonical
    if tag != expected:
        raise _fail(400, "version_tag_does_not_match_claim")
    row = CaliberWorkspaceVersionTag(
        tag_id=new_workspace_version_tag_id(),
        project_id=project_id,
        revision_id=revision.revision_id,
        change_request_id=request.change_request_id,
        tag=tag,
        kind=kind,
        created_by=actor,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise _fail(409, "version_tag_unavailable") from exc
    return row


def accept_change_request(
    session: Session,
    *,
    project_id: str,
    change_request_id: str,
    actor: str,
    qa_evidence: dict[str, object],
) -> bool:
    """Phase-5-gated acceptance CAS; returns false after marking stale.

    Reachable both directly (as Phase 5's QA release service and this
    module's own tests call it) and from
    :func:`caliber.routes.workspace_change_requests.accept_route` (`P4-D`'s
    public acceptance route). ``qa_evidence`` is trusted as given -- this
    function itself does not re-derive or re-verify it against any release
    record; that verification is each caller's responsibility. The HTTP
    route never forwards a caller-supplied body for this parameter; it
    resolves ``qa_evidence`` itself from a durable, server-recorded QA `go`
    decision bound to the exact head being accepted before calling this
    function, so an HTTP caller cannot assert ``"passed": True`` without a
    real QA decision having been recorded first.

    Requires explicit QA evidence and technical approval, then atomically
    advances ``caliber_projects.accepted_revision_id``. A racing request loses
    the conditional update and is marked ``out_of_date`` without changing the
    accepted pointer.
    """
    request = session.get(CaliberWorkspaceChangeRequest, change_request_id)
    project = session.get(CaliberProject, project_id)
    if request is None or project is None or request.project_id != project_id:
        raise _fail(404, "change_request_not_found")
    if request.status not in {CR_TECHNICALLY_APPROVED, CR_QA_IN_PROGRESS}:
        raise _fail(409, "change_request_not_qa_eligible")
    head = _head(session, request)
    revision = _revision(session, project_id, head.revision_id)
    if (
        qa_evidence.get("passed") is not True
        or qa_evidence.get("revision_sha256") != revision.revision_sha256
    ):
        raise _fail(409, "qa_evidence_insufficient")
    claim = _active_claim(session, change_request_id)
    if claim is None:
        raise _fail(409, "version_claim_required")
    _ensure_version_is_new(session, project_id, _claim_version(claim))
    # Read the pointer in this transaction immediately before the CAS. This
    # matters for the Phase-5 race: the session may have loaded a project
    # object before another worker accepted a competing request, while the
    # conditional UPDATE must compare against the current database value.
    # The request's base is the compare-and-set expectation. A request created
    # before the first acceptance therefore carries an explicit NULL
    # expectation, and cannot win after another request advances the pointer.
    # The conditional UPDATE reads the current database value atomically with
    # the write; no stale ORM project value participates in the decision.
    expected = request.base_revision_id
    condition = (
        CaliberProject.accepted_revision_id.is_(None)
        if expected is None
        else CaliberProject.accepted_revision_id == expected
    )
    result = session.execute(
        update(CaliberProject)
        .where(CaliberProject.project_id == project_id, condition)
        .values(accepted_revision_id=head.revision_id)
    )
    if int(getattr(result, "rowcount", 0) or 0) != 1:
        request.status = CR_OUT_OF_DATE
        request.lock_version += 1
        session.commit()
        return False
    tag = create_version_tag(
        session,
        project_id=project_id,
        change_request_id=change_request_id,
        revision_id=head.revision_id,
        tag=claim.semantic_version,
        kind="accepted",
        actor=actor,
    )
    del tag
    claim.status = "accepted"
    claim.accepted_at = _now()
    request.status = CR_ACCEPTED
    request.accepted_at = _now()
    request.accepted_by = actor
    request.lock_version += 1
    audit_record(
        session,
        actor=actor,
        action="accept_workspace_change_request",
        entity_type="workspace_change_request",
        entity_id=change_request_id,
        details={"revision_id": head.revision_id, "tag": claim.semantic_version},
    )
    session.commit()
    return True


def schema_timestamp(value: datetime | None) -> str | None:
    """Public helper shared by the route serializers."""
    return _iso(value)


__all__ = [
    "CR_ACCEPTED",
    "CR_CHANGES_REQUESTED",
    "CR_CLOSED",
    "CR_DRAFT",
    "CR_OPEN",
    "CR_OUT_OF_DATE",
    "CR_QA_IN_PROGRESS",
    "CR_TECHNICALLY_APPROVED",
    "SemanticVersion",
    "accept_change_request",
    "add_comment",
    "assign_reviewer",
    "canonical_semantic_version",
    "close_change_request",
    "create_change_request",
    "create_version_tag",
    "rebase_change_request",
    "record_check",
    "record_external_attestation",
    "remove_reviewer",
    "schema_timestamp",
    "submit_change_request",
    "submit_review",
    "update_change_request_head",
]
