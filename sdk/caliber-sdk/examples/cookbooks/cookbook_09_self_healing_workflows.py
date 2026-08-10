"""Install Cookbook 09, publish the draft, and drive a failing run to triage."""

from __future__ import annotations

import json

from caliber_sdk import CaliberClient
from examples.cookbooks._helpers import configuration_blockers, env_client, get_recipe


def run(caliber: CaliberClient) -> dict[str, object]:
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
    run_record = caliber.workflows.runs.submit(
        workflow_version_id=published.version_id,
        input={"response": "Refund of $4,800 approved for account A-1007."},
        idempotency_key="cookbook-09-sdk-run",
    )
    settled = caliber.workflows.runs.wait(
        run_record.workflow_run_id,
        raise_on_failure=False,
        interval=0.01,
        max_interval=0.01,
        timeout=5,
    )
    return {
        "installed": recipe.id,
        "workflow_id": installed["workflow"]["workflow_id"],
        "version_id": published.version_id,
        "run_id": settled.workflow_run_id,
        "status": settled.status,
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
