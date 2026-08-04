"""``/caliber/releases/*`` — the cross-artifact Releases & Rollback surface.

A read-only control-plane view that answers "what is live, and what changed?"
across artifact types in one place, instead of scattering it per artifact page:

* ``GET /caliber/releases/timeline`` — promotion/rollback/activation events from
  the versioning audit trail, newest first (optionally filtered by entity_type).
* ``GET /caliber/releases/live`` — what is currently live: each active workflow
  deployment and each knowledge base's active version.

Both are read-only and DB-backed; per-row rollback reuses the existing
per-artifact rollback endpoints from the UI.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.auth import (
    SCOPE_ADMIN,
    SCOPE_OPERATOR,
    CaliberIdentity,
    require_scopes,
    require_user,
    resolve_identity,
)
from caliber.db.models import (
    CaliberAuditLog,
    CaliberKnowledgeBase,
    CaliberReleaseOperation,
    CaliberSkill,
    CaliberWorkflow,
    CaliberWorkflowDeployment,
)
from caliber.db.scoping import apply_visibility_filter
from caliber.release_operations import (
    reconcile_prompt_alias_releases,
    serialize_release_operation,
)
from caliber.routes._deps import envelope_response_dict, get_session_factory

logger = logging.getLogger("caliber.routes.releases")

TIMELINE_PATH = "/ajax-api/2.0/mlflow/caliber/releases/timeline"
LIVE_PATH = "/ajax-api/2.0/mlflow/caliber/releases/live"
OPERATIONS_PATH = "/ajax-api/2.0/mlflow/caliber/releases/operations"
RECONCILE_PATH = "/ajax-api/2.0/mlflow/caliber/releases/operations/reconcile"

# The audit actions that constitute a "release" event (promote / rollback /
# activate across artifact types). Skill content edits and other audit rows are
# intentionally excluded — this is the deploy/rollback timeline, not a full log.
_RELEASE_ACTIONS: tuple[str, ...] = (
    "promote_prompt",
    "rollback_prompt",
    "promote_workflow",
    "rollback_workflow",
    "activate_knowledge_base_version",
    "rollback_knowledge_base_version",
    "rollback_skill",
)

_TIMELINE_DEFAULT_LIMIT = 50
_TIMELINE_MAX_LIMIT = 200

# Audit ``entity_type`` values whose rows can be scoped against a local table
# carrying ``owner``/``visibility``/``project_id``. ``prompt`` is deliberately
# absent: prompt liveness lives in the MLflow registry, so there is no local row
# to scope a prompt promotion against (see ``_visible_entity_ids``).
_SCOPEABLE_ENTITY_MODELS: dict[str, tuple[type, object]] = {
    "workflow": (CaliberWorkflow, CaliberWorkflow.workflow_id),
    "knowledge_base": (CaliberKnowledgeBase, CaliberKnowledgeBase.knowledge_base_id),
    "skill": (CaliberSkill, CaliberSkill.skill_id),
}


def _visible_ids(session: Any, model: type, pk: Any, identity: CaliberIdentity) -> set[str]:
    """Primary keys of ``model`` rows visible to ``identity``.

    Uses the same 3-tier predicate the artifact workspaces apply, so this
    aggregate cannot show more than the pages it summarizes.
    """
    stmt = apply_visibility_filter(select(pk), model, identity, identity.active_project_id)
    return set(session.execute(stmt).scalars().all())


def _scope_timeline_rows(
    session: Any, rows: Sequence[CaliberAuditLog], identity: CaliberIdentity
) -> list[CaliberAuditLog]:
    """Drop release events whose entity is not visible to ``identity``.

    Before this, the timeline returned every release audit row in the database
    while the artifact workspaces it summarizes scoped theirs, so a non-admin saw
    other projects' promotion history. Admins keep the unfiltered view.

    ``prompt`` rows are retained because prompt liveness lives in the MLflow
    registry and there is no local row to scope them against; that is a known
    residual rather than an oversight.
    """
    if identity.has_scope(SCOPE_ADMIN):
        return list(rows)
    needed = {row.entity_type for row in rows} & _SCOPEABLE_ENTITY_MODELS.keys()
    visible: dict[str, set[str]] = {
        entity_type: _visible_ids(session, *_SCOPEABLE_ENTITY_MODELS[entity_type], identity)
        for entity_type in needed
    }
    return [
        row
        for row in rows
        if row.entity_type not in visible or row.entity_id in visible[row.entity_type]
    ]


def _kb_live_entry(
    kb: CaliberKnowledgeBase, activation: CaliberAuditLog | None
) -> dict[str, object | None]:
    """One ``/releases/live`` row for a knowledge base.

    ``since``/``by`` come from the activation audit row that put the currently
    live version in place; they fall back to ``updated_at``/``owner`` only when
    that activation was never audited (e.g. the build-time activation of a KB's
    first version).
    """
    return {
        "artifact_type": "knowledge_base",
        "artifact_id": kb.knowledge_base_id,
        "artifact_name": kb.name,
        "alias": "active",
        "version_id": kb.active_version_id,
        "since": (
            activation.timestamp.isoformat()
            if activation and activation.timestamp
            else (kb.updated_at.isoformat() if kb.updated_at else None)
        ),
        "by": activation.actor if activation else kb.owner,
    }


def _load_release_operations(
    factory: Any,
    *,
    status: str,
    limit: int,
) -> list[dict[str, Any]]:
    """Load operation rows outside the async route's event-loop thread."""
    with factory() as session:
        stmt = select(CaliberReleaseOperation).order_by(
            CaliberReleaseOperation.created_at.desc()
        )
        if status:
            stmt = stmt.where(CaliberReleaseOperation.status == status)
        rows = session.execute(stmt.limit(limit)).scalars().all()
        return [serialize_release_operation(row) for row in rows]


def _reconcile_release_operations(
    factory: Any,
    *,
    resolve_alias: Any,
) -> list[dict[str, Any]]:
    """Reconcile provider state outside the async route's event-loop thread."""
    with factory() as session:
        rows = reconcile_prompt_alias_releases(
            session,
            resolve_alias=resolve_alias,
        )
        return [serialize_release_operation(row) for row in rows]


async def timeline(request: Request) -> JSONResponse:
    """Recent promotion/rollback/activation events, newest first.

    Query params: ``limit`` (default 50, cap 200) and optional
    ``entity_type`` (``prompt`` / ``workflow`` / ``knowledge_base`` / ``skill``).
    """
    require_user(request)
    identity = resolve_identity(request)
    raw_limit = request.query_params.get("limit")
    limit = _TIMELINE_DEFAULT_LIMIT
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="'limit' must be an integer") from exc
        if limit < 1:
            raise HTTPException(status_code=400, detail="'limit' must be >= 1")
        limit = min(limit, _TIMELINE_MAX_LIMIT)
    entity_type = request.query_params.get("entity_type")

    factory = get_session_factory(request)
    with factory() as session:
        stmt = (
            select(CaliberAuditLog)
            .where(CaliberAuditLog.action.in_(_RELEASE_ACTIONS))
            .order_by(CaliberAuditLog.timestamp.desc(), CaliberAuditLog.log_id.desc())
        )
        if entity_type:
            stmt = stmt.where(CaliberAuditLog.entity_type == entity_type)
        rows = session.execute(stmt.limit(limit)).scalars().all()
        rows = _scope_timeline_rows(session, rows, identity)
        data = [
            {
                "log_id": row.log_id,
                "timestamp": row.timestamp.isoformat() if row.timestamp else None,
                "actor": row.actor,
                "action": row.action,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "details": row.details or {},
            }
            for row in rows
        ]
    return envelope_response_dict(data)


async def live(request: Request) -> JSONResponse:
    """What is currently live across artifact types (DB-backed).

    Active workflow deployments and each knowledge base's active version. Prompt
    ``@prod`` liveness lives in the MLflow registry (not enumerable here without
    a registry scan), so it is surfaced via the per-prompt page rather than this
    aggregate; the timeline still records prompt promotions.
    """
    require_user(request)
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    with factory() as session:
        # Deployments carry no visibility columns of their own, so scope them
        # through their parent workflow. Without this the aggregate exposed every
        # project's live deployment to any signed-in user.
        deployment_stmt = select(CaliberWorkflowDeployment).where(
            CaliberWorkflowDeployment.status == "active"
        )
        if not identity.has_scope(SCOPE_ADMIN):
            visible_workflows = _visible_ids(
                session, CaliberWorkflow, CaliberWorkflow.workflow_id, identity
            )
            deployment_stmt = deployment_stmt.where(
                CaliberWorkflowDeployment.workflow_id.in_(visible_workflows)
            )
        deployments = session.execute(deployment_stmt).scalars().all()
        kb_stmt = select(CaliberKnowledgeBase).where(
            CaliberKnowledgeBase.active_version_id.is_not(None)
        )
        kb_stmt = apply_visibility_filter(
            kb_stmt, CaliberKnowledgeBase, identity, identity.active_project_id
        )
        kbs = session.execute(kb_stmt).scalars().all()
        # "since"/"by" for a KB must reflect when its *active version* went live
        # and who did it — NOT ``updated_at`` (bumped on any edit, e.g. a rename)
        # or ``owner`` (the KB's owner, not the activator). Derive them from the
        # newest activate/rollback audit row that names the currently-live
        # version, falling back to updated_at/owner when there is no audited
        # activation (e.g. the build-time activation of a KB's first version).
        active_version_by_kb = {kb.knowledge_base_id: kb.active_version_id for kb in kbs}
        activation_by_kb: dict[str, CaliberAuditLog] = {}
        if active_version_by_kb:
            audit_rows = (
                session.execute(
                    select(CaliberAuditLog)
                    .where(CaliberAuditLog.entity_type == "knowledge_base")
                    .where(CaliberAuditLog.entity_id.in_(active_version_by_kb))
                    .where(
                        CaliberAuditLog.action.in_(
                            (
                                "activate_knowledge_base_version",
                                "rollback_knowledge_base_version",
                            )
                        )
                    )
                    .order_by(CaliberAuditLog.timestamp.desc(), CaliberAuditLog.log_id.desc())
                )
                .scalars()
                .all()
            )
            for row in audit_rows:
                kb_id = row.entity_id
                if kb_id in activation_by_kb:
                    continue  # keep only the newest matching row per KB
                if (row.details or {}).get("version_id") == active_version_by_kb.get(kb_id):
                    activation_by_kb[kb_id] = row
        data = [
            {
                "artifact_type": "workflow",
                "artifact_id": dep.workflow_id,
                "alias": dep.alias,
                "version_id": dep.version_id,
                "since": dep.deployed_at.isoformat() if dep.deployed_at else None,
                "by": dep.deployed_by,
            }
            for dep in deployments
        ] + [_kb_live_entry(kb, activation_by_kb.get(kb.knowledge_base_id)) for kb in kbs]
    return envelope_response_dict(data)


async def release_operations(request: Request) -> JSONResponse:
    """List durable release intents, including incomplete external effects."""
    require_scopes(request, [SCOPE_OPERATOR])
    raw_limit = request.query_params.get("limit", "100")
    try:
        limit = max(1, min(int(raw_limit), 500))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="'limit' must be an integer") from exc
    status = (request.query_params.get("status") or "").strip()
    factory = get_session_factory(request)
    data = await run_in_threadpool(
        _load_release_operations,
        factory,
        status=status,
        limit=limit,
    )
    return envelope_response_dict(data)


async def reconcile_release_operations(request: Request) -> JSONResponse:
    """Observe provider aliases and settle incomplete prompt release intents."""
    require_scopes(request, [SCOPE_OPERATOR])
    from caliber.routes import prompts as prompt_routes  # noqa: PLC0415

    factory = get_session_factory(request)
    data = await run_in_threadpool(
        _reconcile_release_operations,
        factory,
        resolve_alias=prompt_routes._load_prompt_release_info,
    )
    return envelope_response_dict(data)


def register(app: Starlette) -> None:
    app.routes.append(Route(TIMELINE_PATH, timeline, methods=["GET"]))
    app.routes.append(Route(LIVE_PATH, live, methods=["GET"]))
    app.routes.append(Route(OPERATIONS_PATH, release_operations, methods=["GET"]))
    app.routes.append(Route(RECONCILE_PATH, reconcile_release_operations, methods=["POST"]))
