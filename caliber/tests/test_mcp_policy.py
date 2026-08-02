"""MCP containment, central policy, and deployment-preflight contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from caliber import mcp_policy
from caliber.config import CaliberConfig
from caliber.mcp_policy import (
    McpPolicyError,
    deployment_blockers,
    execution_readiness,
    extract_dependencies,
    validate_tool_access,
)


def _config(**environ: str) -> CaliberConfig:
    return CaliberConfig.load(environ=environ)


def _server(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "server_id": "MCP-db",
        "transport": "stdio",
        "uri": "",
        "command": "${PYTHON}",
        "args": ("-m", "caliber.mcp_servers.db", "--mode", "relational"),
        "env": {},
        "status": "active",
        "discovered_tools": ({"name": "run_query"},),
        "tool_policies": {
            "run_query": {
                "allowed": True,
                "side_effect_level": "read",
                "requires_approval": False,
            }
        },
        "last_connected_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_default_stdio_profile_is_contained_but_not_a_production_sandbox() -> None:
    profile = execution_readiness(_server(), config=_config())
    assert profile.ready is True
    assert profile.boundary == "local_containment"
    assert profile.production_isolated is False
    assert "private_working_directory" in profile.controls
    assert any("does not block filesystem or network" in item for item in profile.warnings)


def test_stdio_rejects_unlisted_command_python_code_and_process_env() -> None:
    unlisted = execution_readiness(_server(command="npx", args=("server",)), config=_config())
    assert unlisted.ready is False
    assert unlisted.command_allowed is False

    inline = execution_readiness(_server(args=("-c", "print('unsafe')")), config=_config())
    assert any("allowlisted '-m' module" in item for item in inline.blockers)

    poisoned = execution_readiness(_server(env={"PYTHONPATH": "/tmp/inject"}), config=_config())
    assert any("PYTHONPATH" in item for item in poisoned.blockers)


def test_remote_transport_requires_exact_host_and_secure_non_loopback_http() -> None:
    server = _server(
        transport="streamable-http",
        uri="https://mcp.example.test/rpc",
        command="",
        args=(),
    )
    blocked = execution_readiness(server, config=_config())
    assert blocked.remote_host_allowed is False
    allowed = execution_readiness(
        server,
        config=_config(CALIBER_MCP_REMOTE_HOST_ALLOWLIST="mcp.example.test"),
    )
    assert allowed.ready is True
    assert allowed.boundary == "remote_https"
    assert allowed.production_isolated is True

    insecure = execution_readiness(
        _server(transport="sse", uri="http://mcp.example.test/events", command="", args=()),
        config=_config(CALIBER_MCP_REMOTE_HOST_ALLOWLIST="mcp.example.test"),
    )
    assert any("plain HTTP is disabled" in item for item in insecure.blockers)

    oauth = execution_readiness(
        _server(
            transport="streamable-http",
            uri="https://mcp.example.test/rpc",
            command="",
            args=(),
            auth_type="oauth",
        ),
        config=_config(CALIBER_MCP_REMOTE_HOST_ALLOWLIST="mcp.example.test"),
    )
    assert any("OAuth MCP authentication is not implemented" in item for item in oauth.blockers)


def test_shipped_managed_sidecar_is_an_explicit_operator_attested_boundary() -> None:
    server = _server(
        transport="streamable-http",
        uri="http://mcp-db-relational:8101/mcp",
        command="",
        args=(),
    )
    ordinary = execution_readiness(
        server,
        config=_config(CALIBER_MCP_REMOTE_HOST_ALLOWLIST="mcp-db-relational"),
    )
    assert ordinary.production_isolated is False
    assert any("plain HTTP is disabled" in item for item in ordinary.blockers)

    managed = execution_readiness(
        server,
        config=_config(
            CALIBER_MCP_REMOTE_HOST_ALLOWLIST="mcp-db-relational",
            CALIBER_MCP_MANAGED_SIDECAR_HOSTS="mcp-db-relational",
        ),
    )
    assert managed.ready is True
    assert managed.boundary == "managed_sidecar"
    assert managed.production_isolated is True
    assert any("deployment operator" in item for item in managed.warnings)


def test_harmless_wrapper_is_not_misrepresented_as_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_policy.shutil, "which", lambda command: f"/usr/bin/{command}")
    profile = execution_readiness(
        _server(),
        config=_config(CALIBER_MCP_STDIO_ISOLATION_PREFIX="env"),
    )
    assert profile.boundary == "external_wrapper"
    assert profile.production_isolated is False
    assert any("not a verified isolation profile" in item for item in profile.warnings)


def test_recognized_bubblewrap_profile_requires_mandatory_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(mcp_policy.shutil, "which", lambda command: f"/usr/bin/{command}")
    profile = execution_readiness(
        _server(),
        config=_config(
            CALIBER_MCP_STDIO_ISOLATION_PROFILE="bubblewrap",
            CALIBER_MCP_STDIO_ISOLATION_PREFIX=(
                "bwrap --unshare-all --die-with-parent --new-session "
                "--ro-bind / / --proc /proc --dev /dev --tmpfs /tmp --chdir /tmp"
            ),
        ),
    )
    assert profile.boundary == "bubblewrap"
    assert profile.production_isolated is False
    assert "read_only_host_root" in profile.controls

    weakened = execution_readiness(
        _server(),
        config=_config(
            CALIBER_MCP_STDIO_ISOLATION_PROFILE="bubblewrap",
            CALIBER_MCP_STDIO_ISOLATION_PREFIX=(
                "bwrap --unshare-all --die-with-parent --new-session "
                "--ro-bind / / --proc /proc --dev /dev --tmpfs /tmp --chdir /tmp "
                "--share-net"
            ),
        ),
    )
    assert weakened.production_isolated is False


def test_tool_access_is_fail_closed_and_rate_limited() -> None:
    with pytest.raises(McpPolicyError, match="not in the discovered tool allowlist"):
        validate_tool_access(_server(), "delete_everything")
    with pytest.raises(McpPolicyError, match="no explicit allow"):
        validate_tool_access(_server(tool_policies={}), "run_query")
    with pytest.raises(McpPolicyError, match="blocked by policy"):
        validate_tool_access(
            _server(
                tool_policies={
                    "run_query": {
                        "allowed": False,
                        "side_effect_level": "read",
                        "requires_approval": False,
                    }
                }
            ),
            "run_query",
        )

    mcp_policy._reset_rate_limits_for_tests()
    limited = _server(
        tool_policies={
            "run_query": {
                "allowed": True,
                "side_effect_level": "read",
                "requires_approval": False,
                "rate_limit_per_minute": 1,
            }
        }
    )
    validate_tool_access(limited, "run_query")
    with pytest.raises(McpPolicyError, match="process-local limit"):
        validate_tool_access(limited, "run_query")


def test_extract_dependencies_supports_canonical_mapping_and_legacy_list_nodes() -> None:
    manifest = {
        "tools": {
            "docs": {
                "type": "mcp_tool",
                "server_id": "MCP-docs",
                "tool_name": "search",
                "requires_approval": True,
            }
        },
        "nodes": {
            "lookup": {
                "type": "mcp_resource",
                "server_id": "MCP-db",
                "tool_name": "run_query",
            }
        },
    }
    dependencies = extract_dependencies(manifest)
    assert [(item.label, item.server_id) for item in dependencies] == [
        ("tool binding 'docs'", "MCP-docs"),
        ("MCP resource node 'lookup'", "MCP-db"),
    ]
    legacy = extract_dependencies(
        {
            "nodes": [
                {
                    "id": "legacy",
                    "type": "mcp_resource",
                    "server_id": "MCP-db",
                    "tool_name": "run_query",
                }
            ]
        }
    )
    assert legacy[0].label == "MCP resource node 'legacy'"


def test_deployment_preflight_requires_live_discovery_policy_and_prod_boundary() -> None:
    server = _server()

    class _Session:
        def get(self, _model: object, server_id: str) -> SimpleNamespace | None:
            return server if server_id == server.server_id else None

    manifest = {
        "tools": {
            "query": {
                "type": "mcp_tool",
                "server_id": "MCP-db",
                "tool_name": "run_query",
                "side_effect_level": "read",
            }
        }
    }
    assert deployment_blockers(_Session(), manifest, alias="dev", config=_config()) == []
    prod = deployment_blockers(_Session(), manifest, alias="prod", config=_config())
    assert any("requires an external MCP isolation boundary" in item for item in prod)

    server.transport = "streamable-http"
    server.uri = "http://mcp-db-relational:8101/mcp"
    server.command = ""
    server.args = ()
    managed_prod = deployment_blockers(
        _Session(),
        manifest,
        alias="prod",
        config=_config(
            CALIBER_MCP_REMOTE_HOST_ALLOWLIST="mcp-db-relational",
            CALIBER_MCP_MANAGED_SIDECAR_HOSTS="mcp-db-relational",
        ),
    )
    assert managed_prod == []

    server.last_connected_at = None
    dev = deployment_blockers(_Session(), manifest, alias="dev", config=_config())
    assert any("never completed a live discovery" in item for item in dev)


def test_deployment_preflight_rejects_unclassified_mcp_tools() -> None:
    server = _server(tool_policies={})

    class _Session:
        def get(self, _model: object, _server_id: str) -> SimpleNamespace:
            return server

    blockers = deployment_blockers(
        _Session(),
        {
            "tools": {
                "query": {
                    "type": "mcp_tool",
                    "server_id": "MCP-db",
                    "tool_name": "run_query",
                    "side_effect_level": "read",
                }
            }
        },
        alias="dev",
        config=_config(),
    )
    assert any(
        "has no explicit allow, side-effect, and approval classification" in item
        for item in blockers
    )


def test_direct_mcp_resource_cannot_hide_write_policy() -> None:
    server = _server(
        tool_policies={
            "run_query": {
                "allowed": True,
                "side_effect_level": "write",
                "requires_approval": True,
            }
        }
    )

    class _Session:
        def get(self, _model: object, _server_id: str) -> SimpleNamespace:
            return server

    blockers = deployment_blockers(
        _Session(),
        {
            "nodes": {
                "write": {
                    "type": "mcp_resource",
                    "server_id": "MCP-db",
                    "tool_name": "run_query",
                }
            }
        },
        alias="dev",
        config=_config(),
    )
    assert any("direct MCP resource cannot deploy" in item for item in blockers)


# ---------------------------------------------------------------------------
# An allowlisted name is not an authorized address
# ---------------------------------------------------------------------------


def test_an_allowlisted_host_resolving_internally_is_blocked(monkeypatch) -> None:
    """Allowlisting a *name* must not authorize whatever address it points at.

    Workflow HTTP resolves the host and checks the address, because that is the
    only check SSRF cannot walk around: ``mcp.example.com`` with an A record of
    169.254.169.254 passes any name-based allowlist. MCP had the allowlist and
    not the resolution, so the two halves of the product disagreed about what
    "internal" means.
    """
    from caliber import egress as egress_mod
    from caliber.mcp_policy import execution_readiness

    monkeypatch.setattr(egress_mod, "_resolve_addresses", lambda _host: ["169.254.169.254"])

    server = _server(uri="https://mcp.example.com/sse", transport="streamable-http")
    config = CaliberConfig(mcp_remote_host_allowlist="mcp.example.com")

    readiness = execution_readiness(server, config=config)

    assert readiness.ready is False
    assert any("resolves into the blocked" in blocker for blocker in readiness.blockers)


def test_an_allowlisted_host_resolving_publicly_is_still_allowed(monkeypatch) -> None:
    """The check must not break the ordinary case it is protecting."""
    from caliber import egress as egress_mod
    from caliber.mcp_policy import execution_readiness

    # 8.8.8.8 rather than a TEST-NET address: Python 3.12+ classifies
    # 203.0.113.0/24 as private, so the "public" case has to be genuinely public.
    monkeypatch.setattr(egress_mod, "_resolve_addresses", lambda _host: ["8.8.8.8"])

    server = _server(uri="https://mcp.example.com/sse", transport="streamable-http")
    config = CaliberConfig(mcp_remote_host_allowlist="mcp.example.com")

    readiness = execution_readiness(server, config=config)

    assert readiness.ready is True, readiness.blockers


def test_a_dns_failure_is_not_treated_as_a_verdict(monkeypatch) -> None:
    """Readiness runs on a request path; a transient DNS blip must not condemn a server.

    The connection-time egress guard is what fails closed. This check exists to
    catch the configuration that is wrong every time.
    """
    from caliber import egress as egress_mod
    from caliber.mcp_policy import execution_readiness

    def _boom(_host: str) -> list[str]:
        raise OSError("temporary resolution failure")

    monkeypatch.setattr(egress_mod, "_resolve_addresses", _boom)

    server = _server(uri="https://mcp.example.com/sse", transport="streamable-http")
    config = CaliberConfig(mcp_remote_host_allowlist="mcp.example.com")

    assert execution_readiness(server, config=config).ready is True
