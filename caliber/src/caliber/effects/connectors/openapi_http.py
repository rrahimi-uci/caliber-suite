"""The generic OpenAPI connector: a registered OpenAPI tool's operation, as a governed effect (B04.4, #750).

A workflow ``tool`` node bound to an OpenAPI integration (``execution_backend: openapi_http``) used to call the executor
directly. When the call is protected it is a proposal instead, carried by the private ``openapi_http`` backend, which is the
only caller of the private raw transport (``caliber.integrations.openapi.executor._send_raw``) for it; the public
:func:`~caliber.integrations.openapi.executor.execute_openapi_http_tool` refuses any non-read operation.

What the proposal records, and where each part comes from:

* ``resource`` is ``openapi/<METHOD> <server><path template>``, built from the **registry row's own configuration**
  (:class:`OpenApiOperation`), never from the manifest binding, the model or the input: the method, the server and the path
  are what a reviewer approves and the transport re-derives and compares before it sends anything.
* ``args`` are ``{"tool", "version", "input"}``: the validated call input without the two things the platform owns. The
  precondition (``If-Match`` and the like) is lifted into the intent's ``expected_resource_version``; the idempotency key is
  never recorded, because the transport sends the proposal's stable logical key and a caller-supplied key is refused.
* The capability record promises **nothing**: no key scope or retention, no lookup by key, no documented rejection status. An
  operation's own key header is used when the row binds one (that is what makes a retry safe), but the platform does not rely
  on what a downstream does with it, so every answer other than a 2xx is ambiguous and a 2xx is a claim that the run's
  authoritative receipt verifier must confirm.

**Retries.** The transport retries only when the request really carries an idempotency key: an operation whose row binds no
key header is sent once, even for a method the executor would otherwise retry (``PUT``, ``DELETE``). The dispatcher does not
redrive an ambiguous effect: a timeout after the downstream committed leaves the proposal ``unknown`` for the reconciler,
which has no lookup for this connector and so never guesses. Within **one** dispatch, though, the executor's bounded retry
(the row's ``max_attempts``, at most 5) repeats a request that carries the key. That is an accepted risk, not a guarantee:
the capability record leaves key scope and retention unconfirmed, so a downstream that does not honour the key may commit
twice. Leave ``max_attempts`` at 1 unless the downstream is known to.

This module is pure: it reads no database and sends nothing.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from caliber.effects.connectors import ConnectorCapability
from caliber.egress import EgressPolicy
from caliber.integrations.openapi.executor import (
    READ_METHODS,
    OpenApiPreflight,
    OpenApiPreflightError,
    preflight_governed_openapi_request,
)

CONNECTOR: Final = "openapi-http"
VERSION: Final = "1"
BACKEND_KIND: Final = "openapi_http"

#: The refusal a recorded proposal that does not match its trusted operation gets (the transport sends nothing).
INTENT_MISMATCH: Final = "connector_intent_unsupported"

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


@dataclass(frozen=True)
class OpenApiOperation:
    """One registered OpenAPI tool, as its **registry row** describes it: the trust root of a governed call.

    ``execution_config`` and ``input_schema`` are the row's ``backend_config`` and ``input_schema`` (read from the database
    for the tool being run, and compared with what the manifest binding carries before this is built). The runtime builds one
    per call and hands it to the ``openapi_http`` backend, which sends only what this describes.
    """

    tool: str
    version: str
    execution_config: Mapping[str, Any] = field(repr=False)
    input_schema: Mapping[str, Any] | None = field(default=None, repr=False)
    egress_policy: EgressPolicy | None = field(default=None, repr=False)


def operation_resource(preflight: OpenApiPreflight) -> str:
    """The proposal's ``resource``: the operation a reviewer approves (method, server and path template)."""
    return f"openapi/{preflight.method} {preflight.server_url}{preflight.path}"


def preflight(operation: OpenApiOperation, arguments: Mapping[str, Any]) -> OpenApiPreflight:
    """:func:`~caliber.integrations.openapi.executor.preflight_governed_openapi_request` for ``operation``'s row."""
    return preflight_governed_openapi_request(
        execution_config=dict(operation.execution_config),
        input_schema=None if operation.input_schema is None else dict(operation.input_schema),
        input_data=dict(arguments),
        egress_policy=operation.egress_policy,
    )


def intent_args(operation: OpenApiOperation, checked: OpenApiPreflight) -> dict[str, Any]:
    """The immutable arguments of an OpenAPI proposal: the tool, its version and the input the transport replays."""
    return {
        "tool": operation.tool,
        "version": operation.version,
        "input": checked.replay_input,
    }


def transport_request(
    operation: OpenApiOperation,
    *,
    resource: str,
    args: Mapping[str, Any],
    expected_resource_version: str | None,
    logical_key: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The executor's ``(execution_config, input_data)`` for a recorded proposal, or :class:`OpenApiPreflightError`.

    The recorded input is re-checked against the trusted operation exactly as at proposal time (including the egress policy, so a
    destination that stopped being allowed after the approval fails here), and what it resolves to must
    be the recorded ``resource``: a proposal whose operation, method or server is not the one the row describes is refused
    before anything is sent. The stable key and the precondition are put on the request here and nowhere else: the key only
    when the row binds a key header, the precondition only when it binds one and the proposal recorded a value. A recorded
    precondition the row cannot send is refused, never dropped. An operation with no key header is sent once.
    """
    recorded = args.get("input")
    if (
        args.get("tool") != operation.tool
        or args.get("version") != operation.version
        or not isinstance(recorded, Mapping)
    ):
        raise OpenApiPreflightError(INTENT_MISMATCH)
    checked = preflight(operation, recorded)
    if operation_resource(checked) != resource:
        raise OpenApiPreflightError(INTENT_MISMATCH)
    if checked.expected_resource_version is not None:  # the recorded input never carries it
        raise OpenApiPreflightError(INTENT_MISMATCH)
    send = dict(checked.replay_input)
    config = dict(operation.execution_config)
    if checked.idempotency_key_header is not None:
        send["idempotency_key"] = logical_key
    else:
        config["max_attempts"] = 1
    if expected_resource_version is not None:
        if checked.expected_resource_version_header is None:
            raise OpenApiPreflightError(INTENT_MISMATCH)
        send["expected_resource_version"] = expected_resource_version
    return config, send


#: What the transport sends with, pinned in ``caliber.profile.REFERENCE_CONNECTORS``: the shape of the resource it accepts,
#: where its key and precondition come from, and the two things the runtime must supply (the registry row's operation and an
#: authoritative receipt verifier). Changing any of it is a new digest that must be re-admitted.
CONFIGURATION: Final[Mapping[str, Any]] = {
    "resource": "openapi/<METHOD> <server><path template>",
    "args": ["tool", "version", "input"],
    "idempotency_key": "the proposal's logical key, only for an operation that binds a key header",
    "retry": "only with an emitted idempotency key",
    "precondition": "the proposal's expected_resource_version, only for an operation that binds a precondition header",
    "operation": "required",
    "receipt_verifier": "required",
}

__all__ = [
    "BACKEND_KIND",
    "CAPABILITY",
    "CONFIGURATION",
    "CONNECTOR",
    "INTENT_MISMATCH",
    "READ_METHODS",
    "VERSION",
    "OpenApiOperation",
    "intent_args",
    "operation_resource",
    "preflight",
    "transport_request",
]
