"""``/caliber/verification-queue`` endpoints — Stage ① Verify.

Wires :class:`caliber.db.models.CaliberVerificationItem` and its
already-specified request/response schemas (``schemas.py``) to real routes.
Before this module existed, nothing served this path: every job-creation
route (`routes/prompts.py`, `routes/skills.py`,
`routes/workflow_calibration.py`, and the Aria promotion-proposal path)
inserted a verification-queue row already ``status="verified"``, self-stamped
by the actor who created the job, in the same transaction — so "Verify" was a
bookkeeping field job creation wrote about itself, never a decision a
different person, or the same person later, could act on separately. See
``docs/workspace-plan.md`` section 2.2.

This module gives a human that separate action for a *manually* flagged
concern — one not tied to an already-running job:

* ``GET /verification-queue`` — list, filterable by status/severity/agent.
* ``GET /verification-queue/{item_id}`` — one item.
* ``POST /verification-queue`` (operator) — flag a concern; starts ``pending``.
* ``POST /verification-queue/{item_id}/verify`` (operator) — confirm it's real.
* ``POST /verification-queue/{item_id}/dismiss`` (operator) — not real, or a duplicate.
* ``POST /verification-queue/{item_id}/duplicate`` (operator) — dedicated dismiss-as-duplicate route.
* ``POST /verification-queue/batch`` (operator) — verify/dismiss several at once.

**Deliberately not built here:** verifying an item does not create a
:class:`caliber.db.models.CaliberRefinementJob`. The frontend's pre-existing
``VerifyResponse`` type (``caliber-ui/src/api/types.ts``) once claimed every
verify call returns one; building that for real means generalizing three
separate, complex, bespoke job-creation functions behind a shared interface,
which is Phase 4 adapter-shaped work, not this slice. ``batch``'s
``linked_job_id`` is always ``null`` for the same reason. The frontend type
was corrected to say so rather than promise it.

**Known, disclosed limitation:** :class:`CaliberVerificationItem` has no
``created_by`` column, so an operator can verify or dismiss an item they
created themselves — self-verification cannot be prevented here. Closing that
needs a schema change and belongs with the Phase 1 authorization work.

**`P2-Q` (docs/workspace-plan.md's own row, closed here):** this entire
surface used to have no per-project isolation at all -- any operator could
list, get, verify, dismiss, mark-duplicate, or batch-act on every project's
manually-flagged items. Every item's ``agent_id`` (``NOT NULL``,
FK-constrained) already resolves to a real :class:`caliber.db.models.CaliberAgentConfig`
row, itself project-scoped, so ``project_id`` is now derived from that agent
at creation time (going forward, in every one of today's four job-creation
paths plus the manual create route above) and backfilled the same way for
existing rows (migration ``0113``). Every read/action route now scopes
through :func:`_scope_verification_items`/:func:`_scoped_get`, the same
hand-derived ``project_only`` pattern ``routes/releases.py::_scope_release_operations``
established for :class:`caliber.db.models.CaliberReleaseOperation` (`P2-R`):
a ``NULL`` project_id (a personal/global agent) stays visible to everyone; a
non-null one is visible only to an active member of that project; admins
keep their existing unconditional bypass.

Every handler offloads its synchronous SQLAlchemy work to
:func:`starlette.concurrency.run_in_threadpool` rather than opening a session
inline on the event loop — see ``tests/test_async_offload_ratchet.py``, which
fails the build if a new handler adds to that count instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import ColumnElement, and_, exists, or_, select
from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import (
    SCOPE_ADMIN,
    SCOPE_OPERATOR,
    CaliberIdentity,
    require_scopes,
    require_user,
    resolve_identity,
)
from caliber.db.models import CaliberAgentConfig, CaliberProjectMember, CaliberVerificationItem
from caliber.db.scoping import get_visible
from caliber.ids import new_item_id
from caliber.routes._deps import (
    envelope_response,
    envelope_response_dict,
    get_session_factory,
    parse_json_object,
)
from caliber.schemas import (
    VerificationBatchItemResult,
    VerificationBatchRequest,
    VerificationBatchResponse,
    VerificationItemCreateRequest,
    VerificationItemDismissRequest,
    VerificationItemDuplicateRequest,
    VerificationItemSchema,
    VerificationItemVerifyRequest,
)

LIST_PATH = "/ajax-api/2.0/mlflow/caliber/verification-queue"
DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/verification-queue/{item_id}"
VERIFY_PATH = "/ajax-api/2.0/mlflow/caliber/verification-queue/{item_id}/verify"
DISMISS_PATH = "/ajax-api/2.0/mlflow/caliber/verification-queue/{item_id}/dismiss"
DUPLICATE_PATH = "/ajax-api/2.0/mlflow/caliber/verification-queue/{item_id}/duplicate"
BATCH_PATH = "/ajax-api/2.0/mlflow/caliber/verification-queue/batch"

_LIST_STATUS_VALUES: frozenset[str] = frozenset(
    {"pending", "verified", "dismissed", "duplicate", "all"}
)
_MAX_BATCH_ITEMS = 200

_Factory = sessionmaker[Session]


def _scope_verification_items(stmt: Any, identity: CaliberIdentity) -> Any:
    """Restrict a ``CaliberVerificationItem`` query to what ``identity`` may see.

    `P2-Q` (docs/workspace-plan.md's own row, closed here): the row has no
    ``visibility``/owner column of its own -- it's a derived triage-queue
    record, not a first-class owned resource, the same ``project_only``
    shape ``CaliberReleaseOperation`` uses (`P2-R`, confirmed by
    ``db/resource_inventory.py``'s classification). This can't reuse
    :func:`caliber.db.scoping.apply_visibility_filter` directly -- that
    helper hard-requires a ``visibility`` column plus a resolvable owner/
    ``created_by`` column, neither of which this table has -- so the
    equivalent "project" tier is re-derived here by hand, mirroring
    ``routes/releases.py::_scope_release_operations`` exactly: a ``NULL``
    ``project_id`` (the item's agent has no project of its own -- a
    personal/global agent) stays visible to everyone; a non-null
    ``project_id`` is visible only to an active member of that exact
    project (the caller's current ``X-CALIBER-Project``), the same
    membership check ``apply_visibility_filter``'s own project tier
    performs. Admins keep the same unconditional bypass every other read in
    the codebase already grants.
    """
    if identity.has_scope(SCOPE_ADMIN):
        return stmt
    project_id = identity.active_project_id
    conditions: list[ColumnElement[bool]] = [CaliberVerificationItem.project_id.is_(None)]
    if project_id:
        conditions.append(
            and_(
                CaliberVerificationItem.project_id == project_id,
                exists(
                    select(CaliberProjectMember.member_id).where(
                        CaliberProjectMember.project_id == project_id,
                        CaliberProjectMember.user_id == identity.user_id,
                        CaliberProjectMember.status == "active",
                    )
                ),
            )
        )
    return stmt.where(or_(*conditions))


def _scoped_get(
    session: Session, item_id: str, identity: CaliberIdentity
) -> CaliberVerificationItem | None:
    """Fetch one item by id, but only if ``identity`` may see it.

    The scoped equivalent of a bare ``session.get(CaliberVerificationItem,
    item_id)`` -- the same "re-fetch by id through the visibility filter"
    idiom :func:`caliber.db.scoping.get_visible` uses for the full 3-tier
    model, hand-derived here via :func:`_scope_verification_items` for the
    same reason that helper itself exists. An out-of-scope id reports the
    identical "not found" a genuinely missing one does.
    """
    stmt = _scope_verification_items(
        select(CaliberVerificationItem).where(CaliberVerificationItem.item_id == item_id),
        identity,
    )
    return session.execute(stmt).scalars().first()


# ---------------------------------------------------------------------------
# List / get
# ---------------------------------------------------------------------------


def _list_items_sync(
    factory: _Factory,
    *,
    identity: CaliberIdentity,
    status: str,
    severity: str | None,
    agent_id: str | None,
) -> list[VerificationItemSchema]:
    with factory() as session:
        stmt = select(CaliberVerificationItem).order_by(
            CaliberVerificationItem.priority.desc(), CaliberVerificationItem.created_at
        )
        if status != "all":
            stmt = stmt.where(CaliberVerificationItem.status == status)
        if severity:
            stmt = stmt.where(CaliberVerificationItem.severity == severity)
        if agent_id:
            stmt = stmt.where(CaliberVerificationItem.agent_id == agent_id)
        stmt = _scope_verification_items(stmt, identity)
        rows = session.execute(stmt).scalars().all()
        return [VerificationItemSchema.model_validate(row) for row in rows]


async def list_items(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    status = request.query_params.get("status", "pending")
    if status not in _LIST_STATUS_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"invalid value for 'status': {status!r}; expected one of {sorted(_LIST_STATUS_VALUES)}",
        )
    severity = request.query_params.get("severity")
    agent_id = request.query_params.get("agent_id")

    factory = get_session_factory(request)
    schemas = await run_in_threadpool(
        _list_items_sync,
        factory,
        identity=identity,
        status=status,
        severity=severity,
        agent_id=agent_id,
    )
    return envelope_response(schemas)


def _get_item_sync(
    factory: _Factory, *, item_id: str, identity: CaliberIdentity
) -> VerificationItemSchema:
    with factory() as session:
        item = _scoped_get(session, item_id, identity)
        if item is None:
            raise HTTPException(status_code=404, detail=f"verification item {item_id!r} not found")
        return VerificationItemSchema.model_validate(item)


async def get_item(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    item_id = request.path_params["item_id"]
    factory = get_session_factory(request)
    schema = await run_in_threadpool(_get_item_sync, factory, item_id=item_id, identity=identity)
    return envelope_response(schema)


# ---------------------------------------------------------------------------
# Create
# ---------------------------------------------------------------------------


def create_verification_item_record(
    session: Session,
    *,
    payload: VerificationItemCreateRequest,
    identity: CaliberIdentity | None = None,
) -> CaliberVerificationItem:
    """Create a manually-flagged ``pending`` item.

    Raises :class:`starlette.exceptions.HTTPException` (404) if ``agent_id``
    does not reference a real, *visible* agent. Flushes but does not commit —
    the caller owns the transaction. Exposed at module level (rather than
    nested inside the route) so it stays reusable the way
    ``review_queues.create_review_queue_record`` is.

    `P2-Q`: the agent lookup now goes through :func:`caliber.db.scoping.get_visible`
    when ``identity`` is supplied (the same optional-``identity`` convention
    ``prompt_targets.ensure_prompt_target`` already uses), closing a second,
    adjacent gap this row's own scoping work would otherwise have made worse:
    a bare ``session.get`` here previously let an operator point a new item at
    *any* project's ``CaliberAgentConfig`` row (itself a full
    ``SCOPING_VISIBILITY`` model) merely by guessing its id, silently
    inheriting that other project's ``project_id`` onto the new item -- which
    would make it invisible to its own creator while still polluting a
    project they don't belong to. ``identity=None`` (no request context)
    preserves the previous unscoped lookup for any future internal caller.
    """
    agent = (
        get_visible(
            session, CaliberAgentConfig, CaliberAgentConfig.agent_id, payload.agent_id, identity
        )
        if identity is not None
        else session.get(CaliberAgentConfig, payload.agent_id)
    )
    if agent is None:
        raise HTTPException(status_code=404, detail=f"agent {payload.agent_id!r} not found")
    item = CaliberVerificationItem(
        item_id=new_item_id(),
        agent_id=payload.agent_id,
        project_id=agent.project_id,
        category=payload.category,
        free_text=payload.free_text,
        severity=payload.severity,
        artifact_type_hint=payload.artifact_type_hint,
        artifact_ref=payload.artifact_ref,
        submitted_context=payload.submitted_context,
        session_id=payload.session_id,
        workflow_id=payload.workflow_id,
        status="pending",
    )
    session.add(item)
    session.flush()
    return item


def _create_item_sync(
    factory: _Factory,
    *,
    payload: VerificationItemCreateRequest,
    actor: str,
    identity: CaliberIdentity,
) -> VerificationItemSchema:
    with factory() as session:
        item = create_verification_item_record(session, payload=payload, identity=identity)
        audit_record(
            session,
            actor=actor,
            action="create_verification_item",
            entity_type="verification_item",
            entity_id=item.item_id,
            details={"agent_id": item.agent_id, "category": item.category},
        )
        session.commit()
        return VerificationItemSchema.model_validate(item)


async def create_item(request: Request) -> JSONResponse:
    body = await parse_json_object(request)
    payload = VerificationItemCreateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)

    factory = get_session_factory(request)
    schema = await run_in_threadpool(
        _create_item_sync, factory, payload=payload, actor=actor, identity=identity
    )
    return envelope_response(schema, status_code=201)


# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------


def _require_pending(
    session: Session, item_id: str, identity: CaliberIdentity
) -> CaliberVerificationItem:
    """`P2-Q`: re-fetched through :func:`_scoped_get` rather than a bare
    ``session.get`` -- an out-of-scope id now 404s exactly like a missing
    one, instead of letting any operator verify/dismiss another project's
    item by guessing its id."""
    item = _scoped_get(session, item_id, identity)
    if item is None:
        raise HTTPException(status_code=404, detail=f"verification item {item_id!r} not found")
    if item.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"verification item {item_id!r} is already {item.status}",
        )
    return item


def _verify_item_sync(
    factory: _Factory,
    *,
    item_id: str,
    payload: VerificationItemVerifyRequest,
    actor: str,
    identity: CaliberIdentity,
) -> VerificationItemSchema:
    with factory() as session:
        item = _require_pending(session, item_id, identity)
        item.status = "verified"
        item.verified_by = actor
        item.verified_at = datetime.now(timezone.utc)
        if payload.verification_notes is not None:
            item.verification_notes = payload.verification_notes
        if payload.refinement_target is not None:
            item.refinement_target = payload.refinement_target
        if payload.severity is not None:
            item.severity = payload.severity
        audit_record(
            session,
            actor=actor,
            action="verify_verification_item",
            entity_type="verification_item",
            entity_id=item_id,
            details={"refinement_target": item.refinement_target},
        )
        session.commit()
        return VerificationItemSchema.model_validate(item)


async def verify_item(request: Request) -> JSONResponse:
    item_id = request.path_params["item_id"]
    body = await parse_json_object(request, allow_empty=True)
    payload = VerificationItemVerifyRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)

    factory = get_session_factory(request)
    schema = await run_in_threadpool(
        _verify_item_sync,
        factory,
        item_id=item_id,
        payload=payload,
        actor=actor,
        identity=identity,
    )
    # No CaliberRefinementJob is created here — see the module docstring.
    return envelope_response_dict({"item": schema.model_dump(mode="json"), "job": None})


# ---------------------------------------------------------------------------
# Dismiss / duplicate
# ---------------------------------------------------------------------------


def _dismiss(
    session: Session,
    *,
    item_id: str,
    actor: str,
    reason: str | None,
    duplicate_of_id: str | None,
    identity: CaliberIdentity,
) -> CaliberVerificationItem:
    item = _require_pending(session, item_id, identity)
    if duplicate_of_id is not None:
        if duplicate_of_id == item_id:
            raise HTTPException(status_code=400, detail="an item cannot be a duplicate of itself")
        # `P2-Q`: scoped the same way as ``item`` itself -- otherwise an
        # operator could link their own item as a duplicate of another
        # project's item merely by guessing its id, an oracle for that id's
        # existence even though the linking item itself stays in scope.
        original = _scoped_get(session, duplicate_of_id, identity)
        if original is None:
            raise HTTPException(
                status_code=404, detail=f"verification item {duplicate_of_id!r} not found"
            )
        item.status = "duplicate"
        item.duplicate_of_id = duplicate_of_id
    else:
        item.status = "dismissed"
    if reason is not None:
        item.verification_notes = reason
    audit_record(
        session,
        actor=actor,
        action="dismiss_verification_item",
        entity_type="verification_item",
        entity_id=item_id,
        details={"status": item.status, "duplicate_of_id": duplicate_of_id},
    )
    return item


def _dismiss_sync(
    factory: _Factory,
    *,
    item_id: str,
    actor: str,
    reason: str | None,
    duplicate_of_id: str | None,
    identity: CaliberIdentity,
) -> VerificationItemSchema:
    with factory() as session:
        item = _dismiss(
            session,
            item_id=item_id,
            actor=actor,
            reason=reason,
            duplicate_of_id=duplicate_of_id,
            identity=identity,
        )
        session.commit()
        return VerificationItemSchema.model_validate(item)


async def dismiss_item(request: Request) -> JSONResponse:
    item_id = request.path_params["item_id"]
    body = await parse_json_object(request, allow_empty=True)
    payload = VerificationItemDismissRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)

    factory = get_session_factory(request)
    schema = await run_in_threadpool(
        _dismiss_sync,
        factory,
        item_id=item_id,
        actor=actor,
        reason=payload.reason,
        duplicate_of_id=payload.duplicate_of_id,
        identity=identity,
    )
    return envelope_response(schema)


async def mark_duplicate(request: Request) -> JSONResponse:
    item_id = request.path_params["item_id"]
    body = await parse_json_object(request)
    payload = VerificationItemDuplicateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)

    factory = get_session_factory(request)
    schema = await run_in_threadpool(
        _dismiss_sync,
        factory,
        item_id=item_id,
        actor=actor,
        reason=payload.reason,
        duplicate_of_id=payload.duplicate_of_id,
        identity=identity,
    )
    return envelope_response(schema)


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------


def _batch_action_sync(
    factory: _Factory,
    *,
    payload: VerificationBatchRequest,
    actor: str,
    identity: CaliberIdentity,
) -> VerificationBatchResponse:
    results: list[VerificationBatchItemResult] = []
    succeeded = 0
    for item_id in payload.item_ids:
        with factory() as session:
            try:
                item = _require_pending(session, item_id, identity)
                if payload.action == "verify":
                    item.status = "verified"
                    item.verified_by = actor
                    item.verified_at = datetime.now(timezone.utc)
                    if payload.refinement_target is not None:
                        item.refinement_target = payload.refinement_target
                else:
                    item.status = "dismissed"
                if payload.reason is not None:
                    item.verification_notes = payload.reason
                audit_record(
                    session,
                    actor=actor,
                    action=f"batch_{payload.action}_verification_item",
                    entity_type="verification_item",
                    entity_id=item_id,
                    details={},
                )
                session.commit()
            except HTTPException as exc:
                session.rollback()
                results.append(
                    VerificationBatchItemResult(
                        item_id=item_id, status="failed", reason=str(exc.detail)
                    )
                )
                continue
            succeeded += 1
            # ``linked_job_id`` stays None: no job is created by verify here.
            results.append(VerificationBatchItemResult(item_id=item_id, status="succeeded"))

    return VerificationBatchResponse(
        action=payload.action,
        requested=len(payload.item_ids),
        succeeded=succeeded,
        failed=len(payload.item_ids) - succeeded,
        results=results,
    )


async def batch_action(request: Request) -> JSONResponse:
    body = await parse_json_object(request)
    payload = VerificationBatchRequest.model_validate(body)
    if len(payload.item_ids) > _MAX_BATCH_ITEMS:
        raise HTTPException(
            status_code=400,
            detail=f"at most {_MAX_BATCH_ITEMS} items per batch request, got {len(payload.item_ids)}",
        )
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)

    factory = get_session_factory(request)
    response = await run_in_threadpool(
        _batch_action_sync, factory, payload=payload, actor=actor, identity=identity
    )
    return envelope_response(response)


def register(app: Starlette) -> None:
    app.routes.append(Route(LIST_PATH, list_items, methods=["GET"]))
    app.routes.append(Route(LIST_PATH, create_item, methods=["POST"]))
    app.routes.append(Route(BATCH_PATH, batch_action, methods=["POST"]))
    app.routes.append(Route(DETAIL_PATH, get_item, methods=["GET"]))
    app.routes.append(Route(VERIFY_PATH, verify_item, methods=["POST"]))
    app.routes.append(Route(DISMISS_PATH, dismiss_item, methods=["POST"]))
    app.routes.append(Route(DUPLICATE_PATH, mark_duplicate, methods=["POST"]))
