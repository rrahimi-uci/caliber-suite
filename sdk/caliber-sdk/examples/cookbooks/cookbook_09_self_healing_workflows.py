"""Install Cookbook 09 and run the reproduce -> retry -> recover loop."""

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
    recipe = get_recipe(caliber, "09")
    blockers = configuration_blockers(recipe)
    if blockers:
        return {"installed": None, "blocked_by": blockers}

    installed = caliber.cookbooks.install(
        "09",
        name="Cookbook 09 — Self-Healing Workflows (SDK)",
        acknowledge_prerequisites=bool(recipe.prerequisites),
    )
    published = caliber.workflows.versions.publish(installed["version"]["version_id"])
    refund_input = {"response": "Refund of $4,800 approved for account A-1007."}

    # Step 1: reproduce the failure -- reject the pending approval so attempt 1
    # ends `failed` (the recipe's own README: "the reliable reproducible
    # failure for the demo" is `hitl_review` driven through reject -> failed).
    first_attempt = caliber.workflows.runs.submit(
        workflow_version_id=published.version_id,
        input=refund_input,
        idempotency_key="cookbook-09-sdk-run",
    )
    first_attempt = _wait_until_paused_or_settled(caliber, first_attempt.workflow_run_id)
    if first_attempt.status == "waiting_approval":
        caliber.workflows.runs.reject(
            first_attempt.workflow_run_id,
            reason="Refund exceeds the auto-approval threshold without manager sign-off.",
        )
        first_attempt = _wait_until_settled(caliber, first_attempt.workflow_run_id)

    # Step 2: capture diagnostics on the failing run -- checkpoints + the
    # debugger's step trace.
    checkpoints = caliber.workflows.runs.checkpoints(first_attempt.workflow_run_id)
    trace = caliber.workflows.runs.trace(first_attempt.workflow_run_id)

    # Step 3: retry from the last checkpoint. The retry lineage links attempt 2
    # back to attempt 1 (`parent_run_id`), which is what "Attempt 2 of N" reads.
    retried = caliber.workflows.runs.retry(
        first_attempt.workflow_run_id,
        reason="Retry after reject -- same input, this time approve the refund.",
    )
    retried_run_id = retried["workflow_run_id"]
    lineage = caliber.workflows.runs.lineage(retried_run_id)

    # Step 4: this time approve + resume -- confirm the retried attempt
    # recovers to a terminal success instead of failing again.
    second_attempt = _wait_until_paused_or_settled(caliber, retried_run_id)
    if second_attempt.status == "waiting_approval":
        caliber.workflows.runs.approve(
            retried_run_id, reason="Refund and account state verified; approve the retry."
        )
        caliber.workflows.runs.resume(retried_run_id)
        second_attempt = _wait_until_settled(caliber, retried_run_id)

    # Step 5: generate a patch candidate from the failure evidence. This is a
    # real SDK capability the recipe's own UI does not expose (its README:
    # "Patching is manual ... no propose_workflow_patch tool"); `propose_patch`
    # only proposes -- nothing is applied automatically, so actually applying
    # the candidate is the same manual "edit + save a new version" step the
    # recipe describes, intentionally left out of this script.
    patch_candidate = caliber.workflows.versions.propose_patch(
        published.version_id,
        evidence={
            "category": "guardrail",
            "workflow_id": installed["workflow"]["workflow_id"],
            "workflow_version_id": published.version_id,
        },
    )

    return {
        "installed": recipe.id,
        "workflow_id": installed["workflow"]["workflow_id"],
        "version_id": published.version_id,
        "failing_run_id": first_attempt.workflow_run_id,
        "failing_status": first_attempt.status,
        "checkpoint_count": len(checkpoints) if isinstance(checkpoints, list) else None,
        "has_trace": trace is not None,
        "retried_run_id": retried_run_id,
        "lineage": lineage,
        "recovered_run_id": second_attempt.workflow_run_id,
        "recovered_status": second_attempt.status,
        "patch_id": patch_candidate.get("patch_id") if isinstance(patch_candidate, dict) else None,
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
