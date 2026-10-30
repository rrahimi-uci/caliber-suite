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
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from sqlalchemy.orm import Session

from caliber.db.models import CaliberRuntimeApprovalRequest, CaliberWorkflowRun
from caliber.effects.connectors import (
    Classified,
    ConnectorCapability,
    TransportOutcome,
    classify_error,
    classify_result,
)
from caliber.effects.connectors import document_request as _document_request
from caliber.effects.inventory import effect_class, tool_disposition
from caliber.effects.receipts import ReceiptFacts, Settled, settle_dispatch
from caliber.effects.records import (
    APPROVED,
    DISPATCHED,
    PROPOSED,
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
from caliber.events.outbox import enqueue
from caliber.integrations.openapi import executor as _raw_connector_transport
from caliber.ownership import Lease
from caliber.workflows.agent_step import ProposedAction
from caliber.workflows.approval_policy import (
    ApprovalCommit,
    ApprovalCommitRefused,
    approval_valid_at_commit,
)

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
    """A private transport bound to the connector identity it is allowed to send."""

    backend: PrivateBackend | Callable[[EffectRequest, Mapping[str, Any] | None], TransportResult]
    connector: str
    connector_version: str


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
            result = _raw_connector_transport.execute_openapi_http_tool(
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
            found = _raw_connector_transport.execute_openapi_http_tool(
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
            return verifier(request, response)

        return TransportResult(classified, verify, response=response)


class _TransportRefusedError(RuntimeError):
    """A backend refused before sending anything; it is not a downstream result."""

    def __init__(self, code: str) -> None:
        self.code = code
        self.never_sent = True
        self.attempts = 0
        super().__init__(code)


# Backend names are intentionally explicit.  Future B04 children add their
# private adapters here; an arbitrary caller-supplied kind never gets a callable.
BACKENDS: dict[str, BackendRegistration] = {
    "document_request": BackendRegistration(
        backend=_Backend(),
        connector=_document_request.CONNECTOR,
        connector_version=_document_request.VERSION,
    ),
    "http": BackendRegistration(
        backend=_HttpBackend(),
        connector=HTTP_CONNECTOR,
        connector_version=HTTP_VERSION,
    ),
}


def _inventory_effect_class(
    tool_name: str | None,
    *,
    caliber_defined: bool,
    claimed: str | None,
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
    connector: str | None = None,
    connector_version: str | None = None,
    effect_class_name: str | None = None,
    resource: str | None = None,
    args: Mapping[str, Any] | None = None,
    expected_resource_version: str | None = None,
) -> RecordedProposal:
    """Validate inventory-bound intent and persist a proposal without dispatching.

    ``caliber_defined`` is supplied only by a caller that has already proved a
    first-party built-in identity.  Untrusted or unknown tools consequently use
    the inventory's fail-closed class.  ``action`` and the explicit fields are
    convenience forms of the same immutable intent; they cannot disagree.
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
    resolved_class = _inventory_effect_class(
        tool_name,
        caliber_defined=caliber_defined,
        claimed=fields.get("effect_class"),
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


def _claim_dispatch(
    session_or_factory: Session | Callable[[], Session],
    proposal_id: str,
    *,
    approval_id: str,
    approver_scopes: frozenset[str] | set[str],
    lease: Lease,
    backend_connector: str,
    backend_connector_version: str,
    current_resource_version: str | object | None,
    evidence_hash: str | None,
    approval_validator: Callable[..., ApprovalCommit],
    now: datetime | None,
) -> _Claim | EffectOutcome:
    """Atomically validate, consume, claim and enqueue one approved proposal."""
    with _session_scope(session_or_factory) as session:
        state = proposal_state(session, proposal_id)
        if state.state == DISPATCHED:
            return EffectOutcome(
                proposal_id, DISPATCHED, reason="already_dispatched", applied=False
            )
        if state.state != PROPOSED:
            raise EffectRefused("proposal_not_approved")
        view = proposal_view(session, proposal_id)
        intent_record = read_proposal(session, proposal_id)
        if (
            view.connector != backend_connector
            or view.connector_version != backend_connector_version
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
            },
            correlation_id=intent_record["workflow_run_id"],
            now=now,
        )
        session.commit()
        return _Claim(
            request=EffectRequest(view=proposal_view(session, proposal_id), intent=intent_record),
            lease=lease,
            attempt=1,
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

    registration = BACKENDS[backend_kind]
    try:
        claim = _claim_dispatch(
            session_or_factory,
            proposal_id,
            approval_id=approval_id,
            approver_scopes=approver_scopes,
            lease=lease,
            backend_connector=registration.connector,
            backend_connector_version=registration.connector_version,
            current_resource_version=current_resource_version,
            evidence_hash=evidence_hash,
            approval_validator=approval_valid_at_commit,
            now=now,
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
            return _outcome_from_settled(
                proposal_id, transport.outcome, settled, transport.response
            )
    except ProposalError as exc:
        raise EffectRefused(exc.code) from exc


__all__ = [
    "BACKENDS",
    "BackendRegistration",
    "EffectOutcome",
    "EffectRefused",
    "EffectRequest",
    "HTTP_CAPABILITY",
    "HTTP_CONNECTOR",
    "HTTP_VERSION",
    "PrivateBackend",
    "TransportResult",
    "UnsupportedEffectPath",
    "dispatch",
    "submit_proposal",
]
