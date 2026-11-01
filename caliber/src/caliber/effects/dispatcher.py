"""The sole protected-effect boundary (B04.1, #724; ADR 0002/0003).

The dispatcher deliberately has two public operations with different trust
boundaries:

* :func:`submit_proposal` validates and records immutable intent.  It never
  calls a connector and it cannot claim that an effect happened.
* :func:`dispatch` validates an exact approval and the captured execution-owner
  lease in one transaction, consumes the approval, moves the proposal through
  ``approved`` to ``dispatched`` and writes the durable outbox event.  Only
  after that transaction commits does it call a registered private transport.

The transport result is settled by :func:`caliber.effects.receipts.settle_dispatch`.
An HTTP success is therefore only a claim; the transport must provide an
authoritative verifier before the proposal becomes ``committed``.  Ambiguous
outcomes remain recoverable by the B05 reconciler and are never blindly sent a
second time here.

A settle that fails while this dispatcher still holds its lease (the verifier raised, the store went away after the send) never
leaves the attempt ``dispatched``: :func:`dispatch` records ``unknown`` under the same captured lease, unless the failure says
someone else owns the row now, and raises the original failure; the reconciler's bounded in-flight rule is the backstop for a
dispatcher that could not even do that (B05.5d, #997).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy.orm import Session

from caliber import mcp_gateway as _raw_mcp_transport
from caliber import profile as _profile
from caliber.db.models import CaliberRuntimeApprovalRequest, CaliberWorkflowRun
from caliber.effects.admission import (
    AdmissionRefused,
    AdmittedConnector,
    admit,
    check_config,
)
from caliber.effects.connectors import (
    Classified,
    ConnectorCapability,
    TransportOutcome,
    classify_error,
    classify_result,
)
from caliber.effects.connectors import document_request as _document_request
from caliber.effects.connectors import mcp_tool as _mcp_tool
from caliber.effects.connectors import openapi_http as _openapi_http
from caliber.effects.inventory import EFFECT_CLASSES, effect_class, tool_disposition
from caliber.effects.receipts import ReceiptFacts, Settled, settle_dispatch
from caliber.effects.records import (
    APPROVED,
    DISPATCHED,
    ILLEGAL_TRANSITION,
    LEASE_LOST,
    PROPOSED,
    STALE_OWNER,
    STALE_VERSION,
    UNKNOWN,
    Intent,
    ProposalError,
    ProposalView,
    RecordedProposal,
    fence_owner,
    proposal_state,
    proposal_view,
    read_proposal,
    record_proposal,
    transition,
)
from caliber.effects.registered_tools import (
    REGISTERED_TOOL_CONNECTOR,
    REGISTERED_TOOL_VERSION,
    tool_resource,
)
from caliber.events.outbox import enqueue
from caliber.integrations.openapi import executor as _raw_connector_transport
from caliber.integrations.openapi.executor import OpenApiPreflightError
from caliber.observability.span_attributes import GovernedSpan, governed_span
from caliber.ownership import Lease
from caliber.workflows.agent_step import ProposedAction
from caliber.workflows.approval_policy import (
    ApprovalCommit,
    ApprovalCommitRefused,
    approval_valid_at_commit,
)

logger = logging.getLogger("caliber.effects.dispatcher")

_CASE_RESOURCE = re.compile(
    r"^cases/(?P<case_id>[A-Za-z0-9][A-Za-z0-9._-]{0,127})/document-requests$"
)
HTTP_CONNECTOR = "http"
HTTP_VERSION = "1"
# Generic HTTP has no platform-wide promise about key scope, retention, or a
# read-by-key endpoint.  The runtime may still dispatch it, but the caller must
# provide an authoritative receipt verifier for each endpoint.  This capability
# deliberately cannot claim exactly-once semantics on its own.
HTTP_CAPABILITY = ConnectorCapability(
    connector=HTTP_CONNECTOR,
    version=HTTP_VERSION,
    idempotency_key_header="Idempotency-Key",
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


class EffectRefused(RuntimeError):  # noqa: N818 - stable public refusal name in the issue contract
    """A protected effect could not cross the dispatcher boundary."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


class UnsupportedEffectPath(EffectRefused):
    """The inventory or backend registry does not support this path."""

    def __init__(self, code: str = "unsupported_effect_path", message: str | None = None) -> None:
        super().__init__(code, message)


@dataclass(frozen=True)
class EffectOutcome:
    """The durable proposal state after dispatch and any safe settlement."""

    proposal_id: str
    state: str
    outcome: TransportOutcome | None = None
    receipt_id: str | None = None
    reason: str | None = None
    applied: bool = True
    response: Mapping[str, Any] | None = None

    @property
    def transport_outcome(self) -> TransportOutcome | None:
        """Descriptive alias for callers that distinguish state from transport evidence."""
        return self.outcome


@dataclass(frozen=True)
class EffectRequest:
    """The immutable proposal material a private backend may receive."""

    view: ProposalView
    intent: Mapping[str, Any]


@dataclass(frozen=True)
class TransportResult:
    """A classified transport attempt and its optional receipt verifier."""

    outcome: Classified
    verify: Callable[[], ReceiptFacts | None] | None = None
    response: Mapping[str, Any] | None = None


class PrivateBackend(Protocol):
    """Private, connector-specific transport; public HTTP routes are not backends."""

    def perform(
        self, request: EffectRequest, config: Mapping[str, Any] | None
    ) -> TransportResult: ...


@dataclass(frozen=True)
class BackendRegistration:
    """A private transport bound to the connector identity it is allowed to send.

    Registering one does not admit it: ``capability`` (its documented promise) and ``configuration`` (the wire
    configuration it sends with) are what :func:`admit_backend` compares with the pinned record in
    ``caliber.profile.REFERENCE_CONNECTORS``, together with the transport's own implementation identity. A registration
    without a capability record, or whose record does not match, is never dispatched through.
    """

    backend: PrivateBackend | Callable[[EffectRequest, Mapping[str, Any] | None], TransportResult]
    connector: str
    connector_version: str
    capability: ConnectorCapability | None = None
    configuration: Mapping[str, Any] | None = None


class _Backend:
    """Small adapter used by the built-in document-request connector."""

    def perform(self, request: EffectRequest, config: Mapping[str, Any] | None) -> TransportResult:
        if (
            request.view.connector != _document_request.CONNECTOR
            or request.view.connector_version != _document_request.VERSION
        ):
            raise _TransportRefusedError("connector_binding_mismatch")
        settings = dict(config or {})
        base_url = settings.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            raise _TransportRefusedError("backend_config_missing")
        resource = request.view.resource
        match = _CASE_RESOURCE.fullmatch(resource)
        args = request.intent.get("args")
        document_type = args.get("document_type") if isinstance(args, Mapping) else None
        if match is None or not isinstance(document_type, str) or not document_type.strip():
            raise _TransportRefusedError("connector_intent_unsupported")
        expected = request.view.expected_resource_version
        expected_version: int | None = None
        if expected is not None:
            if not expected.isascii() or not expected.isdigit():
                raise _TransportRefusedError("connector_version_invalid")
            expected_version = int(expected)
        try:
            max_attempts = int(settings.get("max_attempts", 1))
        except (TypeError, ValueError) as exc:
            raise _TransportRefusedError("backend_config_invalid") from exc
        try:
            result = _raw_connector_transport._send_raw(
                execution_config=_document_request.submit_config(
                    base_url,
                    max_attempts=max_attempts,
                    timeout_seconds=settings.get("timeout_seconds"),
                ),
                input_schema=None,
                input_data=_document_request.submit_input(
                    case_id=match.group("case_id"),
                    idempotency_key=request.view.logical_key,
                    expected_resource_version=expected_version,
                    body={"document_type": document_type},
                ),
            )
        except Exception as exc:
            return TransportResult(classify_error(exc))
        classified = classify_result(result, capability=_document_request.CAPABILITY)
        if classified.outcome is not TransportOutcome.COMMITTED_RECEIPT:
            return TransportResult(classified)

        def verify() -> ReceiptFacts | None:
            found = _raw_connector_transport._send_raw(
                execution_config=_document_request.lookup_config(base_url),
                input_schema=None,
                input_data=_document_request.lookup_input(request.view.logical_key),
            )
            return _document_request.facts_from_lookup(
                request.view,
                lookup_status=found["status_code"],
                lookup_body=found["json"],
            )

        return TransportResult(classified, verify)


class _HttpBackend:
    """Private raw HTTP adapter for governed webhook and API-request nodes.

    This backend intentionally accepts a transport callable and an authoritative
    receipt verifier through ``config``.  It never calls a public CALIBER route
    or the OpenAPI execution surface recursively.  A caller that cannot verify a
    committed response is refused before sending anything.
    """

    def perform(self, request: EffectRequest, config: Mapping[str, Any] | None) -> TransportResult:
        if (
            request.view.connector != HTTP_CONNECTOR
            or request.view.connector_version != HTTP_VERSION
        ):
            raise _TransportRefusedError("connector_binding_mismatch")
        settings = dict(config or {})
        sender = settings.get("sender")
        verifier = settings.get("receipt_verifier")
        if not callable(sender) or not callable(verifier):
            raise _TransportRefusedError("backend_config_missing")
        args = request.intent.get("args")
        if not isinstance(args, Mapping) or args.get("url") != request.view.resource:
            raise _TransportRefusedError("connector_intent_unsupported")
        outgoing = dict(args)
        headers = outgoing.get("headers")
        if headers is None:
            headers = {}
        if not isinstance(headers, Mapping):
            raise _TransportRefusedError("connector_intent_unsupported")
        outgoing["headers"] = {str(key): str(value) for key, value in headers.items()}
        outgoing["headers"].setdefault("Idempotency-Key", request.view.logical_key)
        try:
            result = sender(outgoing)
        except Exception as exc:
            return TransportResult(classify_error(exc))
        if not isinstance(result, Mapping) or type(result.get("status_code")) is not int:
            raise _TransportRefusedError("transport_result_invalid")
        response = dict(result)
        classified = classify_result(response, capability=HTTP_CAPABILITY)
        if classified.outcome is not TransportOutcome.COMMITTED_RECEIPT:
            return TransportResult(classified, response=response)

        def verify() -> ReceiptFacts | None:
            facts = verifier(request, response)
            if facts is not None and not isinstance(facts, ReceiptFacts):
                raise _TransportRefusedError("receipt_verifier_invalid")
            return facts

        return TransportResult(classified, verify, response=response)


class _RegisteredToolBackend:
    """Private adapter that runs a registry tool for the workflow runtime (B04.3, #741).

    ``config`` carries two runtime-owned callables: ``invoker(arguments, input_text)``, which runs the tool the runtime
    already bound, and ``receipt_verifier``, the same authoritative check the HTTP backend requires. The invoker is
    called with the arguments **recorded in the proposal**, never with what the caller still holds in memory, so what
    runs is what the approver saw. A local callable has no downstream key to replay, so this backend makes no
    exactly-once claim: a tool that raises or times out leaves the proposal ``unknown`` (it may have acted), and
    nothing here calls it a second time.
    """

    def perform(self, request: EffectRequest, config: Mapping[str, Any] | None) -> TransportResult:
        if (
            request.view.connector != REGISTERED_TOOL_CONNECTOR
            or request.view.connector_version != REGISTERED_TOOL_VERSION
        ):
            raise _TransportRefusedError("connector_binding_mismatch")
        settings = dict(config or {})
        invoker = settings.get("invoker")
        verifier = settings.get("receipt_verifier")
        if not callable(invoker) or not callable(verifier):
            raise _TransportRefusedError("backend_config_missing")
        args = request.intent.get("args")
        if not isinstance(args, Mapping):
            raise _TransportRefusedError("connector_intent_unsupported")
        name, version = args.get("tool"), args.get("version")
        arguments, input_text = args.get("arguments"), args.get("input")
        if (
            not isinstance(name, str)
            or not isinstance(version, str)
            or request.view.resource != tool_resource(name, version)
            or not isinstance(arguments, Mapping)
            or not isinstance(input_text, str)
        ):
            raise _TransportRefusedError("connector_intent_unsupported")
        try:
            result = invoker(dict(arguments), input_text)
        except Exception as exc:
            return TransportResult(classify_error(exc))
        response = {"result": result}
        classified = Classified(TransportOutcome.COMMITTED_RECEIPT, "tool_returned", None, 1)

        def verify() -> ReceiptFacts | None:
            facts = verifier(request, response)
            if facts is not None and not isinstance(facts, ReceiptFacts):
                raise _TransportRefusedError("receipt_verifier_invalid")
            return facts

        return TransportResult(classified, verify, response=response)


class _OpenApiBackend:
    """Private adapter that sends a registered OpenAPI tool's operation (B04.4, #750).

    ``config`` carries two runtime-owned values: ``operation`` (:class:`~caliber.effects.connectors.openapi_http.OpenApiOperation`,
    built from the tool's **registry row**) and ``receipt_verifier`` (the same authoritative check the HTTP backend requires).
    The request is rebuilt from the operation and the arguments **recorded in the proposal**, and what that resolves to must
    be the proposal's resource (method, server and path): what is sent is what the approver saw. This is the one caller of the
    raw transport for a governed OpenAPI call, so a call through here can never reach the dispatcher again.

    The stable logical key is the idempotency key, and only for an operation whose row binds a key header; an operation with
    no key header is sent once. A timeout or a dropped answer after the downstream committed is ``ambiguous`` and leaves the
    proposal ``unknown``: this backend never sends it a second time, and the connector has no lookup by key, so only the
    run's receipt verifier or a callback can settle it.
    """

    def perform(self, request: EffectRequest, config: Mapping[str, Any] | None) -> TransportResult:
        if (
            request.view.connector != _openapi_http.CONNECTOR
            or request.view.connector_version != _openapi_http.VERSION
        ):
            raise _TransportRefusedError("connector_binding_mismatch")
        settings = dict(config or {})
        operation = settings.get("operation")
        verifier = settings.get("receipt_verifier")
        if not isinstance(operation, _openapi_http.OpenApiOperation) or not callable(verifier):
            raise _TransportRefusedError("backend_config_missing")
        args = request.intent.get("args")
        if not isinstance(args, Mapping):
            raise _TransportRefusedError("connector_intent_unsupported")
        try:
            execution_config, input_data = _openapi_http.transport_request(
                operation,
                resource=request.view.resource,
                args=args,
                expected_resource_version=request.view.expected_resource_version,
                logical_key=request.view.logical_key,
            )
        except OpenApiPreflightError as exc:
            raise _TransportRefusedError("connector_intent_unsupported") from exc
        try:
            result = _raw_connector_transport._send_raw(
                execution_config=execution_config,
                input_schema=None,  # validated against the row's schema by transport_request
                input_data=input_data,
                egress_policy=operation.egress_policy,
            )
        except Exception as exc:
            return TransportResult(classify_error(exc))
        response = dict(result)
        classified = classify_result(response, capability=_openapi_http.CAPABILITY)
        if classified.outcome is not TransportOutcome.COMMITTED_RECEIPT:
            return TransportResult(classified, response=response)

        def verify() -> ReceiptFacts | None:
            facts = verifier(request, response)
            if facts is not None and not isinstance(facts, ReceiptFacts):
                raise _TransportRefusedError("receipt_verifier_invalid")
            return facts

        return TransportResult(classified, verify, response=response)


class _McpToolBackend:
    """Private adapter that sends a protected MCP tool call (B04.5, #759).

    ``config`` carries two runtime-owned values: ``call`` (:class:`~caliber.effects.connectors.mcp_tool.McpToolCall`: which
    server record and tool, and how to read the record again) and ``receipt_verifier`` (the same authoritative check the HTTP
    backend requires). Before anything is sent the server record is **read again** and the call is classified again, from
    the record, the administrator's tool policy and the inventory (never from what the server reports): the recorded server,
    tool, resource and digest must be the trusted call's, and the call must still be protected. The recorded arguments are
    what is sent, so what runs is what the approver saw. This is the one caller of the gateway's ungated transport for a
    governed MCP call, so it can never reach the dispatcher again.

    There is no idempotency key, no precondition and no lookup by key (the capability record promises none): the call is sent
    **once**. A failure after the request may have left (a timeout, a dropped session, an error the server reports about a call
    it ran) is ``ambiguous`` and leaves the proposal ``unknown``; only a failure the gateway proves came before the request
    (policy, egress, allowlist, a session that never opened) is ``proven_not_sent``. A returned result is untrusted data: it is
    bounded and must be plain JSON, and it is only a claim until the run's receipt verifier confirms the effect.
    """

    def perform(self, request: EffectRequest, config: Mapping[str, Any] | None) -> TransportResult:
        if (
            request.view.connector != _mcp_tool.CONNECTOR
            or request.view.connector_version != _mcp_tool.VERSION
        ):
            raise _TransportRefusedError("connector_binding_mismatch")
        settings = dict(config or {})
        call = settings.get("call")
        verifier = settings.get("receipt_verifier")
        if not isinstance(call, _mcp_tool.McpToolCall) or not callable(verifier):
            raise _TransportRefusedError("backend_config_missing")
        args = request.intent.get("args")
        if not isinstance(args, Mapping):
            raise _TransportRefusedError("connector_intent_unsupported")
        try:
            server, arguments = _mcp_tool.check_recorded(
                call, resource=request.view.resource, args=args
            )
        except _mcp_tool.McpCallRefused as exc:
            raise _TransportRefusedError(exc.code) from exc
        try:
            result = _raw_mcp_transport.invoke_tool_raw_sync(
                server,
                tool_name=call.tool_name,
                arguments=arguments,
                timeout_seconds=call.timeout_seconds,
            )
        except Exception as exc:
            return TransportResult(classify_error(exc))
        bounded = _mcp_tool.bounded_result(result)
        if bounded is None:
            # The server ran the call and answered with something that cannot be read as a bounded plain-JSON result.
            return TransportResult(
                Classified(TransportOutcome.AMBIGUOUS, "result_out_of_bounds", None, 1)
            )
        response = {"result": bounded[0]}
        classified = Classified(TransportOutcome.COMMITTED_RECEIPT, "tool_returned", None, 1)

        def verify() -> ReceiptFacts | None:
            facts = verifier(request, response)
            if facts is not None and not isinstance(facts, ReceiptFacts):
                raise _TransportRefusedError("receipt_verifier_invalid")
            return facts

        return TransportResult(classified, verify, response=response)


class _TransportRefusedError(RuntimeError):
    """A backend refused before sending anything; it is not a downstream result."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.never_sent = True
        self.attempts = 0
        super().__init__(code)


#: What the document-request transport sends with, apart from the downstream's address (an operator setting): the
#: operations it builds (method, path, key header, content types, attempts) and the resource it accepts. Its digest is
#: pinned in ``caliber.profile.REFERENCE_CONNECTORS``, so a transport that starts sending elsewhere is not admitted.
DOCUMENT_REQUEST_CONFIGURATION: Mapping[str, Any] = {
    "submit": _document_request.submit_config("<downstream>"),
    "lookup": _document_request.lookup_config("<downstream>"),
    "resource": _CASE_RESOURCE.pattern,
}
#: The generic HTTP transport has no fixed destination; what it sends with is the key header and the two callables
#: the caller must supply (a sender and an authoritative receipt verifier).
HTTP_CONFIGURATION: Mapping[str, Any] = {
    "idempotency_key_header": HTTP_CAPABILITY.idempotency_key_header,
    "receipt_verifier": "required",
    "sender": "required",
}

#: A local callable has no downstream key to replay, no precondition and no lookup by key: the capability says so, and the
#: pinned record accepts the two risks by name, so no one can read this transport as an exactly-once connector.
REGISTERED_TOOL_CAPABILITY = ConnectorCapability(
    connector=REGISTERED_TOOL_CONNECTOR,
    version=REGISTERED_TOOL_VERSION,
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
#: What the registered-tool transport sends with: the target it accepts and the two callables the runtime must supply
#: (the invoker of the already-bound tool, and an authoritative receipt verifier).
REGISTERED_TOOL_CONFIGURATION: Mapping[str, Any] = {
    "invoker": "required",
    "receipt_verifier": "required",
    "resource": tool_resource("<name>", "<version>"),
}

#: What the OpenAPI transport sends with (B04.4): the connector contract's own configuration, pinned by digest.
OPENAPI_HTTP_CAPABILITY = _openapi_http.CAPABILITY
OPENAPI_HTTP_CONFIGURATION = _openapi_http.CONFIGURATION

#: What the MCP transport sends with (B04.5): the connector contract's own configuration, pinned by digest.
MCP_CAPABILITY = _mcp_tool.CAPABILITY
MCP_CONFIGURATION = _mcp_tool.CONFIGURATION

# Backend names are intentionally explicit.  Future B04 children add their
# private adapters here; an arbitrary caller-supplied kind never gets a callable.
# Being listed is not being admitted: :func:`admit_backend` is the gate (B12.3a, #738).
BACKENDS: dict[str, BackendRegistration] = {
    "document_request": BackendRegistration(
        backend=_Backend(),
        connector=_document_request.CONNECTOR,
        connector_version=_document_request.VERSION,
        capability=_document_request.CAPABILITY,
        configuration=DOCUMENT_REQUEST_CONFIGURATION,
    ),
    "http": BackendRegistration(
        backend=_HttpBackend(),
        connector=HTTP_CONNECTOR,
        connector_version=HTTP_VERSION,
        capability=HTTP_CAPABILITY,
        configuration=HTTP_CONFIGURATION,
    ),
    "registered_tool": BackendRegistration(
        backend=_RegisteredToolBackend(),
        connector=REGISTERED_TOOL_CONNECTOR,
        connector_version=REGISTERED_TOOL_VERSION,
        capability=REGISTERED_TOOL_CAPABILITY,
        configuration=REGISTERED_TOOL_CONFIGURATION,
    ),
    _openapi_http.BACKEND_KIND: BackendRegistration(
        backend=_OpenApiBackend(),
        connector=_openapi_http.CONNECTOR,
        connector_version=_openapi_http.VERSION,
        capability=OPENAPI_HTTP_CAPABILITY,
        configuration=OPENAPI_HTTP_CONFIGURATION,
    ),
    _mcp_tool.BACKEND_KIND: BackendRegistration(
        backend=_McpToolBackend(),
        connector=_mcp_tool.CONNECTOR,
        connector_version=_mcp_tool.VERSION,
        capability=MCP_CAPABILITY,
        configuration=MCP_CONFIGURATION,
    ),
}


def admit_backend(backend_kind: object) -> AdmittedConnector:
    """Admit the transport registered under ``backend_kind``, or refuse before anything is recorded or sent.

    Admission binds the registered transport's implementation identity, its capability record and its wire
    configuration to the pinned record in ``caliber.profile.REFERENCE_CONNECTORS``; ``backend_kind`` only selects which
    record to check and is never trusted on its own. Call it with a kind your own code chose (a constant, or one derived
    from a trusted registry identity), never one a manifest, a tool name, a client or a model supplied: this is how a
    custom tool called ``document_request`` stays untrusted. A kind with no registration is
    :class:`UnsupportedEffectPath` ``unsupported_backend``; any other mismatch is :class:`UnsupportedEffectPath` with
    one of ``caliber.effects.admission.REFUSAL_CODES``.
    """
    if not isinstance(backend_kind, str) or backend_kind not in BACKENDS:
        raise UnsupportedEffectPath("unsupported_backend")
    return _admit_registration(backend_kind, BACKENDS[backend_kind])


def _admit_registration(backend_kind: str, registration: BackendRegistration) -> AdmittedConnector:
    """Admit exactly this registration object, so the one that is admitted is the one that is used."""
    try:
        return admit(
            _profile.REFERENCE_CONNECTORS,
            backend_kind,
            backend=registration.backend,
            connector=registration.connector,
            connector_version=registration.connector_version,
            capability=registration.capability,
            configuration=registration.configuration,
        )
    except AdmissionRefused as exc:
        raise UnsupportedEffectPath(exc.code) from exc


def _verified_admission(admission: object, fields: Mapping[str, Any]) -> AdmittedConnector:
    """``admission`` re-verified against the live registry, and bound to the connector the intent names."""
    if not isinstance(admission, AdmittedConnector):
        raise UnsupportedEffectPath("connector_not_admitted")
    live = admit_backend(admission.backend_kind)
    if live != admission or (fields.get("connector"), fields.get("connector_version")) != (
        live.connector,
        live.connector_version,
    ):
        raise UnsupportedEffectPath("connector_not_admitted")
    return live


def _inventory_effect_class(
    tool_name: str | None,
    *,
    caliber_defined: bool,
    claimed: str | None,
    floor: str | None = None,
) -> str:
    name = "" if tool_name is None else tool_name
    disposition = tool_disposition(name, caliber_defined=caliber_defined)
    if disposition == "refuse":
        raise UnsupportedEffectPath("unsupported_effect_path")
    # The model/action's class is a claim.  The inventory answer wins, and an
    # unknown or untrusted tool therefore remains irreversible_write.
    resolved = effect_class(name, caliber_defined=caliber_defined)
    if claimed is not None and claimed not in (
        "read",
        "reversible_write",
        "export",
        "irreversible_write",
    ):
        raise EffectRefused("invalid_effect_class")
    if floor is not None:
        if floor not in EFFECT_CLASSES:
            raise EffectRefused("invalid_effect_class")
        # A trusted caller's floor can only make the record stricter, never weaker.
        resolved = max(resolved, floor, key=EFFECT_CLASSES.index)
    return resolved


def _action_fields(
    action: ProposedAction | Mapping[str, Any] | None,
    *,
    intent: Intent | None,
    connector: str | None,
    connector_version: str | None,
    effect_class_name: str | None,
    resource: str | None,
    args: Mapping[str, Any] | None,
    expected_resource_version: str | None,
) -> dict[str, Any]:
    if action is not None and intent is not None:
        raise EffectRefused("conflicting_intent_inputs")
    if intent is not None:
        return {
            "connector": intent.connector,
            "connector_version": intent.connector_version,
            "effect_class": intent.effect_class,
            "resource": intent.resource,
            "args": intent.plain_args,
            "expected_resource_version": intent.expected_resource_version,
        }
    if action is not None:
        action_fields: dict[str, Any]
        if isinstance(action, ProposedAction):
            action_fields = {
                "connector": action.connector,
                "connector_version": action.connector_version,
                "effect_class": action.effect_class,
                "resource": action.resource,
                "args": action.args,
                "expected_resource_version": action.expected_resource_version,
            }
        elif isinstance(action, Mapping):
            action_fields = {
                key: action.get(key)
                for key in (
                    "connector",
                    "connector_version",
                    "effect_class",
                    "resource",
                    "args",
                    "expected_resource_version",
                )
            }
        else:
            raise EffectRefused("invalid_proposed_action")
        explicit_fields = {
            "connector": connector,
            "connector_version": connector_version,
            "effect_class": effect_class_name,
            "resource": resource,
            "args": args,
            "expected_resource_version": expected_resource_version,
        }
        if any(
            value is not None and value != action_fields[key]
            for key, value in explicit_fields.items()
        ):
            raise EffectRefused("conflicting_intent_inputs")
        return action_fields
    return {
        "connector": connector,
        "connector_version": connector_version,
        "effect_class": effect_class_name,
        "resource": resource,
        "args": args,
        "expected_resource_version": expected_resource_version,
    }


def submit_proposal(
    session: Session,
    *,
    workflow_run_id: str,
    node_id: str,
    occurrence_id: str,
    action: ProposedAction | Mapping[str, Any] | None = None,
    proposed_action: ProposedAction | Mapping[str, Any] | None = None,
    intent: Intent | None = None,
    tool_name: str | None = None,
    caliber_defined: bool = False,
    min_effect_class: str | None = None,
    connector: str | None = None,
    connector_version: str | None = None,
    effect_class_name: str | None = None,
    resource: str | None = None,
    args: Mapping[str, Any] | None = None,
    expected_resource_version: str | None = None,
    admission: AdmittedConnector | None = None,
) -> RecordedProposal:
    """Validate inventory-bound intent and persist a proposal without dispatching.

    ``caliber_defined`` is supplied only by a caller that has already proved a
    first-party built-in identity.  Untrusted or unknown tools consequently use
    the inventory's fail-closed class.  ``action`` and the explicit fields are
    convenience forms of the same immutable intent; they cannot disagree.

    ``min_effect_class`` is a floor from a caller that has classified the tool from its own trusted registry row (B04.3):
    the recorded class is the stricter of the inventory's answer and the floor. Unlike ``effect_class_name`` it is not a
    claim, so it is never taken from a model or a manifest.

    ``admission`` (:func:`admit_backend`, called by trusted code with a backend kind it chose) is required: a proposal
    that only *names* a connector, in an action, a manifest or a tool call, is refused with ``connector_not_admitted``
    before anything is written, so no approval is ever requested for a connector nobody admitted. The intent's
    connector and version must be the admitted ones.
    """
    if action is not None and proposed_action is not None:
        raise EffectRefused("conflicting_proposed_actions")
    supplied = action if action is not None else proposed_action
    fields = _action_fields(
        supplied,
        intent=intent,
        connector=connector,
        connector_version=connector_version,
        effect_class_name=effect_class_name,
        resource=resource,
        args=args,
        expected_resource_version=expected_resource_version,
    )
    _verified_admission(admission, fields)
    resolved_class = _inventory_effect_class(
        tool_name,
        caliber_defined=caliber_defined,
        claimed=fields.get("effect_class"),
        floor=min_effect_class,
    )
    try:
        bound_intent = Intent(
            connector=fields["connector"],
            connector_version=fields["connector_version"],
            effect_class=resolved_class,
            resource=fields["resource"],
            args=fields["args"],
            expected_resource_version=fields.get("expected_resource_version"),
        )
        return record_proposal(
            session,
            workflow_run_id=workflow_run_id,
            node_id=node_id,
            occurrence_id=occurrence_id,
            intent=bound_intent,
        )
    except ProposalError as exc:
        raise EffectRefused(exc.code) from exc


@contextmanager
def _session_scope(source: Session | Callable[[], Session]) -> Iterator[Session]:
    if isinstance(source, Session):
        try:
            yield source
        except Exception:
            source.rollback()
            raise
        return
    with source() as session:
        try:
            yield session
        except Exception:
            session.rollback()
            raise


def _naive_utc(moment: datetime | None) -> datetime:
    value = datetime.now(timezone.utc) if moment is None else moment
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _event_id(proposal_id: str, version: int) -> str:
    return f"effect-proposal:{proposal_id}:dispatched:{version}"


def _backend_call(
    backend: PrivateBackend | Callable[[EffectRequest, Mapping[str, Any] | None], TransportResult],
    request: EffectRequest,
    config: Mapping[str, Any] | None,
) -> TransportResult:
    if callable(backend) and not hasattr(backend, "perform"):
        result = backend(request, config)
    else:
        result = backend.perform(request, config)
    if not isinstance(result, TransportResult):
        raise TypeError("private backend must return TransportResult")
    return result


def _outcome_from_settled(
    proposal_id: str,
    classified: Classified,
    settled: Settled,
    response: Mapping[str, Any] | None = None,
) -> EffectOutcome:
    return EffectOutcome(
        proposal_id=proposal_id,
        state=settled.state,
        outcome=classified.outcome,
        receipt_id=settled.receipt_id,
        reason=settled.reason,
        applied=settled.applied,
        response=response,
    )


@dataclass(frozen=True)
class _Claim:
    request: EffectRequest
    lease: Lease
    attempt: int
    approval_id: str
    decision_id: str


def _claim_dispatch(
    session_or_factory: Session | Callable[[], Session],
    proposal_id: str,
    *,
    approval_id: str,
    approver_scopes: frozenset[str] | set[str],
    lease: Lease,
    admitted: AdmittedConnector,
    current_resource_version: str | object | None,
    evidence_hash: str | None,
    approval_validator: Callable[..., ApprovalCommit],
    now: datetime | None,
    span: GovernedSpan,
) -> _Claim | EffectOutcome:
    """Atomically validate, consume, claim and enqueue one approved proposal.

    ``span`` is the dispatch span.  It is given ids only after they have been read from a row: the
    proposal's from the proposal read, the approval's and the decision's from the validator's
    ``ApprovalCommit`` (never from the caller's ``approval_id`` argument, which is a claim until the
    validator has bound it to this proposal).
    """
    with _session_scope(session_or_factory) as session:
        state = proposal_state(session, proposal_id)
        span.bind(proposal_id=state.proposal_id)
        if state.state == DISPATCHED:
            return EffectOutcome(
                proposal_id, DISPATCHED, reason="already_dispatched", applied=False
            )
        if state.state != PROPOSED:
            raise EffectRefused("proposal_not_approved")
        view = proposal_view(session, proposal_id)
        intent_record = read_proposal(session, proposal_id)
        if (
            view.connector != admitted.connector
            or view.connector_version != admitted.connector_version
        ):
            raise EffectRefused("backend_intent_mismatch")
        try:
            # Lease first, then the validator's run -> proposal -> approval
            # locks.  No connector is reachable before this transaction commits.
            fence_owner(session, proposal_id, lease.owner_id, lease)
            checked = approval_validator(
                session,
                approval_id=approval_id,
                proposal_id=proposal_id,
                approver_scopes=approver_scopes,
                current_resource_version=current_resource_version,
                evidence_hash=evidence_hash,
                now=now,
            )
        except ApprovalCommitRefused as exc:
            raise EffectRefused(exc.code) from exc
        except ProposalError as exc:
            raise EffectRefused(exc.code) from exc
        except Exception as exc:
            raise EffectRefused("approval_validator_failed") from exc
        if not isinstance(checked, ApprovalCommit) or checked.proposal_id != proposal_id:
            raise EffectRefused("approval_validator_invalid")
        span.bind(approval_id=checked.approval_id, decision_id=checked.decision_id)

        approved = transition(
            session,
            proposal_id,
            APPROVED,
            expected_version=state.version,
            approval=checked,  # type: ignore[arg-type]
            now=now,
        )
        dispatched = transition(
            session,
            proposal_id,
            DISPATCHED,
            expected_version=approved.version,
            owner_id=lease.owner_id,
            attempt=1,
            lease=lease,
            now=now,
        )
        approval_row = session.get(CaliberRuntimeApprovalRequest, checked.approval_id)
        if approval_row is None or approval_row.consumed_at is not None:
            raise EffectRefused("approval_already_consumed")
        approval_row.consumed_at = _naive_utc(now)
        # The same UPDATE records which commit-time decision authorised this effect (B11.3c, #1061); a trigger refuses any
        # other write of the column, and a rollback of this claim leaves neither it nor the consumption.
        approval_row.commit_decision_id = checked.decision_id
        run = session.get(CaliberWorkflowRun, intent_record["workflow_run_id"])
        # The proposal reader is authoritative for intent.  The run lookup is
        # only for the outbox aggregate's project scope.
        if run is None:
            raise EffectRefused("run_not_found")
        enqueue(
            session,
            event_id=_event_id(proposal_id, dispatched.version),
            event_type="effect.proposal.dispatched",
            aggregate_type="effect_proposal",
            aggregate_id=proposal_id,
            project_id=run.project_id,
            payload={
                "proposal_id": proposal_id,
                "workflow_run_id": intent_record["workflow_run_id"],
                "node_id": intent_record["node_id"],
                "occurrence_id": intent_record["occurrence_id"],
                "intent_hash": view.intent_hash,
                "state": DISPATCHED,
                "version": dispatched.version,
                "attempt": 1,
                "owner_id": lease.owner_id,
                "dispatch_token": lease.fencing_token,
                "connector_admission": admitted.evidence(),
            },
            correlation_id=intent_record["workflow_run_id"],
            now=now,
        )
        session.commit()
        return _Claim(
            request=EffectRequest(view=proposal_view(session, proposal_id), intent=intent_record),
            lease=lease,
            attempt=1,
            approval_id=checked.approval_id,
            decision_id=checked.decision_id,
        )


def dispatch(
    session_or_factory: Session | Callable[[], Session],
    proposal_id: str,
    *,
    approval_id: str | None,
    approver_scopes: frozenset[str] | set[str] = frozenset(),
    lease: Lease | None,
    backend_kind: str,
    backend_config: Mapping[str, Any] | None = None,
    current_resource_version: str | object | None = None,
    evidence_hash: str | None = None,
    approval_validator: Callable[..., ApprovalCommit] | None = None,
    now: datetime | None = None,
) -> EffectOutcome:
    """Claim and perform one approved proposal through a registered backend.

    The caller must explicitly provide the B06.3 validator.  A missing validator
    is a refusal, never an implicit allow.  The supplied ``Lease`` is the lease
    captured by the execution owner; it is checked again in the claim and in
    every settlement write.
    """
    with governed_span("effect.dispatch", span_type="TOOL") as span:
        if approval_validator is None:
            raise EffectRefused("approval_validator_missing")
        if lease is None:
            raise EffectRefused("lease_missing")
        if not isinstance(backend_kind, str) or backend_kind not in BACKENDS:
            raise UnsupportedEffectPath("unsupported_backend")
        if not isinstance(approval_id, str) or not approval_id.strip():
            raise EffectRefused("proposal_not_approved")
        if approval_validator is not approval_valid_at_commit:
            raise EffectRefused("approval_validator_untrusted")

        # Admission comes before the claim: a transport nobody admitted, or settings it does not accept, is refused
        # with the proposal still ``proposed`` and its approval unconsumed.
        registration = BACKENDS[backend_kind]
        admitted = _admit_registration(backend_kind, registration)
        try:
            check_config(admitted, backend_config)
        except AdmissionRefused as exc:
            raise UnsupportedEffectPath(exc.code) from exc
        try:
            claim = _claim_dispatch(
                session_or_factory,
                proposal_id,
                approval_id=approval_id,
                approver_scopes=approver_scopes,
                lease=lease,
                admitted=admitted,
                current_resource_version=current_resource_version,
                evidence_hash=evidence_hash,
                approval_validator=approval_valid_at_commit,
                now=now,
                span=span,
            )
        except ProposalError as exc:
            raise EffectRefused(exc.code) from exc
        if isinstance(claim, EffectOutcome):
            return claim
        request, captured_lease, attempt = claim.request, claim.lease, claim.attempt
        try:
            transport = _backend_call(registration.backend, request, backend_config)
        except Exception as exc:
            transport = TransportResult(classify_error(exc))

        try:
            with _session_scope(session_or_factory) as session:
                state = proposal_state(session, proposal_id)
                settled = settle_dispatch(
                    session,
                    proposal_id,
                    expected_version=state.version,
                    owner_id=captured_lease.owner_id,
                    attempt=attempt,
                    lease=captured_lease,
                    outcome=transport.outcome,
                    verify=transport.verify or (lambda: None),
                    now=now,
                )
                session.commit()
                outcome = _outcome_from_settled(
                    proposal_id, transport.outcome, settled, transport.response
                )
            # The effect is the verified receipt; it exists only for a committed settlement and is
            # named here after the transaction that wrote it has committed.
            span.bind(effect_id=outcome.receipt_id)
            return outcome
        except ProposalError as exc:
            _unknown_after_failed_settle(
                session_or_factory, proposal_id, captured_lease, attempt, exc.code, now
            )
            raise EffectRefused(exc.code) from exc
        except Exception as exc:
            # The send happened (or may have) and nothing was recorded: a verifier that raised, a store that went away.
            _unknown_after_failed_settle(
                session_or_factory, proposal_id, captured_lease, attempt, type(exc).__name__, now
            )
            raise


#: Why a settle failed that says someone else owns the row now (a takeover, another owner or attempt, a move already made):
#: the dispatcher must not write over it. Every other failure leaves the attempt ambiguous in *this* dispatcher's hands.
_NOT_OURS = frozenset({LEASE_LOST, STALE_OWNER, STALE_VERSION, ILLEGAL_TRANSITION})


def _unknown_after_failed_settle(
    session_or_factory: Session | Callable[[], Session],
    proposal_id: str,
    lease: Lease,
    attempt: int,
    why: str,
    now: datetime | None,
) -> None:
    """Record ``unknown`` for an attempt this dispatcher sent and could not settle, so it is not ``dispatched`` for good (B05.5d).

    A settle that fails while this dispatcher still holds its lease leaves the row ``dispatched`` under the *current* fencing
    token, which the reconciler's takeover rule does not touch. It is moved to ``unknown`` here, in a fresh transaction under
    the same captured lease (the move is fenced like the settle: a lost lease, another owner or a row someone else already
    moved records nothing), and the reconciler asks the downstream by key. Best effort by design: if the store is still down
    the move fails too, nothing is raised from here (the caller raises the original failure), and the reconciler's bounded
    in-flight rule (:func:`caliber.effects.records.expire_in_flight`) moves the row once it has been in flight too long.
    Only identifiers and exception class names are logged: never a payload.
    """
    if why in _NOT_OURS:
        return
    try:
        with _session_scope(session_or_factory) as session:
            state = proposal_state(session, proposal_id)
            if state.state != DISPATCHED:
                return
            transition(
                session,
                proposal_id,
                UNKNOWN,
                expected_version=state.version,
                owner_id=lease.owner_id,
                attempt=attempt,
                reason=f"settle_failed:{why}"[:128],
                lease=lease,
                now=now,
            )
            session.commit()
    except Exception as exc:
        logger.warning(
            "effect %s: the settle failed (%s) and recording unknown failed too (%s); "
            "the reconciler's in-flight bound will move it",
            proposal_id,
            why,
            type(exc).__name__,
        )


__all__ = [
    "BACKENDS",
    "DOCUMENT_REQUEST_CONFIGURATION",
    "HTTP_CAPABILITY",
    "HTTP_CONFIGURATION",
    "HTTP_CONNECTOR",
    "HTTP_VERSION",
    "MCP_CAPABILITY",
    "MCP_CONFIGURATION",
    "OPENAPI_HTTP_CAPABILITY",
    "OPENAPI_HTTP_CONFIGURATION",
    "BackendRegistration",
    "EffectOutcome",
    "EffectRefused",
    "EffectRequest",
    "PrivateBackend",
    "TransportResult",
    "UnsupportedEffectPath",
    "admit_backend",
    "dispatch",
    "submit_proposal",
]
