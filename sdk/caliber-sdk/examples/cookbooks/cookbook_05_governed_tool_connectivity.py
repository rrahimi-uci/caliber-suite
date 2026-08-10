"""Install Cookbook 05 and govern the MCP integration path."""

from __future__ import annotations

import json

from caliber_sdk import CaliberClient
from examples.cookbooks._helpers import configuration_blockers, env_client, get_recipe


def run(caliber: CaliberClient) -> dict[str, object]:
    recipe = get_recipe(caliber, "05")
    blockers = configuration_blockers(recipe)
    if blockers:
        return {"installed": None, "blocked_by": blockers}

    installed = caliber.cookbooks.install(
        "05",
        name="Cookbook 05 — Governed Tool Connectivity (SDK)",
        acknowledge_prerequisites=bool(recipe.prerequisites),
    )
    server = caliber.mcp_servers.create(
        "github-governed",
        transport="streamable_http",
        url="https://api.githubcopilot.com/mcp/",
        env={"GITHUB_PERSONAL_ACCESS_TOKEN": "${secret://github-pat}"},
    )
    connection = caliber.mcp_servers.test_connection(server.server_id)
    caliber.mcp_servers.discover_tools(server.server_id)
    caliber.mcp_servers.update_tool_policy(
        server.server_id,
        "issue_write",
        allowed=False,
        side_effect_level="write",
    )
    caliber.mcp_servers.save_test_cases(
        server.server_id,
        "search_repositories",
        [{"name": "find caliber", "arguments": {"query": "caliber-suite"}}],
    )
    calibration = caliber.mcp_servers.calibrate_tool(server.server_id, "search_repositories")
    return {
        "installed": recipe.id,
        "workflow_id": installed["workflow"]["workflow_id"],
        "server_id": server.server_id,
        "connection": connection,
        "calibration": calibration,
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
