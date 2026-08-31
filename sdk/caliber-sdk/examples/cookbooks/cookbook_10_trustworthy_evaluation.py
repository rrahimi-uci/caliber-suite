"""Install Cookbook 10 and compute judge/human alignment on a reviewed trace."""

from __future__ import annotations

import json
from typing import Any

from caliber_sdk import CaliberClient
from examples.cookbooks._helpers import configuration_blockers, env_client, get_recipe


def run(caliber: CaliberClient) -> dict[str, Any]:
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
    dataset = caliber.eval_datasets.create(
        "evaluation-candidates", owner=owner, description="Cookbook 10 comparison set"
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
        questions=[
            {
                "key": "judge_is_correct",
                "title": "Did the judge verdict match your own read of the trace?",
                "type": "pass_fail",
                "required": True,
            }
        ],
    )

    # The recipe's defining mechanic: judge/human alignment (Cohen's kappa),
    # not just that a judge and a queue exist. Enqueue a real trace, answer it,
    # then compute how well the judge agrees with that human label.
    alignment = None
    traces = caliber.observability.traces(limit=1)
    if traces:
        items = caliber.review_queues.enqueue(queue.queue_id, trace_ids=[traces[0].trace_id])
        item_id = items[0]["item_id"]
        caliber.review_queues.submit(queue.queue_id, item_id, judge_is_correct=True)
        raw_examples = caliber.review_queues.alignment_examples(
            queue.queue_id, question_key="judge_is_correct"
        )
        # `alignment_examples()` rows carry a `provenance` audit trail
        # alongside the four fields `judges.alignment()` accepts; its request
        # schema forbids any extra key, so strip provenance before the call.
        scored_examples = [
            {key: example[key] for key in ("inputs", "outputs", "expectations", "label")}
            for example in raw_examples["examples"]
        ]
        if scored_examples:
            alignment = caliber.judges.alignment(judge.judge_id, examples=scored_examples)

    return {
        "installed": recipe.id,
        "workflow_id": installed["workflow"]["workflow_id"],
        "dataset_id": dataset.dataset_id,
        "judge_id": judge.judge_id,
        "evaluation_id": evaluation.evaluation_id,
        "queue_id": queue.queue_id,
        "alignment_kappa": alignment.kappa if alignment else None,
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
