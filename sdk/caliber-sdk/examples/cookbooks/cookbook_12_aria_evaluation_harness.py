"""Install Cookbook 12 and drive the Aria plan from intent to real artifacts."""

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
    recipe = get_recipe(caliber, "12")
    blockers = configuration_blockers(recipe)
    if blockers:
        return {"installed": None, "blocked_by": blockers}

    installed = caliber.cookbooks.install("12", name="Cookbook 12 — Aria Evaluation Harness (SDK)")
    owner = caliber.me.get().user_id
    detail = caliber.aria.create_plan(
        "Create a judge for answer faithfulness and an eval dataset to run it on."
    )

    def on_step(capability: str | None) -> tuple[bool, dict[str, Any]]:
        # The shipped HeuristicPlanner leaves every step's inputs empty (the
        # recipe's own README, "Execution gap (verified)"), so approving the
        # interaction alone would not create anything: the real artifact for
        # each step is created here, right after answering it.
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
                "support-faithfulness-eval",
                owner=owner,
                description="Cookbook 12 faithfulness eval set",
            )
            return True, {"dataset_id": dataset.dataset_id}
        return True, {}

    settled, created = drive_aria_plan(caliber, detail.plan.plan_id, on_step=on_step)
    return {
        "installed": recipe.id,
        "workflow_id": installed["workflow"]["workflow_id"],
        "plan_id": settled.plan.plan_id,
        "status": settled.plan.status,
        "steps": len(settled.steps),
        **created,
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
