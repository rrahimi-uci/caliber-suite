"""Optimizer selection — picks which optimizer the candidate stage will use.

The full selection logic:

* Bundle shape → MultiAgentCoord
* Conversation policy → MemAlign
* Cost optimization mode → PromptDistill
* Iterative retry → TextGrad
* DSPy program → DSPyMIPRO
* Few-shot with exemplar diagnosis → DSPyBootstrapFewShot
* Low diagnosis confidence → GEPA
* Default → MetaPrompt

Phase 2.8+ supports MetaPrompt, SkillMetaPrompt, and GEPA.
The seam is in place so the remaining selection rules can land without
touching the orchestrator. The agent's per-agent override is also
respected so an operator can pin a specific optimizer for testing.
"""

from __future__ import annotations

import logging

from caliber.db.models import CaliberAgentConfig, CaliberRefinementJob

logger = logging.getLogger("caliber.orchestrator.optimizer_select")

# GEPA selection thresholds.
_GEPA_CONFIDENCE_THRESHOLD = 0.7
_GEPA_MIN_ALTERNATIVES = 3
_COMPETING_OBJECTIVES_KEYWORDS = frozenset(
    {"competing objectives", "competing dimensions", "tradeoff", "trade-off", "pareto"}
)

# DSPyBootstrapFewShot selection — diagnosis language that points at a
# missing-exemplars failure mode (the model would benefit from worked few-shot
# examples rather than a prompt rewrite). Auto-selection is gated behind an
# agent opt-in flag (see :func:`select_optimizer`) so existing agents are
# unaffected; the manual override path always works regardless of the flag.
_FEWSHOT_KEYWORDS = frozenset(
    {"few-shot", "few shot", "exemplar", "example", "demonstration", "in-context"}
)


def _diagnosis_suggests_fewshot(job: CaliberRefinementJob) -> bool:
    """Return True when the diagnosis points at a missing-exemplars failure.

    Looks for few-shot / example / demonstration language in the diagnosis
    ``root_cause``, ``recommended_action``, and ``alternatives`` — the signal
    that worked examples (DSPyBootstrapFewShot) would help more than a prompt
    rewrite.
    """
    diag = job.diagnosis
    if not isinstance(diag, dict):
        return False

    parts: list[str] = []
    for key in ("root_cause", "recommended_action"):
        value = diag.get(key)
        if isinstance(value, str):
            parts.append(value)
    alternatives = diag.get("alternatives", [])
    if isinstance(alternatives, list):
        parts.extend(str(a) for a in alternatives)

    searchable = " ".join(parts).lower()
    return any(kw in searchable for kw in _FEWSHOT_KEYWORDS)


def _diagnosis_suggests_gepa(job: CaliberRefinementJob) -> bool:
    """Return True when the diagnosis payload matches GEPA selection criteria.

    Criteria:

    * ``diagnosis.confidence < 0.7``
    * ``len(diagnosis.alternatives) >= 3``
    * diagnosis root_cause or alternatives cite 'competing objectives'
    * previous candidate for the same agent was rolled back (rollback_count > 0)
    """
    diag = job.diagnosis
    if not isinstance(diag, dict):
        return False

    confidence = diag.get("confidence", 1.0)
    if isinstance(confidence, (int, float)) and confidence < _GEPA_CONFIDENCE_THRESHOLD:
        return True

    alternatives = diag.get("alternatives", [])
    if isinstance(alternatives, list) and len(alternatives) >= _GEPA_MIN_ALTERNATIVES:
        return True

    # Check for competing-objectives language in root_cause or alternatives.
    raw_root_cause = diag.get("root_cause")
    searchable = (raw_root_cause if isinstance(raw_root_cause, str) else "").lower()
    if isinstance(alternatives, list):
        searchable += " " + " ".join(str(a).lower() for a in alternatives)
    if any(kw in searchable for kw in _COMPETING_OBJECTIVES_KEYWORDS):
        return True

    # Rollback history — if the job was created after a rollback. ``rollback_count``
    # is an optional/dynamic attribute, so read it defensively via getattr.
    rollback_count = getattr(job, "rollback_count", 0) or 0
    return rollback_count > 0


def select_optimizer(agent: CaliberAgentConfig, job: CaliberRefinementJob) -> str:
    """Return the optimizer name the candidate stage should run.

    Selection rule:

    1. ``job.optimizer_type`` if set and not ``"Auto"`` — manual runs can
       pin a one-off optimizer without mutating agent configuration.
    2. ``agent.optimizer_config["type"]`` if set and not ``"Auto"`` — the
       explicit operator override always wins. Recorded as an audit tag
       elsewhere.
    3. If the diagnosis matches GEPA criteria (low confidence, many
       alternatives, competing objectives, or rollback history) →
       ``"GEPA"``. This applies to both prompt and skill jobs.
    4. If the agent opted in (``optimizer_config["dspy_fewshot_auto"]``) and a
       prompt job's diagnosis points at a missing-exemplars failure →
       ``"DSPyBootstrapFewShot"``. Default-off so existing agents are
       unaffected.
    5. For ``artifact_type="skill"`` jobs: ``"SkillMetaPrompt"`` — a
       specialization of MetaPrompt that preserves XML structure and
       validates ``allowed_tools`` constraints.
    6. Otherwise ``"MetaPrompt"`` — the default optimizer.

    The signature takes the full agent and job rows rather than just the
    diagnosis so the real selection rule (which inspects ``bundle_targets``,
    ``agent.optimize_for``, prior job state, etc.) can land here without
    a contract change.
    """
    job_override = job.optimizer_type
    if isinstance(job_override, str) and job_override and job_override.lower() != "auto":
        return job_override

    override = agent.optimizer_config.get("type") if agent.optimizer_config else None
    if isinstance(override, str) and override and override.lower() != "auto":
        return override

    # GEPA selection — applies to both prompt and skill jobs.
    if _diagnosis_suggests_gepa(job):
        logger.info(
            "GEPA selected for job=%s (diagnosis meets GEPA criteria)",
            job.job_id,
        )
        return "GEPA"

    # DSPyBootstrapFewShot selection — opt-in per agent (default-off so existing
    # agents are unaffected) and only for prompt jobs whose diagnosis points at
    # a missing-exemplars failure. DSPy bootstraps few-shot demos from the
    # agent's eval dataset; with no examples the candidate stage falls back to
    # MetaPrompt, so this stays safe even when the dataset is empty.
    optimizer_cfg = agent.optimizer_config or {}
    if (
        optimizer_cfg.get("dspy_fewshot_auto")
        and job.artifact_type == "prompt"
        and _diagnosis_suggests_fewshot(job)
    ):
        logger.info(
            "DSPyBootstrapFewShot selected for job=%s (agent opt-in + few-shot diagnosis)",
            job.job_id,
        )
        return "DSPyBootstrapFewShot"

    # Skill-targeted jobs use a skill-specific optimizer variant.
    if job.artifact_type == "skill":
        return "SkillMetaPrompt"

    return "MetaPrompt"
