"""Install Cookbook 06 and build the knowledge-base side of the scenario."""

from __future__ import annotations

import json

from caliber_sdk import CaliberClient
from examples.cookbooks._helpers import configuration_blockers, env_client, get_recipe


def run(caliber: CaliberClient) -> dict[str, object]:
    recipe = get_recipe(caliber, "06")
    blockers = configuration_blockers(recipe)
    if blockers:
        return {"installed": None, "blocked_by": blockers}

    installed = caliber.cookbooks.install(
        "06",
        name="Cookbook 06 — Grounded Knowledge Assistant (SDK)",
        acknowledge_prerequisites=bool(recipe.prerequisites),
    )
    kb = caliber.knowledge_bases.create(
        "support-policy-kb",
        description="Cookbook 06 policy corpus",
    )
    version = caliber.knowledge_bases.create_version(
        kb.knowledge_base_id,
        name="v1",
        sources=[{"uri": "s3://cookbooks/policy-handbook.md", "kind": "markdown"}],
    )
    answer = caliber.knowledge_bases.query(
        knowledge_base_id=kb.knowledge_base_id,
        question="What is the escalation policy for billing disputes?",
        top_k=3,
    )
    calibration = caliber.knowledge_bases.calibrate(
        kb.knowledge_base_id,
        version_id=version["version_id"],
    )
    return {
        "installed": recipe.id,
        "workflow_id": installed["workflow"]["workflow_id"],
        "knowledge_base_id": kb.knowledge_base_id,
        "version_id": version["version_id"],
        "answer": answer,
        "calibration": calibration,
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
