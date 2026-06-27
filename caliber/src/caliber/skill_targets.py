"""Auto-provisioned hidden runtime identities for skill testing/calibration.

The "pytest for skills" model treats a *skill* as the unit under test. But
every downstream pipeline machine — refinement jobs, verification items, MLflow
traces — keys off an ``agent_id`` (a :class:`~caliber.db.models.CaliberAgentConfig`
row). Forcing authors to register an agent (or "select an agent to test a
skill") before they can test or calibrate a skill is friction we don't want.

This module closes that gap, mirroring :mod:`caliber.prompt_targets`: when a
skill needs a runtime identity, we get-or-create a *hidden*
``CaliberAgentConfig`` row keyed on the skill name. The row is marked with
``optimizer_config.source_type == "skill_target"`` so agent/inventory listings
can filter these rows out — they are an implementation detail, never something
an operator manages directly.

The hidden ``agent_id`` uses a ``skill::`` prefix so a skill target can never
collide with a prompt target that happens to share the same name (a prompt
target is keyed on the bare prompt name). The prefix also makes the row's
purpose obvious in audit trails and FK references.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from caliber.db.models import CaliberAgentConfig
from caliber.ids import _suffix

# Marker stored under ``optimizer_config.source_type`` on the hidden row.
SKILL_TARGET_SOURCE_TYPE = "skill_target"

# Prefix for the hidden ``agent_id`` — keeps skill harnesses in their own
# namespace so they never collide with a same-named prompt target.
SKILL_TARGET_PREFIX = "skill::"


def skill_target_agent_id(skill_name: str) -> str:
    """Return the stable hidden ``agent_id`` for a skill harness.

    Keyed on the skill *name* (the agent-cited handle), not the surrogate
    ``skill_id``, so the same harness is reused regardless of how the skill is
    addressed and so it survives a skill_id churn.
    """
    return f"{SKILL_TARGET_PREFIX}{skill_name}"


def is_hidden_skill_target(cfg: CaliberAgentConfig) -> bool:
    """Return True when ``cfg`` is an auto-provisioned hidden skill target.

    These rows exist purely to give a skill a runtime identity; they must be
    filtered out of every agent-listing / inventory surface.
    """
    return (
        isinstance(cfg.optimizer_config, dict)
        and cfg.optimizer_config.get("source_type") == SKILL_TARGET_SOURCE_TYPE
    )


def ensure_skill_target(
    session: Session,
    skill_name: str,
    *,
    owner: str,
    project_id: str | None = None,
    visibility: str | None = None,
) -> CaliberAgentConfig:
    """Get-or-create the hidden runtime identity for ``skill_name``.

    Idempotent: the row is keyed on ``agent_id == skill::{skill_name}``, so
    repeated calls (a second calibrate, a re-run of testing) return the existing
    row rather than duplicating it.

    The created row mirrors the prompt-target marker precedent: a stable, unique
    ``experiment_id`` and an ``optimizer_config`` whose ``source_type`` flags it
    as a hidden skill target. ``artifact_types=["skill"]`` records that this
    harness governs a skill (not a prompt).
    """
    agent_id = skill_target_agent_id(skill_name)
    existing = session.get(CaliberAgentConfig, agent_id)
    if existing is not None:
        return existing

    resolved_visibility = visibility or ("project" if project_id else "user")
    target = CaliberAgentConfig(
        agent_id=agent_id,
        experiment_id=f"skill-target-{_suffix()}",
        name=skill_name,
        owner=owner,
        project_id=project_id,
        visibility=resolved_visibility,
        artifact_types=["skill"],
        eval_thresholds={},
        optimizer_config={
            "source_type": SKILL_TARGET_SOURCE_TYPE,
            "skill_name": skill_name,
            "bound_to": None,
        },
        approval_policy={},
        enabled=True,
        required_approvals=0,
    )
    session.add(target)
    session.flush()
    return target


def skill_target_status(
    *,
    target: CaliberAgentConfig | None,
    has_test_run: bool,
    has_applied_job: bool,
    has_scenarios: bool,
) -> str:
    """Compute the skill lifecycle status.

    Precedence (highest first): **Bound > Calibrated > Tested > Has scenarios >
    Draft**. The boolean signals are gathered by the caller (so list endpoints
    can batch the DB lookups instead of N+1'ing per row):

    * ``has_applied_job`` — a :class:`~caliber.db.models.CaliberRefinementJob`
      for this skill target's ``agent_id`` reached the ``applied``
      terminal-success state (set on Apply via
      :func:`caliber.apply.apply_candidate`).
    * ``has_test_run`` — ≥1 :class:`~caliber.db.models.CaliberSkillTestRun` for
      this skill.
    * ``has_scenarios`` — the skill has a recorded scenario set. There is no
      dedicated scenario storage yet, so callers base this on ≥1 durable run of
      kind in {selection, scenario}.
    """
    cfg = target.optimizer_config if target is not None else None
    cfg = cfg if isinstance(cfg, dict) else {}

    if cfg.get("bound_to") is not None:
        return "Bound"
    if has_applied_job:
        return "Calibrated"
    if has_test_run:
        return "Tested"
    if has_scenarios:
        return "Has scenarios"
    return "Draft"
