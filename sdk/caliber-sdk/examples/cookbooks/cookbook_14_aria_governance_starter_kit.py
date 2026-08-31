"""Install Cookbook 14 and drive the Aria plan to a whole governance kit."""

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
    recipe = get_recipe(caliber, "14")
    blockers = configuration_blockers(recipe)
    if blockers:
        return {"installed": None, "blocked_by": blockers}

    installed = caliber.cookbooks.install(
        "14",
        name="Cookbook 14 — Aria Governance Starter Kit (SDK)",
    )
    owner = caliber.me.get().user_id
    detail = caliber.aria.create_plan(
        "Stand up our governance starter kit: a judge for answer faithfulness, "
        "an eval dataset to score against, and a review queue for human checks."
    )

    def on_step(capability: str | None) -> tuple[bool, dict[str, Any]]:
        # Execution gap (see Cookbook 12): each of the three artifacts is
        # created here, right after approving the step that asked for it.
        if capability == "judge.create":
            judge = caliber.judges.create(
                "AnswerFaithfulness",
                instructions=(
                    "Return true when {{ outputs }} is faithful to "
                    "{{ expectations }} for {{ inputs }}."
                ),
                feedback_value_type="bool",
            )
            return True, {"judge_id": judge.judge_id}
        if capability == "eval_dataset.create":
            dataset = caliber.eval_datasets.create(
                "release-candidates-eval",
                owner=owner,
                description="Cookbook 14 governance eval set",
            )
            return True, {"dataset_id": dataset.dataset_id}
        if capability == "review_queue.create":
            queue = caliber.review_queues.create(
                "governance-review",
                questions=[
                    {
                        "key": "faithful",
                        "title": "Is the reply faithful to its sources?",
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
                        "key": "tone",
                        "title": "Is the tone appropriate?",
                        "type": "pass_fail",
                        "required": True,
                    },
                    {
                        "key": "notes",
                        "title": "Reviewer notes",
                        "type": "text",
                        "required": False,
                    },
                ],
            )
            return True, {"queue_id": queue.queue_id}
        if capability == "review_queue.add_items":
            # No traces yet -- deny; the kit is complete without it.
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
