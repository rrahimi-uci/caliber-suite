"""Workflow calibration routes."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_OPERATOR, require_scopes, require_user
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberRefinementJob,
    CaliberVerificationItem,
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowVersion,
)
from caliber.ids import new_item_id, new_job_id
from caliber.routes._deps import envelope_response, get_session_factory, parse_json_object
from caliber.schemas import (
    RefinementJobSchema,
    VerificationItemSchema,
    WorkflowCalibrationOptionsResponse,
    WorkflowCalibrationRunRequest,
    WorkflowCalibrationRunResponse,
)
from caliber.workflows.judge import llm_judge_status
from caliber.workflows.manifest import WorkflowManifest, parse_manifest

PREFIX = "/ajax-api/2.0/mlflow/caliber"
OPTIONS_PATH = PREFIX + "/workflows/{workflow_id}/calibration/options"
RUNS_PATH = PREFIX + "/workflows/{workflow_id}/calibration/runs"

_OBJECTIVES = ["quality", "tool_correctness", "tool_adherence"]
_MOVES = [
    "add_grounding_guardrail",
    "update_tool_constraint",
    "reroute_handoff",
    "relax_soft_guardrail",
]
_SCORERS = ["quality_match", "tool_adherence", "completion", "safety"]


def _baseline_version(session: Session, workflow_id: str) -> CaliberWorkflowVersion | None:
    deployments = (
        session.execute(
            select(CaliberWorkflowDeployment).where(
                CaliberWorkflowDeployment.workflow_id == workflow_id
            )
        )
        .scalars()
        .all()
    )
    by_alias = {row.alias: row for row in deployments}
    for alias in ("prod", "staging", "dev"):
        deployment = by_alias.get(alias)
        if deployment is None:
            continue
        version = session.get(CaliberWorkflowVersion, deployment.version_id)
        if version is not None:
            return version

    versions = (
        session.execute(
            select(CaliberWorkflowVersion)
            .where(CaliberWorkflowVersion.workflow_id == workflow_id)
            .order_by(CaliberWorkflowVersion.version_number.desc())
        )
        .scalars()
        .all()
    )
    for version in versions:
        if version.status == "published":
            return version
    return versions[0] if versions else None


def _dataset_name(manifest: WorkflowManifest, dataset_ref: str) -> str:
    artifact = manifest.artifacts.eval_datasets.get(dataset_ref)
    return artifact.dataset_name if artifact else dataset_ref


def _active_dataset_examples(
    session: Session,
    *,
    dataset_name: str,
    limit: int | None = None,
) -> tuple[CaliberEvalDataset | None, list[CaliberEvalDatasetExample]]:
    dataset = (
        session.execute(
            select(CaliberEvalDataset).where(
                CaliberEvalDataset.name == dataset_name,
                CaliberEvalDataset.status == "active",
            )
        )
        .scalars()
        .first()
    )
    if dataset is None:
        return None, []
    stmt = (
        select(CaliberEvalDatasetExample)
        .where(
            CaliberEvalDatasetExample.dataset_id == dataset.dataset_id,
            CaliberEvalDatasetExample.superseded_at.is_(None),
        )
        .order_by(CaliberEvalDatasetExample.created_at.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    examples = list(session.execute(stmt).scalars().all())
    return dataset, examples


def _available_dataset_summary(
    session: Session,
    manifest: WorkflowManifest,
    *,
    dataset_ref: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    gate_refs = [gate.dataset_ref for gate in manifest.deploy_gates.values()]
    if dataset_ref:
        gate_refs = [dataset_ref]
    checked: list[dict[str, Any]] = []
    for ref in gate_refs:
        name = _dataset_name(manifest, ref)
        dataset, examples = _active_dataset_examples(session, dataset_name=name, limit=limit)
        summary = {
            "dataset_ref": ref,
            "dataset_name": name,
            "dataset_id": dataset.dataset_id if dataset is not None else None,
            "active": dataset is not None,
            "example_count": len(examples),
        }
        checked.append(summary)
        if dataset is not None and examples:
            return {**summary, "available": True, "checked": checked}
    return {
        "available": False,
        "checked": checked,
        "reason": "No active deploy-gate eval dataset with non-superseded examples.",
    }


def _require_dataset(
    session: Session,
    manifest: WorkflowManifest,
    payload: WorkflowCalibrationRunRequest,
) -> dict[str, Any]:
    summary = _available_dataset_summary(
        session,
        manifest,
        dataset_ref=payload.dataset.dataset_ref,
        limit=payload.budget.max_eval_examples,
    )
    if not summary.get("available"):
        raise HTTPException(
            status_code=400,
            detail="Workflow calibration requires an active deploy-gate eval dataset with examples.",
        )
    return summary


def _judge_summary(config: Any) -> dict[str, Any]:
    status = llm_judge_status(config)
    payload: dict[str, Any] = {
        "available": bool(status["available"]),
        "provider": status.get("provider"),
        "model": status.get("model"),
    }
    if status.get("reason"):
        payload["reason"] = status["reason"]
    return payload


def _validate_run_payload(payload: WorkflowCalibrationRunRequest, *, config: Any) -> None:
    if payload.judge.enabled:
        judge = _judge_summary(config)
        if not judge["available"]:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Workflow calibration LLM judge is unavailable. "
                    f"{judge.get('reason') or 'Configure a supported provider to enable judge scoring.'}"
                ),
            )
    if payload.budget.min_examples > payload.budget.max_eval_examples:
        raise HTTPException(status_code=400, detail="min_examples cannot exceed max_eval_examples.")


def enqueue_workflow_calibration_run(
    *,
    session: Session,
    workflow_id: str,
    payload: WorkflowCalibrationRunRequest,
    actor: str,
    config: Any,
) -> WorkflowCalibrationRunResponse:
    """Create the verified item + queued job for a workflow calibration run."""
    _validate_run_payload(payload, config=config)
    workflow = session.get(CaliberWorkflow, workflow_id)
    if workflow is None:
        raise HTTPException(status_code=404, detail=f"workflow {workflow_id!r} not found")
    agent = session.get(CaliberAgentConfig, payload.agent_id)
    if agent is None:
        raise HTTPException(status_code=400, detail=f"agent {payload.agent_id!r} is not registered")
    if not agent.enabled:
        raise HTTPException(status_code=400, detail=f"agent {payload.agent_id!r} is disabled")
    version = _baseline_version(session, workflow_id)
    if version is None:
        raise HTTPException(
            status_code=400, detail=f"workflow {workflow_id!r} has no baseline version"
        )
    manifest = parse_manifest(version.manifest)
    dataset_summary = _require_dataset(session, manifest, payload)
    spec = payload.model_dump(mode="json")
    spec["workflow_id"] = workflow_id
    spec["workflow_version_id"] = version.version_id
    spec["dataset_summary"] = dataset_summary
    spec["low_confidence"] = int(dataset_summary["example_count"]) < payload.budget.min_examples

    item = CaliberVerificationItem(
        item_id=new_item_id(),
        agent_id=payload.agent_id,
        workflow_id=workflow_id,
        category="workflow_calibration",
        free_text=f"Workflow calibration requested for {workflow.name}.",
        severity="standard",
        artifact_type_hint="workflow_manifest",
        artifact_ref=version.version_id,
        submitted_context={
            "workflow_id": workflow_id,
            "workflow_version_id": version.version_id,
            "calibration_spec": spec,
        },
        status="verified",
        verified_by=actor,
        verified_at=datetime.now(timezone.utc),
        refinement_target="workflow_manifest",
    )
    session.add(item)
    session.flush()
    job = CaliberRefinementJob(
        job_id=new_job_id(),
        agent_id=payload.agent_id,
        workflow_id=workflow_id,
        primary_item_id=item.item_id,
        artifact_type="workflow_manifest",
        status="queued",
        current_stage="diagnosis",
        bundle_targets=[],
        calibration_spec=spec,
    )
    session.add(job)
    session.flush()
    audit_record(
        session,
        actor=actor,
        action="create_workflow_calibration_item",
        entity_type="verification_item",
        entity_id=item.item_id,
        details={"workflow_id": workflow_id, "job_id": job.job_id},
    )
    audit_record(
        session,
        actor=actor,
        action="create_workflow_calibration_job",
        entity_type="refinement_job",
        entity_id=job.job_id,
        details={"workflow_id": workflow_id, "calibration_spec": spec},
    )
    session.commit()
    return WorkflowCalibrationRunResponse(
        item=VerificationItemSchema.model_validate(item),
        job=RefinementJobSchema.model_validate(job),
    )


async def get_options(request: Request) -> JSONResponse:
    require_user(request)
    workflow_id = request.path_params["workflow_id"]
    factory = get_session_factory(request)
    judge = _judge_summary(request.app.state.config)
    with factory() as session:
        if session.get(CaliberWorkflow, workflow_id) is None:
            raise HTTPException(status_code=404, detail=f"workflow {workflow_id!r} not found")
        version = _baseline_version(session, workflow_id)
        data: dict[str, Any]
        if version is None:
            data = {"available": False, "reason": "Workflow has no versions.", "judge": judge}
        else:
            manifest = parse_manifest(version.manifest)
            data = {
                "workflow_version_id": version.version_id,
                "deploy_gate_dataset": _available_dataset_summary(session, manifest),
                "judge": judge,
            }
    return envelope_response(
        WorkflowCalibrationOptionsResponse(
            supported_objectives=_OBJECTIVES,
            supported_move_set=_MOVES,
            scorer_options=_SCORERS,
            default_budget={"max_candidates": 3, "max_eval_examples": 20, "min_examples": 2},
            data=data,
        )
    )


async def create_run(request: Request) -> JSONResponse:
    actor = require_scopes(request, [SCOPE_OPERATOR])
    workflow_id = request.path_params["workflow_id"]
    body = await parse_json_object(request)
    try:
        payload = WorkflowCalibrationRunRequest.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    factory = get_session_factory(request)
    with factory() as session:
        response = enqueue_workflow_calibration_run(
            session=session,
            workflow_id=workflow_id,
            payload=payload,
            actor=actor,
            config=request.app.state.config,
        )
    return envelope_response(response, status_code=201)


def register(app: Starlette) -> None:
    app.routes.append(Route(OPTIONS_PATH, get_options, methods=["GET"]))
    app.routes.append(Route(RUNS_PATH, create_run, methods=["POST"]))
