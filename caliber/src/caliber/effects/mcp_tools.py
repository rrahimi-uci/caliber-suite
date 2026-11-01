"""Which way an MCP tool call runs: inline, through the dispatcher, or not at all (B04.5, #759; ADR 0002).

An MCP server is untrusted: what it says about itself (its tool names, descriptions, annotations such as ``readOnlyHint``,
the results it returns) is data, never a classification. The trust root of a call is three things CALIBER itself holds:

* the **MCP server record** (``CaliberMcpServer``: transport, command or URI, arguments, the names of its environment
  variables and headers), read from the database for the server being called and never from a manifest binding, a model or the
  server's own answers;
* the **tool policy an administrator wrote** for exactly that tool on that server (``tool_policies``: ``allowed``,
  ``side_effect_level``, ``requires_approval``), which :func:`caliber.mcp_policy.validate_tool_access` requires to be explicit
  and complete: a tool the server lists but nobody classified is refused, not guessed at; and
* the **protected-effect inventory** (EP-114), which applies to the first-party PostgreSQL MCP server only, identified by the
  record (:func:`caliber.mcp_policy.is_first_party_db_server`): the write tools it names are refused **under the reference
  profile** (the refusal switch for every inventory row is #774; the development profile keeps the alpha behaviour).

The decision, from those alone:

* a refusal of the policy gate (not ready, not discovered, not classified, not allowed) is :data:`REFUSE` with that gate's code;
* an inventory ``refuse`` (a first-party database write tool) is :data:`REFUSE` with ``unsupported_effect_path``;
* otherwise the administrator's ``side_effect_level`` is the effect class (``read``, ``reversible_write`` for ``write``,
  ``irreversible_write`` for ``external_action``): ``read`` runs :data:`INLINE`, everything else is :data:`DISPATCH`.

``requires_approval`` is not an effect class: an approval-requiring *read* keeps the approval gate the workflow binding already
applies (and that deployment checks against the policy), and a protected call is approved by the dispatcher's exact-intent
approval regardless of it.

The digest returned with a route binds the record fields and the classification the decision was made from, so the dispatcher's
backend can re-read the record when it is about to send and refuse a call whose server or classification changed after the
approval was requested. The values of environment variables, headers and credentials are never part of it (and never recorded).

This module reads no database and sends nothing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Final

from caliber.config import CaliberConfig
from caliber.effects.inventory import FAIL_CLOSED, tool_disposition_for_mcp_tool
from caliber.mcp_policy import McpPolicyError, is_first_party_db_server, validate_tool_access

INLINE: Final = "inline"
DISPATCH: Final = "dispatch"
REFUSE: Final = "refuse"

#: The refusal for a first-party database write tool (inventory EP-114): the dispatcher cannot yet carry it.
INVENTORY_REFUSAL: Final = "unsupported_effect_path"

#: The effect class an administrator's ``side_effect_level`` implies.
_DECLARED_CLASS: Final = {
    "read": "read",
    "write": "reversible_write",
    "external_action": "irreversible_write",
}


@dataclass(frozen=True)
class McpToolRoute:
    """The decision for one MCP tool call.

    ``code`` is the refusal code when ``route`` is :data:`REFUSE`. ``policy_refused`` says the refusal came from the policy
    gate (so a caller that reaches the gate itself raises its own error) rather than the inventory. ``digest`` identifies the
    server record and classification the decision was made from; it is ``None`` for a refusal.
    """

    route: str
    effect_class: str
    code: str | None = None
    policy_refused: bool = False
    digest: str | None = None
    first_party: bool = False
    #: Whether the decision was made under the reference profile, the only profile in which a protected call that reaches the
    #: gateway without a proposal, and a first-party database write tool, are refused (the development profile behaves as
    #: before the dispatcher: see :func:`route_mcp_tool`).
    reference_profile: bool = False


def server_digest(server: Any, tool_name: str, policy: dict[str, Any]) -> str:
    """``sha256:`` of what identifies the server record and the tool's classification, with no secret value in it.

    Covered: the transport, URI, command and arguments; the *names* of the environment variables and headers and the
    authentication type (a changed credential value is an operator change this digest does not see); the server's id; the
    tool; and the three classification fields an administrator wrote.
    """
    env = getattr(server, "env", None) or {}
    headers = getattr(server, "headers", None) or {}
    record = {
        "server_id": str(getattr(server, "server_id", "")),
        "tool": tool_name,
        "transport": str(getattr(server, "transport", "")).strip().lower(),
        "uri": str(getattr(server, "uri", "")).strip(),
        "command": str(getattr(server, "command", "")).strip(),
        "args": [str(arg) for arg in (getattr(server, "args", ()) or ())],
        "env_names": sorted(str(key) for key in env),
        "header_names": sorted(str(key) for key in headers),
        "auth_type": str(getattr(server, "auth_type", "")).strip().lower(),
        "allowed": policy.get("allowed"),
        "side_effect_level": policy.get("side_effect_level"),
        "requires_approval": policy.get("requires_approval"),
    }
    data = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def route_mcp_tool(server: Any, tool_name: str, *, config: Any = None) -> McpToolRoute:
    """Decide how a call of ``tool_name`` on ``server`` runs, from the server record, its tool policy and the inventory.

    ``server`` is the record as the gateway loaded it (``McpServerConfig``). The policy gate runs without consuming the
    rate limit (the call that really goes out consumes it). Never raises for a refusal: it returns :data:`REFUSE` with a code.

    The **classification** (``read`` / ``dispatch``) is the same in every profile, so a governed run (one with an authority
    bound) always proposes a protected call. What differs is what the *gateway* does with a protected call that reaches it
    with no proposal, and the inventory refusal of EP-114: both are enforced only under ``CALIBER_PROFILE=reference``
    (:attr:`McpToolRoute.reference_profile`). The development profile keeps the alpha behaviour for them (#1068), which is
    **not** the governed path: no exact-intent approval, lease, proposal or receipt.
    """
    resolved = config if config is not None else CaliberConfig.load()
    reference = getattr(resolved, "profile", "") == "reference"
    try:
        policy = validate_tool_access(server, tool_name, consume_rate=False, config=resolved)
    except McpPolicyError as exc:
        return McpToolRoute(
            REFUSE, FAIL_CLOSED, exc.code, policy_refused=True, reference_profile=reference
        )
    first_party = is_first_party_db_server(server, config=resolved)
    if (
        reference
        and tool_disposition_for_mcp_tool(tool_name, first_party_db_server=first_party) == "refuse"
    ):
        return McpToolRoute(
            REFUSE, FAIL_CLOSED, INVENTORY_REFUSAL, first_party=first_party, reference_profile=True
        )
    effect = _DECLARED_CLASS.get(str(policy.get("side_effect_level")), FAIL_CLOSED)
    return McpToolRoute(
        INLINE if effect == "read" else DISPATCH,
        effect,
        digest=server_digest(server, tool_name, policy),
        first_party=first_party,
        reference_profile=reference,
    )


__all__ = [
    "DISPATCH",
    "INLINE",
    "INVENTORY_REFUSAL",
    "REFUSE",
    "McpToolRoute",
    "route_mcp_tool",
    "server_digest",
]
