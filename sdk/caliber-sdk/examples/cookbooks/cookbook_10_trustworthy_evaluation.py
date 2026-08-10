"""Install Cookbook 10 and assemble the evaluation and review assets."""

from __future__ import annotations

import json

from caliber_sdk import CaliberClient
from examples.cookbooks._helpers import configuration_blockers, env_client, get_recipe


def run(caliber: CaliberClient) -> dict[str, object]:
    recipe = get_recipe(caliber, "10")
    blockers = configuration_blockers(recipe)
    if blockers:
        return {"installed": None, "blocked_by": blockers}

    installed = caliber.cookbooks.install(
        "10",
        name="Cookbook 10 — Trustworthy Evaluation (SDK)",
        acknowledge_prerequisites=bool(recipe.prerequisites),
    )
    owner = caliber.me.get().user_id
    dataset = caliber.datasets.create(
        "evaluation-candidates",
        owner=owner,
        description="Cookbook 10 comparison set",
    )
    judge = caliber.judges.create(
        "AnswerFaithfulness",
        instructions=(
            "Return true when {{ outputs }} is faithful to {{ expectations }} for {{ inputs }}."
        ),
        feedback_value_type="bool",
    )
    evaluation = caliber.evaluations.create(
        dataset.dataset_id,
        scorers=["non_empty", f"Judge.{judge.judge_id}"],
    )
    queue = caliber.review_queues.create(
        "evaluation-disagreements",
        questions=[{"name": "judge_is_correct", "type": "pass_fail", "required": True}],
    )
    return {
        "installed": recipe.id,
        "workflow_id": installed["workflow"]["workflow_id"],
        "dataset_id": dataset.dataset_id,
        "judge_id": judge.judge_id,
        "evaluation_id": evaluation.evaluation_id,
        "queue_id": queue.queue_id,
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
