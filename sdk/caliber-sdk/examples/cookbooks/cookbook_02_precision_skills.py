"""Install Cookbook 02 and calibrate the packaged skill through the SDK."""

from __future__ import annotations

import json
from typing import Any

from caliber_sdk import CaliberClient
from examples.cookbooks._helpers import configuration_blockers, env_client, get_recipe


def run(caliber: CaliberClient) -> dict[str, Any]:
    recipe = get_recipe(caliber, "02")
    blockers = configuration_blockers(recipe)
    if blockers:
        return {"installed": None, "blocked_by": blockers}

    installed = caliber.cookbooks.install("02", name="Cookbook 02 — Precision Skills (SDK)")
    owner = caliber.me.get().user_id
    skill = caliber.skills.create(
        "support-tone-and-deflection",
        owner=owner,
        summary="Support tone guardrails",
        content="When answering {{ audience }}, stay concise and grounded in {{ policy_name }}.",
        tags=["cookbook-02", "support"],
    )
    rendered = caliber.skills.render(
        skill.skill_id,
        variables={
            "audience": "enterprise admins",
            "policy_name": "Refund Policy",
        },
    )
    selection = caliber.skills.test_selection(
        skill.skill_id,
        "Respond to an angry billing escalation",
    )
    calibration = caliber.raw.post(
        f"/skills/{skill.skill_id}/calibrate",
        json={
            "scenario_set": "cookbook-02",
            "metadata": {"workflow_id": installed["workflow"]["workflow_id"]},
        },
    )
    return {
        "installed": recipe.id,
        "skill_id": skill.skill_id,
        "rendered_word_count": rendered.word_count,
        "selection_score": selection.selection_score,
        "calibration": calibration,
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
