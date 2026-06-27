"""Workflow-manifest variants of the diagnosis / candidate / eval stages (plan §12, §17).

When a refinement job's ``artifact_type == "workflow_manifest"`` the standard
prompt-centric stages delegate here. These functions reuse the exact same
state-machine contract (eligible status/stage, single commit, audit row, stage
advance, terminate-at-``candidate_ready``) so the worker loop keeps working
unchanged — only the *content* of each stage is workflow-aware:

* **diagnosis** → :func:`caliber.workflows.refinement.localize_failure` over the
  verification item's workflow/node evidence.
* **candidate** → :func:`~caliber.workflows.refinement.generate_workflow_patch`,
  persisting the candidate manifest + semantic ops + graph diff (and a
  ``CaliberWorkflowPatch`` row for the patches UI).
* **eval** → :func:`~caliber.workflows.refinement.evaluate_candidate`, compiling
  baseline vs candidate and gating on the workflow's deploy-gate thresholds,
  then terminating at ``candidate_ready/done`` on pass.

Promotion (publish a new workflow version + rotate the alias) is handled by the
Apply endpoint via :func:`caliber.apply.apply_candidate`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.audit import record as audit_record
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberRefinementJob,
    CaliberVerificationItem,
    CaliberWorkflowDeployment,
    CaliberWorkflowPatch,
    CaliberWorkflowVersion,
)
from caliber.ids import new_workflow_patch_id
from caliber.workflows.calibration import (
    WorkflowCalibrationError,
    WorkflowCalibrationSpec,
    evaluate_workflow_calibration_candidates,
    generate_workflow_calibration_candidates,
    resolve_workflow_calibration_examples,
)
from caliber.workflows.judge import build_llm_judge_scorer
from caliber.workflows.manifest import WorkflowManifest, parse_manifest
from caliber.workflows.promoter import (
    build_executor,
    build_knowledge_runtime_runners,
    build_workflow_identity,
    resolver_from_session,
)
from caliber.workflows.refinement import (
    WorkflowDiagnosis,
    evaluate_candidate,
    generate_workflow_patch,
    localize_failure,
)
from caliber.workflows.runtime import WorkflowExecutor

logger = logging.getLogger("caliber.orchestrator.workflow_stages")


class WorkflowStageError(Exception):
    """Raised when a workflow stage can't run (missing workflow/version)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_baseline_version(
    session: Session,
    job: CaliberRefinementJob,
    item: CaliberVerificationItem | None,
) -> CaliberWorkflowVersion:
    """Pick the workflow version a patch is computed against (plan §17.4).

    Precedence: the version named in the item's ``submitted_context`` →
    a deployment alias's current target (prefer ``prod``) → the newest
    published version → the newest version of any status.
    """
    workflow_id = job.workflow_id
    if not workflow_id:
        raise WorkflowStageError(f"job {job.job_id!r} has no workflow_id")

    ctx = (item.submitted_context if item else None) or {}
    pinned = ctx.get("workflow_version_id")
    if isinstance(pinned, str):
        version = session.get(CaliberWorkflowVersion, pinned)
        if version is not None:
            return version

    deployments = (
        session.execute(
            select(CaliberWorkflowDeployment).where(
                CaliberWorkflowDeployment.workflow_id == workflow_id
            )
        )
        .scalars()
        .all()
    )
    by_alias = {d.alias: d for d in deployments}
    for alias in ("prod", "staging", "dev"):
        dep = by_alias.get(alias)
        if dep is not None:
            version = session.get(CaliberWorkflowVersion, dep.version_id)
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
    for v in versions:
        if v.status == "published":
            return v
    if versions:
        return versions[0]
    raise WorkflowStageError(f"workflow {workflow_id!r} has no versions")


def _target_alias(session: Session, workflow_id: str) -> str:
    """Alias the refined version should land on. Prefer the deployed target;
    default to ``dev`` (ungated) so the auto-loop can rotate without a gate."""
    deployments = (
        session.execute(
            select(CaliberWorkflowDeployment).where(
                CaliberWorkflowDeployment.workflow_id == workflow_id
            )
        )
        .scalars()
        .all()
    )
    aliases = {d.alias for d in deployments}
    for alias in ("staging", "dev"):
        if alias in aliases:
            return alias
    # If only prod is deployed, still target dev for the auto-loop (prod needs
    # its own gated promotion). If nothing is deployed, dev is the safe default.
    return "dev"


def _evidence_from_item(
    job: CaliberRefinementJob,
    item: CaliberVerificationItem | None,
) -> dict[str, Any]:
    ctx = (item.submitted_context if item else None) or {}
    evidence: dict[str, Any] = {
        "category": item.category if item else "",
        "free_text": item.free_text if item else "",
        "workflow_id": job.workflow_id,
    }
    for key in (
        "node_id",
        "edge_id",
        "workflow_version_id",
        "required_tools",
        "observed_tool_calls",
        "wrong_handoff",
        "guardrail_false_positive",
        "hallucination",
        "suggested_handoff",
    ):
        if key in ctx:
            evidence[key] = ctx[key]
    return evidence


def _dataset_inputs(
    session: Session,
    manifest: WorkflowManifest,
    item: CaliberVerificationItem | None,
) -> list[str]:
    """Resolve replay inputs from a deploy-gate dataset, else the flagged text."""
    for gate in manifest.deploy_gates.values():
        artifact = manifest.artifacts.eval_datasets.get(gate.dataset_ref)
        name = artifact.dataset_name if artifact else gate.dataset_ref
        dataset = (
            session.execute(select(CaliberEvalDataset).where(CaliberEvalDataset.name == name))
            .scalars()
            .first()
        )
        if dataset is None:
            continue
        examples = (
            session.execute(
                select(CaliberEvalDatasetExample).where(
                    CaliberEvalDatasetExample.dataset_id == dataset.dataset_id,
                    CaliberEvalDatasetExample.superseded_at.is_(None),
                )
            )
            .scalars()
            .all()
        )
        inputs = [_example_text(e.input) for e in examples]
        if inputs:
            return inputs
    flagged = (item.free_text if item else "") or "Replay the flagged scenario."
    return [flagged]


def _example_text(data: dict[str, Any] | None) -> str:
    data = data or {}
    for key in ("input", "user_message", "message", "query", "text"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    return json.dumps(data)


def _workflow_manifest_executor_factory(
    config: CaliberConfig | None,
) -> Callable[[dict[str, Any]], WorkflowExecutor]:
    """Build manifest-aware runtime executors lazily during workflow replay."""

    def _factory(manifest_dict: dict[str, Any]) -> WorkflowExecutor:
        try:
            manifest = parse_manifest(manifest_dict)
            return build_executor(config, manifest=manifest)
        except (ValueError, RuntimeError) as exc:
            raise ValueError(f"failed to resolve workflow executor: {exc}") from exc

    return _factory


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def run_workflow_diagnosis(
    session: Session, job: CaliberRefinementJob, *, actor: str = "system"
) -> CaliberRefinementJob:
    """Localize a workflow failure and advance to ``candidate``."""
    item = session.get(CaliberVerificationItem, job.primary_item_id)
    diagnosis = localize_failure(_evidence_from_item(job, item))

    job.current_stage = "candidate"
    job.diagnosis = {**diagnosis.to_dict(), "workflow": True}
    audit_record(
        session,
        actor=actor,
        action="advance_stage",
        entity_type="refinement_job",
        entity_id=job.job_id,
        details={"from_stage": "diagnosis", "to_stage": "candidate", "diagnosis": job.diagnosis},
    )
    session.commit()
    logger.info(
        "workflow diagnosis complete: job=%s components=%s",
        job.job_id,
        diagnosis.affected_components,
    )
    return job


def run_workflow_candidate(
    session: Session,
    job: CaliberRefinementJob,
    *,
    actor: str = "system",
    config: CaliberConfig | None = None,
) -> CaliberRefinementJob:
    """Generate a workflow patch candidate and advance to ``eval``."""
    if not job.diagnosis:
        raise WorkflowStageError(f"job {job.job_id!r} has no diagnosis recorded")
    if job.calibration_spec:
        return _run_workflow_calibration_candidate(session, job, actor=actor, config=config)
    item = session.get(CaliberVerificationItem, job.primary_item_id)
    baseline_version = _resolve_baseline_version(session, job, item)
    resolver = resolver_from_session(session)
    base_manifest = parse_manifest(baseline_version.manifest)

    diag = WorkflowDiagnosis(
        root_cause=str(job.diagnosis.get("root_cause", "")),
        affected_components=list(job.diagnosis.get("affected_components", [])),
        localized_to=dict(job.diagnosis.get("localized_to", {})),
        recommended_patch_type=str(
            job.diagnosis.get("recommended_patch_type", "workflow_manifest")
        ),
        confidence=float(job.diagnosis.get("confidence", 0.7)),
    )
    candidate = generate_workflow_patch(base_manifest, diag, resolver=resolver)
    target_alias = _target_alias(session, job.workflow_id or "")

    job.current_stage = "eval"
    job.candidate = {
        "artifact_type": "workflow_manifest",
        # ``content`` kept as a string so generic readers (and the approval
        # snapshot) always have one; the structured fields drive promotion.
        "content": json.dumps(candidate.candidate_manifest, sort_keys=True),
        "patch_kind": candidate.patch_kind,
        "summary": candidate.summary,
        "prompt_suggestion": candidate.prompt_suggestion,
        "semantic_ops": candidate.semantic_ops,
        "graph_diff": candidate.graph_diff,
        "candidate_manifest": candidate.candidate_manifest,
        "baseline_manifest": base_manifest.to_dict(),
        "base_version_id": baseline_version.version_id,
        "workflow_id": job.workflow_id,
        "target_alias": target_alias,
    }

    patch = CaliberWorkflowPatch(
        patch_id=new_workflow_patch_id(),
        job_id=job.job_id,
        workflow_id=job.workflow_id or "",
        base_version_id=baseline_version.version_id,
        candidate_manifest=candidate.candidate_manifest,
        semantic_ops=candidate.semantic_ops,
        patch_summary=candidate.summary,
        graph_diff=candidate.graph_diff,
        risk_summary=f"patch_kind={candidate.patch_kind}; components={','.join(diag.affected_components)}",
    )
    session.add(patch)

    audit_record(
        session,
        actor=actor,
        action="advance_stage",
        entity_type="refinement_job",
        entity_id=job.job_id,
        details={
            "from_stage": "candidate",
            "to_stage": "eval",
            "patch_kind": candidate.patch_kind,
            "patch_id": patch.patch_id,
            "summary": candidate.summary,
        },
    )
    session.commit()
    logger.info(
        "workflow candidate complete: job=%s patch=%s kind=%s",
        job.job_id,
        patch.patch_id,
        candidate.patch_kind,
    )
    return job


def _run_workflow_calibration_candidate(
    session: Session,
    job: CaliberRefinementJob,
    *,
    actor: str = "system",
    config: CaliberConfig | None = None,
) -> CaliberRefinementJob:
    """Run bounded workflow calibration search and select one winner."""
    item = session.get(CaliberVerificationItem, job.primary_item_id)
    baseline_version = _resolve_baseline_version(session, job, item)
    resolver = resolver_from_session(session)
    base_manifest = parse_manifest(baseline_version.manifest)
    target_alias = _target_alias(session, job.workflow_id or "")
    diagnosis = job.diagnosis or {}
    diag = WorkflowDiagnosis(
        root_cause=str(diagnosis.get("root_cause", "")),
        affected_components=list(diagnosis.get("affected_components", [])),
        localized_to=dict(diagnosis.get("localized_to", {})),
        recommended_patch_type=str(diagnosis.get("recommended_patch_type", "workflow_manifest")),
        confidence=float(diagnosis.get("confidence", 0.7)),
    )
    calibration_spec = WorkflowCalibrationSpec.from_raw(job.calibration_spec)

    try:
        examples = resolve_workflow_calibration_examples(
            session, base_manifest, item, calibration_spec
        )
        candidates = generate_workflow_calibration_candidates(
            base_manifest,
            diag,
            examples,
            calibration_spec,
            resolver=resolver,
        )
        # Real executor + LLM-judge scorer when configured; ``build_executor(None)``
        # → FakeWorkflowExecutor and ``build_llm_judge_scorer(None)`` → None
        # (structural scoring), so behavior is unchanged until opted in
        # (golden-path roadmap, Wave 5). Bounded by the calibration budget.
        executor_factory = _workflow_manifest_executor_factory(config)
        scorer = build_llm_judge_scorer(config) if calibration_spec.judge_enabled else None
        workflow_identity = build_workflow_identity(
            session,
            baseline_version.workflow_id,
            fallback_user=baseline_version.created_by or "",
        )
        knowledge_query_runner, knowledge_build_runner = build_knowledge_runtime_runners(
            session,
            identity=workflow_identity,
            config=config,
            actor=actor,
        )
        result = evaluate_workflow_calibration_candidates(
            base_manifest.to_dict(),
            candidates,
            examples,
            calibration_spec,
            resolver=resolver,
            baseline_executor_factory=executor_factory,
            candidate_executor_factory=executor_factory,
            scorer=scorer,
            knowledge_query_runner=knowledge_query_runner,
            knowledge_build_runner=knowledge_build_runner,
        )
    except WorkflowCalibrationError as exc:
        job.status = "rejected"
        job.current_stage = "done"
        job.error_message = f"workflow calibration failed: {exc}"
        job.eval_results = {
            "workflow": True,
            "calibration": True,
            "gate": {"passed": False, "reasons": [str(exc)]},
        }
        audit_record(
            session,
            actor=actor,
            action="reject_by_gate",
            entity_type="refinement_job",
            entity_id=job.job_id,
            details={"from_stage": "candidate", "to_stage": "done", "reason": str(exc)},
        )
        session.commit()
        return job

    result_payload = result.to_dict(include_manifests=False)
    if not result.passed or result.winner is None or result.winner.candidate_manifest is None:
        best_manifest = (
            result.winner.candidate_manifest if result.winner else base_manifest.to_dict()
        )
        job.status = "rejected"
        job.current_stage = "done"
        reasons = result.gate.get("reasons") or ["no candidate met calibration gates"]
        job.error_message = f"workflow calibration rejected: {'; '.join(str(r) for r in reasons)}"
        job.candidate = {
            "artifact_type": "workflow_manifest",
            "content": json.dumps(best_manifest, sort_keys=True),
            "patch_kind": result.winner.patch_kind if result.winner else "workflow_manifest",
            "summary": result.winner.summary
            if result.winner
            else "No valid calibration candidate.",
            "semantic_ops": result.winner.semantic_ops if result.winner else [],
            "graph_diff": result.winner.graph_diff if result.winner else {},
            "candidate_manifest": best_manifest,
            "baseline_manifest": base_manifest.to_dict(),
            "base_version_id": baseline_version.version_id,
            "workflow_id": job.workflow_id,
            "target_alias": target_alias,
            "calibration": True,
            "calibration_spec": job.calibration_spec,
            "calibration_candidates": result_payload["candidates"],
            "calibration_winner_id": result.winner.candidate_id if result.winner else None,
            "calibration_low_confidence": result.low_confidence,
            "calibration_gate": result.gate,
        }
        job.eval_results = _calibration_eval_payload(result, candidate=job.candidate)
        audit_record(
            session,
            actor=actor,
            action="reject_by_gate",
            entity_type="refinement_job",
            entity_id=job.job_id,
            details={"from_stage": "candidate", "to_stage": "done", "gate": result.gate},
        )
        session.commit()
        logger.info("workflow calibration rejected: job=%s", job.job_id)
        return job

    winner = result.winner
    patch = CaliberWorkflowPatch(
        patch_id=new_workflow_patch_id(),
        job_id=job.job_id,
        workflow_id=job.workflow_id or "",
        base_version_id=baseline_version.version_id,
        candidate_manifest=winner.candidate_manifest,
        semantic_ops=winner.semantic_ops,
        patch_summary=winner.summary,
        graph_diff=winner.graph_diff,
        risk_summary=(
            f"calibration=true; low_confidence={result.low_confidence}; "
            f"objective={result.objective}; candidate_id={winner.candidate_id}"
        ),
    )
    session.add(patch)

    job.current_stage = "eval"
    job.candidate = {
        "artifact_type": "workflow_manifest",
        "content": json.dumps(winner.candidate_manifest, sort_keys=True),
        "patch_kind": winner.patch_kind,
        "summary": winner.summary,
        "prompt_suggestion": winner.prompt_suggestion,
        "semantic_ops": winner.semantic_ops,
        "graph_diff": winner.graph_diff,
        "candidate_manifest": winner.candidate_manifest,
        "baseline_manifest": base_manifest.to_dict(),
        "base_version_id": baseline_version.version_id,
        "workflow_id": job.workflow_id,
        "target_alias": target_alias,
        "calibration": True,
        "calibration_spec": job.calibration_spec,
        "calibration_candidates": result_payload["candidates"],
        "calibration_winner_id": winner.candidate_id,
        "calibration_low_confidence": result.low_confidence,
        "calibration_patch_id": patch.patch_id,
        "calibration_baseline_scores": result.baseline_scores,
        "calibration_gate": result.gate,
        "calibration_n_examples": result.n_examples,
    }

    audit_record(
        session,
        actor=actor,
        action="advance_stage",
        entity_type="refinement_job",
        entity_id=job.job_id,
        details={
            "from_stage": "candidate",
            "to_stage": "eval",
            "patch_id": patch.patch_id,
            "calibration": True,
            "winner_id": winner.candidate_id,
        },
    )
    session.commit()
    logger.info(
        "workflow calibration candidate complete: job=%s patch=%s winner=%s",
        job.job_id,
        patch.patch_id,
        winner.candidate_id,
    )
    return job


def run_workflow_eval(
    session: Session,
    job: CaliberRefinementJob,
    *,
    actor: str = "system",
    config: CaliberConfig | None = None,
) -> CaliberRefinementJob:
    """Compile + replay baseline vs candidate, gate, and terminate at candidate_ready."""
    if not job.candidate:
        raise WorkflowStageError(f"job {job.job_id!r} has no candidate recorded")
    candidate = job.candidate
    candidate_manifest = candidate.get("candidate_manifest")
    baseline_manifest = candidate.get("baseline_manifest")
    if not isinstance(candidate_manifest, dict) or not isinstance(baseline_manifest, dict):
        raise WorkflowStageError(f"job {job.job_id!r} candidate is missing manifests")
    if candidate.get("calibration") is True:
        return _run_workflow_calibration_eval(session, job, actor=actor)

    item = session.get(CaliberVerificationItem, job.primary_item_id)
    resolver = resolver_from_session(session)
    manifest = parse_manifest(baseline_manifest)
    inputs = _dataset_inputs(session, manifest, item)

    # Gate thresholds from the workflow's deploy gates (if any), else permissive.
    thresholds: dict[str, float] = {}
    for gate in manifest.deploy_gates.values():
        thresholds.update({k: float(v) for k, v in gate.thresholds.items()})
    thresholds.setdefault("min_overall_delta", 0.0)

    # Real executor + LLM-judge scorer when configured; fake/structural by
    # default (Wave 5). ``build_llm_judge_scorer(None)`` → None → evaluate_candidate
    # falls back to the structural ``default_run_scorer``.
    executor_factory = _workflow_manifest_executor_factory(config)
    scorer = build_llm_judge_scorer(config)
    workflow_identity = build_workflow_identity(
        session,
        manifest.workflow_id,
        fallback_user=job.agent_id or actor,
    )
    knowledge_query_runner, knowledge_build_runner = build_knowledge_runtime_runners(
        session,
        identity=workflow_identity,
        config=config,
        actor=actor,
    )
    result = evaluate_candidate(
        baseline_manifest,
        candidate_manifest,
        inputs,
        resolver=resolver,
        thresholds=thresholds,
        baseline_executor_factory=executor_factory,
        candidate_executor_factory=executor_factory,
        scorer=scorer,
        knowledge_query_runner=knowledge_query_runner,
        knowledge_build_runner=knowledge_build_runner,
    )
    job.eval_results = {
        "candidate": {
            "overall": result.candidate_scores.get("quality", 0.0),
            "dimensions": result.candidate_scores,
        },
        "baseline": {
            "overall": result.baseline_scores.get("quality", 0.0),
            "dimensions": result.baseline_scores,
        },
        "deltas": result.deltas,
        "n_examples": result.n_examples,
        "gate": result.gate,
        "workflow": True,
    }

    if not result.passed:
        job.status = "rejected"
        job.current_stage = "done"
        reasons = result.gate.get("reasons") or [result.gate.get("reason", "gate failed")]
        job.error_message = f"workflow eval gate failed: {'; '.join(str(r) for r in reasons)}"
        audit_record(
            session,
            actor=actor,
            action="reject_by_gate",
            entity_type="refinement_job",
            entity_id=job.job_id,
            details={"from_stage": "eval", "to_stage": "done", "gate": result.gate},
        )
        session.commit()
        logger.info("workflow eval rejected: job=%s", job.job_id)
        return job

    # Passed the deploy gate. No approval is created — the candidate lands at
    # the terminal ``candidate_ready`` state for an operator to Apply later.
    job.status = "candidate_ready"
    job.current_stage = "done"
    audit_record(
        session,
        actor=actor,
        action="candidate_ready",
        entity_type="refinement_job",
        entity_id=job.job_id,
        details={
            "from_stage": "eval",
            "to_stage": "done",
            "workflow_id": job.workflow_id,
            "kind": "workflow_manifest",
        },
    )
    session.commit()
    logger.info("workflow eval passed: job=%s status=candidate_ready", job.job_id)
    return job


def _calibration_eval_payload(
    result: Any,
    *,
    candidate: dict[str, Any],
) -> dict[str, Any]:
    winner_id = candidate.get("calibration_winner_id")
    winner = None
    for entry in candidate.get("calibration_candidates", []):
        if isinstance(entry, dict) and entry.get("candidate_id") == winner_id:
            winner = entry
            break
    if winner is None and result.winner is not None:
        winner = result.winner.to_dict(include_manifest=False)
    winner_scores = winner.get("scores", {}) if isinstance(winner, dict) else {}
    return {
        "candidate": {
            "overall": winner_scores.get("quality", 0.0),
            "dimensions": winner_scores,
        },
        "baseline": {
            "overall": result.baseline_scores.get("quality", 0.0),
            "dimensions": result.baseline_scores,
        },
        "deltas": winner.get("deltas", {}) if isinstance(winner, dict) else {},
        "n_examples": result.n_examples,
        "gate": result.gate,
        "workflow": True,
        "calibration": True,
        "calibration_candidates": candidate.get("calibration_candidates", []),
        "calibration_winner_id": winner_id,
        "calibration_low_confidence": candidate.get("calibration_low_confidence", False),
    }


def _run_workflow_calibration_eval(
    session: Session, job: CaliberRefinementJob, *, actor: str = "system"
) -> CaliberRefinementJob:
    """Reuse selected calibration winner evidence and terminate at candidate_ready."""
    candidate = job.candidate or {}
    candidates = candidate.get("calibration_candidates")
    if not isinstance(candidates, list):
        raise WorkflowStageError(
            f"job {job.job_id!r} calibration candidate is missing score summaries"
        )
    winner_id = candidate.get("calibration_winner_id")
    winner = next(
        (
            entry
            for entry in candidates
            if isinstance(entry, dict) and entry.get("candidate_id") == winner_id
        ),
        None,
    )
    if not isinstance(winner, dict):
        raise WorkflowStageError(f"job {job.job_id!r} calibration winner is missing")
    gate = dict(candidate.get("calibration_gate") or winner.get("gate") or {})
    passed = bool(gate.get("passed", True)) and bool(winner.get("accepted", False))
    winner_scores = dict(winner.get("scores") or {})
    baseline_scores = dict(candidate.get("calibration_baseline_scores") or {})
    job.eval_results = {
        "candidate": {"overall": winner_scores.get("quality", 0.0), "dimensions": winner_scores},
        "baseline": {"overall": baseline_scores.get("quality", 0.0), "dimensions": baseline_scores},
        "deltas": dict(winner.get("deltas") or {}),
        "n_examples": int(candidate.get("calibration_n_examples") or 0),
        "gate": gate,
        "workflow": True,
        "calibration": True,
        "calibration_candidates": candidates,
        "calibration_winner_id": winner_id,
        "calibration_low_confidence": bool(candidate.get("calibration_low_confidence", False)),
        "calibration_patch_id": candidate.get("calibration_patch_id"),
    }

    if not passed:
        job.status = "rejected"
        job.current_stage = "done"
        reasons = gate.get("reasons") or [winner.get("rejected_reason", "gate failed")]
        job.error_message = (
            f"workflow calibration eval gate failed: {'; '.join(str(r) for r in reasons)}"
        )
        audit_record(
            session,
            actor=actor,
            action="reject_by_gate",
            entity_type="refinement_job",
            entity_id=job.job_id,
            details={"from_stage": "eval", "to_stage": "done", "gate": gate, "calibration": True},
        )
        session.commit()
        logger.info("workflow calibration eval rejected: job=%s", job.job_id)
        return job

    # Passed calibration gate — terminate at ``candidate_ready/done``. The
    # operator promotes the selected winner later via the Apply endpoint.
    job.status = "candidate_ready"
    job.current_stage = "done"
    audit_record(
        session,
        actor=actor,
        action="candidate_ready",
        entity_type="refinement_job",
        entity_id=job.job_id,
        details={
            "from_stage": "eval",
            "to_stage": "done",
            "workflow_id": job.workflow_id,
            "kind": "workflow_manifest",
            "calibration": True,
        },
    )
    session.commit()
    logger.info("workflow calibration eval passed: job=%s status=candidate_ready", job.job_id)
    return job


__all__ = [
    "WorkflowStageError",
    "run_workflow_candidate",
    "run_workflow_diagnosis",
    "run_workflow_eval",
]
