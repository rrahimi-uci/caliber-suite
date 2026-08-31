"""Install Cookbook 06 and build the knowledge-base side of the scenario."""

from __future__ import annotations

import json
from typing import Any

from caliber_sdk import CaliberClient
from examples.cookbooks._helpers import configuration_blockers, env_client, get_recipe


def run(caliber: CaliberClient) -> dict[str, Any]:
    recipe = get_recipe(caliber, "06")
    blockers = configuration_blockers(recipe)
    if blockers:
        return {"installed": None, "blocked_by": blockers}

    installed = caliber.cookbooks.install(
        "06",
        name="Cookbook 06 — Grounded Knowledge Assistant (SDK)",
        acknowledge_prerequisites=bool(recipe.prerequisites),
    )

    # `create()`/`create_version()` require a chunking strategy and an
    # embedding model; both are deployment-specific catalog entries (not
    # free-form strings), so read the real choices from `options()` rather
    # than guessing an id that may not exist on this deployment.
    options = caliber.knowledge_bases.options()
    chunking_strategy = options["chunking_strategies"][0]["id"]
    embedding_model = options["embedding_models"][0]["id"]

    kb = caliber.knowledge_bases.create(
        "support-policy-kb",
        description="Cookbook 06 policy corpus",
        source_bucket="cookbook-06-corpus",
        sources=[{"kind": "file", "path": "policy-handbook.md"}],
        chunking_strategy=chunking_strategy,
        embedding_model=embedding_model,
    )
    version = caliber.knowledge_bases.create_version(
        kb.knowledge_base_id,
        sources=[{"kind": "file", "path": "policy-handbook.md"}],
        chunking_strategy=chunking_strategy,
        embedding_model=embedding_model,
    )
    version_id = version["version_id"]

    # `query()` is versioned (`version_ids`), not knowledge-base-scoped -- a
    # knowledge base has no single "current" version to query implicitly.
    answer = caliber.knowledge_bases.query(
        version_ids=[version_id],
        question="What is the escalation policy for billing disputes?",
        top_k=3,
    )

    # `calibrate()` scores the version against a real eval dataset, not just
    # a version id -- the dataset is the answer key `calibrate` grades against.
    owner = caliber.me.get().user_id
    eval_dataset = caliber.eval_datasets.create(
        "support-policy-kb-eval", owner=owner, description="Cookbook 06 retrieval QA set"
    )
    caliber.eval_datasets.add_example(
        eval_dataset.dataset_id,
        input={"question": "What is the escalation policy for billing disputes?"},
        expected={"contains": "escalation"},
    )
    calibration = caliber.knowledge_bases.calibrate(
        kb.knowledge_base_id,
        version_id=version_id,
        eval_dataset_id=eval_dataset.dataset_id,
    )
    return {
        "installed": recipe.id,
        "workflow_id": installed["workflow"]["workflow_id"],
        "knowledge_base_id": kb.knowledge_base_id,
        "version_id": version_id,
        "eval_dataset_id": eval_dataset.dataset_id,
        "answer": answer,
        "calibration": calibration,
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
