"""Install Cookbook 13 and drive the Aria plan to a real review queue."""

from __future__ import annotations

import json
from typing import Any

from caliber_sdk import CaliberClient
from examples.cookbooks._helpers import (
    configuration_blockers,
    drive_aria_plan,
    env_client,
    get_recipe,
)


def run(caliber: CaliberClient) -> dict[str, Any]:
    recipe = get_recipe(caliber, "13")
    blockers = configuration_blockers(recipe)
    if blockers:
        return {"installed": None, "blocked_by": blockers}

    installed = caliber.cookbooks.install(
        "13",
        name="Cookbook 13 — Aria Review Queue (SDK)",
        acknowledge_prerequisites=bool(recipe.prerequisites),
    )
    detail = caliber.aria.create_plan(
        "Set up a review queue for human labeling of agent replies for safety, citation, and tone."
    )

    def on_step(capability: str | None) -> tuple[bool, dict[str, Any]]:
        # Execution gap (see Cookbook 12): the plan alone does not create the
        # queue, so it is created here, right after approving that step.
        if capability == "review_queue.create":
            queue = caliber.review_queues.create(
                "agent-reply-review",
                questions=[
                    {
                        "key": "safety",
                        "title": "Is the reply safe?",
                        "type": "pass_fail",
                        "required": True,
                    },
                    {
                        "key": "citation_ok",
                        "title": "Are citations accurate?",
                        "type": "pass_fail",
                        "required": True,
                    },
                    {
                        "key": "tone_ok",
                        "title": "Is the tone appropriate?",
                        "type": "pass_fail",
                        "required": True,
                    },
                    {
                        "key": "reviewer_notes",
                        "title": "Reviewer notes",
                        "type": "text",
                        "required": False,
                    },
                ],
            )
            return True, {"queue_id": queue.queue_id}
        if capability == "review_queue.add_items":
            # No flagged traces yet -- deny per the recipe (queue-first,
            # enqueue-later); the queue is still complete without it.
            return False, {}
        return True, {}

    settled, created = drive_aria_plan(caliber, detail.plan.plan_id, on_step=on_step)
    return {
        "installed": recipe.id,
        "workflow_id": installed["workflow"]["workflow_id"],
        "plan_id": settled.plan.plan_id,
        "status": settled.plan.status,
        **created,
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
