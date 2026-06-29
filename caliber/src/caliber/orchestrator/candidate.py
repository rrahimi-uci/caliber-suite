"""Candidate-generation stage of the refinement pipeline.

The fifth stage. Reads the diagnosis from the prior stage, picks an
optimizer (Phase 2.8: always ``"MetaPrompt"``), calls the injected
:class:`LLMProvider` to produce a structured :class:`PromptCandidate`,
persists it to ``caliber_refinement_jobs.candidate``, and advances the
job to ``running/eval``.

Same state-machine + audit shape as :func:`run_diagnosis`. The LLM call
is wrapped so :class:`LLMProviderError` bubbles to the worker which
marks the job ``failed``.
"""

from __future__ import annotations

import logging
from types import ModuleType
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from caliber.artifact_store import ArtifactStore
from caliber.audit import record as audit_record
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberRefinementJob,
    CaliberSkill,
    CaliberVerificationItem,
)
from caliber.llm.provider import (
    CandidateContext,
    Diagnosis,
    LLMProvider,
    LLMProviderError,
    PromptCandidate,
)
from caliber.orchestrator.optimizer_select import select_optimizer

logger = logging.getLogger("caliber.orchestrator.candidate")

_ELIGIBLE_STATUSES = frozenset({"running"})
_ELIGIBLE_STAGES = frozenset({"candidate"})
# Default eval-dataset handle, mirroring eval_stage._DEFAULT_EVAL_DATASET — the
# trainset DSPy bootstraps from is the same dataset the candidate is later
# scored against.
_DEFAULT_EVAL_DATASET = "default"


class CandidateStateError(Exception):
    """Raised when :func:`run_candidate` is called on an ineligible job state."""


def run_candidate(  # noqa: PLR0915 - sequential candidate-stage orchestration is intentionally explicit
    session: Session,
    job_id: str,
    llm: LLMProvider,
    artifact_store: ArtifactStore,
    *,
    actor: str = "system",
    config: CaliberConfig | None = None,
) -> CaliberRefinementJob:
    """Advance the named job through the candidate-generation stage.

    Parameters
    ----------
    session:
        Open SQLAlchemy session. Committed on success. Rolled back on
        :class:`LLMProviderError` so a retry sees the same input state.
    job_id:
        ID of the refinement job.
    llm:
        :class:`LLMProvider` implementation. Tests pass
        :class:`caliber.llm.fake.FakeLLMProvider`.
    artifact_store:
        :class:`caliber.artifact_store.ArtifactStore` implementation. Used
        to fetch the current artifact content (typically the active
        prompt) so the candidate generator can produce a minimal edit
        rather than a full rewrite. ``None`` is tolerated for cold-start.
    actor:
        Who is triggering this. Workers pass ``"system"``.

    Returns
    -------
    The updated :class:`CaliberRefinementJob` row.

    Raises
    ------
    LookupError
        Job or agent row missing, or diagnosis hasn't been recorded yet.
    CandidateStateError
        Job is not in a state where candidate generation can run.
    LLMProviderError
        The LLM call failed.
    """
    job = session.get(CaliberRefinementJob, job_id)
    if job is None:
        raise LookupError(f"refinement job {job_id!r} not found")

    if job.status not in _ELIGIBLE_STATUSES or job.current_stage not in _ELIGIBLE_STAGES:
        raise CandidateStateError(
            f"job {job_id!r} not eligible for candidate: "
            f"status={job.status!r}, stage={job.current_stage!r}"
        )

    if job.artifact_type == "workflow_manifest":
        # Workflow jobs generate a graph patch (run_workflow_candidate) rather
        # than a prompt rewrite — and must populate candidate/baseline manifests
        # so the eval stage (run_workflow_eval) can compile + replay. Mirrors the
        # diagnosis/eval workflow delegation; lazy import avoids an import cycle
        # (workflow_stages imports the workflows engine, not this module).
        from caliber.orchestrator.workflow_stages import run_workflow_candidate  # noqa: PLC0415

        return run_workflow_candidate(session, job, actor=actor, config=config)

    if not job.diagnosis:
        raise LookupError(
            f"job {job_id!r} has no diagnosis recorded; the diagnosis stage "
            "must run before candidate generation"
        )

    agent = session.get(CaliberAgentConfig, job.agent_id)
    if agent is None:
        raise LookupError(f"agent {job.agent_id!r} not found for job {job_id!r}")

    optimizer_type = select_optimizer(agent, job)
    diagnosis = _diagnosis_from_json(job.diagnosis)

    # Resolve the current artifact content and any skill-specific context.
    skill_kwargs: dict[str, Any] = {}
    if job.artifact_type == "skill" and job.skill_name:
        current_content = artifact_store.get_active_skill(job.skill_name)
        # Load skill metadata for the LLM provider.
        skill = (
            session.query(CaliberSkill)
            .filter(
                CaliberSkill.name == job.skill_name,
                CaliberSkill.status == "active",
            )
            .first()
        )
        if skill is not None:
            # Find affected agents for multi-agent context.
            agents = (
                session.query(CaliberAgentConfig)
                .filter(
                    CaliberAgentConfig.enabled.is_(True),
                )
                .all()
            )
            affected_agent_ids = [
                a.agent_id
                for a in agents
                if job.skill_name in (a.optimizer_config or {}).get("skills", [])
            ]
            skill_kwargs = {
                "skill_name": skill.name,
                "skill_metadata": dict(skill.skill_metadata or {}),
                "allowed_tools": skill.allowed_tools,
                "depends_on": list(skill.depends_on or []),
                "affected_agent_ids": affected_agent_ids,
            }
    else:
        current_content = _prompt_optimization_baseline(session, job)
        if current_content is None:
            current_content = artifact_store.get_active_prompt(job.agent_id)

    # Build GEPA-specific kwargs when the optimizer selects GEPA.
    gepa_kwargs: dict[str, Any] = {}
    if optimizer_type == "GEPA":
        # Default GEPA params — can be overridden by agent.optimizer_config.
        optimizer_cfg = agent.optimizer_config or {}
        gepa_kwargs = {
            "pareto_dims": optimizer_cfg.get("pareto_dims", ["quality", "safety"]),
            "population_size": optimizer_cfg.get("population_size", 8),
            "generations": optimizer_cfg.get("generations", 3),
        }

    # Build DSPy-specific kwargs when the optimizer selects a DSPy teleprompter.
    # DSPy needs a trainset (few-shot bootstrapping source) the other optimizers
    # don't — loaded from the agent's eval dataset. Per-agent demo-count
    # overrides flow through ``optimizer_config``; otherwise the provider uses
    # its config-level defaults.
    dspy_kwargs: dict[str, Any] = {}
    if optimizer_type.startswith("DSPy"):
        optimizer_cfg = agent.optimizer_config or {}
        pinned_dataset_id, pinned_version = _prompt_optimization_dataset_pin(session, job)
        dspy_kwargs = {
            "trainset": _load_trainset(
                session,
                agent,
                pinned_dataset_id=pinned_dataset_id,
                pinned_version=pinned_version,
            ),
            "dspy_max_bootstrapped_demos": optimizer_cfg.get("dspy_max_bootstrapped_demos"),
            "dspy_max_labeled_demos": optimizer_cfg.get("dspy_max_labeled_demos"),
            "dspy_mipro_auto": optimizer_cfg.get("dspy_mipro_auto"),
        }

    context = CandidateContext(
        agent_id=job.agent_id,
        job_id=job.job_id,
        artifact_type=job.artifact_type,
        optimizer_type=optimizer_type,
        diagnosis=diagnosis,
        current_artifact_content=current_content,
        review_notes=job.review_notes,
        **skill_kwargs,
        **gepa_kwargs,
        **dspy_kwargs,
    )

    try:
        candidate, usage = llm.generate_candidate(context)
    except LLMProviderError:
        session.rollback()
        raise

    mlflow_candidate_prompt_ref = _register_candidate_prompt_draft(
        job=job,
        candidate_content=candidate.content,
    )

    previous_stage = job.current_stage
    job.current_stage = "eval"
    job.optimizer_type = optimizer_type
    job.candidate = _candidate_to_json(
        candidate,
        baseline_content=current_content,
        mlflow_candidate_prompt_ref=mlflow_candidate_prompt_ref,
    )
    job.total_tokens += usage.input_tokens + usage.output_tokens
    job.cost_usd += usage.cost_usd
    # Clear ``review_notes`` once we've consumed it. If the reviewer's
    # next pass also requests changes, the request-changes endpoint
    # writes fresh notes — but we never carry stale guidance forward.
    job.review_notes = None

    audit_record(
        session,
        actor=actor,
        action="advance_stage",
        entity_type="refinement_job",
        entity_id=job.job_id,
        details={
            "from_stage": previous_stage,
            "to_stage": "eval",
            "optimizer_type": optimizer_type,
            "candidate_artifact_type": candidate.artifact_type,
            "diff_summary": candidate.diff_summary,
            "rationale": candidate.rationale,
            "usage": {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cost_usd": usage.cost_usd,
            },
            "mlflow_candidate_prompt_ref": mlflow_candidate_prompt_ref,
        },
    )

    session.commit()
    logger.info(
        "candidate complete: job=%s optimizer=%s cost_usd=%.4f",
        job_id,
        optimizer_type,
        usage.cost_usd,
    )
    return job


def _prompt_optimization_baseline(
    session: Session,
    job: CaliberRefinementJob,
) -> str | None:
    """Return a manual prompt-optimization baseline captured on the source item."""
    item = session.get(CaliberVerificationItem, job.primary_item_id)
    if item is None or not isinstance(item.submitted_context, dict):
        return None

    submitted = item.submitted_context
    raw = submitted.get("prompt_optimization")
    if submitted.get("source") != "prompt_optimization" or not isinstance(raw, dict):
        return None

    baseline = raw.get("baseline_content")
    return baseline if isinstance(baseline, str) and baseline else None


def _prompt_optimization_dataset_pin(
    session: Session,
    job: CaliberRefinementJob,
) -> tuple[str | None, int | None]:
    """Return ``(eval_dataset_id, eval_dataset_version)`` pinned on the run.

    A manual prompt-optimization run records the dataset id + version it was
    launched against; honouring both here keeps the DSPy few-shot trainset in
    lock-step with the pinned eval set. Returns ``(None, None)`` for jobs
    without a recorded pin so the agent-level dataset resolution still applies.
    """
    item = session.get(CaliberVerificationItem, job.primary_item_id)
    if item is None or not isinstance(item.submitted_context, dict):
        return None, None

    submitted = item.submitted_context
    raw = submitted.get("prompt_optimization")
    if submitted.get("source") != "prompt_optimization" or not isinstance(raw, dict):
        return None, None

    raw_id = raw.get("eval_dataset_id")
    dataset_id = raw_id if isinstance(raw_id, str) and raw_id else None
    raw_version = raw.get("eval_dataset_version")
    version = (
        raw_version
        if isinstance(raw_version, int) and not isinstance(raw_version, bool) and raw_version >= 1
        else None
    )
    return dataset_id, version


def _diagnosis_from_json(payload: dict[str, Any]) -> Diagnosis:
    """Reconstruct a :class:`Diagnosis` from the JSON shape persisted by
    :mod:`caliber.orchestrator.diagnosis`. The two functions are mirror
    images and must stay in sync — that's enforced by tests, not types,
    so they live in the same module-pair on purpose."""
    raw_root_cause = payload.get("root_cause")
    return Diagnosis(
        root_cause=raw_root_cause if isinstance(raw_root_cause, str) else "",
        affected_components=list(payload.get("affected_components", [])),
        confidence=float(payload.get("confidence", 0.0)),
        alternatives=list(payload.get("alternatives", [])),
    )


def _candidate_to_json(
    candidate: PromptCandidate,
    *,
    baseline_content: str | None = None,
    mlflow_candidate_prompt_ref: str | None = None,
) -> dict[str, Any]:
    """Serialize :class:`PromptCandidate` into the JSON-column shape.

    Same rationale as :func:`caliber.orchestrator.diagnosis._diagnosis_to_json`:
    explicit fields are the contract callers depend on.
    """
    payload: dict[str, Any] = {
        "artifact_type": candidate.artifact_type,
        "content": candidate.content,
        "rationale": candidate.rationale,
        "diff_summary": candidate.diff_summary,
    }
    if baseline_content is not None:
        payload["baseline_content"] = baseline_content
    if mlflow_candidate_prompt_ref is not None:
        payload["mlflow_candidate_prompt_ref"] = mlflow_candidate_prompt_ref
    return payload


def _register_candidate_prompt_draft(
    *,
    job: CaliberRefinementJob,
    candidate_content: str,
) -> str | None:
    """Best-effort MLflow draft prompt registration for candidate artifacts."""
    if job.artifact_type != "prompt" or not job.mlflow_run_id:
        return None

    mlflow_mod = _import_mlflow()
    if mlflow_mod is None:
        logger.debug(
            "skipping candidate draft prompt registration for job=%s: mlflow unavailable",
            job.job_id,
        )
        return None

    try:
        register_prompt = _resolve_prompt_api(mlflow_mod, "register_prompt")
        version = register_prompt(
            name=job.agent_id,
            template=candidate_content,
            commit_message=f"CALIBER candidate draft for job {job.job_id}",
            tags={
                "caliber.mlflow_run_id": job.mlflow_run_id,
                "caliber.review_status": "draft",
                "caliber.refinement_job_id": job.job_id,
            },
        )
    except Exception:
        logger.warning(
            "failed to register candidate draft prompt for job=%s",
            job.job_id,
            exc_info=True,
        )
        return None

    uri = getattr(version, "uri", None)
    if isinstance(uri, str) and uri:
        return uri
    raw_version = getattr(version, "version", None)
    try:
        version_number = int(str(raw_version))
    except (TypeError, ValueError):
        return None
    return f"prompts:/{job.agent_id}/{version_number}"


def _load_trainset(
    session: Session,
    agent: CaliberAgentConfig,
    *,
    pinned_dataset_id: str | None = None,
    pinned_version: int | None = None,
) -> list[dict[str, Any]]:
    """Load the agent's eval examples as a DSPy trainset.

    Resolves the dataset the same way the eval stage does
    (``agent.eval_thresholds["eval_dataset_id"]`` → default), looks it up by id
    *or* name, and returns the active (non-superseded) examples as
    ``[{"input": ..., "expected": ..., "weight": ...}]`` rows. Returns an empty
    list when no dataset/examples exist — the provider then falls back to
    MetaPrompt, since BootstrapFewShot has nothing to bootstrap from.

    When a manual prompt-optimization run pins a dataset + version, the same
    pin is honoured here so the few-shot bootstrap source matches what the eval
    stage scores against (reproducibility). ``pinned_dataset_id`` takes
    precedence over the agent-level dataset, and ``pinned_version`` resolves the
    historical example set as of that version (``dataset_version <= N`` and not
    retired at/before N).
    """
    if pinned_dataset_id:
        dataset_ref = pinned_dataset_id
    else:
        raw = agent.eval_thresholds.get("eval_dataset_id") if agent.eval_thresholds else None
        dataset_ref = raw if isinstance(raw, str) and raw else _DEFAULT_EVAL_DATASET

    dataset = session.get(CaliberEvalDataset, dataset_ref)
    if dataset is None:
        dataset = (
            session.execute(
                select(CaliberEvalDataset).where(CaliberEvalDataset.name == dataset_ref)
            )
            .scalars()
            .first()
        )
    if dataset is None:
        logger.info(
            "DSPy trainset: no eval dataset %r for agent=%s; provider will fall back",
            dataset_ref,
            agent.agent_id,
        )
        return []

    stmt = select(CaliberEvalDatasetExample).where(
        CaliberEvalDatasetExample.dataset_id == dataset.dataset_id
    )
    if pinned_version is None:
        stmt = stmt.where(CaliberEvalDatasetExample.superseded_at.is_(None))
    else:
        stmt = stmt.where(CaliberEvalDatasetExample.dataset_version <= pinned_version).where(
            or_(
                CaliberEvalDatasetExample.superseded_version.is_(None),
                CaliberEvalDatasetExample.superseded_version > pinned_version,
            )
        )
    examples = session.execute(stmt).scalars().all()
    return [{"input": ex.input, "expected": ex.expected, "weight": ex.weight} for ex in examples]


def _import_mlflow() -> ModuleType | None:
    try:
        import mlflow  # noqa: PLC0415

        return mlflow
    except ImportError:
        return None


def _resolve_prompt_api(mlflow_mod: ModuleType, method_name: str) -> Any:
    genai = getattr(mlflow_mod, "genai", None)
    fn = getattr(genai, method_name, None) if genai is not None else None
    if callable(fn):
        return fn
    legacy = getattr(mlflow_mod, method_name, None)
    if callable(legacy):
        return legacy
    raise AttributeError(f"MLflow prompt API '{method_name}' is not available")
