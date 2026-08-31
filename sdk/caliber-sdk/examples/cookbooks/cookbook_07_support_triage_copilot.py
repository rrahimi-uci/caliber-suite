"""Install Cookbook 07 and drive the approval-gated support triage loop."""

from __future__ import annotations

import json
from typing import Any

from caliber_sdk import CaliberClient, wait_for
from examples.cookbooks._helpers import (
    PAUSED_OR_TERMINAL_RUN_STATES,
    configuration_blockers,
    env_client,
    get_recipe,
)


def _wait_until_paused_or_settled(caliber: CaliberClient, run_id: str) -> Any:
    return wait_for(
        lambda: caliber.workflows.runs.get(run_id),
        is_done=lambda run: run.status in PAUSED_OR_TERMINAL_RUN_STATES,
        interval=0.01,
        max_interval=0.01,
        timeout=5,
    )


def _wait_until_settled(caliber: CaliberClient, run_id: str) -> Any:
    return wait_for(
        lambda: caliber.workflows.runs.get(run_id),
        is_done=lambda run: run.is_terminal,
        interval=0.01,
        max_interval=0.01,
        timeout=5,
    )


def run(caliber: CaliberClient) -> dict[str, Any]:
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

    # The evaluation and review assets this loop reports through.
    queue = caliber.review_queues.create(
        "support-risk-review",
        questions=[
            {
                "key": "is_high_risk",
                "title": "Is this a high-risk action?",
                "type": "pass_fail",
                "required": True,
            },
            {
                "key": "escalation_notes",
                "title": "Escalation notes",
                "type": "text",
                "required": False,
            },
        ],
    )
    dataset = caliber.eval_datasets.create(
        "support-ticket-cases", owner=owner, description="Cookbook 07 evaluation set"
    )
    judge = caliber.judges.create(
        "GroundedSupportReply",
        instructions=(
            "Return true when {{ outputs }} is grounded in {{ expectations }} for {{ inputs }}."
        ),
        feedback_value_type="bool",
    )
    evaluation = caliber.evaluations.create(dataset.dataset_id, scorers=[f"Judge.{judge.judge_id}"])

    # Publish the installed draft so it can actually run, then drive the two
    # safety branches the recipe is named for: an approved bug-escalation
    # write, and a rejected one that must never reach the external write node.
    published = caliber.workflows.versions.publish(installed["version"]["version_id"])
    escalation_input = {
        "ticket_text": "Please file bug now and refund immediately",
        "channel": "chat",
        "account_id": "acct_9999",
    }

    approved_run = caliber.workflows.runs.submit(
        workflow_version_id=published.version_id,
        input=escalation_input,
        idempotency_key="cookbook-07-sdk-approve",
    )
    approved_run = _wait_until_paused_or_settled(caliber, approved_run.workflow_run_id)
    if approved_run.status == "waiting_approval":
        caliber.workflows.runs.approve(
            approved_run.workflow_run_id,
            reason="Refund and bug filing verified against policy.",
        )
        caliber.workflows.runs.resume(approved_run.workflow_run_id)
        approved_run = _wait_until_settled(caliber, approved_run.workflow_run_id)

    rejected_run = caliber.workflows.runs.submit(
        workflow_version_id=published.version_id,
        input=escalation_input,
        idempotency_key="cookbook-07-sdk-reject",
    )
    rejected_run = _wait_until_paused_or_settled(caliber, rejected_run.workflow_run_id)
    if rejected_run.status == "waiting_approval":
        caliber.workflows.runs.reject(
            rejected_run.workflow_run_id,
            reason="Refund amount exceeds policy without manager sign-off.",
        )
        rejected_run = _wait_until_settled(caliber, rejected_run.workflow_run_id)

    return {
        "installed": recipe.id,
        "workflow_id": installed["workflow"]["workflow_id"],
        "queue_id": queue.queue_id,
        "dataset_id": dataset.dataset_id,
        "judge_id": judge.judge_id,
        "evaluation_id": evaluation.evaluation_id,
        "approved_run_id": approved_run.workflow_run_id,
        "approved_status": approved_run.status,
        "rejected_run_id": rejected_run.workflow_run_id,
        "rejected_status": rejected_run.status,
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
