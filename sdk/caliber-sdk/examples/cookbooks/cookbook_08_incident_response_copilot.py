"""Install Cookbook 08 and collect the incident-triage evidence surfaces."""

from __future__ import annotations

import json

from caliber_sdk import CaliberClient
from examples.cookbooks._helpers import configuration_blockers, env_client, get_recipe


def run(caliber: CaliberClient) -> dict[str, object]:
    recipe = get_recipe(caliber, "08")
    blockers = configuration_blockers(recipe)
    if blockers:
        return {"installed": None, "blocked_by": blockers}

    installed = caliber.cookbooks.install(
        "08",
        name="Cookbook 08 — Incident Response Copilot (SDK)",
        acknowledge_prerequisites=bool(recipe.prerequisites),
    )
    traces = caliber.observability.traces(limit=5, status="error")
    metrics = caliber.observability.metrics(window="24h")
    queue = caliber.review_queues.create(
        "incident-decision-review",
        questions=[
            {"name": "action_is_safe", "type": "pass_fail", "required": True},
            {"name": "rollback_needed", "type": "pass_fail", "required": True},
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
        "trace_ids": [trace.trace_id for trace in traces],
        "metrics": metrics,
        "queue_id": queue.queue_id,
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
