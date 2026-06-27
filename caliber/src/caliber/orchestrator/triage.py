"""Triage stage of the refinement pipeline.

The triage stage is the first thing CALIBER does to a freshly-verified
feedback item. Its job is light: classify the item's failure mode, set a
preliminary ``artifact_type``, and decide whether the job should proceed
straight to Evidence or be held for manual review (e.g. when severity is
critical *and* policy says critical items require an admin gate before
running through the optimizer).

The stage:

* fetches the job and its source verification item,
* validates the state machine (only ``queued`` jobs in ``triage`` stage are
  eligible),
* classifies the failure mode (see below),
* sets ``current_stage = "evidence"`` and ``status = "running"``,
* bumps ``attempt_count``,
* writes an audit-log row.

Classification (:func:`_classify`) is LLM-driven when the worker passes a
configured :class:`LLMProvider` — it reasons over the item's free text via
:meth:`LLMProvider.classify_triage` — and falls back to a deterministic
heuristic on any LLM error so a flaky model never fails the job. The
state-machine code around it stays deterministic regardless.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from caliber.audit import record as audit_record
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberRefinementJob,
    CaliberSkill,
    CaliberVerificationItem,
)
from caliber.llm.provider import TriageContext

if TYPE_CHECKING:
    from caliber.llm.provider import LLMProvider

logger = logging.getLogger("caliber.orchestrator.triage")

# State machine guards. The worker's atomic claim transitions the job
# from ``queued`` to ``running`` *before* calling this stage, so triage's
# eligible status is ``running`` (matching every other stage). Phase 3.1
# changed this from ``queued`` to ``running`` when the atomic claim landed.
_ELIGIBLE_STATUSES = frozenset({"running"})
_ELIGIBLE_STAGES = frozenset({"triage"})


class TriageStateError(Exception):
    """Raised when :func:`run_triage` is called on an ineligible job state."""


def run_triage(
    session: Session,
    job_id: str,
    *,
    actor: str = "system",
    llm: LLMProvider | None = None,
) -> CaliberRefinementJob:
    """Advance the named job through the triage stage.

    Parameters
    ----------
    session:
        Open SQLAlchemy session. Committed inside this function so partial
        state is never observable to readers.
    job_id:
        ID of the refinement job to triage.
    actor:
        Who is triggering this. The worker will pass ``"system"``; if a human
        kicks triage manually, their identity flows through to the audit log.

    Returns
    -------
    The updated :class:`CaliberRefinementJob` row.

    Raises
    ------
    LookupError
        Job does not exist.
    TriageStateError
        Job exists but is not in a state where triage can run.
    """
    job = session.get(CaliberRefinementJob, job_id)
    if job is None:
        raise LookupError(f"refinement job {job_id!r} not found")

    if job.status not in _ELIGIBLE_STATUSES or job.current_stage not in _ELIGIBLE_STAGES:
        raise TriageStateError(
            f"job {job_id!r} not eligible for triage: "
            f"status={job.status!r}, stage={job.current_stage!r}"
        )

    item = session.get(CaliberVerificationItem, job.primary_item_id)
    if item is None:
        # Should be impossible given the FK, but the orchestrator is
        # defensive here because background workers can't ask a user.
        raise LookupError(f"verification item {job.primary_item_id!r} not found for job {job_id!r}")

    classification = _classify(session, item, job.agent_id, llm=llm)
    previous_stage = job.current_stage

    # Status stays ``running`` — the worker already transitioned the job
    # from ``queued`` to ``running`` during the atomic claim (§Phase 3.1).
    job.current_stage = "evidence"
    job.attempt_count += 1
    if job.artifact_type == "":
        # Triage backfills the artifact type when the verifier didn't pin one.
        job.artifact_type = classification["artifact_type"]
    # When triage identifies a skill target, record it so downstream stages
    # (evidence, candidate, eval, promoter) know which skill to read/write.
    if classification.get("skill_name"):
        job.skill_name = classification["skill_name"]

    audit_record(
        session,
        actor=actor,
        action="advance_stage",
        entity_type="refinement_job",
        entity_id=job.job_id,
        details={
            "from_stage": previous_stage,
            "to_stage": "evidence",
            "classification": classification,
        },
    )

    session.commit()
    logger.info("triage complete: job=%s class=%s", job_id, classification)
    return job


def _classify(  # noqa: PLR0911 — one return per failure-mode bucket + LLM/explicit paths
    session: Session,
    item: CaliberVerificationItem,
    agent_id: str,
    *,
    llm: LLMProvider | None = None,
) -> dict[str, Any]:
    """Classify a verification item into a failure-mode bucket.

    When an ``llm`` provider is supplied (the worker passes the configured
    one), the classifier reasons over the item's free-text via
    :meth:`LLMProvider.classify_triage`; on any LLM error it falls back to the
    deterministic heuristic below so a flaky model never fails the job. The
    explicit skill-hint shortcut and the skill *resolution* stay deterministic
    so the model never invents a skill name.

    Skill attribution (Phase 6): when the item's ``artifact_type_hint`` is
    ``"skill"`` and ``artifact_ref`` names a known skill, route to
    ``artifact_type="skill"`` so the downstream stages operate on the skill
    rather than the agent's main prompt. Also checks whether the agent
    references any skills and the feedback category matches a tool-use
    failure — if the agent has skills with ``allowed_tools`` defined, the
    failure is likely in the skill's tool-use instructions.

    Returning a dict (rather than a Literal) future-proofs the call site for
    when the classifier starts returning richer structured outputs
    (confidence, cluster ID, etc.).
    """
    # Explicit skill hint from the verifier / operator.
    if item.artifact_type_hint == "skill" and item.artifact_ref:
        skill = (
            session.query(CaliberSkill)
            .filter(
                CaliberSkill.name == item.artifact_ref,
                CaliberSkill.status == "active",
            )
            .first()
        )
        if skill is not None:
            return {
                "cluster": "skill",
                "artifact_type": "skill",
                "skill_name": skill.name,
                "confidence": 0.8,
            }

    # LLM-driven classification when a provider is configured. Falls through to
    # the deterministic heuristic below on any error (logged, never fatal).
    if llm is not None:
        llm_result = _classify_with_llm(session, item, agent_id, llm)
        if llm_result is not None:
            return llm_result

    # Implicit skill attribution: for tool-use failures, check whether
    # the agent references skills with ``allowed_tools`` defined.
    category = item.category.lower()
    if category in {"tool_use", "tool_calling"}:
        skill_name = _find_tool_skill(session, agent_id)
        if skill_name is not None:
            return {
                "cluster": "tool_use",
                "artifact_type": "skill",
                "skill_name": skill_name,
                "confidence": 0.6,
            }
        return {"cluster": "tool_use", "artifact_type": "prompt", "confidence": 0.6}

    if category in {"hallucination", "factual"}:
        return {"cluster": "hallucination", "artifact_type": "prompt", "confidence": 0.6}
    if category in {"context_drift", "memory"}:
        return {"cluster": "context_drift", "artifact_type": "prompt", "confidence": 0.6}
    return {"cluster": "other", "artifact_type": "prompt", "confidence": 0.4}


def _classify_with_llm(
    session: Session,
    item: CaliberVerificationItem,
    agent_id: str,
    llm: LLMProvider,
) -> dict[str, Any] | None:
    """Classify via the LLM, resolving any skill deterministically.

    Returns the classification dict, or ``None`` on any LLM error so the caller
    falls back to the heuristic. The model decides cluster/artifact_type, but a
    "skill" decision (or a tool-use category) is grounded against the agent's
    real skills via :func:`_find_tool_skill` — the model never names a skill.
    """
    agent = session.get(CaliberAgentConfig, agent_id)
    agent_has_skills = bool((agent.optimizer_config or {}).get("skills")) if agent else False
    context = TriageContext(
        agent_id=agent_id,
        item_id=item.item_id,
        category=item.category,
        severity=item.severity,
        free_text=item.free_text or "",
        agent_has_skills=agent_has_skills,
    )
    try:
        result, _usage = llm.classify_triage(context)
    except Exception as exc:
        # Any LLM failure (timeout, malformed output, ...) → deterministic fallback.
        logger.warning("LLM triage failed for item %s (%s); using heuristic", item.item_id, exc)
        return None

    artifact_type = result.artifact_type
    category = (item.category or "").lower()
    skill_name: str | None = None
    if artifact_type == "skill" or category in {"tool_use", "tool_calling"}:
        skill_name = _find_tool_skill(session, agent_id)
        if skill_name is not None:
            artifact_type = "skill"
        elif artifact_type == "skill":
            # Model wanted a skill but the agent references none — target the prompt.
            artifact_type = "prompt"

    classification: dict[str, Any] = {
        "cluster": result.cluster,
        "artifact_type": artifact_type,
        "confidence": result.confidence,
        "rationale": result.rationale,
        "source": "llm",
    }
    if skill_name is not None:
        classification["skill_name"] = skill_name
    return classification


def _find_tool_skill(session: Session, agent_id: str) -> str | None:
    """Return the name of a tool-related skill the agent references, if any.

    Looks up the agent's ``optimizer_config.skills`` list, then finds the
    first active skill with ``allowed_tools`` defined. Returns ``None`` if
    the agent has no skill references or none have tool restrictions.
    """
    agent = session.get(CaliberAgentConfig, agent_id)
    if agent is None:
        return None
    skill_names: list[str] = (agent.optimizer_config or {}).get("skills", [])
    if not skill_names:
        return None
    for name in skill_names:
        skill = (
            session.query(CaliberSkill)
            .filter(
                CaliberSkill.name == name,
                CaliberSkill.status == "active",
                CaliberSkill.allowed_tools.isnot(None),
            )
            .first()
        )
        if skill is not None:
            return skill.name
    return None
