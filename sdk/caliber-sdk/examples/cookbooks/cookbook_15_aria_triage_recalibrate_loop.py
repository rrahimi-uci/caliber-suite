"""Install Cookbook 15 and inspect the review queues and jobs Aria creates."""

from __future__ import annotations

import json

from caliber_sdk import CaliberClient
from examples.cookbooks._helpers import configuration_blockers, env_client, get_recipe


def run(caliber: CaliberClient) -> dict[str, object]:
    recipe = get_recipe(caliber, "15")
    blockers = configuration_blockers(recipe)
    if blockers:
        return {"installed": None, "blocked_by": blockers}

    installed = caliber.cookbooks.install(
        "15",
        name="Cookbook 15 — Aria Triage & Recalibrate Loop (SDK)",
        acknowledge_prerequisites=bool(recipe.prerequisites),
    )
    detail = caliber.aria.create_plan(
        "Triage the flagged traces and launch recalibration for Cookbook 15",
    )
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
    jobs = caliber.jobs.list()
    return {
        "installed": recipe.id,
        "workflow_id": installed["workflow"]["workflow_id"],
        "plan_id": settled.plan.plan_id,
        "status": settled.plan.status,
        "queue_ids": [queue.queue_id for queue in queues],
        "job_ids": [job.job_id for job in jobs],
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
