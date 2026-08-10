"""Install Cookbook 13 and create the review queue through the Aria loop."""

from __future__ import annotations

import json

from caliber_sdk import CaliberClient
from examples.cookbooks._helpers import configuration_blockers, env_client, get_recipe


def run(caliber: CaliberClient) -> dict[str, object]:
    recipe = get_recipe(caliber, "13")
    blockers = configuration_blockers(recipe)
    if blockers:
        return {"installed": None, "blocked_by": blockers}

    installed = caliber.cookbooks.install(
        "13",
        name="Cookbook 13 — Aria Review Queue (SDK)",
        acknowledge_prerequisites=bool(recipe.prerequisites),
    )
    detail = caliber.aria.create_plan("Create the human-review queue for Cookbook 13")
    settled = caliber.aria.wait_for_plan(
        detail.plan.plan_id,
        interval=0.01,
        max_interval=0.01,
        timeout=5,
    )
    if settled.plan.needs_you:
        caliber.aria.approve_plan(settled.plan.plan_id)
        settled = caliber.aria.execute_plan(settled.plan.plan_id)
    queues = caliber.review_queues.list()
    return {
        "installed": recipe.id,
        "workflow_id": installed["workflow"]["workflow_id"],
        "plan_id": settled.plan.plan_id,
        "status": settled.plan.status,
        "queues": [queue.queue_id for queue in queues],
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
