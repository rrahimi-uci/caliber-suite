"""Eval stage of the refinement pipeline.

The sixth stage. Reads the candidate from the prior stage, runs it through
the injected :class:`EvalProvider` against the agent's eval thresholds,
applies the regression gate, and either:

* On pass → marks the job terminal at ``candidate_ready/done``. No approval
  is created; an operator promotes the candidate later via the Apply
  endpoint. A regression-replay provenance row is still recorded (with
  ``approval_id=None``).
* On fail → marks the job ``rejected`` with the gate reasons recorded in
  ``error_message``. Job parks at stage ``done``.

The module is named ``eval_stage`` rather than ``eval`` to avoid shadowing
Python's builtin :func:`eval` — a real concern since ``from caliber.orchestrator
import eval`` would otherwise overwrite a callable a user might import in the
same scope.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from caliber.artifact_store import ArtifactStore
from caliber.audit import record as audit_record
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberRefinementJob,
    CaliberVerificationItem,
)
from caliber.eval.gate import apply_gate
from caliber.eval.provider import EvalComparison, EvalProvider, EvalProviderError, EvalRequest
from caliber.regression import record_regression_run

logger = logging.getLogger("caliber.orchestrator.eval_stage")

_ELIGIBLE_STATUSES = frozenset({"running"})
_ELIGIBLE_STAGES = frozenset({"eval"})

_DEFAULT_EVAL_DATASET = "default"


class EvalStateError(Exception):
    """Raised when :func:`run_eval` is called on an ineligible job state."""


def run_eval(  # noqa: PLR0915 — sequential stage: pass→candidate_ready, fail→retry-loop, fail→reject
    session: Session,
    job_id: str,
    eval_provider: EvalProvider,
    *,
    artifact_store: ArtifactStore | None = None,
    actor: str = "system",
    config: CaliberConfig | None = None,
) -> CaliberRefinementJob:
    """Advance the named job through the eval stage.

    Parameters
    ----------
    session:
        Open SQLAlchemy session. Committed on success or on gate rejection
        (both are terminal-ish states for the worker's tick). Not committed
        if the eval provider raises — the job stays at ``running/eval`` so
        a retry sees the same input.
    job_id:
        ID of the refinement job.
    eval_provider:
        :class:`EvalProvider` implementation. Tests pass
        :class:`caliber.eval.fake.FakeEvalProvider`.
    artifact_store:
        Optional active-artifact reader. When supplied, eval compares the
        candidate against the currently deployed artifact; otherwise it falls
        back to the baseline content captured by the candidate stage.
    actor:
        Who is triggering this. Workers pass ``"system"``.

    Returns
    -------
    The updated :class:`CaliberRefinementJob` row.

    Raises
    ------
    LookupError
        Job, agent, or candidate is missing.
    EvalStateError
        Job is not in a state where eval can run.
    EvalProviderError
        The eval provider failed.
    """
    job = session.get(CaliberRefinementJob, job_id)
    if job is None:
        raise LookupError(f"refinement job {job_id!r} not found")

    if job.status not in _ELIGIBLE_STATUSES or job.current_stage not in _ELIGIBLE_STAGES:
        raise EvalStateError(
            f"job {job_id!r} not eligible for eval: "
            f"status={job.status!r}, stage={job.current_stage!r}"
        )

    if job.artifact_type == "workflow_manifest":
        # Workflow jobs carry graph manifests and calibration evidence; the
        # workflow-aware eval path compiles/replays manifests or reuses the
        # selected calibration winner instead of treating content as a prompt.
        from caliber.orchestrator.workflow_stages import run_workflow_eval  # noqa: PLC0415

        return run_workflow_eval(session, job, actor=actor, config=config)

    if not job.candidate:
        raise LookupError(
            f"job {job_id!r} has no candidate recorded; the candidate stage must run before eval"
        )

    agent = session.get(CaliberAgentConfig, job.agent_id)
    if agent is None:
        raise LookupError(f"agent {job.agent_id!r} not found for job {job_id!r}")

    raw_content = job.candidate.get("content")
    if not isinstance(raw_content, str) or not raw_content:
        raise LookupError(f"job {job_id!r} candidate has empty or invalid content")
    candidate_content = raw_content

    prompt_optimization = _resolve_prompt_optimization_context(session, job)
    baseline_content = _resolve_baseline_content(
        job,
        artifact_store,
        prompt_optimization=prompt_optimization,
    )
    scorer_names, scorer_configs, scorer_weights = _resolve_scorer_overrides(prompt_optimization)

    eval_dataset_id = _resolve_eval_dataset(agent, prompt_optimization)
    eval_dataset_version = _resolve_eval_dataset_version(prompt_optimization)
    request = EvalRequest(
        agent_id=job.agent_id,
        job_id=job.job_id,
        artifact_type=job.artifact_type,
        candidate_content=candidate_content,
        baseline_content=baseline_content,
        eval_dataset_id=eval_dataset_id,
        eval_dataset_version=eval_dataset_version,
        scorer_names=scorer_names,
        scorer_configs=scorer_configs,
        scorer_weights=scorer_weights,
    )

    try:
        comparison = eval_provider.evaluate(request)
    except EvalProviderError:
        session.rollback()
        raise

    decision = apply_gate(comparison, _resolve_eval_thresholds(agent, prompt_optimization))
    job.eval_results = _comparison_to_json(comparison, decision)

    previous_stage = job.current_stage
    if decision.passed:
        # No human-feedback approval is created. The candidate that cleared
        # the gate lands at the terminal ``candidate_ready`` state; an
        # operator promotes it later via the Apply endpoint. The eval-replay
        # provenance row is still recorded (with ``approval_id=None``).
        regression_run = record_regression_run(
            session,
            job=job,
            approval=None,
            comparison=comparison,
            gate=decision,
        )
        job.status = "candidate_ready"
        job.current_stage = "done"
        audit_record(
            session,
            actor=actor,
            action="candidate_ready",
            entity_type="refinement_job",
            entity_id=job.job_id,
            details={
                "from_stage": previous_stage,
                "to_stage": "done",
                "gate": decision.to_json(),
            },
        )
        audit_record(
            session,
            actor=actor,
            action="record_regression_replay",
            entity_type="regression_run",
            entity_id=regression_run.run_id,
            details={
                "job_id": job.job_id,
                "approval_id": None,
                "status": regression_run.status,
                "dataset_ids": regression_run.dataset_ids,
                "trace_sample_ids": regression_run.trace_sample_ids,
            },
        )
        session.commit()
        logger.info(
            "eval passed: job=%s status=candidate_ready overall=%.3f",
            job_id,
            comparison.candidate.overall,
        )
        return job

    # Gate failed. Record the failed replay, then either loop back to the
    # candidate stage (automatic self-correction, when iterations remain) or
    # reject. The failed regression run is recorded either way.
    rejection_reason = "; ".join(decision.reasons) or "unknown reason"
    regression_run = record_regression_run(
        session,
        job=job,
        approval=None,
        comparison=comparison,
        gate=decision,
    )
    audit_record(
        session,
        actor=actor,
        action="record_regression_replay",
        entity_type="regression_run",
        entity_id=regression_run.run_id,
        details={
            "job_id": job.job_id,
            "approval_id": None,
            "status": regression_run.status,
            "dataset_ids": regression_run.dataset_ids,
            "trace_sample_ids": regression_run.trace_sample_ids,
        },
    )

    max_iterations = config.refinement_max_iterations if config is not None else 0
    if job.refine_iteration < max_iterations:
        # Self-correction loop: feed the gate reasons back as review notes (the
        # candidate stage consumes ``job.review_notes``) and re-run candidate.
        job.refine_iteration += 1
        job.review_notes = _retry_feedback(decision, comparison)
        job.status = "running"
        job.current_stage = "candidate"
        audit_record(
            session,
            actor=actor,
            action="refine_retry",
            entity_type="refinement_job",
            entity_id=job.job_id,
            details={
                "from_stage": previous_stage,
                "to_stage": "candidate",
                "iteration": job.refine_iteration,
                "max_iterations": max_iterations,
                "gate": decision.to_json(),
            },
        )
        session.commit()
        logger.info(
            "eval gate failed; retrying candidate: job=%s iteration=%d/%d overall=%.3f",
            job_id,
            job.refine_iteration,
            max_iterations,
            comparison.candidate.overall,
        )
        return job

    # Iterations exhausted (or disabled, the default) — terminal rejection.
    job.status = "rejected"
    job.current_stage = "done"
    job.error_message = f"regression gate failed: {rejection_reason}"
    audit_record(
        session,
        actor=actor,
        action="reject_by_gate",
        entity_type="refinement_job",
        entity_id=job.job_id,
        details={
            "from_stage": previous_stage,
            "to_stage": "done",
            "gate": decision.to_json(),
        },
    )
    session.commit()
    logger.info(
        "eval rejected: job=%s reasons=%s overall=%.3f",
        job_id,
        decision.reasons,
        comparison.candidate.overall,
    )
    return job


def _retry_feedback(decision: Any, comparison: EvalComparison) -> str:
    """Build the review-note guidance fed back to the candidate stage on retry.

    Surfaces the gate reasons + the lowest-scoring dimensions so the next
    candidate addresses the specific failures rather than rewriting blindly.
    """
    reasons = "; ".join(decision.reasons) or "scored below the gate threshold"
    dims = comparison.candidate.dimensions
    low = sorted(dims, key=lambda d: dims[d])[:3]
    dims_txt = ", ".join(f"{d}={dims[d]:.2f}" for d in low) or "n/a"
    return (
        f"The previous candidate failed the regression gate ({reasons}; "
        f"overall={comparison.candidate.overall:.2f}). Lowest-scoring dimensions: "
        f"{dims_txt}. Revise to address these specifically without regressing the others."
    )


def _resolve_prompt_optimization_context(
    session: Session, job: CaliberRefinementJob
) -> dict[str, Any]:
    """Return manual prompt-optimization context recorded on the source item."""
    item = session.get(CaliberVerificationItem, job.primary_item_id)
    if item is None or not isinstance(item.submitted_context, dict):
        return {}

    submitted = item.submitted_context
    raw = submitted.get("prompt_optimization")
    if submitted.get("source") != "prompt_optimization" or not isinstance(raw, dict):
        return {}
    return raw


def _resolve_scorer_overrides(
    prompt_optimization: dict[str, Any],
) -> tuple[list[str], dict[str, dict[str, object]], dict[str, float]]:
    """Extract scorer names, configs, and weights from prompt optimization context."""
    scorer_names: list[str] = []
    scorer_configs: dict[str, dict[str, object]] = {}
    scorer_weights: dict[str, float] = {}

    raw_scorers = prompt_optimization.get("scorers")
    if not isinstance(raw_scorers, list):
        return scorer_names, scorer_configs, scorer_weights

    for scorer in raw_scorers:
        if not isinstance(scorer, dict):
            continue
        raw_name = scorer.get("name")
        if not isinstance(raw_name, str) or not raw_name:
            continue
        scorer_names.append(raw_name)

        raw_config = scorer.get("config")
        if isinstance(raw_config, dict):
            scorer_configs[raw_name] = dict(raw_config)

        raw_weight = scorer.get("weight")
        if isinstance(raw_weight, (int, float)) and not isinstance(raw_weight, bool):
            scorer_weights[raw_name] = float(raw_weight)

    return scorer_names, scorer_configs, scorer_weights


def _resolve_eval_thresholds(
    agent: CaliberAgentConfig,
    prompt_optimization: dict[str, Any],
) -> dict[str, Any]:
    """Merge agent thresholds with per-run prompt-optimization gate overrides."""
    thresholds = dict(agent.eval_thresholds or {})
    raw_gate = prompt_optimization.get("gate")
    if not isinstance(raw_gate, dict):
        return thresholds

    for key in ("min_aggregate_score", "max_regression_delta"):
        raw_value = raw_gate.get(key)
        if isinstance(raw_value, (int, float)) and not isinstance(raw_value, bool):
            thresholds[key] = float(raw_value)
    return thresholds


def _resolve_eval_dataset(
    agent: CaliberAgentConfig,
    prompt_optimization: dict[str, Any] | None = None,
) -> str:
    """Return the eval-dataset identifier for this agent.

    Precedence:

    1. Manual prompt-optimization ``eval_dataset_id`` if the job came from
       the optimization UI.
    2. ``agent.eval_thresholds["eval_dataset_id"]`` if present — operator override.
    3. Else the module-level default ``"default"``.

    The real cross-agent dataset resolution (per-experiment defaults, etc.)
    lives in a later milestone alongside the production eval runner.
    """
    if prompt_optimization:
        raw_override = prompt_optimization.get("eval_dataset_id")
        if isinstance(raw_override, str) and raw_override:
            return raw_override

    raw = agent.eval_thresholds.get("eval_dataset_id") if agent.eval_thresholds else None
    if isinstance(raw, str) and raw:
        return raw
    return _DEFAULT_EVAL_DATASET


def _resolve_eval_dataset_version(
    prompt_optimization: dict[str, Any] | None = None,
) -> int | None:
    """Return the pinned eval-dataset version for a prompt-optimization run.

    Reproducibility guarantee: a manual prompt-optimization run records the
    eval-dataset version it was launched against (see the prompt run route).
    When present, the eval loads the example set *as of* that version so a
    later dataset edit can't silently change what the run scored against.

    Returns ``None`` for jobs without a recorded pin (legacy runs, the
    automatic feedback-harvest pipeline) — those keep using the dataset's
    current active set. Workflow calibration never reaches this path (it
    short-circuits to ``run_workflow_eval`` and pins via deploy-gate refs).
    """
    if not prompt_optimization:
        return None
    raw = prompt_optimization.get("eval_dataset_version")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int) and raw >= 1:
        return raw
    return None


def _resolve_baseline_content(
    job: CaliberRefinementJob,
    artifact_store: ArtifactStore | None,
    *,
    prompt_optimization: dict[str, Any] | None = None,
) -> str | None:
    """Return the current production artifact content for replay comparison.

    For skill-targeted jobs, reads the active skill content instead of the
    agent's prompt.
    """
    if prompt_optimization:
        raw_baseline = prompt_optimization.get("baseline_content")
        if isinstance(raw_baseline, str) and raw_baseline:
            return raw_baseline

    if artifact_store is not None:
        if job.artifact_type == "skill" and job.skill_name:
            current = artifact_store.get_active_skill(job.skill_name)
        else:
            current = artifact_store.get_active_prompt(job.agent_id)
        if current is not None:
            return current
    if isinstance(job.candidate, dict):
        raw = job.candidate.get("baseline_content")
        if isinstance(raw, str):
            return raw
    return None


def _comparison_to_json(comparison: EvalComparison, decision: Any) -> dict[str, Any]:
    """Render the comparison + gate decision into the JSON-column shape.

    Mirrors the analogous helpers in :mod:`caliber.orchestrator.diagnosis`
    and :mod:`caliber.orchestrator.candidate` — explicit fields are the
    contract callers (and the UI) depend on.

    The ``caliber_tags`` block carries the CALIBER-namespaced aggregates
    listed in the implementation-parity checklist §4. They live on the JSON column today; when MLflow run
    integration lands they'll also be logged as tags on the parent run.
    Computing them here (vs. relying on whatever the eval provider
    happened to emit) keeps CALIBER as the source of truth for the gate.
    """
    baseline_dump: dict[str, Any] | None
    if comparison.baseline is not None:
        baseline_dump = {
            "overall": comparison.baseline.overall,
            "dimensions": dict(comparison.baseline.dimensions),
        }
    else:
        baseline_dump = None
    return {
        "candidate": {
            "overall": comparison.candidate.overall,
            "dimensions": dict(comparison.candidate.dimensions),
        },
        "baseline": baseline_dump,
        "deltas": dict(comparison.deltas),
        "eval_dataset_id": comparison.eval_dataset_id,
        "n_examples": comparison.n_examples,
        "gate": decision.to_json(),
        "caliber_tags": _caliber_tags(comparison, decision),
    }


def _caliber_tags(comparison: EvalComparison, decision: Any) -> dict[str, Any]:
    """Compute the CALIBER-namespaced eval tags.

    Keys match the parity checklist exactly so an audit query can find
    them by name. ``caliber.regression_detected`` is True iff any per-
    dimension delta is below the negative threshold the gate enforced;
    ``caliber.max_regression_delta`` is the largest *positive* magnitude
    of a regression (or 0.0 when no dimension regressed).
    """
    deltas_no_overall = {k: v for k, v in comparison.deltas.items() if k != "overall"}
    regressions = [-v for v in deltas_no_overall.values() if v < 0.0]
    max_regression = max(regressions) if regressions else 0.0
    return {
        "caliber.aggregate_score": comparison.candidate.overall,
        "caliber.test_case_count": comparison.n_examples,
        "caliber.max_regression_delta": round(max_regression, 4),
        "caliber.regression_detected": bool(regressions),
        "caliber.gate_passed": bool(decision.passed),
    }
