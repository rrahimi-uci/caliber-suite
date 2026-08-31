"""Install Cookbook 08 and collect the incident-triage evidence surfaces."""

from __future__ import annotations

import json
import time

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
    # `metrics()` reads `experiment_id`/`since_ms`, not `window` -- an unknown
    # query param is silently ignored (a GET, not schema-validated), so
    # `window="24h"` used to look like a real filter while doing nothing.
    since_ms = int((time.time() - 24 * 60 * 60) * 1000)
    metrics = caliber.observability.metrics(since_ms=since_ms)
    queue = caliber.review_queues.create(
        "incident-decision-review",
        questions=[
            {
                "key": "action_is_safe",
                "title": "Is the proposed action safe?",
                "type": "pass_fail",
                "required": True,
            },
            {
                "key": "rollback_needed",
                "title": "Is a rollback needed?",
                "type": "pass_fail",
                "required": True,
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
        "trace_ids": [trace.trace_id for trace in traces],
        "metrics": metrics,
        "queue_id": queue.queue_id,
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
