"""The MCP connector: a protected MCP tool call, as a governed effect (B04.5, #759).

A workflow ``tool`` node bound to an MCP tool, or an MCP node, used to call the MCP gateway directly. When the call is
protected it is a proposal instead, carried by the private ``mcp`` backend, which is the only caller of the gateway's ungated
transport (:func:`caliber.mcp_gateway.invoke_tool_raw_sync`) for a governed call.

An MCP server is **untrusted** (see :mod:`caliber.effects.mcp_tools`): this module never reads a classification, a hint or a
description from it. What the proposal records, and where each part comes from:

* ``resource`` is ``mcp/<server_id>/<tool>``: the server **record's** id and the tool the node or binding names, which is a
  reviewer-facing target and not a trust claim; the backend re-reads the record and compares before it sends.
* ``args`` are ``{"server_id", "tool", "server_digest", "arguments"}``. ``server_digest`` binds the record fields and the
  administrator's tool classification the decision was made from (no credential value is in it), so a server or a policy that
  changed after the approval was requested is refused when the backend is about to send, never silently run.
* The capability record promises **nothing**: MCP has no idempotency key, no precondition and no lookup by key. So every outcome
  other than a returned result is ambiguous, and a returned result is a claim the run's authoritative receipt verifier must
  confirm (most MCP servers cannot confirm their own effects; a deployment that cannot verify gets ``unknown``, never
  ``committed``).

**No retries.** The dispatcher sends the call at most once. A failure after the request may have reached the server (a timeout,
a dropped session, a tool-reported error, which an untrusted server can say about an effect that happened) leaves the proposal
``unknown`` for the reconciler, which has no lookup for this connector and so never guesses. Only a failure the gateway proves
happened *before* the request (policy, egress, command allowlist, a session that never opened) is ``proven_not_sent``.

**The result is data.** What the server returns is bounded (:data:`MAX_RESULT_BYTES`, :data:`MAX_RESULT_DEPTH`) and must be
plain JSON; anything else is ``ambiguous`` (``result_out_of_bounds``), because the effect may have happened and no receipt can be
built from what cannot be read.

This module reads the database only through the caller-supplied ``load_server`` and sends nothing.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from caliber.effects.connectors import ConnectorCapability
from caliber.effects.mcp_tools import DISPATCH, McpToolRoute, route_mcp_tool

CONNECTOR: Final = "mcp-tool"
VERSION: Final = "1"
BACKEND_KIND: Final = "mcp"

#: The refusal a recorded proposal that does not match its trusted server and tool gets (the transport sends nothing).
INTENT_MISMATCH: Final = "connector_intent_unsupported"
#: The server record could not be read (missing, not visible to the run's identity, or the store failed).
SERVER_UNAVAILABLE: Final = "mcp_server_unavailable"
#: The call's timeout is not a finite number of seconds the connector accepts.
TIMEOUT_INVALID: Final = "mcp_timeout_invalid"

#: What a server's result may weigh once it is plain JSON, and how deep it may nest.
MAX_RESULT_BYTES: Final = 256 * 1024
MAX_RESULT_DEPTH: Final = 32
MAX_TIMEOUT_SECONDS: Final = 300.0
#: The gateway's own default for one call, used when a node or binding sets none.
DEFAULT_TIMEOUT_SECONDS: Final = 45.0

CAPABILITY: Final = ConnectorCapability(
    connector=CONNECTOR,
    version=VERSION,
    idempotency_key_header=None,
    key_scope="unknown",
    key_retention="unconfirmed",
    key_retention_seconds=None,
    key_conflict_status=None,
    precondition_header=None,
    stale_precondition_status=None,
    definitive_rejection_statuses=frozenset(),
    lookup_by_key=False,
    lookup_consistency="unknown",
    absence_proves_noncommit=False,
)

#: What the transport sends with, pinned in ``caliber.profile.REFERENCE_CONNECTORS``: the shape of the resource it accepts, what
#: is recorded, where the classification comes from, that nothing is retried, how a result is bounded, and the two things the
#: runtime must supply (the call, which re-reads the server record, and an authoritative receipt verifier). Changing any of it
#: is a new digest that must be re-admitted.
CONFIGURATION: Final[Mapping[str, Any]] = {
    "resource": "mcp/<server_id>/<tool>",
    "args": ["server_id", "tool", "server_digest", "arguments"],
    "classification": "server record and administrator tool policy, re-read at dispatch; never server-reported",
    "transport": "the gateway's ungated raw transport (tool policy, egress and stdio checks run again)",
    "idempotency_key": "none: MCP has no key, no precondition and no lookup",
    "retry": "none",
    "result": {
        "max_bytes": MAX_RESULT_BYTES,
        "max_depth": MAX_RESULT_DEPTH,
        "format": "plain json",
    },
    "call": "required",
    "receipt_verifier": "required",
}


class McpCallRefused(Exception):  # noqa: N818 - a refusal, named for what it says
    """The call cannot be proposed or sent; ``code`` is stable and carries no payload."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class McpToolCall:
    """One MCP tool call's trusted operation: which server record and which tool, and how to read the record again.

    ``load_server`` returns the server record (``McpServerConfig``) fresh from the database, scoped to the identity of the
    run: it is called when the proposal is built and again when the backend is about to send, so a server or a policy that
    changed in between is seen. The runtime builds one per call; the ``mcp`` backend sends only what it describes.
    """

    server_id: str
    tool_name: str
    load_server: Callable[[], Any] = field(repr=False)
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


@dataclass(frozen=True)
class PreparedMcpCall:
    """The proposal material for a call whose route is not a refusal; ``args`` is ``None`` for a refusal."""

    route: McpToolRoute
    resource: str
    args: dict[str, Any] | None = field(repr=False)


def mcp_resource(server_id: str, tool_name: str) -> str:
    """The proposal's ``resource`` for an MCP tool: the target a reviewer approves."""
    return f"mcp/{server_id}/{tool_name}"


def _timeout(call: McpToolCall) -> None:
    value = call.timeout_seconds
    # Compared without converting an ``int`` to ``float``: ``float(10**400)`` raises ``OverflowError``.
    ok = (type(value) is int and 0 < value <= MAX_TIMEOUT_SECONDS) or (
        type(value) is float and math.isfinite(value) and 0 < value <= MAX_TIMEOUT_SECONDS
    )
    if not ok:
        raise McpCallRefused(TIMEOUT_INVALID)


def _server(call: McpToolCall) -> Any:
    try:
        server = call.load_server()
    except Exception as exc:  # the cause (a missing row, a store error) is never carried
        raise McpCallRefused(SERVER_UNAVAILABLE) from exc
    if str(getattr(server, "server_id", "")) != call.server_id:
        raise McpCallRefused(SERVER_UNAVAILABLE)
    return server


def prepare(call: McpToolCall, arguments: Mapping[str, Any]) -> PreparedMcpCall:
    """Read the server record and decide the call's route; build the proposal material for a call that is not refused.

    Raises :class:`McpCallRefused` for a timeout the connector does not accept or a record that cannot be read. A refusal of
    the policy gate or the inventory is a :data:`~caliber.effects.mcp_tools.REFUSE` route (with its code), not an exception.
    """
    _timeout(call)
    server = _server(call)
    route = route_mcp_tool(server, call.tool_name)
    resource = mcp_resource(call.server_id, call.tool_name)
    if route.digest is None:
        return PreparedMcpCall(route, resource, None)
    args = {
        "server_id": call.server_id,
        "tool": call.tool_name,
        "server_digest": route.digest,
        "arguments": dict(arguments),
    }
    return PreparedMcpCall(route, resource, args)


def check_recorded(
    call: McpToolCall, *, resource: str, args: Mapping[str, Any]
) -> tuple[Any, dict[str, Any]]:
    """The ``(server record, arguments)`` to send for a recorded proposal, or :class:`McpCallRefused`.

    The record is read again and the decision is made again, exactly as at proposal time: the recorded server, tool, resource
    and digest must be the trusted call's, and the call must still be protected (``dispatch``): a tool an administrator has
    since blocked, reclassified or left unclassified, or a server record that changed, is refused before anything is sent.
    The recorded arguments are what is sent, never what the caller still holds in memory.
    """
    _timeout(call)
    if (
        set(args) != {"server_id", "tool", "server_digest", "arguments"}
        or args["server_id"] != call.server_id
        or args["tool"] != call.tool_name
        or resource != mcp_resource(call.server_id, call.tool_name)
        or not isinstance(args["arguments"], Mapping)
    ):
        raise McpCallRefused(INTENT_MISMATCH)
    server = _server(call)
    route = route_mcp_tool(server, call.tool_name)
    if route.route != DISPATCH or route.digest != args["server_digest"]:
        raise McpCallRefused(INTENT_MISMATCH)
    return server, dict(args["arguments"])


def _within_depth(value: Any, depth: int) -> bool:
    if depth > MAX_RESULT_DEPTH:
        return False
    if isinstance(value, Mapping):
        return all(_within_depth(item, depth + 1) for item in value.values())
    if isinstance(value, (list, tuple)):
        return all(_within_depth(item, depth + 1) for item in value)
    return True


def bounded_result(value: Any) -> tuple[Any] | None:
    """The server's result as plain JSON inside the bounds, as a one-tuple (a result may itself be ``None``), else ``None``.

    The result is untrusted data: it is refused (``None``) when it nests deeper than :data:`MAX_RESULT_DEPTH`, is not JSON
    (a non-finite number, an object that is not a plain value, unencodable text) or weighs more than
    :data:`MAX_RESULT_BYTES` as compact UTF-8 JSON. What is returned is the JSON round trip, so no object of the server's
    making survives into a record or a response.
    """
    if not _within_depth(value, 1):
        return None
    try:
        data = json.dumps(value, allow_nan=False, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    except (TypeError, ValueError, RecursionError):
        return None
    if len(data) > MAX_RESULT_BYTES:
        return None
    return (json.loads(data),)


__all__ = [
    "BACKEND_KIND",
    "CAPABILITY",
    "CONFIGURATION",
    "CONNECTOR",
    "DEFAULT_TIMEOUT_SECONDS",
    "INTENT_MISMATCH",
    "MAX_RESULT_BYTES",
    "MAX_RESULT_DEPTH",
    "MAX_TIMEOUT_SECONDS",
    "SERVER_UNAVAILABLE",
    "TIMEOUT_INVALID",
    "VERSION",
    "McpCallRefused",
    "McpToolCall",
    "PreparedMcpCall",
    "bounded_result",
    "check_recorded",
    "mcp_resource",
    "prepare",
]
