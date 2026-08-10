"""Install Cookbook 12 and drive the Aria plan from intent to execution."""

from __future__ import annotations

import json

from caliber_sdk import CaliberClient
from examples.cookbooks._helpers import configuration_blockers, env_client, get_recipe


def run(caliber: CaliberClient) -> dict[str, object]:
    recipe = get_recipe(caliber, "12")
    blockers = configuration_blockers(recipe)
    if blockers:
        return {"installed": None, "blocked_by": blockers}

    installed = caliber.cookbooks.install("12", name="Cookbook 12 — Aria Evaluation Harness (SDK)")
    detail = caliber.aria.create_plan("Create the evaluation harness for Cookbook 12")
    settled = caliber.aria.wait_for_plan(
        detail.plan.plan_id,
        interval=0.01,
        max_interval=0.01,
        timeout=5,
    )
    if settled.plan.needs_you:
        caliber.aria.approve_plan(settled.plan.plan_id)
        settled = caliber.aria.execute_plan(settled.plan.plan_id)
    return {
        "installed": recipe.id,
        "workflow_id": installed["workflow"]["workflow_id"],
        "plan_id": settled.plan.plan_id,
        "status": settled.plan.status,
        "steps": len(settled.steps),
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
