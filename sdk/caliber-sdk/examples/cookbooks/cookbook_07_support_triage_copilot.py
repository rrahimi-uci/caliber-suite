"""Install Cookbook 07 and create the evaluation and review assets it needs."""

from __future__ import annotations

import json

from caliber_sdk import CaliberClient
from examples.cookbooks._helpers import configuration_blockers, env_client, get_recipe


def run(caliber: CaliberClient) -> dict[str, object]:
    recipe = get_recipe(caliber, "07")
    blockers = configuration_blockers(recipe)
    if blockers:
        return {"installed": None, "blocked_by": blockers}

    installed = caliber.cookbooks.install(
        "07",
        name="Cookbook 07 — Support Triage Copilot (SDK)",
        acknowledge_prerequisites=bool(recipe.prerequisites),
    )
    owner = caliber.me.get().user_id
    queue = caliber.review_queues.create(
        "support-risk-review",
        questions=[
            {"name": "is_high_risk", "type": "pass_fail", "required": True},
            {"name": "escalation_notes", "type": "text", "required": False},
        ],
    )
    dataset = caliber.datasets.create(
        "support-ticket-cases",
        owner=owner,
        description="Cookbook 07 evaluation set",
    )
    judge = caliber.judges.create(
        "GroundedSupportReply",
        instructions=(
            "Return true when {{ outputs }} is grounded in {{ expectations }} for {{ inputs }}."
        ),
        feedback_value_type="bool",
    )
    evaluation = caliber.evaluations.create(dataset.dataset_id, scorers=[f"Judge.{judge.judge_id}"])
    return {
        "installed": recipe.id,
        "workflow_id": installed["workflow"]["workflow_id"],
        "queue_id": queue.queue_id,
        "dataset_id": dataset.dataset_id,
        "judge_id": judge.judge_id,
        "evaluation_id": evaluation.evaluation_id,
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
