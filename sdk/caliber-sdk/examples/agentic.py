"""Drive an Aria plan and a cookbook install — the agentic surfaces.

Both are beta. Both share a property worth designing around: they stop for a
person. A script that treats "paused" as transient waits forever.
"""

from __future__ import annotations

from typing import Any

from caliber_sdk import CaliberClient


def plan_from_intent(caliber: CaliberClient, goal: str) -> dict[str, Any]:
    """State an intent, wait for Aria to plan it, and approve if it asks.

    ``wait_for_plan`` returns as soon as the plan pauses, because a paused plan
    makes no further progress on its own — polling past it would burn the whole
    timeout on the expected outcome.
    """
    plan = caliber.aria.create_plan(goal)
    settled = caliber.aria.wait_for_plan(plan.plan_id, timeout=120)

    if settled.needs_you:
        # The plan is waiting on a human decision. Approving is that decision,
        # made explicitly rather than inferred from the script continuing.
        caliber.aria.approve_plan(settled.plan_id)
        settled = caliber.aria.execute_plan(settled.plan_id)

    return {"plan_id": settled.plan_id, "status": settled.status, "steps": settled.step_count}


def install_ready_cookbook(caliber: CaliberClient) -> dict[str, Any]:
    """Install the first cookbook whose prerequisites are already satisfied.

    Readiness is checked before installing rather than after failing: the
    recipe's unmet checks name what is missing, and each one that can be fixed
    carries the route that fixes it.
    """
    recipes = caliber.cookbooks.list()
    ready = [recipe for recipe in recipes if recipe.is_ready]
    if not ready:
        blocked = {
            recipe.id: [check.get("label") for check in recipe.unmet_checks] for recipe in recipes
        }
        return {"installed": None, "blocked_by": blocked}

    recipe = ready[0]
    result = caliber.cookbooks.install(recipe.id, name=f"{recipe.title} (SDK)")
    # Installed paused, never running: an example manifest can carry model,
    # connector, or side-effect bindings an operator should review first.
    workflow = result.get("workflow") if isinstance(result, dict) else None
    return {
        "installed": recipe.id,
        "workflow_status": (workflow or {}).get("status"),
    }
