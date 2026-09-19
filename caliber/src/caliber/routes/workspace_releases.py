"""Workspace release lifecycle routes (`P5-F`).

Phase 5 (`P5-A` through `P5-D`) built a complete, well-tested release state
machine, decision/governance layer, and durable adapter-execution pipeline --
:mod:`caliber.workspace_release_service`, :mod:`caliber.workspace_release_governance`,
:mod:`caliber.workspace_release_operations` -- but the only piece of it ever
reachable over HTTP was the operations sub-resource
(:mod:`caliber.routes.workspace_release_operations`), which presupposes a
release already exists. Nothing could create, list, evaluate, or decide one.
:mod:`caliber.routes.workspace_change_requests`'s own module docstring names
this gap explicitly: "the Phase-5 acceptance primitive is intentionally not
routed here." This module closes it.

Two authorization shapes coexist here, both already established by the
modules this wraps rather than invented for this file:

* Create/read/evaluate call :func:`caliber.resource_access.require_project_access`
  in the route layer first, exactly like every other Workspace resource --
  ``workspace_release_service`` does no authorization of its own.
* Quality-signoff/approve/break-glass delegate authorization entirely to
  :mod:`caliber.workspace_release_governance`, which needs identity-specific
  role checks (Reviewer vs. Owner, platform Admin for break-glass) a single
  scope check cannot express. The route only resolves an identity and maps
  the governance module's own exception hierarchy to HTTP status codes.

List endpoints use ``limit``/``offset`` (:func:`caliber.routes._deps.list_limit`),
matching this same release family's sibling
(:func:`caliber.routes.workspace_release_operations.list_operations`), not the
cursor convention :mod:`caliber.routes.workspace_change_requests` and
:mod:`caliber.routes.workspace` use -- the two conventions already coexist in
shipped code, and matching the immediate sibling keeps this one resource
family internally consistent rather than picking a third option.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.auth import CaliberIdentity, require_user, resolve_identity
from caliber.db.models import (
    CaliberWorkspaceRelease,
    CaliberWorkspaceReleaseEvaluation,
    CaliberWorkspaceReleaseEvidence,
)
from caliber.resource_access import require_project_access
from caliber.routes._deps import (
    envelope_response,
    get_session_factory,
    list_limit,
    parse_json_object,
)
from caliber.schemas import (
    WorkspaceBreakGlassApplyRequest,
    WorkspaceReleaseCreateRequest,
    WorkspaceReleaseDecisionRequest,
    WorkspaceReleaseDecisionSchema,
    WorkspaceReleaseEvaluateRequest,
    WorkspaceReleaseEvaluationSchema,
    WorkspaceReleaseEvidenceSchema,
    WorkspaceReleaseSchema,
)
from caliber.workspace_release_governance import (
    WorkspaceBreakGlassError,
    WorkspaceReleaseAuthorizationError,
    WorkspaceReleaseDecisionConflictError,
    WorkspaceReleaseGovernanceError,
    create_workspace_break_glass_apply,
    derive_break_glass_gate_evidence,
    record_workspace_release_decision,
)
from caliber.workspace_release_service import (
    RELEASE_DRAFT,
    WorkspaceReleaseConflictError,
    create_workspace_release,
    create_workspace_release_evaluation,
    start_workspace_release_evaluation,
)

PREFIX = "/ajax-api/2.0/mlflow/caliber"
RELEASES_PATH = PREFIX + "/projects/{project_id}/releases"
RELEASE_DETAIL_PATH = RELEASES_PATH + "/{release_id}"
EVIDENCE_PATH = RELEASE_DETAIL_PATH + "/evidence"
EVALUATE_PATH = RELEASE_DETAIL_PATH + "/evaluate"
EVALUATIONS_PATH = RELEASE_DETAIL_PATH + "/evaluations"
EVALUATION_DETAIL_PATH = EVALUATIONS_PATH + "/{evaluation_id}"
QUALITY_SIGNOFF_PATH = RELEASE_DETAIL_PATH + "/quality-signoff"
APPROVE_PATH = RELEASE_DETAIL_PATH + "/approve"
BREAK_GLASS_APPLY_PATH = RELEASE_DETAIL_PATH + "/break-glass-apply"

_Factory = sessionmaker[Session]


def _require_workspace_header(request: Request, project_id: str) -> None:
    header = request.headers.get("X-CALIBER-Project")
    if header is not None and header.strip() != project_id:
        raise HTTPException(status_code=400, detail="workspace_context_mismatch")


def _release_for_project(
    session: Session, project_id: str, release_id: str
) -> CaliberWorkspaceRelease:
    release = session.get(CaliberWorkspaceRelease, release_id)
    if release is None or release.project_id != project_id:
        raise HTTPException(status_code=404, detail="workspace release not found")
    return release


def _governance_error(exc: WorkspaceReleaseGovernanceError) -> HTTPException:
    """Map the governance module's exception hierarchy to a status code.

    Order matters at the call site: the more specific subclasses must be
    caught first, since a bare :class:`WorkspaceReleaseGovernanceError` (not
    found) and its subclasses (authorization/conflict/break-glass refusal)
    all share one base the governance module raises directly for "not found".
    """
    if isinstance(exc, WorkspaceReleaseAuthorizationError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, (WorkspaceReleaseDecisionConflictError, WorkspaceBreakGlassError)):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=404, detail=str(exc))


def _list_releases_sync(
    factory: _Factory,
    *,
    project_id: str,
    identity: CaliberIdentity,
    project_action: str,
    limit: int,
    offset: int,
    status: str | None,
    environment_id: str | None,
) -> list[dict[str, object]]:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        stmt = select(CaliberWorkspaceRelease).where(
            CaliberWorkspaceRelease.project_id == project_id
        )
        if status:
            stmt = stmt.where(CaliberWorkspaceRelease.status == status)
        if environment_id:
            stmt = stmt.where(CaliberWorkspaceRelease.environment_id == environment_id)
        rows = (
            session.execute(
                stmt.order_by(
                    CaliberWorkspaceRelease.created_at.desc(),
                    CaliberWorkspaceRelease.release_id.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        return [WorkspaceReleaseSchema.model_validate(row).model_dump(mode="json") for row in rows]


def _get_release_sync(
    factory: _Factory,
    *,
    project_id: str,
    release_id: str,
    identity: CaliberIdentity,
    project_action: str,
) -> WorkspaceReleaseSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        release = _release_for_project(session, project_id, release_id)
        return WorkspaceReleaseSchema.model_validate(release)


def _create_release_sync(
    factory: _Factory,
    *,
    project_id: str,
    identity: CaliberIdentity,
    project_action: str,
    payload: WorkspaceReleaseCreateRequest,
) -> WorkspaceReleaseSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        try:
            release = create_workspace_release(
                session,
                project_id=project_id,
                revision_id=payload.revision_id,
                environment_id=payload.environment_id,
                environment_config_sha256=payload.environment_config_sha256,
                runtime_dependencies_sha256=payload.runtime_dependencies_sha256,
                policy_sha256=payload.policy_sha256,
                request_idempotency_key=payload.request_idempotency_key,
                requested_by=identity.user_id,
                change_request_id=payload.change_request_id,
                change_request_head_id=payload.change_request_head_id,
                version_tag_id=payload.version_tag_id,
                predecessor_release_id=payload.predecessor_release_id,
            )
        except (WorkspaceReleaseConflictError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session.commit()
        return WorkspaceReleaseSchema.model_validate(release)


def _list_evidence_sync(
    factory: _Factory,
    *,
    project_id: str,
    release_id: str,
    identity: CaliberIdentity,
    project_action: str,
    limit: int,
    offset: int,
) -> list[dict[str, object]]:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        _release_for_project(session, project_id, release_id)
        rows = (
            session.execute(
                select(CaliberWorkspaceReleaseEvidence)
                .where(CaliberWorkspaceReleaseEvidence.workspace_release_id == release_id)
                .order_by(
                    CaliberWorkspaceReleaseEvidence.recorded_at.desc(),
                    CaliberWorkspaceReleaseEvidence.evidence_id.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        return [
            WorkspaceReleaseEvidenceSchema.model_validate(row).model_dump(mode="json")
            for row in rows
        ]


def _evaluate_sync(
    factory: _Factory,
    *,
    project_id: str,
    release_id: str,
    identity: CaliberIdentity,
    project_action: str,
    payload: WorkspaceReleaseEvaluateRequest,
) -> WorkspaceReleaseEvaluationSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        release = _release_for_project(session, project_id, release_id)
        try:
            if release.status == RELEASE_DRAFT:
                start_workspace_release_evaluation(
                    session,
                    release,
                    actor=identity.user_id,
                    expected_lock_version=release.lock_version,
                )
            evaluation = create_workspace_release_evaluation(
                session,
                project_id=project_id,
                workspace_release_id=release_id,
                idempotency_key=payload.idempotency_key,
                evaluation_plan_sha256=payload.evaluation_plan_sha256,
                input_sha256=payload.input_sha256,
                requested_by=identity.user_id,
            )
        except (WorkspaceReleaseConflictError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        session.commit()
        return WorkspaceReleaseEvaluationSchema.model_validate(evaluation)


def _list_evaluations_sync(
    factory: _Factory,
    *,
    project_id: str,
    release_id: str,
    identity: CaliberIdentity,
    project_action: str,
    limit: int,
    offset: int,
) -> list[dict[str, object]]:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        _release_for_project(session, project_id, release_id)
        rows = (
            session.execute(
                select(CaliberWorkspaceReleaseEvaluation)
                .where(CaliberWorkspaceReleaseEvaluation.workspace_release_id == release_id)
                .order_by(
                    CaliberWorkspaceReleaseEvaluation.requested_at.desc(),
                    CaliberWorkspaceReleaseEvaluation.evaluation_id.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )
        return [
            WorkspaceReleaseEvaluationSchema.model_validate(row).model_dump(mode="json")
            for row in rows
        ]


def _get_evaluation_sync(
    factory: _Factory,
    *,
    project_id: str,
    release_id: str,
    evaluation_id: str,
    identity: CaliberIdentity,
    project_action: str,
) -> WorkspaceReleaseEvaluationSchema:
    with factory() as session:
        require_project_access(session, identity, project_id, project_action)
        _release_for_project(session, project_id, release_id)
        evaluation = session.get(CaliberWorkspaceReleaseEvaluation, evaluation_id)
        if evaluation is None or evaluation.workspace_release_id != release_id:
            raise HTTPException(status_code=404, detail="workspace release evaluation not found")
        return WorkspaceReleaseEvaluationSchema.model_validate(evaluation)


def _decide_sync(
    factory: _Factory,
    *,
    project_id: str,
    release_id: str,
    identity: CaliberIdentity,
    kind: str,
    payload: WorkspaceReleaseDecisionRequest,
) -> WorkspaceReleaseDecisionSchema:
    with factory() as session:
        try:
            decision = record_workspace_release_decision(
                session,
                project_id=project_id,
                workspace_release_id=release_id,
                identity=identity,
                kind=kind,  # type: ignore[arg-type]  # validated by record_workspace_release_decision itself
                decision=payload.decision,
                rationale=payload.rationale,
                gate_evidence_sha256=payload.gate_evidence_sha256,
                change_request_head_id=payload.change_request_head_id,
            )
        except WorkspaceReleaseGovernanceError as exc:
            raise _governance_error(exc) from exc
        session.commit()
        return WorkspaceReleaseDecisionSchema.model_validate(decision)


def _break_glass_apply_sync(
    factory: _Factory,
    *,
    project_id: str,
    release_id: str,
    identity: CaliberIdentity,
    payload: WorkspaceBreakGlassApplyRequest,
) -> dict[str, object]:
    with factory() as session:
        try:
            # `create_workspace_break_glass_apply`'s own docstring: "P5-C
            # must replace/revalidate [these] from persisted machine and
            # integrity evidence immediately before the first external
            # effect." `derive_break_glass_gate_evidence` is that
            # revalidation: it reads the release's own currently-persisted
            # status and evidence digest right here, rather than trusting a
            # caller-supplied or previously-cached value, so a release whose
            # evidence has moved on since an earlier precondition check is
            # caught live. `create_workspace_break_glass_apply`'s existing
            # hard deny-on-false check (unchanged) still refuses the apply
            # outright if either comes back false -- break-glass never
            # bypasses a failed machine gate or integrity check.
            machine_gates_passed, integrity_checks_passed = derive_break_glass_gate_evidence(
                session,
                project_id=project_id,
                workspace_release_id=release_id,
                gate_evidence_sha256=payload.gate_evidence_sha256,
            )
            result = create_workspace_break_glass_apply(
                session,
                project_id=project_id,
                workspace_release_id=release_id,
                identity=identity,
                reason=payload.reason,
                incident_ref=payload.incident_ref,
                authorization_ref=payload.authorization_ref,
                expires_at=payload.expires_at,
                gate_evidence_sha256=payload.gate_evidence_sha256,
                expected_current_release_id=payload.expected_current_release_id,
                expected_environment_lock_version=payload.expected_environment_lock_version,
                idempotency_key=payload.idempotency_key,
                machine_gates_passed=machine_gates_passed,
                integrity_checks_passed=integrity_checks_passed,
            )
        except WorkspaceReleaseGovernanceError as exc:
            raise _governance_error(exc) from exc
        session.commit()
        return {
            "authorization_id": result.authorization.authorization_id,
            "operation_id": result.operation.operation_id,
        }


async def list_releases(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_user(request)
    identity = resolve_identity(request)
    limit, offset = list_limit(request, default=50, cap=200)
    data = await run_in_threadpool(
        _list_releases_sync,
        get_session_factory(request),
        project_id=project_id,
        identity=identity,
        project_action="read",
        limit=limit,
        offset=offset,
        status=request.query_params.get("status"),
        environment_id=request.query_params.get("environment_id"),
    )
    return JSONResponse({"data": data})


async def create_release(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    payload = WorkspaceReleaseCreateRequest.model_validate(await parse_json_object(request))
    require_user(request)
    identity = resolve_identity(request)
    schema = await run_in_threadpool(
        _create_release_sync,
        get_session_factory(request),
        project_id=project_id,
        identity=identity,
        project_action="release.request",
        payload=payload,
    )
    return envelope_response(schema, status_code=201)


async def get_release(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_user(request)
    identity = resolve_identity(request)
    schema = await run_in_threadpool(
        _get_release_sync,
        get_session_factory(request),
        project_id=project_id,
        release_id=request.path_params["release_id"],
        identity=identity,
        project_action="read",
    )
    return envelope_response(schema)


async def list_evidence(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_user(request)
    identity = resolve_identity(request)
    limit, offset = list_limit(request, default=50, cap=200)
    data = await run_in_threadpool(
        _list_evidence_sync,
        get_session_factory(request),
        project_id=project_id,
        release_id=request.path_params["release_id"],
        identity=identity,
        project_action="read",
        limit=limit,
        offset=offset,
    )
    return JSONResponse({"data": data})


async def evaluate_release(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    payload = WorkspaceReleaseEvaluateRequest.model_validate(await parse_json_object(request))
    require_user(request)
    identity = resolve_identity(request)
    schema = await run_in_threadpool(
        _evaluate_sync,
        get_session_factory(request),
        project_id=project_id,
        release_id=request.path_params["release_id"],
        identity=identity,
        project_action="release.evaluate",
        payload=payload,
    )
    return envelope_response(schema, status_code=202)


async def list_evaluations(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_user(request)
    identity = resolve_identity(request)
    limit, offset = list_limit(request, default=50, cap=200)
    data = await run_in_threadpool(
        _list_evaluations_sync,
        get_session_factory(request),
        project_id=project_id,
        release_id=request.path_params["release_id"],
        identity=identity,
        project_action="read",
        limit=limit,
        offset=offset,
    )
    return JSONResponse({"data": data})


async def get_evaluation(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    require_user(request)
    identity = resolve_identity(request)
    schema = await run_in_threadpool(
        _get_evaluation_sync,
        get_session_factory(request),
        project_id=project_id,
        release_id=request.path_params["release_id"],
        evaluation_id=request.path_params["evaluation_id"],
        identity=identity,
        project_action="read",
    )
    return envelope_response(schema)


async def quality_signoff(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    payload = WorkspaceReleaseDecisionRequest.model_validate(await parse_json_object(request))
    require_user(request)
    identity = resolve_identity(request)
    schema = await run_in_threadpool(
        _decide_sync,
        get_session_factory(request),
        project_id=project_id,
        release_id=request.path_params["release_id"],
        identity=identity,
        kind="quality",
        payload=payload,
    )
    return envelope_response(schema, status_code=201)


async def approve_release(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    payload = WorkspaceReleaseDecisionRequest.model_validate(await parse_json_object(request))
    require_user(request)
    identity = resolve_identity(request)
    schema = await run_in_threadpool(
        _decide_sync,
        get_session_factory(request),
        project_id=project_id,
        release_id=request.path_params["release_id"],
        identity=identity,
        kind="release",
        payload=payload,
    )
    return envelope_response(schema, status_code=201)


async def break_glass_apply(request: Request) -> JSONResponse:
    project_id = request.path_params["project_id"]
    _require_workspace_header(request, project_id)
    payload = WorkspaceBreakGlassApplyRequest.model_validate(await parse_json_object(request))
    require_user(request)
    identity = resolve_identity(request)
    data = await run_in_threadpool(
        _break_glass_apply_sync,
        get_session_factory(request),
        project_id=project_id,
        release_id=request.path_params["release_id"],
        identity=identity,
        payload=payload,
    )
    return JSONResponse({"data": data}, status_code=201)


def register(app: Starlette) -> None:
    app.routes.append(Route(RELEASES_PATH, list_releases, methods=["GET"]))
    app.routes.append(Route(RELEASES_PATH, create_release, methods=["POST"]))
    app.routes.append(Route(RELEASE_DETAIL_PATH, get_release, methods=["GET"]))
    app.routes.append(Route(EVIDENCE_PATH, list_evidence, methods=["GET"]))
    app.routes.append(Route(EVALUATE_PATH, evaluate_release, methods=["POST"]))
    app.routes.append(Route(EVALUATIONS_PATH, list_evaluations, methods=["GET"]))
    app.routes.append(Route(EVALUATION_DETAIL_PATH, get_evaluation, methods=["GET"]))
    app.routes.append(Route(QUALITY_SIGNOFF_PATH, quality_signoff, methods=["POST"]))
    app.routes.append(Route(APPROVE_PATH, approve_release, methods=["POST"]))
    app.routes.append(Route(BREAK_GLASS_APPLY_PATH, break_glass_apply, methods=["POST"]))


__all__ = ["register"]
