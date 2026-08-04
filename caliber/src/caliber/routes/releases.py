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

import hashlib
import json
import logging
from collections.abc import Sequence
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
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
from caliber.db.models import (
    CaliberAuditLog,
    CaliberKnowledgeBase,
    CaliberReleaseCandidate,
    CaliberReleaseOperation,
    CaliberReleaseReportJob,
    CaliberReleaseSignoff,
    CaliberSkill,
    CaliberWorkflow,
    CaliberWorkflowDeployment,
)
from caliber.db.scoping import apply_visibility_filter, get_visible
from caliber.ids import (
    new_release_candidate_id,
    new_release_report_job_id,
    new_release_signoff_id,
)
from caliber.release_operations import (
    PreparedReleaseResolutionError,
    abandon_prepared_prompt_release,
    execute_prompt_alias_release,
    reconcile_prompt_alias_releases,
    serialize_release_operation,
)
from caliber.routes._deps import (
    envelope_response,
    envelope_response_dict,
    get_session_factory,
    parse_json_object,
)
from caliber.schemas import (
    ReleaseCandidateCreateRequest,
    ReleaseCandidateSchema,
    ReleaseReportJobSchema,
    ReleaseSignoffRequest,
    ReleaseSignoffSchema,
    ReleaseWaiverRequest,
)

logger = logging.getLogger("caliber.routes.releases")

TIMELINE_PATH = "/ajax-api/2.0/mlflow/caliber/releases/timeline"
LIVE_PATH = "/ajax-api/2.0/mlflow/caliber/releases/live"
OPERATIONS_PATH = "/ajax-api/2.0/mlflow/caliber/releases/operations"
RECONCILE_PATH = "/ajax-api/2.0/mlflow/caliber/releases/operations/reconcile"
RESOLVE_OPERATION_PATH = "/ajax-api/2.0/mlflow/caliber/releases/operations/{operation_id}/resolve"
CANDIDATES_PATH = "/ajax-api/2.0/mlflow/caliber/releases/candidates"
CANDIDATE_PATH = CANDIDATES_PATH + "/{candidate_id}"
CANDIDATE_EVALUATE_PATH = CANDIDATE_PATH + "/evaluate"
CANDIDATE_WAIVERS_PATH = CANDIDATE_PATH + "/waivers"
CANDIDATE_SIGNOFF_PATH = CANDIDATE_PATH + "/signoffs"
CANDIDATE_REPORTS_PATH = CANDIDATE_PATH + "/reports"
REPORT_JOB_PATH = "/ajax-api/2.0/mlflow/caliber/releases/report-jobs/{report_job_id}"

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


def _evaluate_candidate(candidate: CaliberReleaseCandidate) -> None:
    criteria = list(candidate.criteria or [])
    total_weight = sum(float(item.get("weight", 0)) for item in criteria)
    weighted = sum(float(item.get("weight", 0)) * float(item.get("score", 0)) for item in criteria)
    candidate.weighted_score = round(weighted / total_weight, 6) if total_weight else 0.0
    waived = {str(item.get("criterion_key")) for item in list(candidate.waivers or [])}
    blockers = [
        {
            "criterion_key": str(item.get("key")),
            "title": str(item.get("title")),
            "score": float(item.get("score", 0)),
            "threshold": float(item.get("threshold", 0.5)),
        }
        for item in criteria
        if bool(item.get("blocking"))
        and float(item.get("score", 0)) < float(item.get("threshold", 0.5))
        and str(item.get("key")) not in waived
    ]
    candidate.blockers = blockers
    candidate.status = (
        "ready"
        if candidate.weighted_score >= candidate.required_score and not blockers
        else "blocked"
    )


def _candidate_snapshot(candidate: CaliberReleaseCandidate) -> dict[str, Any]:
    return ReleaseCandidateSchema.model_validate(candidate).model_dump(mode="json")


def list_release_candidates(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    with factory() as session:
        stmt = apply_visibility_filter(
            select(CaliberReleaseCandidate).order_by(CaliberReleaseCandidate.created_at.desc()),
            CaliberReleaseCandidate,
            identity,
            identity.active_project_id,
        )
        rows = session.execute(stmt).scalars().all()
        data = [ReleaseCandidateSchema.model_validate(row) for row in rows]
    return envelope_response(data)


def _create_release_candidate(
    factory: Any,
    *,
    actor: str,
    identity: CaliberIdentity,
    payload: ReleaseCandidateCreateRequest,
) -> ReleaseCandidateSchema:
    with factory() as session:
        candidate = CaliberReleaseCandidate(
            candidate_id=new_release_candidate_id(),
            project_id=identity.active_project_id,
            visibility="project" if identity.active_project_id else "user",
            name=payload.name,
            artifact_type=payload.artifact_type,
            artifact_ref=payload.artifact_ref,
            version_ref=payload.version_ref,
            criteria=[item.model_dump(mode="json") for item in payload.criteria],
            evidence=[item.model_dump(mode="json") for item in payload.evidence],
            waivers=[],
            required_score=payload.required_score,
            blockers=[],
            planned_action=dict(payload.planned_action),
            rollback_target=dict(payload.rollback_target),
            status="draft",
            owner=actor,
        )
        session.add(candidate)
        session.flush()
        _evaluate_candidate(candidate)
        audit_record(
            session,
            actor=actor,
            action="create_release_candidate",
            entity_type="release_candidate",
            entity_id=candidate.candidate_id,
            details={
                "artifact_type": candidate.artifact_type,
                "artifact_ref": candidate.artifact_ref,
            },
        )
        session.commit()
        return ReleaseCandidateSchema.model_validate(candidate)


async def create_release_candidate(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    payload = ReleaseCandidateCreateRequest.model_validate(await parse_json_object(request))
    data = await run_in_threadpool(
        _create_release_candidate,
        get_session_factory(request),
        actor=actor,
        identity=identity,
        payload=payload,
    )
    return envelope_response(data, status_code=201)


def get_release_candidate(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    candidate_id = request.path_params["candidate_id"]
    factory = get_session_factory(request)
    with factory() as session:
        candidate = get_visible(
            session,
            CaliberReleaseCandidate,
            CaliberReleaseCandidate.candidate_id,
            candidate_id,
            identity,
        )
        if candidate is None:
            raise HTTPException(
                status_code=404, detail=f"release candidate {candidate_id!r} not found"
            )
        signoffs = (
            session.execute(
                select(CaliberReleaseSignoff)
                .where(CaliberReleaseSignoff.candidate_id == candidate_id)
                .order_by(CaliberReleaseSignoff.created_at.desc())
            )
            .scalars()
            .all()
        )
        jobs = (
            session.execute(
                select(CaliberReleaseReportJob)
                .where(CaliberReleaseReportJob.candidate_id == candidate_id)
                .order_by(CaliberReleaseReportJob.created_at.desc())
            )
            .scalars()
            .all()
        )
        data = {
            "candidate": ReleaseCandidateSchema.model_validate(candidate).model_dump(mode="json"),
            "signoffs": [
                ReleaseSignoffSchema.model_validate(row).model_dump(mode="json") for row in signoffs
            ],
            "report_jobs": [
                ReleaseReportJobSchema.model_validate(row).model_dump(mode="json") for row in jobs
            ],
        }
    return envelope_response_dict(data)


def evaluate_release_candidate(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    candidate_id = request.path_params["candidate_id"]
    factory = get_session_factory(request)
    with factory() as session:
        candidate = get_visible(
            session,
            CaliberReleaseCandidate,
            CaliberReleaseCandidate.candidate_id,
            candidate_id,
            identity,
        )
        if candidate is None:
            raise HTTPException(
                status_code=404, detail=f"release candidate {candidate_id!r} not found"
            )
        if candidate.status in {"signed_go", "signed_no_go"}:
            raise HTTPException(status_code=409, detail="signed release candidates are immutable")
        _evaluate_candidate(candidate)
        audit_record(
            session,
            actor=actor,
            action="evaluate_release_candidate",
            entity_type="release_candidate",
            entity_id=candidate_id,
            details={"weighted_score": candidate.weighted_score, "blockers": candidate.blockers},
        )
        session.commit()
        data = ReleaseCandidateSchema.model_validate(candidate)
    return envelope_response(data)


def _add_release_waiver(
    factory: Any,
    *,
    actor: str,
    identity: CaliberIdentity,
    candidate_id: str,
    payload: ReleaseWaiverRequest,
) -> ReleaseCandidateSchema:
    with factory() as session:
        candidate = get_visible(
            session,
            CaliberReleaseCandidate,
            CaliberReleaseCandidate.candidate_id,
            candidate_id,
            identity,
        )
        if candidate is None:
            raise HTTPException(
                status_code=404, detail=f"release candidate {candidate_id!r} not found"
            )
        criterion_keys = {str(item.get("key")) for item in candidate.criteria}
        if payload.criterion_key not in criterion_keys:
            raise HTTPException(status_code=400, detail="waiver criterion does not exist")
        waiver = {
            "criterion_key": payload.criterion_key,
            "reason": payload.reason,
            "expires_at": payload.expires_at.isoformat() if payload.expires_at else None,
            "approved_by": actor,
            "approved_at": datetime.now(timezone.utc).isoformat(),
        }
        candidate.waivers = [*list(candidate.waivers or []), waiver]
        _evaluate_candidate(candidate)
        audit_record(
            session,
            actor=actor,
            action="waive_release_blocker",
            entity_type="release_candidate",
            entity_id=candidate_id,
            details=waiver,
        )
        session.commit()
        return ReleaseCandidateSchema.model_validate(candidate)


async def add_release_waiver(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_ADMIN])
    identity = resolve_identity(request)
    candidate_id = request.path_params["candidate_id"]
    payload = ReleaseWaiverRequest.model_validate(await parse_json_object(request))
    data = await run_in_threadpool(
        _add_release_waiver,
        get_session_factory(request),
        actor=actor,
        identity=identity,
        candidate_id=candidate_id,
        payload=payload,
    )
    return envelope_response(data)


def _signoff_release_candidate(
    factory: Any,
    *,
    actor: str,
    identity: CaliberIdentity,
    candidate_id: str,
    payload: ReleaseSignoffRequest,
) -> ReleaseSignoffSchema:
    with factory() as session:
        candidate = get_visible(
            session,
            CaliberReleaseCandidate,
            CaliberReleaseCandidate.candidate_id,
            candidate_id,
            identity,
        )
        if candidate is None:
            raise HTTPException(
                status_code=404, detail=f"release candidate {candidate_id!r} not found"
            )
        if candidate.status in {"signed_go", "signed_no_go"}:
            raise HTTPException(
                status_code=409, detail="release candidate already has a final signoff"
            )
        _evaluate_candidate(candidate)
        if payload.decision == "go":
            if candidate.status != "ready":
                raise HTTPException(status_code=409, detail="go signoff requires a ready candidate")
            if not candidate.planned_action or not candidate.rollback_target:
                raise HTTPException(
                    status_code=409,
                    detail="go signoff requires a planned release action and rollback target",
                )
        signoff = CaliberReleaseSignoff(
            signoff_id=new_release_signoff_id(),
            candidate_id=candidate_id,
            decision=payload.decision,
            rationale=payload.rationale,
            decided_by=actor,
            candidate_snapshot=_candidate_snapshot(candidate),
        )
        candidate.status = f"signed_{payload.decision}"
        session.add(signoff)
        audit_record(
            session,
            actor=actor,
            action="signoff_release_candidate",
            entity_type="release_candidate",
            entity_id=candidate_id,
            details={"signoff_id": signoff.signoff_id, "decision": payload.decision},
        )
        session.commit()
        return ReleaseSignoffSchema.model_validate(signoff)


async def signoff_release_candidate(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_ADMIN])
    identity = resolve_identity(request)
    candidate_id = request.path_params["candidate_id"]
    payload = ReleaseSignoffRequest.model_validate(await parse_json_object(request))
    data = await run_in_threadpool(
        _signoff_release_candidate,
        get_session_factory(request),
        actor=actor,
        identity=identity,
        candidate_id=candidate_id,
        payload=payload,
    )
    return envelope_response(data, status_code=201)


def _allure_report(candidate: CaliberReleaseCandidate) -> dict[str, Any]:
    snapshot = _candidate_snapshot(candidate)
    digest = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    waived = {str(item.get("criterion_key")) for item in candidate.waivers}
    results = []
    for criterion in candidate.criteria:
        key = str(criterion.get("key"))
        passed = float(criterion.get("score", 0)) >= float(criterion.get("threshold", 0.5))
        status = "passed" if passed else ("skipped" if key in waived else "failed")
        results.append(
            {
                "uuid": f"{candidate.candidate_id}:{key}",
                "name": str(criterion.get("title")),
                "status": status,
                "statusDetails": {
                    "message": "waived"
                    if status == "skipped"
                    else f"score={criterion.get('score')} threshold={criterion.get('threshold')}"
                },
                "labels": [
                    {"name": "suite", "value": candidate.name},
                    {"name": "criterion", "value": key},
                ],
                "links": [
                    {"name": ref, "type": "evidence", "url": ref}
                    for ref in criterion.get("evidence_refs", [])
                ],
            }
        )
    return {
        "schema_version": 1,
        "format": "allure-compatible-results",
        "candidate_id": candidate.candidate_id,
        "candidate_snapshot_sha256": digest,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "results": results,
    }


def create_release_report_job(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    identity = resolve_identity(request)
    candidate_id = request.path_params["candidate_id"]
    factory = get_session_factory(request)
    with factory() as session:
        candidate = get_visible(
            session,
            CaliberReleaseCandidate,
            CaliberReleaseCandidate.candidate_id,
            candidate_id,
            identity,
        )
        if candidate is None:
            raise HTTPException(
                status_code=404, detail=f"release candidate {candidate_id!r} not found"
            )
        job = CaliberReleaseReportJob(
            report_job_id=new_release_report_job_id(),
            candidate_id=candidate_id,
            status="completed",
            format="allure-json",
            report=_allure_report(candidate),
            created_by=actor,
            completed_at=datetime.now(timezone.utc),
        )
        session.add(job)
        audit_record(
            session,
            actor=actor,
            action="generate_release_allure_report",
            entity_type="release_candidate",
            entity_id=candidate_id,
            details={"report_job_id": job.report_job_id, "format": job.format},
        )
        session.commit()
        data = ReleaseReportJobSchema.model_validate(job)
    return envelope_response(data, status_code=201)


def get_release_report_job(request: Request) -> JSONResponse:
    require_user(request)
    identity = resolve_identity(request)
    report_job_id = request.path_params["report_job_id"]
    factory = get_session_factory(request)
    with factory() as session:
        job = session.get(CaliberReleaseReportJob, report_job_id)
        if job is None:
            raise HTTPException(
                status_code=404, detail=f"release report job {report_job_id!r} not found"
            )
        candidate = get_visible(
            session,
            CaliberReleaseCandidate,
            CaliberReleaseCandidate.candidate_id,
            job.candidate_id,
            identity,
        )
        if candidate is None:
            raise HTTPException(
                status_code=404, detail=f"release report job {report_job_id!r} not found"
            )
        data = ReleaseReportJobSchema.model_validate(job)
    return envelope_response(data)


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
        stmt = select(CaliberReleaseOperation).order_by(CaliberReleaseOperation.created_at.desc())
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


def _resolve_prepared_release_operation(
    factory: Any,
    *,
    action: str,
    operation_id: str,
    actor: str,
    reason: str,
) -> dict[str, object]:
    """Resolve a prepared intent outside the async route's event-loop thread."""
    with factory() as session:
        row = session.get(CaliberReleaseOperation, operation_id)
        if row is None:
            raise HTTPException(
                status_code=404, detail=f"release operation {operation_id!r} not found"
            )
        if row.resource_type != "prompt" or row.status != "prepared":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"release operation {operation_id!r} is {row.status!r}; "
                    "only prepared prompt releases can be retried or abandoned"
                ),
            )
        if action == "retry":
            from caliber.routes import prompts as prompt_routes  # noqa: PLC0415

            result = execute_prompt_alias_release(
                session,
                row,
                mutate_alias=prompt_routes.set_prompt_alias_version,
            )
            result["operation_id"] = operation_id
            result["release_status"] = "applied"
            return result
        if action == "abandon":
            try:
                resolved = abandon_prepared_prompt_release(
                    session,
                    operation_id=operation_id,
                    actor=actor,
                    reason=reason,
                )
            except PreparedReleaseResolutionError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            return serialize_release_operation(resolved)
        raise HTTPException(status_code=400, detail="'action' must be 'retry' or 'abandon'")


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


async def resolve_release_operation(request: Request) -> JSONResponse:
    """Retry or abandon a pre-effect prompt release intent.

    Only ``prepared`` rows are accepted. Once a row reaches ``applying``, the
    provider may already have changed and reconciliation—not blind retry—is the
    safe operation.
    """
    actor = require_scopes(request, [SCOPE_OPERATOR])
    body = await parse_json_object(request)
    action = str(body.get("action") or "").strip().casefold()
    operation_id = request.path_params["operation_id"]
    factory = get_session_factory(request)
    data = await run_in_threadpool(
        _resolve_prepared_release_operation,
        factory,
        action=action,
        operation_id=operation_id,
        actor=actor,
        reason=str(body.get("reason") or ""),
    )
    return envelope_response_dict(data)


def register(app: Starlette) -> None:
    app.routes.append(Route(CANDIDATES_PATH, list_release_candidates, methods=["GET"]))
    app.routes.append(Route(CANDIDATES_PATH, create_release_candidate, methods=["POST"]))
    app.routes.append(Route(CANDIDATE_PATH, get_release_candidate, methods=["GET"]))
    app.routes.append(Route(CANDIDATE_EVALUATE_PATH, evaluate_release_candidate, methods=["POST"]))
    app.routes.append(Route(CANDIDATE_WAIVERS_PATH, add_release_waiver, methods=["POST"]))
    app.routes.append(Route(CANDIDATE_SIGNOFF_PATH, signoff_release_candidate, methods=["POST"]))
    app.routes.append(Route(CANDIDATE_REPORTS_PATH, create_release_report_job, methods=["POST"]))
    app.routes.append(Route(REPORT_JOB_PATH, get_release_report_job, methods=["GET"]))
    app.routes.append(Route(TIMELINE_PATH, timeline, methods=["GET"]))
    app.routes.append(Route(LIVE_PATH, live, methods=["GET"]))
    app.routes.append(Route(OPERATIONS_PATH, release_operations, methods=["GET"]))
    app.routes.append(Route(RECONCILE_PATH, reconcile_release_operations, methods=["POST"]))
    app.routes.append(Route(RESOLVE_OPERATION_PATH, resolve_release_operation, methods=["POST"]))
