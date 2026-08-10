"""Install Cookbook 03 and harden the deterministic decision tool."""

from __future__ import annotations

import json

from caliber_sdk import CaliberClient
from examples.cookbooks._helpers import configuration_blockers, env_client, get_recipe


def run(caliber: CaliberClient) -> dict[str, object]:
    recipe = get_recipe(caliber, "03")
    blockers = configuration_blockers(recipe)
    if blockers:
        return {"installed": None, "blocked_by": blockers}

    installed = caliber.cookbooks.install(
        "03",
        name="Cookbook 03 — Policy-Safe Decision Tool (SDK)",
        acknowledge_prerequisites=bool(recipe.prerequisites),
    )
    tool = caliber.tools.register(
        "lookup_refund_policy",
        version="1",
        module_path="caliber.workflows.demo_tools",
        callable_name="lookup_policy",
        input_schema={
            "type": "object",
            "required": ["amount"],
            "properties": {"amount": {"type": "number"}},
        },
        output_schema={"type": "object"},
        side_effect_level="read",
        allow_in_preview=True,
    )
    caliber.raw.put(
        f"/tools/{tool.tool_id}/test-cases",
        json={
            "test_cases": [
                {
                    "name": "small refund",
                    "input": {"amount": 45},
                    "expected_output": {"eligible": True},
                },
                {
                    "name": "large refund",
                    "input": {"amount": 1200},
                    "expected_output": {"eligible": False},
                },
            ]
        },
    )
    calibration = caliber.tools.calibrate(tool.tool_id, metadata={"cookbook_id": "03"})
    return {
        "installed": recipe.id,
        "workflow_id": installed["workflow"]["workflow_id"],
        "tool_id": tool.tool_id,
        "calibration_job_id": calibration.job_id,
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
