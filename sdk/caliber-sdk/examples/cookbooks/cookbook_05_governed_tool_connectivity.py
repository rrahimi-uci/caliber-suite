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
    # `transport` is `streamable-http` (hyphen) and the URL field is `uri` --
    # both are validated by the request schema, so the wrong spelling/field
    # used to 422 against a real server despite matching a canned mock reply.
    server = caliber.mcp_servers.create(
        "github-governed",
        transport="streamable-http",
        uri="https://api.githubcopilot.com/mcp/",
        env={"GITHUB_PERSONAL_ACCESS_TOKEN": "${secret://github-pat}"},
    )
    connection = caliber.mcp_servers.test_connection(server.server_id)
    caliber.mcp_servers.discover_tools(server.server_id)

    # Prove the tool actually runs before locking it down -- the "governed"
    # half of this recipe is a before/after comparison, not just a static
    # policy rule with nothing behind it.
    allowed_call = caliber.mcp_servers.invoke_tool(
        server.server_id, "search_repositories", arguments={"query": "caliber-suite"}
    )
    caliber.mcp_servers.update_tool_policy(
        server.server_id,
        "issue_write",
        allowed=False,
        side_effect_level="write",
    )
    # A saved case is `input`, not `arguments` -- `CalibrationCase` (shared
    # with `tools.save_test_cases`) forbids extra fields.
    caliber.mcp_servers.save_test_cases(
        server.server_id,
        "search_repositories",
        [{"name": "find caliber", "input": {"query": "caliber-suite"}}],
    )
    # Re-invoke the now-blocked tool: the governed path returns a structured
    # refusal (`success: false`, `error` set) rather than raising, so policy
    # enforcement is itself part of the evidence, not just the absence of a
    # side effect.
    blocked_call = caliber.mcp_servers.invoke_tool(
        server.server_id, "issue_write", arguments={"title": "Cookbook 05 test issue"}
    )
    calibration = caliber.mcp_servers.calibrate_tool(server.server_id, "search_repositories")
    return {
        "installed": recipe.id,
        "workflow_id": installed["workflow"]["workflow_id"],
        "server_id": server.server_id,
        "connection": connection,
        "allowed_call": allowed_call,
        "blocked_call": blocked_call,
        "calibration": calibration,
    }


def main() -> None:
    with env_client() as caliber:
        print(json.dumps(run(caliber), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
