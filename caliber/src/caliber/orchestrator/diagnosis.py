"""Diagnosis stage of the refinement pipeline.

The first stage with real LLM work. Given a job that the evidence stage has
parked at ``running/diagnosis``, this stage:

1. Builds an :class:`EvidenceContext` from the verification item + the
   evidence summary recorded earlier on the audit log.
2. Calls the injected :class:`LLMProvider` to produce a structured
   :class:`Diagnosis` plus :class:`LLMUsage` telemetry.
3. Writes the diagnosis to ``caliber_refinement_jobs.diagnosis`` and bumps
   ``total_tokens`` / ``cost_usd``.
4. Advances the job to ``running/candidate`` and writes the audit row.

If the LLM call fails (``LLMProviderError``), the stage rolls back partial
state and re-raises so the worker marks the job ``failed`` with the
provider's error message.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from caliber.audit import record as audit_record
from caliber.db.models import CaliberRefinementJob, CaliberVerificationItem
from caliber.llm.provider import Diagnosis, EvidenceContext, LLMProvider, LLMProviderError

logger = logging.getLogger("caliber.orchestrator.diagnosis")

_ELIGIBLE_STATUSES = frozenset({"running"})
_ELIGIBLE_STAGES = frozenset({"diagnosis"})


class DiagnosisStateError(Exception):
    """Raised when :func:`run_diagnosis` is called on an ineligible job state."""


def run_diagnosis(
    session: Session,
    job_id: str,
    llm: LLMProvider,
    *,
    actor: str = "system",
) -> CaliberRefinementJob:
    """Advance the named job through the diagnosis stage.

    Parameters
    ----------
    session:
        Open SQLAlchemy session. Committed inside this function on success.
        Not committed if the LLM call raises — the job stays in
        ``running/diagnosis`` so a retry picks it up.
    job_id:
        ID of the refinement job to diagnose.
    llm:
        :class:`LLMProvider` implementation. Production injects
        :class:`OpenAIAgentsLLMProvider`; tests inject
        :class:`FakeLLMProvider`.
    actor:
        Who is triggering this. Workers pass ``"system"``.

    Returns
    -------
    The updated :class:`CaliberRefinementJob` row.

    Raises
    ------
    LookupError
        Job or source item is missing.
    DiagnosisStateError
        Job is not in a state where diagnosis can run.
    LLMProviderError
        The LLM call failed. Re-raised after rolling back; worker handles.
    """
    job = session.get(CaliberRefinementJob, job_id)
    if job is None:
        raise LookupError(f"refinement job {job_id!r} not found")

    if job.status not in _ELIGIBLE_STATUSES or job.current_stage not in _ELIGIBLE_STAGES:
        raise DiagnosisStateError(
            f"job {job_id!r} not eligible for diagnosis: "
            f"status={job.status!r}, stage={job.current_stage!r}"
        )

    if job.artifact_type == "workflow_manifest":
        # Workflow jobs localize the failure to graph components rather than
        # calling the prompt-diagnosis LLM (plan §17.2). Lazy import avoids a
        # cycle (workflow_stages imports the workflows engine, not this module).
        from caliber.orchestrator.workflow_stages import run_workflow_diagnosis  # noqa: PLC0415

        return run_workflow_diagnosis(session, job, actor=actor)

    item = session.get(CaliberVerificationItem, job.primary_item_id)
    if item is None:
        raise LookupError(f"verification item {job.primary_item_id!r} not found for job {job_id!r}")

    evidence = EvidenceContext(
        agent_id=job.agent_id,
        item_id=item.item_id,
        category=item.category,
        severity=item.severity,
        free_text=item.free_text,
        trace_id=item.trace_id,
        session_id=item.session_id,
        evidence_summary={
            "experiment_id": item.experiment_id,
            "artifact_type_hint": item.artifact_type_hint,
        },
    )

    try:
        diagnosis, usage = llm.diagnose(evidence)
    except LLMProviderError:
        session.rollback()
        raise

    previous_stage = job.current_stage
    job.current_stage = "candidate"
    job.diagnosis = _diagnosis_to_json(diagnosis)
    job.total_tokens += usage.input_tokens + usage.output_tokens
    job.cost_usd += usage.cost_usd

    audit_record(
        session,
        actor=actor,
        action="advance_stage",
        entity_type="refinement_job",
        entity_id=job.job_id,
        details={
            "from_stage": previous_stage,
            "to_stage": "candidate",
            "diagnosis": _diagnosis_to_json(diagnosis),
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cost_usd": usage.cost_usd,
            },
        },
    )

    session.commit()
    logger.info(
        "diagnosis complete: job=%s confidence=%.2f cost_usd=%.4f",
        job_id,
        diagnosis.confidence,
        usage.cost_usd,
    )
    return job


def _diagnosis_to_json(diagnosis: Diagnosis) -> dict[str, Any]:
    """Serialize a :class:`Diagnosis` into the JSON-column shape.

    We don't store the raw Pydantic dump because we want to keep the
    schema stable across Diagnosis-model version bumps. Explicit fields
    here are the contract callers (and the UI) can rely on.
    """
    return {
        "root_cause": diagnosis.root_cause,
        "affected_components": list(diagnosis.affected_components),
        "confidence": diagnosis.confidence,
        "alternatives": list(diagnosis.alternatives),
    }
