"""Submit a workflow run and wait for it, without a hand-rolled poll loop."""

from __future__ import annotations

from typing import Any

from caliber_sdk import CaliberClient, WorkflowRunFailed


def run_and_wait(
    caliber: CaliberClient, *, workflow_id: str, alias: str = "prod"
) -> dict[str, Any]:
    """Invoke a deployed workflow by alias and block until it stops.

    Targeting an alias rather than a version id is the point of deploying one:
    the caller does not change when a new version is promoted.
    """
    run = caliber.workflows.runs.submit(
        workflow_id=workflow_id,
        alias=alias,
        input={"question": "what is the refund policy?"},
        # Submission is the one mutating call the SDK will not retry for you,
        # because it cannot know whether a failure happened before or after the
        # run was created. An idempotency key makes your own retry safe.
        idempotency_key="example-run-1",
    )

    try:
        finished = caliber.workflows.runs.wait(run.workflow_run_id, timeout=300)
    except WorkflowRunFailed as failure:
        # Raised for a run that reached a terminal failure state. The run object
        # is attached, so the caller can report *why* rather than only that.
        return {"run_id": failure.run.workflow_run_id, "status": failure.run.status}

    return {
        "run_id": finished.workflow_run_id,
        "status": finished.status,
        "output": finished.output,
    }
