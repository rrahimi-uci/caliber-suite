"""Workspace Change Request, review-history, and version-tag routes (`P4-D`).

The HTTP surface exposes recoverable history and authorizes every operation in
the same SQLAlchemy session that reads or mutates the project-scoped rows.

``accept_route`` (``POST .../change-requests/{id}:accept``) is the public
acceptance route: it wires
:func:`caliber.workspace_change_request_service.accept_change_request`'s
compare-and-swap up to HTTP. That function takes a caller-supplied
``qa_evidence: dict[str, object]`` and trusts it as given -- fine for a
direct Python call from a test or from Phase 5's own QA release service, but
a real authorization hole if an HTTP caller could just POST
``{"passed": true}``. ``accept_route`` never accepts (or even parses) a
request body for this reason; ``_resolve_verified_qa_evidence`` derives
``qa_evidence`` itself from a durable, server-recorded
`CaliberWorkspaceReleaseDecision` row (`kind="quality"`, `decision="go"`,
Phase 5's `workspace_release_governance.py::record_workspace_release_decision`)
that is bound to the Change Request's *current* head
(`change_request_head_id` equality) and *current* revision digest
(`revision_sha256` equality) via a `CaliberWorkspaceRelease` in a
`qa`-class environment for this Change Request. See
``_resolve_verified_qa_evidence``'s own docstring for the exact query and
the edge cases it was written to cover.
"""

from __future__ import annotations

import base64
from datetime import datetime
from typing import Any, Literal, cast

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.auth import (
    SCOPE_OPERATOR,
    CaliberIdentity,
    require_scopes,
    require_user,
    resolve_identity,
)
from caliber.db.models import (
    CaliberWorkspaceChangeRequest,
    CaliberWorkspaceChangeRequestCheck,
    CaliberWorkspaceChangeRequestComment,
    CaliberWorkspaceChangeRequestHead,
    CaliberWorkspaceChangeRequestReview,
    CaliberWorkspaceChangeRequestReviewer,
    CaliberWorkspaceEnvironment,
    CaliberWorkspaceExternalReviewAttestation,
    CaliberWorkspaceRelease,
    CaliberWorkspaceReleaseDecision,
    CaliberWorkspaceVersionClaim,
    CaliberWorkspaceVersionTag,
)
from caliber.resource_access import require_project_access
from caliber.routes._deps import (
    envelope_response,
    get_session_factory,
    list_limit,
    parse_json_object,
)
from caliber.schemas import (
    WorkspaceChangeRequestCheckSchema,
    WorkspaceChangeRequestCloseRequest,
    WorkspaceChangeRequestCommentRequest,
    WorkspaceChangeRequestCommentSchema,
    WorkspaceChangeRequestCreateRequest,
    WorkspaceChangeRequestHeadSchema,
    WorkspaceChangeRequestHeadUpdateRequest,
    WorkspaceChangeRequestListSchema,
    WorkspaceChangeRequestRebaseRequest,
    WorkspaceChangeRequestReviewerRequest,
    WorkspaceChangeRequestReviewerSchema,
    WorkspaceChangeRequestReviewRequest,
    WorkspaceChangeRequestReviewSchema,
    WorkspaceChangeRequestSchema,
    WorkspaceChangeRequestSubmitRequest,
    WorkspaceExternalReviewAttestationSchema,
    WorkspaceVersionTagSchema,
)
from caliber.workspace_change_request_service import (
    accept_change_request,
    add_comment,
    assign_reviewer,
    close_change_request,
    create_change_request,
    rebase_change_request,
    remove_reviewer,
    submit_change_request,
    submit_review,
    update_change_request_head,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber"
LIST_PATH = PREFIX + "/projects/{project_id}/change-requests"
DETAIL_PATH = LIST_PATH + "/{change_request_id}"
SUBMIT_PATH = DETAIL_PATH + ":submit"
UPDATE_HEAD_PATH = DETAIL_PATH + ":update-head"
REBASE_PATH = DETAIL_PATH + ":rebase"
CLOSE_PATH = DETAIL_PATH + ":close"
ACCEPT_PATH = DETAIL_PATH + ":accept"
COMMENTS_PATH = DETAIL_PATH + "/comments"
REVIEWERS_PATH = DETAIL_PATH + "/reviewers"
REVIEWER_DETAIL_PATH = REVIEWERS_PATH + "/{user_id}"
REVIEWS_PATH = DETAIL_PATH + "/reviews"
ATTESTATIONS_PATH = DETAIL_PATH + "/external-review-attestations"
REFRESH_EXTERNAL_PATH = DETAIL_PATH + ":refresh-external-review"
CHECKS_PATH = DETAIL_PATH + "/checks"
TAGS_PATH = PREFIX + "/projects/{project_id}/version-tags"
TAG_DETAIL_PATH = TAGS_PATH + "/{tag}"

_Factory = sessionmaker[Session]


def _require_workspace_header(request: Request, project_id: str) -> None:
    header = request.headers.get("X-CALIBER-Project")
    if header is not None and header.strip() != project_id:
        raise HTTPException(status_code=400, detail="workspace_context_mismatch")


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _head_schema(row: CaliberWorkspaceChangeRequestHead) -> WorkspaceChangeRequestHeadSchema:
    return WorkspaceChangeRequestHeadSchema(
        head_id=row.head_id,
        change_request_id=row.change_request_id,
        generation=row.generation,
        revision_id=row.revision_id,
        revision_sha256=row.revision_sha256,
        review_policy_version=row.review_policy_version,
        review_policy_sha256=row.review_policy_sha256,
        changed_by=row.changed_by,
        change_summary=row.change_summary,
        created_at=_iso(row.created_at),
    )


def _head_dict(row: CaliberWorkspaceChangeRequestHead) -> dict[str, object]:
    return _head_schema(row).model_dump(mode="json")


def _claim_dict(row: Any) -> dict[str, object]:
    return {
        "claim_id": row.claim_id,
        "project_id": row.project_id,
        "change_request_id": row.change_request_id,
        "semantic_version": row.semantic_version,
        "status": row.status,
        "claimed_by": row.claimed_by,
        "claimed_at": _iso(row.claimed_at),
        "accepted_at": _iso(row.accepted_at),
        "abandoned_at": _iso(row.abandoned_at),
    }


def _request_schema(
    session: Session, row: CaliberWorkspaceChangeRequest
) -> WorkspaceChangeRequestSchema:
    head = session.execute(
        select(CaliberWorkspaceChangeRequestHead).where(
            CaliberWorkspaceChangeRequestHead.change_request_id == row.change_request_id,
            CaliberWorkspaceChangeRequestHead.generation == row.head_generation,
        )
    ).scalar_one()
    claim = session.execute(
        select(CaliberWorkspaceVersionClaim).where(
            CaliberWorkspaceVersionClaim.change_request_id == row.change_request_id,
            CaliberWorkspaceVersionClaim.status == "reserved",
        )
    ).scalar_one_or_none()
    if claim is None:
        # Abandoned/accepted claims remain as immutable history. Once a
        # rebase replaces a reservation there are multiple rows, so never use
        # scalar_one_or_none() for the historical fallback.
        claim = (
            session.execute(
                select(CaliberWorkspaceVersionClaim)
                .where(CaliberWorkspaceVersionClaim.change_request_id == row.change_request_id)
                .order_by(
                    CaliberWorkspaceVersionClaim.claimed_at.desc(),
                    CaliberWorkspaceVersionClaim.claim_id.desc(),
                )
            )
            .scalars()
            .first()
        )
    count = session.scalar(
        select(func.count())
        .select_from(CaliberWorkspaceChangeRequestReviewer)
        .where(
            CaliberWorkspaceChangeRequestReviewer.change_request_id == row.change_request_id,
            CaliberWorkspaceChangeRequestReviewer.active.is_(True),
        )
    )
    return WorkspaceChangeRequestSchema(
        change_request_id=row.change_request_id,
        project_id=row.project_id,
        base_revision_id=row.base_revision_id,
        current_head_revision_id=row.current_head_revision_id,
        created_by=row.created_by,
        title=row.title,
        description=row.description,
        head_generation=row.head_generation,
        status=cast(
            Literal[
                "draft",
                "open",
                "changes_requested",
                "technically_approved",
                "qa_in_progress",
                "out_of_date",
                "accepted",
                "closed",
            ],
            row.status,
        ),
        review_backend=cast(Literal["caliber", "source_provider"], row.review_backend),
        accepted_at=_iso(row.accepted_at),
        accepted_by=row.accepted_by,
        closed_reason=row.closed_reason,
        lock_version=row.lock_version,
        created_at=_iso(row.created_at),
        updated_at=_iso(row.updated_at),
        current_head=_head_dict(head),
        version_claim=_claim_dict(claim) if claim is not None else None,
        active_reviewer_count=int(count or 0),
    )


def _decode_cursor(raw: str | None) -> tuple[datetime, str] | None:
    if not raw:
        return None
    try:
        stamp, row_id = base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8").split("\n", 1)
        return datetime.fromisoformat(stamp), row_id
    except (ValueError, UnicodeDecodeError, UnicodeError) as exc:
        raise HTTPException(status_code=400, detail="invalid_cursor") from exc


def _encode_cursor(stamp: datetime, row_id: str) -> str:
    return base64.urlsafe_b64encode(f"{stamp.isoformat()}\n{row_id}".encode()).decode("ascii")


def _page_clause(column: Any, row_id_column: Any, cursor: tuple[datetime, str] | None) -> Any:
    if cursor is None:
        return None
    stamp, row_id = cursor
    return (column < stamp) | ((column == stamp) & (row_id_column < row_id))


def _list_requests_sync(
    factory: _Factory,
    *,
    project_id: str,
    identity: CaliberIdentity,
    project_action: str,
    limit: int,
    cursor: tuple[datetime, str] | None,
    status: str | None,
    created_by: str | None,
    reviewer_user_id: str | None,
    semantic_version: str | None,
) -> tuple[list[WorkspaceChangeRequestSchema], str | None]:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        stmt = select(CaliberWorkspaceChangeRequest).where(
            CaliberWorkspaceChangeRequest.project_id == project_id
        )
        if status:
            stmt = stmt.where(CaliberWorkspaceChangeRequest.status == status)
        if created_by:
            stmt = stmt.where(CaliberWorkspaceChangeRequest.created_by == created_by)
        if reviewer_user_id:
            stmt = stmt.where(
                select(CaliberWorkspaceChangeRequestReviewer.reviewer_id)
                .where(
                    CaliberWorkspaceChangeRequestReviewer.change_request_id
                    == CaliberWorkspaceChangeRequest.change_request_id,
                    CaliberWorkspaceChangeRequestReviewer.user_id == reviewer_user_id,
                )
                .exists()
            )
        if semantic_version:
            stmt = stmt.where(
                select(CaliberWorkspaceVersionClaim.claim_id)
                .where(
                    CaliberWorkspaceVersionClaim.change_request_id
                    == CaliberWorkspaceChangeRequest.change_request_id,
                    CaliberWorkspaceVersionClaim.semantic_version == semantic_version,
                )
                .exists()
            )
        clause = _page_clause(
            CaliberWorkspaceChangeRequest.created_at,
            CaliberWorkspaceChangeRequest.change_request_id,
            cursor,
        )
        if clause is not None:
            stmt = stmt.where(clause)
        rows = (
            session.execute(
                stmt.order_by(
                    CaliberWorkspaceChangeRequest.created_at.desc(),
                    CaliberWorkspaceChangeRequest.change_request_id.desc(),
                ).limit(limit + 1)
            )
            .scalars()
            .all()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (
            _encode_cursor(rows[-1].created_at, rows[-1].change_request_id) if has_more else None
        )
        return [_request_schema(session, row) for row in rows], next_cursor


def _get_request_sync(
    factory: _Factory,
    *,
    project_id: str,
    change_request_id: str,
    identity: CaliberIdentity,
    project_action: str,
) -> WorkspaceChangeRequestSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        row = session.execute(
            select(CaliberWorkspaceChangeRequest).where(
                CaliberWorkspaceChangeRequest.project_id == project_id,
                CaliberWorkspaceChangeRequest.change_request_id == change_request_id,
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="change_request_not_found")
        return _request_schema(session, row)


def _list_related_sync(
    factory: _Factory,
    *,
    project_id: str,
    change_request_id: str,
    identity: CaliberIdentity,
    kind: Literal["heads", "comments", "reviewers", "reviews", "attestations", "checks"],
    limit: int,
    cursor: tuple[datetime, str] | None,
    project_action: str,
) -> tuple[list[Any], str | None]:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        request = session.execute(
            select(CaliberWorkspaceChangeRequest).where(
                CaliberWorkspaceChangeRequest.project_id == project_id,
                CaliberWorkspaceChangeRequest.change_request_id == change_request_id,
            )
        ).scalar_one_or_none()
        if request is None:
            raise HTTPException(status_code=404, detail="change_request_not_found")
        config: dict[str, Any] = {
            "heads": (
                CaliberWorkspaceChangeRequestHead,
                CaliberWorkspaceChangeRequestHead.created_at,
                CaliberWorkspaceChangeRequestHead.head_id,
            ),
            "comments": (
                CaliberWorkspaceChangeRequestComment,
                CaliberWorkspaceChangeRequestComment.created_at,
                CaliberWorkspaceChangeRequestComment.comment_id,
            ),
            "reviewers": (
                CaliberWorkspaceChangeRequestReviewer,
                CaliberWorkspaceChangeRequestReviewer.assigned_at,
                CaliberWorkspaceChangeRequestReviewer.reviewer_id,
            ),
            "reviews": (
                CaliberWorkspaceChangeRequestReview,
                CaliberWorkspaceChangeRequestReview.created_at,
                CaliberWorkspaceChangeRequestReview.review_id,
            ),
            "attestations": (
                CaliberWorkspaceExternalReviewAttestation,
                CaliberWorkspaceExternalReviewAttestation.verified_at,
                CaliberWorkspaceExternalReviewAttestation.attestation_id,
            ),
            "checks": (
                CaliberWorkspaceChangeRequestCheck,
                CaliberWorkspaceChangeRequestCheck.created_at,
                CaliberWorkspaceChangeRequestCheck.check_id,
            ),
        }
        model, timestamp_column, id_column = config[kind]
        parent_column = (
            model.change_request_id
            if kind != "checks"
            else CaliberWorkspaceChangeRequestHead.change_request_id
        )
        stmt = select(model)
        if kind == "checks":
            stmt = stmt.join(
                CaliberWorkspaceChangeRequestHead,
                model.head_id == CaliberWorkspaceChangeRequestHead.head_id,
            )
            stmt = stmt.where(parent_column == request.change_request_id)
        else:
            stmt = stmt.where(parent_column == request.change_request_id)
        clause = _page_clause(timestamp_column, id_column, cursor)
        if clause is not None:
            stmt = stmt.where(clause)
        rows = (
            session.execute(
                stmt.order_by(timestamp_column.desc(), id_column.desc()).limit(limit + 1)
            )
            .scalars()
            .all()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (
            _encode_cursor(
                getattr(rows[-1], timestamp_column.key), getattr(rows[-1], id_column.key)
            )
            if has_more
            else None
        )
        payload: list[Any]
        if kind == "heads":
            payload = [_head_schema(row) for row in rows]
        elif kind == "comments":
            payload = [
                WorkspaceChangeRequestCommentSchema.model_validate(
                    {**row.__dict__, "created_at": _iso(row.created_at)}
                )
                for row in rows
            ]
        elif kind == "reviewers":
            payload = [
                WorkspaceChangeRequestReviewerSchema.model_validate(
                    {
                        **row.__dict__,
                        "assigned_at": _iso(row.assigned_at),
                        "removed_at": _iso(row.removed_at),
                    }
                )
                for row in rows
            ]
        elif kind == "reviews":
            payload = [
                WorkspaceChangeRequestReviewSchema.model_validate(
                    {**row.__dict__, "created_at": _iso(row.created_at)}
                )
                for row in rows
            ]
        elif kind == "attestations":
            payload = [
                WorkspaceExternalReviewAttestationSchema.model_validate(
                    {
                        **row.__dict__,
                        "verified_at": _iso(row.verified_at),
                        "merged_at": _iso(row.merged_at),
                    }
                )
                for row in rows
            ]
        else:
            payload = [
                WorkspaceChangeRequestCheckSchema.model_validate(
                    {
                        **row.__dict__,
                        "claimed_at": _iso(row.claimed_at),
                        "lease_expires_at": _iso(row.lease_expires_at),
                        "completed_at": _iso(row.completed_at),
                        "created_at": _iso(row.created_at),
                    }
                )
                for row in rows
            ]
        return payload, next_cursor


def _list_tags_sync(
    factory: _Factory,
    *,
    project_id: str,
    identity: CaliberIdentity,
    limit: int,
    cursor: tuple[datetime, str] | None,
    project_action: str,
) -> tuple[list[WorkspaceVersionTagSchema], str | None]:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        stmt = select(CaliberWorkspaceVersionTag).where(
            CaliberWorkspaceVersionTag.project_id == project_id
        )
        clause = _page_clause(
            CaliberWorkspaceVersionTag.created_at, CaliberWorkspaceVersionTag.tag_id, cursor
        )
        if clause is not None:
            stmt = stmt.where(clause)
        rows = (
            session.execute(
                stmt.order_by(
                    CaliberWorkspaceVersionTag.created_at.desc(),
                    CaliberWorkspaceVersionTag.tag_id.desc(),
                ).limit(limit + 1)
            )
            .scalars()
            .all()
        )
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = _encode_cursor(rows[-1].created_at, rows[-1].tag_id) if has_more else None
        return [
            WorkspaceVersionTagSchema.model_validate(
                {**row.__dict__, "created_at": _iso(row.created_at)}
            )
            for row in rows
        ], next_cursor


def _get_tag_sync(
    factory: _Factory,
    *,
    project_id: str,
    tag: str,
    identity: CaliberIdentity,
    project_action: str,
) -> WorkspaceVersionTagSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        row = session.execute(
            select(CaliberWorkspaceVersionTag).where(
                CaliberWorkspaceVersionTag.project_id == project_id,
                CaliberWorkspaceVersionTag.tag == tag,
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="version_tag_not_found")
        return WorkspaceVersionTagSchema.model_validate(
            {**row.__dict__, "created_at": _iso(row.created_at)}
        )


def _page(request: Request) -> tuple[int, tuple[datetime, str] | None]:
    limit, _offset = list_limit(request, default=50, cap=200)
    return limit, _decode_cursor(request.query_params.get("cursor"))


def _create_change_request_sync(
    factory: _Factory,
    *,
    project_id: str,
    identity: CaliberIdentity,
    config: Any,
    payload: WorkspaceChangeRequestCreateRequest,
    project_action: str,
) -> WorkspaceChangeRequestSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        row = create_change_request(
            session,
            project_id=project_id,
            identity=identity,
            config=config,
            **payload.model_dump(),
        )
        return _request_schema(session, row)


def _submit_change_request_sync(
    factory: _Factory,
    *,
    project_id: str,
    change_request_id: str,
    identity: CaliberIdentity,
    config: Any,
    project_action: str,
) -> WorkspaceChangeRequestSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        row = submit_change_request(
            session,
            project_id=project_id,
            change_request_id=change_request_id,
            identity=identity,
            config=config,
        )
        return _request_schema(session, row)


def _update_head_sync(
    factory: _Factory,
    *,
    project_id: str,
    change_request_id: str,
    identity: CaliberIdentity,
    payload: WorkspaceChangeRequestHeadUpdateRequest,
    project_action: str,
) -> WorkspaceChangeRequestSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        row = update_change_request_head(
            session,
            project_id=project_id,
            change_request_id=change_request_id,
            identity=identity,
            **payload.model_dump(),
        )
        return _request_schema(session, row)


def _rebase_sync(
    factory: _Factory,
    *,
    project_id: str,
    change_request_id: str,
    identity: CaliberIdentity,
    payload: WorkspaceChangeRequestRebaseRequest,
    project_action: str,
) -> WorkspaceChangeRequestSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        row = rebase_change_request(
            session,
            project_id=project_id,
            change_request_id=change_request_id,
            identity=identity,
            **payload.model_dump(),
        )
        return _request_schema(session, row)


def _close_sync(
    factory: _Factory,
    *,
    project_id: str,
    change_request_id: str,
    identity: CaliberIdentity,
    payload: WorkspaceChangeRequestCloseRequest,
    project_action: str,
) -> WorkspaceChangeRequestSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        row = close_change_request(
            session,
            project_id=project_id,
            change_request_id=change_request_id,
            identity=identity,
            **payload.model_dump(),
        )
        return _request_schema(session, row)


def _resolve_verified_qa_evidence(
    session: Session,
    *,
    project_id: str,
    change_request_id: str,
    head: CaliberWorkspaceChangeRequestHead,
) -> dict[str, object]:
    """Derive ``qa_evidence`` from a durable, server-verified QA decision.

    Never trusts a caller-supplied claim of ``"passed": true`` -- see this
    module's own docstring for the full reasoning. A qualifying decision is
    a ``kind="quality"``/``decision="go"`` `CaliberWorkspaceReleaseDecision`
    row that is:

    * bound to *this exact* Change Request head (`change_request_head_id`
      equality against the current head, not merely some past head of this
      request);
    * bound to *this exact* revision digest (`revision_sha256` equality
      against the current head's digest, the same field
      `accept_change_request` itself re-checks); and
    * recorded against a `CaliberWorkspaceRelease` for this Change Request
      whose environment is `qa`-class (Phase 5's QA gate, not a staging/
      production release, and not a release for a different Change
      Request that merely happens to touch the same project).

    Edge cases this was written to handle:

    * **Multiple releases/retries against the same head.** Nothing here
      requires exactly one release or one decision -- `LIMIT 1` on any
      matching row is sufficient. A failed QA attempt followed by a
      passing retry (two separate `CaliberWorkspaceRelease` rows, two
      separate decisions) qualifies via its `go` row.
    * **A `no_go` decision existing alongside a later `go`.** Each
      decision is scoped to one `workspace_release_id`
      (`CaliberWorkspaceReleaseDecision`'s own
      `(workspace_release_id, kind)` uniqueness), so a `no_go` recorded
      against one release attempt never shadows a `go` recorded against a
      different one for the same head -- the query only cares whether a
      qualifying `go` row exists at all, not what else exists alongside
      it.
    * **An out-of-date (stale) head.** After a rebase, the Change
      Request's current head is a new `head_id`/`revision_sha256` pair. A
      decision recorded against the prior head fails both equality checks
      and is correctly excluded, forcing a fresh QA pass on the rebased
      head -- exactly like `accept_change_request`'s own base-revision CAS
      already forces re-review after a competing acceptance.
    * **No QA decision at all, or only a `no_go`.** Both fail closed with
      a `409`, never falling back to trusting a request body.
    """
    decision = session.execute(
        select(CaliberWorkspaceReleaseDecision)
        .join(
            CaliberWorkspaceRelease,
            CaliberWorkspaceReleaseDecision.workspace_release_id
            == CaliberWorkspaceRelease.release_id,
        )
        .join(
            CaliberWorkspaceEnvironment,
            CaliberWorkspaceRelease.environment_id == CaliberWorkspaceEnvironment.environment_id,
        )
        .where(
            CaliberWorkspaceRelease.project_id == project_id,
            CaliberWorkspaceRelease.change_request_id == change_request_id,
            CaliberWorkspaceEnvironment.environment_class == "qa",
            CaliberWorkspaceReleaseDecision.kind == "quality",
            CaliberWorkspaceReleaseDecision.decision == "go",
            CaliberWorkspaceReleaseDecision.change_request_head_id == head.head_id,
            CaliberWorkspaceReleaseDecision.revision_sha256 == head.revision_sha256,
        )
        .order_by(CaliberWorkspaceReleaseDecision.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if decision is None:
        raise HTTPException(status_code=409, detail="qa_go_decision_required")
    return {"passed": True, "revision_sha256": decision.revision_sha256}


def _accept_sync(
    factory: _Factory,
    *,
    project_id: str,
    change_request_id: str,
    identity: CaliberIdentity,
    project_action: str,
) -> WorkspaceChangeRequestSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        request = session.execute(
            select(CaliberWorkspaceChangeRequest).where(
                CaliberWorkspaceChangeRequest.project_id == project_id,
                CaliberWorkspaceChangeRequest.change_request_id == change_request_id,
            )
        ).scalar_one_or_none()
        if request is None:
            raise HTTPException(status_code=404, detail="change_request_not_found")
        head = session.execute(
            select(CaliberWorkspaceChangeRequestHead).where(
                CaliberWorkspaceChangeRequestHead.change_request_id == change_request_id,
                CaliberWorkspaceChangeRequestHead.generation == request.head_generation,
            )
        ).scalar_one()
        qa_evidence = _resolve_verified_qa_evidence(
            session,
            project_id=project_id,
            change_request_id=change_request_id,
            head=head,
        )
        accepted = accept_change_request(
            session,
            project_id=project_id,
            change_request_id=change_request_id,
            actor=identity.user_id,
            qa_evidence=qa_evidence,
        )
        if not accepted:
            raise HTTPException(status_code=409, detail="change_request_out_of_date")
        return _request_schema(session, request)


def _add_comment_sync(
    factory: _Factory,
    *,
    project_id: str,
    change_request_id: str,
    identity: CaliberIdentity,
    payload: WorkspaceChangeRequestCommentRequest,
    project_action: str,
) -> WorkspaceChangeRequestCommentSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        row = add_comment(
            session,
            project_id=project_id,
            change_request_id=change_request_id,
            identity=identity,
            **payload.model_dump(),
        )
        return WorkspaceChangeRequestCommentSchema.model_validate(
            {**row.__dict__, "created_at": _iso(row.created_at)}
        )


def _assign_reviewer_sync(
    factory: _Factory,
    *,
    project_id: str,
    change_request_id: str,
    identity: CaliberIdentity,
    config: Any,
    user_id: str,
    payload: WorkspaceChangeRequestReviewerRequest,
    project_action: str,
) -> WorkspaceChangeRequestReviewerSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        row = assign_reviewer(
            session,
            project_id=project_id,
            change_request_id=change_request_id,
            identity=identity,
            config=config,
            user_id=user_id,
            **payload.model_dump(),
        )
        return WorkspaceChangeRequestReviewerSchema.model_validate(
            {
                **row.__dict__,
                "assigned_at": _iso(row.assigned_at),
                "removed_at": _iso(row.removed_at),
            }
        )


def _remove_reviewer_sync(
    factory: _Factory,
    *,
    project_id: str,
    change_request_id: str,
    identity: CaliberIdentity,
    user_id: str,
    payload: WorkspaceChangeRequestReviewerRequest,
    project_action: str,
) -> WorkspaceChangeRequestSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        row = remove_reviewer(
            session,
            project_id=project_id,
            change_request_id=change_request_id,
            identity=identity,
            user_id=user_id,
            **payload.model_dump(),
        )
        return _request_schema(session, row)


def _submit_review_sync(
    factory: _Factory,
    *,
    project_id: str,
    change_request_id: str,
    identity: CaliberIdentity,
    config: Any,
    payload: WorkspaceChangeRequestReviewRequest,
    project_action: str,
) -> WorkspaceChangeRequestReviewSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        row = submit_review(
            session,
            project_id=project_id,
            change_request_id=change_request_id,
            identity=identity,
            config=config,
            **payload.model_dump(),
        )
        return WorkspaceChangeRequestReviewSchema.model_validate(
            {**row.__dict__, "created_at": _iso(row.created_at)}
        )


def _refresh_external_sync(
    factory: _Factory,
    *,
    project_id: str,
    change_request_id: str,
    identity: CaliberIdentity,
    project_action: str,
) -> None:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        row = session.execute(
            select(CaliberWorkspaceChangeRequest).where(
                CaliberWorkspaceChangeRequest.project_id == project_id,
                CaliberWorkspaceChangeRequest.change_request_id == change_request_id,
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail="change_request_not_found")
        if row.review_backend != "source_provider":
            raise HTTPException(status_code=409, detail="review_backends_cannot_be_combined")
        raise HTTPException(status_code=409, detail="source_provider_unavailable")


async def list_change_requests(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_user(request)
    identity = resolve_identity(request)
    limit, cursor = _page(request)
    status = request.query_params.get("status")
    if status and status not in {
        "draft",
        "open",
        "changes_requested",
        "technically_approved",
        "qa_in_progress",
        "out_of_date",
        "accepted",
        "closed",
    }:
        raise HTTPException(status_code=400, detail="invalid_change_request_status")
    items, next_cursor = await run_in_threadpool(
        _list_requests_sync,
        get_session_factory(request),
        project_id=project_id,
        identity=identity,
        project_action="read",
        limit=limit,
        cursor=cursor,
        status=status,
        created_by=request.query_params.get("created_by"),
        reviewer_user_id=request.query_params.get("reviewer_user_id"),
        semantic_version=request.query_params.get("semantic_version"),
    )
    return JSONResponse(
        {
            "data": WorkspaceChangeRequestListSchema(items=items).model_dump(mode="json"),
            "next_cursor": next_cursor,
        }
    )


async def get_change_request(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_user(request)
    identity = resolve_identity(request)
    schema = await run_in_threadpool(
        _get_request_sync,
        get_session_factory(request),
        project_id=project_id,
        change_request_id=request.path_params["change_request_id"],
        identity=identity,
        project_action="read",
    )
    return envelope_response(schema)


async def create_change_request_route(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    body = await parse_json_object(request)
    payload = WorkspaceChangeRequestCreateRequest.model_validate(body)
    require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    schema = await run_in_threadpool(
        _create_change_request_sync,
        get_session_factory(request),
        project_id=project_id,
        identity=identity,
        project_action="change_request.create",
        config=request.app.state.config,
        payload=payload,
    )
    return envelope_response(schema, status_code=201)


async def submit_change_request_route(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    body = await parse_json_object(request, allow_empty=True)
    WorkspaceChangeRequestSubmitRequest.model_validate(body)
    require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    schema = await run_in_threadpool(
        _submit_change_request_sync,
        get_session_factory(request),
        project_id=project_id,
        change_request_id=request.path_params["change_request_id"],
        identity=identity,
        project_action="change_request.update",
        config=request.app.state.config,
    )
    return envelope_response(schema)


async def update_head_route(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    payload = WorkspaceChangeRequestHeadUpdateRequest.model_validate(
        await parse_json_object(request)
    )
    require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    schema = await run_in_threadpool(
        _update_head_sync,
        get_session_factory(request),
        project_id=project_id,
        change_request_id=request.path_params["change_request_id"],
        identity=identity,
        project_action="change_request.update",
        payload=payload,
    )
    return envelope_response(schema)


async def rebase_route(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    payload = WorkspaceChangeRequestRebaseRequest.model_validate(await parse_json_object(request))
    require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    schema = await run_in_threadpool(
        _rebase_sync,
        get_session_factory(request),
        project_id=project_id,
        change_request_id=request.path_params["change_request_id"],
        identity=identity,
        project_action="change_request.update",
        payload=payload,
    )
    return envelope_response(schema)


async def close_route(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    payload = WorkspaceChangeRequestCloseRequest.model_validate(await parse_json_object(request))
    require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    schema = await run_in_threadpool(
        _close_sync,
        get_session_factory(request),
        project_id=project_id,
        change_request_id=request.path_params["change_request_id"],
        identity=identity,
        project_action="change_request.update",
        payload=payload,
    )
    return envelope_response(schema)


async def accept_route(request: Request) -> JSONResponse:
    """Accept a Change Request, advancing the project's accepted revision.

    Deliberately takes no request body: unlike `close`/`rebase`/etc., there
    is nothing here for a caller to legitimately supply -- ``qa_evidence``
    is derived entirely server-side by ``_resolve_verified_qa_evidence``
    from a durably recorded QA decision (see this module's own docstring).
    A client that could pass ``qa_evidence`` directly could simply assert
    ``"passed": true`` with no real QA process behind it, which is exactly
    the authorization hole this design avoids.
    """
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    schema = await run_in_threadpool(
        _accept_sync,
        get_session_factory(request),
        project_id=project_id,
        change_request_id=request.path_params["change_request_id"],
        identity=identity,
        project_action="change_request.accept",
    )
    return envelope_response(schema)


async def _list_related_route(
    request: Request,
    kind: Literal["comments", "reviewers", "reviews", "attestations", "checks"],
) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_user(request)
    identity = resolve_identity(request)
    limit, cursor = _page(request)
    items, next_cursor = await run_in_threadpool(
        _list_related_sync,
        get_session_factory(request),
        project_id=project_id,
        change_request_id=request.path_params["change_request_id"],
        identity=identity,
        kind=kind,
        limit=limit,
        cursor=cursor,
        project_action="read",
    )
    return JSONResponse(
        {"data": [item.model_dump(mode="json") for item in items], "next_cursor": next_cursor}
    )


async def list_comments_route(request: Request) -> JSONResponse:
    require_user(request)
    return await _list_related_route(request, "comments")


async def list_reviewers_route(request: Request) -> JSONResponse:
    require_user(request)
    return await _list_related_route(request, "reviewers")


async def list_reviews_route(request: Request) -> JSONResponse:
    require_user(request)
    return await _list_related_route(request, "reviews")


async def list_attestations_route(request: Request) -> JSONResponse:
    require_user(request)
    return await _list_related_route(request, "attestations")


async def list_checks_route(request: Request) -> JSONResponse:
    require_user(request)
    return await _list_related_route(request, "checks")


async def add_comment_route(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    payload = WorkspaceChangeRequestCommentRequest.model_validate(await parse_json_object(request))
    require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    schema = await run_in_threadpool(
        _add_comment_sync,
        get_session_factory(request),
        project_id=project_id,
        change_request_id=request.path_params["change_request_id"],
        identity=identity,
        project_action="change_request.comment",
        payload=payload,
    )
    return envelope_response(schema, status_code=201)


async def assign_reviewer_route(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    payload = WorkspaceChangeRequestReviewerRequest.model_validate(await parse_json_object(request))
    require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    schema = await run_in_threadpool(
        _assign_reviewer_sync,
        get_session_factory(request),
        project_id=project_id,
        change_request_id=request.path_params["change_request_id"],
        identity=identity,
        project_action="change_request.manage",
        config=request.app.state.config,
        user_id=request.path_params["user_id"],
        payload=payload,
    )
    return envelope_response(schema, status_code=201)


async def remove_reviewer_route(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    payload = WorkspaceChangeRequestReviewerRequest.model_validate(await parse_json_object(request))
    require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    schema = await run_in_threadpool(
        _remove_reviewer_sync,
        get_session_factory(request),
        project_id=project_id,
        change_request_id=request.path_params["change_request_id"],
        identity=identity,
        project_action="change_request.manage",
        user_id=request.path_params["user_id"],
        payload=payload,
    )
    return envelope_response(schema)


async def submit_review_route(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    payload = WorkspaceChangeRequestReviewRequest.model_validate(await parse_json_object(request))
    require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    schema = await run_in_threadpool(
        _submit_review_sync,
        get_session_factory(request),
        project_id=project_id,
        change_request_id=request.path_params["change_request_id"],
        identity=identity,
        project_action="change_request.review",
        config=request.app.state.config,
        payload=payload,
    )
    return envelope_response(schema, status_code=201)


async def refresh_external_route(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    await run_in_threadpool(
        _refresh_external_sync,
        get_session_factory(request),
        project_id=project_id,
        change_request_id=request.path_params["change_request_id"],
        identity=identity,
        project_action="change_request.update",
    )
    return JSONResponse({"data": {"status": "queued"}}, status_code=202)


async def list_tags_route(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_user(request)
    identity = resolve_identity(request)
    limit, cursor = _page(request)
    items, next_cursor = await run_in_threadpool(
        _list_tags_sync,
        get_session_factory(request),
        project_id=project_id,
        identity=identity,
        project_action="read",
        limit=limit,
        cursor=cursor,
    )
    return JSONResponse(
        {"data": [item.model_dump(mode="json") for item in items], "next_cursor": next_cursor}
    )


async def get_tag_route(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_user(request)
    identity = resolve_identity(request)
    schema = await run_in_threadpool(
        _get_tag_sync,
        get_session_factory(request),
        project_id=project_id,
        tag=request.path_params["tag"],
        identity=identity,
        project_action="read",
    )
    return envelope_response(schema)


def register(app: Starlette) -> None:
    app.routes.append(Route(LIST_PATH, list_change_requests, methods=["GET"]))
    app.routes.append(Route(LIST_PATH, create_change_request_route, methods=["POST"]))
    app.routes.append(Route(DETAIL_PATH, get_change_request, methods=["GET"]))
    app.routes.append(Route(SUBMIT_PATH, submit_change_request_route, methods=["POST"]))
    app.routes.append(Route(UPDATE_HEAD_PATH, update_head_route, methods=["POST"]))
    app.routes.append(Route(REBASE_PATH, rebase_route, methods=["POST"]))
    app.routes.append(Route(CLOSE_PATH, close_route, methods=["POST"]))
    app.routes.append(Route(ACCEPT_PATH, accept_route, methods=["POST"]))
    app.routes.append(Route(COMMENTS_PATH, list_comments_route, methods=["GET"]))
    app.routes.append(Route(COMMENTS_PATH, add_comment_route, methods=["POST"]))
    app.routes.append(Route(REVIEWERS_PATH, list_reviewers_route, methods=["GET"]))
    app.routes.append(Route(REVIEWER_DETAIL_PATH, assign_reviewer_route, methods=["PUT"]))
    app.routes.append(Route(REVIEWER_DETAIL_PATH, remove_reviewer_route, methods=["DELETE"]))
    app.routes.append(Route(REVIEWS_PATH, list_reviews_route, methods=["GET"]))
    app.routes.append(Route(REVIEWS_PATH, submit_review_route, methods=["POST"]))
    app.routes.append(Route(ATTESTATIONS_PATH, list_attestations_route, methods=["GET"]))
    app.routes.append(Route(REFRESH_EXTERNAL_PATH, refresh_external_route, methods=["POST"]))
    app.routes.append(Route(CHECKS_PATH, list_checks_route, methods=["GET"]))
    app.routes.append(Route(TAGS_PATH, list_tags_route, methods=["GET"]))
    app.routes.append(Route(TAG_DETAIL_PATH, get_tag_route, methods=["GET"]))
