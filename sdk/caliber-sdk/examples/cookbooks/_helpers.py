"""Shared helpers for the runnable SDK cookbook examples."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from caliber_sdk import CaliberClient

#: A workflow run has stopped waiting for anything further to happen, either
#: because it finished or because it is paused on a human decision. Several
#: cookbooks poll for this exact set rather than only the SDK's own terminal
#: states, since ``waiting_approval`` is not terminal but a script must still
#: stop polling there to answer it.
PAUSED_OR_TERMINAL_RUN_STATES = frozenset(
    {"waiting_approval", "succeeded", "failed", "cancelled", "canceled", "timed_out"}
)


def get_recipe(caliber: CaliberClient, cookbook_id: str) -> Any:
    return {item.id: item for item in caliber.cookbooks.list()}[cookbook_id]


def configuration_blockers(recipe: Any) -> list[str]:
    return [
        str(check.get("label"))
        for check in recipe.unmet_checks
        if check.get("status") == "configuration_required"
    ]


def env_client() -> CaliberClient:
    return CaliberClient(
        os.environ["CALIBER_BASE_URL"],
        token=os.environ["CALIBER_TOKEN"],
    )


def drive_aria_plan(
    caliber: CaliberClient,
    plan_id: str,
    *,
    on_step: Callable[[str | None], tuple[bool, dict[str, Any]]],
) -> tuple[Any, dict[str, Any]]:
    """Advance an Aria goal-plan through approval and its per-step interactions.

    Every Aria cookbook's own README documents the same verified execution
    gap: the shipped ``HeuristicPlanner`` decomposes a goal into steps but
    leaves each mutate step's inputs empty, and an interaction answer
    (``{approved, choice, value}``) cannot carry a payload. So today, driving
    a plan to completion is not enough to produce the artifacts it describes
    -- each one still has to be created through its own typed SDK call,
    matched to the step that asked for it by ``capability_key``, right after
    approving (or denying) that step's interaction.

    ``on_step`` is called once per pending interaction with that step's
    ``capability_key`` (``None`` if the step can't be matched) and returns
    ``(approve, created)``: whether to approve the interaction, and any ids
    to fold into the dict this returns alongside the final plan detail.
    """
    detail = caliber.aria.wait_for_plan(plan_id, interval=0.01, max_interval=0.01, timeout=5)
    if detail.plan.needs_you:
        caliber.aria.approve_plan(plan_id)
        detail = caliber.aria.execute_plan(plan_id)
    results: dict[str, Any] = {}
    for interaction in caliber.aria.interactions(plan_id):
        step = next((s for s in detail.steps if s.step_id == interaction.step_id), None)
        approve, created = on_step(step.capability_key if step else None)
        detail = caliber.aria.answer(interaction.interaction_id, approved=approve)
        results.update(created)
    return detail, results
