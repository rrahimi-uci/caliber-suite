"""Install Cookbook 15 and drive Aria's triage-then-recalibrate remediation loop."""

from __future__ import annotations

import json
from typing import Any

from caliber_sdk import CaliberClient, wait_for
from examples.cookbooks._helpers import (
    configuration_blockers,
    drive_aria_plan,
    env_client,
    get_recipe,
)


def run(caliber: CaliberClient) -> dict[str, Any]:
    recipe = get_recipe(caliber, "15")
    blockers = configuration_blockers(recipe)
    if blockers:
        return {"installed": None, "blocked_by": blockers}

    installed = caliber.cookbooks.install(
        "15",
        name="Cookbook 15 — Aria Triage & Recalibrate Loop (SDK)",
        acknowledge_prerequisites=bool(recipe.prerequisites),
    )

    # This is a *remediation* loop: it operates on a workflow that already
    # exists (the recipe's own README: "Aria does not create the subject
    # workflow or its traces -- build that in SCN-07 first"). Reuse this
    # recipe's own installed workflow as the subject instead, so the example
    # is self-contained rather than depending on Cookbook 07 having run
    # first. Against a real deployment, ``agent_id`` must be a real agent
    # already bound to the target workflow -- see the recipe's own
    # ``assets/calibrate.json``, which documents both ids as placeholders for
    # exactly this reason.
    workflow_id = installed["workflow"]["workflow_id"]
    agent_id = workflow_id
    trace_ids = [trace.trace_id for trace in caliber.observability.traces(limit=3, status="error")]

    detail = caliber.aria.create_plan(
        "Our workflow's recent runs look weak -- set up a review queue for the "
        "flagged traces and kick off a workflow calibration."
    )

    state: dict[str, Any] = {}

    def on_step(capability: str | None) -> tuple[bool, dict[str, Any]]:
        # Execution gap (see Cookbook 12): the queue, the enqueue, and the
        # calibration job are each driven here, right after approving the
        # step that asked for them.
        if capability == "review_queue.create":
            queue = caliber.review_queues.create(
                "weak-runs-triage",
                questions=[
                    {
                        "key": "root_cause_known",
                        "title": "Is the root cause known?",
                        "type": "pass_fail",
                        "required": True,
                    },
                    {
                        "key": "needs_recalibration",
                        "title": "Does this need recalibration?",
                        "type": "pass_fail",
                        "required": True,
                    },
                ],
            )
            state["queue_id"] = queue.queue_id
            return True, {"queue_id": queue.queue_id}
        if capability == "review_queue.add_items":
            if not trace_ids:
                return False, {}
            caliber.review_queues.enqueue(state["queue_id"], trace_ids=trace_ids)
            return True, {"enqueued_trace_ids": trace_ids}
        if capability == "workflow.calibrate":
            job = caliber.workflows.create_calibration_run(workflow_id, agent_id=agent_id)
            state["calibration_job_id"] = job.get("job_id") if isinstance(job, dict) else None
            return True, {"calibration_job_id": state["calibration_job_id"]}
        return True, {}

    settled, created = drive_aria_plan(caliber, detail.plan.plan_id, on_step=on_step)

    # `workflow.calibrate` is Aria's first ASYNC capability: the step parks in
    # `waiting_job` and the plan stays `running` (it does not pause for a
    # human) until the refinement job resolves. Poll until it does rather
    # than treating `running` as a finished state.
    if settled.plan.status == "running":
        settled = wait_for(
            lambda: caliber.aria.poll_plan(settled.plan.plan_id),
            is_done=lambda d: (
                d.plan.status in {"completed", "succeeded", "failed", "cancelled", "rejected"}
            ),
            interval=0.01,
            max_interval=0.01,
            timeout=5,
        )

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
