"""``/caliber/workflows/{id}/deployments`` + workflow-promotion endpoints (plan §15.3).

Promote runs the deploy gates relevant to the target alias. For **ungated**
aliases (``dev``/``staging``) it rotates the alias immediately. For **gated**
aliases (``prod``) it requires a passing deploy gate and then records a *pending*
:class:`CaliberWorkflowPromotion`; the alias only rotates once a reviewer
approves that promotion. Rollback restores the alias's previous target from its
rollback-checkpoint stack.

RBAC (plan §18.3): promoting a gated alias requires ``caliber.admin``; promoting
a non-gated alias requires ``caliber.operator``; approving/rejecting a pending
promotion requires ``caliber.approver``; rollback requires ``caliber.operator``
(matched to promote so whoever can move the live alias forward can also undo it).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import (
    SCOPE_ADMIN,
    SCOPE_APPROVER,
    SCOPE_OPERATOR,
    require_scopes,
    require_user,
    resolve_identity,
)
from caliber.db.models import (
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowPromotion,
    CaliberWorkflowVersion,
)
from caliber.db.scoping import get_visible
from caliber.mcp_policy import deployment_blockers
from caliber.observability import metrics
from caliber.routes._deps import envelope_response, get_session_factory, parse_json_object
from caliber.schemas import (
    PromoteRequest,
    PromotionDecisionRequest,
    WorkflowDeploymentSchema,
    WorkflowPromotionSchema,
)
from caliber.workflows.promoter import (
    AliasPreflightError,
    DeployError,
    DeployGateFailedError,
    RollbackError,
    approve_promotion,
    promote,
    reject_promotion,
    requires_human_approval,
    rollback,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber"
LIST_PATH = PREFIX + "/workflows/{workflow_id}/deployments"
PROMOTE_PATH = PREFIX + "/workflows/{workflow_id}/deployments/{alias}/promote"
ROLLBACK_PATH = PREFIX + "/workflows/{workflow_id}/deployments/{alias}/rollback"
PROMOTIONS_PATH = PREFIX + "/workflows/{workflow_id}/promotions"
PROMOTION_APPROVE_PATH = PREFIX + "/workflow-promotions/{promotion_id}/approve"
PROMOTION_REJECT_PATH = PREFIX + "/workflow-promotions/{promotion_id}/reject"


def _require_mcp_dependencies_ready(
    session: Any,
    version: CaliberWorkflowVersion,
    *,
    alias: str,
) -> None:
    blockers = deployment_blockers(
        session, version.manifest or {}, alias=alias, require_resolvable_subworkflows=True
    )
    if blockers:
        raise HTTPException(
            status_code=400,
            detail="MCP deployment preflight failed: " + "; ".join(blockers),
        )


def _envelope(payload: dict[str, Any], status_code: int = 200) -> JSONResponse:
    return JSONResponse({"data": payload}, status_code=status_code)


def _visible_workflow(session: Session, request: Request, workflow_id: str) -> Any:
    """The workflow, only if visible to the caller; ``None`` otherwise.

    Callers turn ``None`` into the same 404 a missing workflow produces, so "exists but
    forbidden" is never distinguishable from "does not exist" -- the distinction is
    itself a disclosure.
    """
    return get_visible(
        session,
        CaliberWorkflow,
        CaliberWorkflow.workflow_id,
        workflow_id,
        resolve_identity(request),
    )


def _visible_promotion(session: Session, request: Request, promotion_id: str) -> Any:
    """A promotion, only if its parent workflow is visible.

    Promotion approve/reject previously fetched by promotion id with no project scoping
    at all, so a known id could approve a rotation in another project -- a release
    decision made by someone with no access to the thing being released.
    """
    promotion = session.get(CaliberWorkflowPromotion, promotion_id)
    if promotion is None:
        return None
    if _visible_workflow(session, request, promotion.workflow_id) is None:
        return None
    return promotion


async def list_deployments(request: Request) -> JSONResponse:
    require_user(request)
    workflow_id = request.path_params["workflow_id"]
    factory = get_session_factory(request)
    with factory() as session:
        if _visible_workflow(session, request, workflow_id) is None:
            raise HTTPException(status_code=404, detail=f"workflow {workflow_id!r} not found")
        rows = (
            session.execute(
                select(CaliberWorkflowDeployment)
                .where(CaliberWorkflowDeployment.workflow_id == workflow_id)
                .order_by(CaliberWorkflowDeployment.alias)
            )
            .scalars()
            .all()
        )
        items = [WorkflowDeploymentSchema.model_validate(r) for r in rows]
    return envelope_response(items)


async def promote_deployment(request: Request) -> JSONResponse:
    workflow_id = request.path_params["workflow_id"]
    alias = request.path_params["alias"]
    body = await parse_json_object(request)
    payload = PromoteRequest.model_validate(body)
    # A promotion that pauses for human approval is an admin action; an
    # immediate rotation stays operator-scoped. Resolved from release policy so the
    # scope requirement and the approval requirement cannot disagree.
    config = request.app.state.config
    required = SCOPE_ADMIN if requires_human_approval(alias, config) else SCOPE_OPERATOR
    actor = require_scopes(request, [required])

    factory = get_session_factory(request)
    with factory() as session:
        if _visible_workflow(session, request, workflow_id) is None:
            raise HTTPException(status_code=404, detail=f"workflow {workflow_id!r} not found")
        version = session.get(CaliberWorkflowVersion, payload.version_id)
        if version is None or version.workflow_id != workflow_id:
            raise HTTPException(
                status_code=404,
                detail=f"version {payload.version_id!r} not found for workflow {workflow_id!r}",
            )
        _require_mcp_dependencies_ready(session, version, alias=alias)
        # Optimistic-concurrency guard: refuse if the alias has moved since the
        # caller last saw it (avoids clobbering a concurrent promotion).
        # ``expected_version_id=null`` asserts the alias is not yet deployed.
        if "expected_version_id" in body:
            expected_version_id = body.get("expected_version_id")
            if expected_version_id is not None and not isinstance(expected_version_id, str):
                raise HTTPException(
                    status_code=400, detail="'expected_version_id' must be a string or null"
                )
            current_dep = (
                session.execute(
                    select(CaliberWorkflowDeployment)
                    .where(CaliberWorkflowDeployment.workflow_id == workflow_id)
                    .where(CaliberWorkflowDeployment.alias == alias)
                )
                .scalars()
                .first()
            )
            current_vid = current_dep.version_id if current_dep is not None else None
            if current_vid != expected_version_id:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"alias {alias!r} now serves {current_vid or 'no version'}, "
                        f"not the expected {expected_version_id or 'no version'}; reload and retry"
                    ),
                )
        try:
            result = promote(
                session,
                workflow_id,
                alias,
                version,
                actor=actor,
                config=request.app.state.config,
            )
        except DeployError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except AliasPreflightError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except DeployGateFailedError as exc:
            metrics.record_deploy_gate(alias, passed=False)
            return JSONResponse(
                {"detail": str(exc), "status_code": 400, "gate": exc.detail},
                status_code=400,
            )

        if result.gate_result.has_gate:
            metrics.record_deploy_gate(alias, passed=True)

        if result.rotated and result.deployment is not None:
            metrics.record_workflow_promotion(alias)
            audit_record(
                session,
                actor=actor,
                action="promote_workflow",
                entity_type="workflow",
                entity_id=workflow_id,
                details={
                    "alias": alias,
                    "version_id": version.version_id,
                    "gate": result.gate_result.to_dict(),
                },
            )
            session.commit()
            return _envelope(
                {
                    "rotated": True,
                    "deployment": WorkflowDeploymentSchema.model_validate(
                        result.deployment
                    ).model_dump(mode="json"),
                    "promotion": None,
                    "gate": result.gate_result.to_dict(),
                }
            )

        # Gated alias: pending approval, alias unchanged.
        promotion = result.promotion
        assert promotion is not None
        audit_record(
            session,
            actor=actor,
            action="request_workflow_promotion",
            entity_type="workflow",
            entity_id=workflow_id,
            details={
                "alias": alias,
                "version_id": version.version_id,
                "promotion_id": promotion.promotion_id,
                "gate": result.gate_result.to_dict(),
            },
        )
        session.commit()
        return _envelope(
            {
                "rotated": False,
                "deployment": None,
                "promotion": WorkflowPromotionSchema.model_validate(promotion).model_dump(
                    mode="json"
                ),
                "gate": result.gate_result.to_dict(),
            },
            status_code=202,
        )


async def list_promotions(request: Request) -> JSONResponse:
    require_user(request)
    workflow_id = request.path_params["workflow_id"]
    factory = get_session_factory(request)
    with factory() as session:
        if _visible_workflow(session, request, workflow_id) is None:
            raise HTTPException(status_code=404, detail=f"workflow {workflow_id!r} not found")
        stmt = (
            select(CaliberWorkflowPromotion)
            .where(CaliberWorkflowPromotion.workflow_id == workflow_id)
            .order_by(CaliberWorkflowPromotion.requested_at.desc())
        )
        status = request.query_params.get("status")
        if status is not None:
            stmt = stmt.where(CaliberWorkflowPromotion.status == status)
        rows = session.execute(stmt).scalars().all()
        items = [WorkflowPromotionSchema.model_validate(r) for r in rows]
    return envelope_response(items)


async def approve_promotion_route(request: Request) -> JSONResponse:
    promotion_id = request.path_params["promotion_id"]
    body = await parse_json_object(request, allow_empty=True)
    payload = PromotionDecisionRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_APPROVER])
    factory = get_session_factory(request)
    with factory() as session:
        promotion = _visible_promotion(session, request, promotion_id)
        if promotion is None:
            raise HTTPException(status_code=404, detail=f"promotion {promotion_id!r} not found")
        version = session.get(CaliberWorkflowVersion, promotion.version_id)
        if version is None:
            raise HTTPException(
                status_code=409,
                detail=f"promotion version {promotion.version_id!r} no longer exists",
            )
        try:
            _require_mcp_dependencies_ready(session, version, alias=promotion.alias)
        except HTTPException as exc:
            raise HTTPException(status_code=409, detail=exc.detail) from exc
        try:
            deployment = approve_promotion(
                session, promotion, actor=actor, config=request.app.state.config
            )
        except DeployError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AliasPreflightError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        metrics.record_workflow_promotion(promotion.alias)
        audit_record(
            session,
            actor=actor,
            action="approve_workflow_promotion",
            entity_type="workflow",
            entity_id=promotion.workflow_id,
            details={
                "alias": promotion.alias,
                "version_id": promotion.version_id,
                "promotion_id": promotion.promotion_id,
                "notes": payload.reason,
            },
        )
        session.commit()
        return _envelope(
            {
                "rotated": True,
                "deployment": WorkflowDeploymentSchema.model_validate(deployment).model_dump(
                    mode="json"
                ),
                "promotion": WorkflowPromotionSchema.model_validate(promotion).model_dump(
                    mode="json"
                ),
            }
        )


async def reject_promotion_route(request: Request) -> JSONResponse:
    promotion_id = request.path_params["promotion_id"]
    body = await parse_json_object(request, allow_empty=True)
    payload = PromotionDecisionRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_APPROVER])
    factory = get_session_factory(request)
    with factory() as session:
        promotion = _visible_promotion(session, request, promotion_id)
        if promotion is None:
            raise HTTPException(status_code=404, detail=f"promotion {promotion_id!r} not found")
        try:
            reject_promotion(session, promotion, actor=actor, reason=payload.reason)
        except DeployError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit_record(
            session,
            actor=actor,
            action="reject_workflow_promotion",
            entity_type="workflow",
            entity_id=promotion.workflow_id,
            details={"promotion_id": promotion.promotion_id, "reason": payload.reason},
        )
        session.commit()
        data = WorkflowPromotionSchema.model_validate(promotion)
    return envelope_response(data)


async def rollback_deployment(request: Request) -> JSONResponse:
    workflow_id = request.path_params["workflow_id"]
    alias = request.path_params["alias"]
    # Rollback is scoped to operator to match promote (workflow promote is
    # operator-scoped while GATED_ALIASES is empty): whoever can move the live
    # alias forward can also undo it. Re-raise to SCOPE_ADMIN here if a future
    # build wants prod rollbacks gated to admins.
    actor = require_scopes(request, [SCOPE_OPERATOR])
    factory = get_session_factory(request)
    with factory() as session:
        if _visible_workflow(session, request, workflow_id) is None:
            raise HTTPException(status_code=404, detail=f"workflow {workflow_id!r} not found")
        try:
            deployment = rollback(
                session, workflow_id, alias, actor=actor, config=request.app.state.config
            )
        except RollbackError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AliasPreflightError as exc:
            # 409, not 404: the checkpoint exists and the request is well formed —
            # the target's MCP dependencies are no longer deployable.
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        audit_record(
            session,
            actor=actor,
            action="rollback_workflow",
            entity_type="workflow",
            entity_id=workflow_id,
            details={
                "alias": alias,
                "version_id": deployment.version_id,
                "environment": deployment.environment,
            },
        )
        session.commit()
        data = WorkflowDeploymentSchema.model_validate(deployment)
    return envelope_response(data)


def register(app: Starlette) -> None:
    app.routes.append(Route(LIST_PATH, list_deployments, methods=["GET"]))
    app.routes.append(Route(PROMOTE_PATH, promote_deployment, methods=["POST"]))
    app.routes.append(Route(ROLLBACK_PATH, rollback_deployment, methods=["POST"]))
    app.routes.append(Route(PROMOTIONS_PATH, list_promotions, methods=["GET"]))
    app.routes.append(Route(PROMOTION_APPROVE_PATH, approve_promotion_route, methods=["POST"]))
    app.routes.append(Route(PROMOTION_REJECT_PATH, reject_promotion_route, methods=["POST"]))
