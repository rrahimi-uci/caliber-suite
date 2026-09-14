"""Auto-provisioned hidden runtime identities for prompt testing/calibration.

The "pytest for prompts" model treats a *prompt* as the unit under test. But
every downstream pipeline machine — refinement jobs, prompt-test runs, MLflow
traces — keys off an ``agent_id`` (a :class:`~caliber.db.models.CaliberAgentConfig`
row). Forcing authors to register an agent before they can test or calibrate a
prompt is friction we don't want.

This module closes that gap: when a prompt needs a runtime identity, we
get-or-create a *hidden* ``CaliberAgentConfig`` row keyed on the prompt name.
The row is marked with ``optimizer_config.source_type == "prompt_target"`` (the
same marker precedent the workflow fleet uses with ``source_workflow_id`` in
:mod:`caliber.workflows.promoter`). :func:`is_hidden_prompt_target` recognizes
that marker so agent/inventory listings can filter these rows out — they are an
implementation detail, never something an operator manages directly.
"""

from __future__ import annotations

from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException

from caliber.auth import CaliberIdentity
from caliber.db.models import CaliberAgentConfig
from caliber.db.scoping import get_visible
from caliber.ids import _suffix

# Marker stored under ``optimizer_config.source_type`` on the hidden row.
PROMPT_TARGET_SOURCE_TYPE = "prompt_target"


def is_hidden_prompt_target(cfg: CaliberAgentConfig) -> bool:
    """Return True when ``cfg`` is an auto-provisioned hidden prompt target.

    These rows exist purely to give a prompt a runtime identity; they must be
    filtered out of every agent-listing / inventory surface.
    """
    return (
        isinstance(cfg.optimizer_config, dict)
        and cfg.optimizer_config.get("source_type") == PROMPT_TARGET_SOURCE_TYPE
    )


def ensure_prompt_target(
    session: Session,
    prompt_name: str,
    *,
    owner: str,
    identity: CaliberIdentity | None = None,
    model: str | None = None,
    project_id: str | None = None,
    visibility: str | None = None,
) -> CaliberAgentConfig:
    """Get-or-create the hidden runtime identity for ``prompt_name``.

    Idempotent: the row is keyed on ``agent_id == prompt_name``, so repeated
    calls (a second prompt save, a re-run of calibration) return the existing
    row rather than duplicating it. When ``model`` is supplied and the stored
    target has not yet recorded one, the model is backfilled so the author step
    can pin the chosen model after the fact.

    The created row mirrors the workflow-fleet marker precedent: it carries a
    stable, unique ``experiment_id`` and an ``optimizer_config`` whose
    ``source_type`` flags it as a hidden prompt target.

    `P2` (isolation closure, item 4): the get-or-create shape means a *second*
    project registering/testing the same prompt name previously took over the
    first project's target with no visibility check at all -- whichever
    project got here first silently owned the row forever, and any later
    caller could mutate it (e.g. backfill ``model``). ``identity`` is
    optional: route call sites pass the resolved caller identity so an
    existing target invisible to them raises 404 rather than being reused or
    mutated; internal callers with no request context omit it and get the
    unscoped lookup, the same convention ``PlanService.get_plan``'s own
    optional ``identity`` parameter already established.
    """
    existing = session.get(CaliberAgentConfig, prompt_name)
    if existing is not None:
        if identity is not None:
            visible = get_visible(
                session, CaliberAgentConfig, CaliberAgentConfig.agent_id, prompt_name, identity
            )
            if visible is None:
                raise HTTPException(status_code=404, detail=f"prompt {prompt_name!r} not found")
        if model is not None and isinstance(existing.optimizer_config, dict):
            stored_model = existing.optimizer_config.get("model")
            if stored_model in (None, ""):
                # JSON columns mutate in place poorly across flush boundaries —
                # reassign a fresh dict so SQLAlchemy reliably marks it dirty.
                existing.optimizer_config = {**existing.optimizer_config, "model": model}
        return existing

    resolved_visibility = visibility or ("project" if project_id else "user")
    target = CaliberAgentConfig(
        agent_id=prompt_name,
        experiment_id=f"prompt-target-{_suffix()}",
        name=prompt_name,
        owner=owner,
        project_id=project_id,
        visibility=resolved_visibility,
        artifact_types=["prompt"],
        eval_thresholds={},
        optimizer_config={
            "source_type": PROMPT_TARGET_SOURCE_TYPE,
            "model": model,
            "bound_to": None,
        },
        approval_policy={},
        enabled=True,
        required_approvals=0,
    )
    session.add(target)
    session.flush()
    return target


def prompt_target_status(
    *,
    target: CaliberAgentConfig | None,
    has_test_run: bool,
    has_applied_job: bool,
) -> str:
    """Compute the prompt lifecycle status.

    Precedence (highest first): **Bound > Calibrated > Tested > Has test set >
    Draft**. The three boolean signals are gathered by the caller (so list
    endpoints can batch the DB lookups instead of N+1'ing per row):

    * ``has_test_run`` — ≥1 :class:`CaliberPromptTestRun` for this ``agent_id``.
    * ``has_applied_job`` — a :class:`CaliberRefinementJob` for this ``agent_id``
      reached the ``applied`` terminal-success state (the status
      :func:`caliber.routes.jobs.apply_job` sets on Apply via
      :func:`caliber.apply.apply_candidate`).

    "Has test set" is read off the target's ``optimizer_config.dataset_id``.
    """
    cfg = target.optimizer_config if target is not None else None
    cfg = cfg if isinstance(cfg, dict) else {}

    if cfg.get("bound_to") is not None:
        return "Bound"
    if has_applied_job:
        return "Calibrated"
    if has_test_run:
        return "Tested"
    if cfg.get("dataset_id"):
        return "Has test set"
    return "Draft"
