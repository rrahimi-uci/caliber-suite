"""Install Cookbook 11 and drive the release-candidate lifecycle via the SDK."""

from __future__ import annotations

import json

from caliber_sdk import CaliberClient
from examples.cookbooks._helpers import configuration_blockers, env_client, get_recipe


def run(caliber: CaliberClient) -> dict[str, object]:
    recipe = get_recipe(caliber, "11")
    blockers = configuration_blockers(recipe)
    if blockers:
        return {"installed": None, "blocked_by": blockers}

    installed = caliber.cookbooks.install(
        "11",
        name="Cookbook 11 — Release Signoff Factory (SDK)",
        acknowledge_prerequisites=bool(recipe.prerequisites),
    )
    # `ReleaseCandidateCreateRequest`/`ReleaseCriterion` forbid extra fields:
    # the version reference is `version_ref` (not `artifact_version`), and
    # each criterion needs a `title` plus `score` (not `observed_score`) --
    # this call used to 422 against a real server on its very first
    # mutating step despite matching a canned mocked reply.
    candidate = caliber.releases.create_candidate(
        "support-copilot-2026-08-11",
        artifact_type="workflow",
        artifact_ref=installed["workflow"]["workflow_id"],
        version_ref=installed["version"]["version_id"],
        required_score=0.9,
        planned_action={"action": "publish"},
        rollback_target={"workflow_version_id": installed["version"]["version_id"]},
        criteria=[
            {
                "key": "workflow_readiness",
                "title": "Workflow readiness",
                "weight": 0.4,
                "score": 0.92,
                "threshold": 0.9,
                "blocking": True,
            },
            {
                "key": "review_coverage",
                "title": "Review coverage",
                "weight": 0.3,
                "score": 0.95,
                "threshold": 0.8,
                "blocking": True,
            },
        ],
    )
    reevaluated = caliber.releases.evaluate(candidate.candidate_id)
    report = caliber.releases.generate_report(candidate.candidate_id, format="allure")
    signoff = caliber.releases.sign(
        candidate.candidate_id,
        decision="go",
        rationale="All release gates are satisfied.",
    )
    return {
        "installed": recipe.id,
        "workflow_id": installed["workflow"]["workflow_id"],
        "candidate_id": candidate.candidate_id,
        "score": reevaluated.weighted_score,
        "report": report,
        "signoff": signoff,
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
