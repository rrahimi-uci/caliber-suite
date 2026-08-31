"""Install Cookbook 16 and turn production traces into triageable evidence."""

from __future__ import annotations

import json

from caliber_sdk import CaliberClient
from examples.cookbooks._helpers import configuration_blockers, env_client, get_recipe


def run(caliber: CaliberClient) -> dict[str, object]:
    recipe = get_recipe(caliber, "16")
    blockers = configuration_blockers(recipe)
    if blockers:
        return {"installed": None, "blocked_by": blockers}

    installed = caliber.cookbooks.install(
        "16",
        name="Cookbook 16 — Production Observability & Triage (SDK)",
        acknowledge_prerequisites=bool(recipe.prerequisites),
    )
    traces = caliber.observability.traces(limit=3, status="error")
    owner = caliber.me.get().user_id
    dataset = caliber.eval_datasets.create(
        "prod-regression-cases",
        owner=owner,
        description="Cookbook 16 regression evidence",
    )
    if traces:
        caliber.eval_datasets.add_from_trace(
            dataset.dataset_id,
            traces[0].trace_id,
            expected={"status": "resolved"},
        )
    queue = caliber.review_queues.create(
        "prod-triage",
        questions=[
            {"name": "root_cause_known", "type": "pass_fail", "required": True},
            {
                "name": "failure_mode",
                "type": "categorical",
                "options": ["prompt", "tool", "retrieval", "data", "infra"],
            },
        ],
    )
    if traces:
        caliber.review_queues.enqueue(
            queue.queue_id,
            trace_ids=[trace.trace_id for trace in traces],
        )
    return {
        "installed": recipe.id,
        "workflow_id": installed["workflow"]["workflow_id"],
        "dataset_id": dataset.dataset_id,
        "queue_id": queue.queue_id,
        "trace_ids": [trace.trace_id for trace in traces],
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
