"""Operational surfaces for effects that need a human: stuck claims and lost webhooks.

Two related recoveries live here because both are "CALIBER could not complete something
outward-facing, and only a person can decide what happens next":

* ``/caliber/system/effects`` — indeterminate effect-ledger claims (L4); and
* ``/caliber/system/webhook-dead-letters`` — outbound events never delivered (L6).

``/caliber/system/effects`` — inspect and resolve indeterminate external effects.

The effect ledger gives external effects at-most-once semantics across a run
restart, and reports an ``indeterminate`` claim when a process died between
performing an effect and recording its outcome. That report is correct — only a human
can know whether the request reached the remote system — but the independent review
found the instruction unactionable:

    the instructed manual resolution for ``indeterminate`` has no API, CLI, or UI.

A control whose only remediation is "open a database client" is not an operational
procedure, and a blocked run stays blocked until someone does. These two endpoints
are that procedure:

``GET /caliber/system/effects``
    List ledger rows, filterable by status and run. Default ``in_progress``, because
    that is the set that needs a decision.
``POST /caliber/system/effects/{effect_key}/resolve``
    Record the decision: ``skip`` if the effect *did* happen (do not repeat it),
    ``retry`` if it did not.

Resolution is admin-scoped and audited. It asserts something about the outside world
that CALIBER cannot verify, so who claimed it and why is part of the record — this is
release-relevant evidence, not a convenience toggle.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, sessionmaker
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_ADMIN, SCOPE_OPERATOR, require_scopes
from caliber.config import CaliberConfig
from caliber.db.models import CaliberEffectProposal, CaliberWebhookDeadLetter, CaliberWorkflowRun
from caliber.effects.callbacks import CallbackBinding, check_signature, parse_bindings
from caliber.effects.connectors.document_request import (
    CONNECTOR as DOCUMENT_REQUEST,
)
from caliber.effects.connectors.document_request import (
    facts_from_lookup,
    lookup_config,
    lookup_input,
)
from caliber.effects.receipts import ReceiptFacts, record_receipt
from caliber.effects.records import (
    COMMITTED,
    DISPATCHED,
    FAILED,
    RECONCILED,
    UNKNOWN,
    ProposalError,
    ProposalView,
    proposal_state,
    proposal_view,
    transition,
)
from caliber.events.inbox import InboxPayloadMismatch, process_once
from caliber.events.outbox import payload_hash
from caliber.integrations.openapi.executor import OpenApiExecutionError, execute_openapi_http_tool
from caliber.routes._deps import (
    envelope_response_dict,
    get_session_factory,
    parse_json_object,
    read_request_body,
)
from caliber.secrets import resolve_secret
from caliber.workflows.effect_ledger import (
    IN_PROGRESS,
    RESOLUTIONS,
    EffectResolutionError,
    list_effects,
    resolve_effect,
)

EFFECTS_PATH = "/ajax-api/2.0/mlflow/caliber/system/effects"
EFFECT_RESOLVE_PATH = "/ajax-api/2.0/mlflow/caliber/system/effects/{effect_key}/resolve"
DEAD_LETTERS_PATH = "/ajax-api/2.0/mlflow/caliber/system/webhook-dead-letters"
DEAD_LETTER_REPLAY_PATH = (
    "/ajax-api/2.0/mlflow/caliber/system/webhook-dead-letters/{dead_letter_id}/replay"
)
DEAD_LETTER_ACK_PATH = (
    "/ajax-api/2.0/mlflow/caliber/system/webhook-dead-letters/{dead_letter_id}/acknowledge"
)

RECEIPT_CALLBACK_PATH = "/ajax-api/2.0/mlflow/caliber/effects/callbacks/receipt"
_CALLBACK_MAX_BODY_BYTES = 4096
_CALLBACK_CONSUMER = "effect-callback"
_MAX_KEY_CHARS = 255
_FIRST_ERROR_STATUS = 400
#: The connectors whose lookup the platform knows how to run and verify (one today).
_CALLBACK_CONNECTORS = frozenset({DOCUMENT_REQUEST})

_STATUS_ANY = "any"
_DEAD_LETTER_OPEN = "open"
_DEAD_LETTER_ACKNOWLEDGED = "acknowledged"
#: A replay that actually reached the receiver. Distinct from "acknowledged":
#: one means the event was delivered after all, the other means a human decided
#: it no longer needs to be.
_DEAD_LETTER_REPLAYED = "replayed"
#: Claimed by an in-flight replay. Transient but *persisted*, so a concurrent
#: caller is refused by the database rather than by luck.
_DEAD_LETTER_REPLAYING = "replaying"


async def list_system_effects(request: Request) -> JSONResponse:
    """``GET /caliber/system/effects`` — ledger rows needing or recording a decision.

    Operator scope to read: a stuck effect is an operational fact an on-call
    responder needs, and the row carries no payload (see ``list_effects``).

    Defaults to ``in_progress`` rather than everything, because an unbounded list of
    completed effects buries the handful that are actually blocking a run.
    """
    require_scopes(request, [SCOPE_OPERATOR])
    status = (request.query_params.get("status") or IN_PROGRESS).strip()
    run_id = (request.query_params.get("workflow_run_id") or "").strip()
    try:
        limit = int(request.query_params.get("limit") or 100)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="'limit' must be an integer") from exc

    factory = get_session_factory(request)
    with factory() as session:
        rows = list_effects(
            session,
            status=None if status.casefold() == _STATUS_ANY else status,
            workflow_run_id=run_id or None,
            limit=limit,
        )
    return envelope_response_dict(
        {
            "effects": rows,
            "status": status,
            "resolutions": list(RESOLUTIONS),
        }
    )


async def resolve_system_effect(request: Request) -> JSONResponse:
    """``POST /caliber/system/effects/{effect_key}/resolve`` — record the decision.

    Admin-scoped and audited. ``skip`` asserts an effect reached a remote system that
    CALIBER never confirmed; that assertion can mask a genuinely lost mutation, so it
    must be attributable.
    """
    effect_key_value = request.path_params["effect_key"]
    actor = require_scopes(request, [SCOPE_ADMIN])
    body = await parse_json_object(request)
    resolution = str(body.get("resolution") or "").strip()
    reason = str(body.get("reason") or "").strip()

    factory = get_session_factory(request)
    with factory() as session:
        try:
            resolved = resolve_effect(
                session,
                effect_key_value=effect_key_value,
                resolution=resolution,
                actor=actor,
                reason=reason,
            )
        except EffectResolutionError as exc:
            # 400 rather than 404 even for an unknown key: the caller is asserting a
            # fact about a specific row, and "that row is not resolvable" is the same
            # class of answer however it fails.
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit_record(
            session,
            actor=actor,
            action="resolve_effect",
            entity_type="effect_ledger",
            entity_id=effect_key_value,
            details={
                "resolution": resolution,
                "reason": reason,
                "workflow_run_id": resolved["workflow_run_id"],
                "node_id": resolved["node_id"],
                "status": resolved["status"],
            },
        )
        session.commit()
    return envelope_response_dict(resolved)


async def list_webhook_dead_letters(request: Request) -> JSONResponse:
    """``GET /caliber/system/webhook-dead-letters`` — outbound events never delivered.

    The durable counterpart to the dispatcher's in-memory ring. An undelivered webhook
    means a downstream system was not told something happened, so the record has to
    survive the restart an operator performs to fix the receiver — the ring did not.

    Defaults to ``open``, so the list is a work queue rather than a growing wall of
    already-handled failures that operators learn to scroll past.
    """
    require_scopes(request, [SCOPE_OPERATOR])
    status = (request.query_params.get("status") or _DEAD_LETTER_OPEN).strip()
    kind = (request.query_params.get("kind") or "").strip()
    try:
        limit = max(1, min(int(request.query_params.get("limit") or 100), 1000))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="'limit' must be an integer") from exc

    factory = get_session_factory(request)
    with factory() as session:
        query = select(CaliberWebhookDeadLetter).order_by(CaliberWebhookDeadLetter.failed_at.desc())
        if status.casefold() != _STATUS_ANY:
            query = query.where(CaliberWebhookDeadLetter.status == status)
        if kind:
            query = query.where(CaliberWebhookDeadLetter.kind == kind)
        rows = session.execute(query.limit(limit)).scalars().all()
        open_count = int(
            session.execute(
                select(func.count())
                .select_from(CaliberWebhookDeadLetter)
                .where(CaliberWebhookDeadLetter.status == _DEAD_LETTER_OPEN)
            ).scalar()
            or 0
        )
        entries = [
            {
                "dead_letter_id": row.dead_letter_id,
                "url": row.url,
                "event_type": row.event_type,
                "reason": row.reason,
                "attempts": row.attempts,
                # ``exhausted`` means the receiver is broken; ``overflow`` means
                # CALIBER shed load. Different causes, different fixes.
                "kind": row.kind,
                "status": row.status,
                "failed_at": row.failed_at.isoformat() if row.failed_at else None,
                "acknowledged_by": row.acknowledged_by,
                "acknowledged_at": (
                    row.acknowledged_at.isoformat() if row.acknowledged_at else None
                ),
                # The stored event can be replayed by hand, but it is a payload, so it
                # is fetched deliberately rather than included in every list read.
                "has_event": row.event is not None,
            }
            for row in rows
        ]
    return envelope_response_dict(
        {"dead_letters": entries, "status": status, "open_count": open_count}
    )


async def acknowledge_webhook_dead_letter(request: Request) -> JSONResponse:
    """``POST .../webhook-dead-letters/{id}/acknowledge`` — mark one handled.

    Acknowledging rather than deleting: the delivery failure is evidence about what a
    downstream system was never told, and letting an operator erase it would make the
    record unreliable in the one direction that matters.
    """
    dead_letter_id = request.path_params["dead_letter_id"]
    actor = require_scopes(request, [SCOPE_OPERATOR])
    body = await parse_json_object(request)
    note = str(body.get("note") or "").strip()

    factory = get_session_factory(request)
    with factory() as session:
        row = session.get(CaliberWebhookDeadLetter, dead_letter_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"dead letter {dead_letter_id!r} not found")
        row.status = _DEAD_LETTER_ACKNOWLEDGED
        row.acknowledged_by = actor
        row.acknowledged_at = datetime.now(timezone.utc)
        if note:
            row.reason = f"{row.reason} | acknowledged: {note}"[:2000]
        audit_record(
            session,
            actor=actor,
            action="acknowledge_webhook_dead_letter",
            entity_type="webhook_dead_letter",
            entity_id=dead_letter_id,
            details={"note": note, "url": row.url, "event_type": row.event_type},
        )
        session.commit()
        payload = {
            "dead_letter_id": row.dead_letter_id,
            "status": row.status,
            "acknowledged_by": row.acknowledged_by,
        }
    return envelope_response_dict(payload)


async def replay_webhook_dead_letter(request: Request) -> JSONResponse:
    """``POST .../webhook-dead-letters/{id}/replay`` — re-send one lost event.

    The report recorded "no automatic redelivery from the durable record; replay is
    manual" as an open gap, and manual meant *reconstruct the POST yourself*. Storing
    the full event was pointless without something that could send it again.

    Operator-triggered rather than automatic, deliberately. A dead letter reached this
    table because delivery already failed, and a system that retries on its own
    schedule re-sends into an outage it cannot see. The operator knows the receiver is
    fixed; the product does not.

    A replay that fails leaves the row **open** and records why, so a failed recovery
    never looks like a completed one.
    """
    dead_letter_id = request.path_params["dead_letter_id"]
    actor = require_scopes(request, [SCOPE_OPERATOR])

    dispatcher = getattr(request.app.state, "webhook_dispatcher", None)
    # ``is_enabled`` too, not merely "an object with the method exists". The app always
    # installs a dispatcher, so a presence check alone let an operator reach replay on a
    # deployment where dispatch is switched off, where it could not be signed anyway.
    if dispatcher is None or not hasattr(dispatcher, "replay_event"):
        raise HTTPException(status_code=503, detail="no webhook dispatcher is configured")
    if not getattr(dispatcher, "is_enabled", False):
        raise HTTPException(
            status_code=503,
            detail=(
                "webhook dispatch is disabled (no URLs or no signing secret), so a "
                "replay could not be signed or delivered"
            ),
        )

    factory = get_session_factory(request)
    with factory() as session:
        row = session.get(CaliberWebhookDeadLetter, dead_letter_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"dead letter {dead_letter_id!r} not found")
        if not isinstance(row.event, dict):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"dead letter {dead_letter_id!r} has no stored event to replay "
                    "(it predates event retention)"
                ),
            )
        # Claim the row before sending. Without this, two concurrent replays — or an
        # impatient double-click — each read ``open`` and each POST, duplicating a
        # delivery the operator asked for once. A conditional UPDATE lets the database
        # arbitrate rather than application ordering.
        # ``CursorResult.rowcount`` is the row count for a DML statement; the base
        # ``Result`` protocol mypy infers from ``Session.execute`` does not declare it,
        # so the narrowing is explicit rather than ignored.
        claim_result = cast(
            "CursorResult[Any]",
            session.execute(
                update(CaliberWebhookDeadLetter)
                .where(
                    CaliberWebhookDeadLetter.dead_letter_id == dead_letter_id,
                    CaliberWebhookDeadLetter.status == _DEAD_LETTER_OPEN,
                )
                .values(status=_DEAD_LETTER_REPLAYING)
            ),
        )
        claimed = claim_result.rowcount
        if not claimed:
            session.commit()
            raise HTTPException(
                status_code=409,
                detail=(
                    f"dead letter {dead_letter_id!r} is {row.status!r}, not "
                    f"{_DEAD_LETTER_OPEN!r}; another replay may be in flight"
                ),
            )
        session.commit()
        url, event = row.url, dict(row.event)

    delivered, detail = dispatcher.replay_event(url, event)

    with factory() as session:
        row = session.get(CaliberWebhookDeadLetter, dead_letter_id)
        if row is not None:
            if delivered:
                row.status = _DEAD_LETTER_REPLAYED
                row.acknowledged_by = actor
                row.acknowledged_at = datetime.now(timezone.utc)
            else:
                # Released, not left claimed: a failed replay must remain a work item
                # rather than becoming permanently unreplayable.
                row.status = _DEAD_LETTER_OPEN
            row.reason = f"{row.reason} | replay by {actor}: {detail}"[:2000]
        audit_record(
            session,
            actor=actor,
            action="replay_webhook_dead_letter",
            entity_type="webhook_dead_letter",
            entity_id=dead_letter_id,
            details={"delivered": delivered, "detail": detail, "url": url},
        )
        session.commit()

    return envelope_response_dict(
        {"dead_letter_id": dead_letter_id, "delivered": delivered, "detail": detail}
    )


# ---------------------------------------------------------------- the signed receipt callback (B05.4b, #725)


def _callback_rejected() -> JSONResponse:
    """The one refusal for every authentication failure (unknown connector or project, bad or stale signature, a secret that
    cannot be read): a probe cannot tell them apart."""
    return JSONResponse({"detail": "callback rejected"}, status_code=401)


def _callback_reply(status_code: int, **body: Any) -> JSONResponse:
    return JSONResponse(
        {"data": body} if status_code < _FIRST_ERROR_STATUS else body, status_code=status_code
    )


def _authenticated_binding(
    config: CaliberConfig, headers: dict[str, str], raw: bytes, *, now: float
) -> tuple[CallbackBinding, str] | None:
    """The binding whose secret signed this request and the delivery id, or ``None`` for any failure."""
    connector = headers.get("x-caliber-connector", "")
    project_id = headers.get("x-caliber-project", "")
    binding = next(
        (
            b
            for b in parse_bindings(config.effect_callback_connectors)
            if b.connector == connector
            and b.project_id == project_id
            and b.connector in _CALLBACK_CONNECTORS
        ),
        None,
    )
    if binding is None:
        return None
    secret = resolve_secret(binding.secret_source)
    delivery = headers.get("x-caliber-delivery", "")
    if not secret or not check_signature(
        secret,
        connector=connector,
        project_id=project_id,
        timestamp=headers.get("x-caliber-timestamp", ""),
        delivery=delivery,
        signature=headers.get("x-caliber-signature", ""),
        body=raw,
        now=now,
    ):
        return None
    return binding, delivery


def _callback_key(raw: bytes) -> str | None:
    """The idempotency key a callback names, or ``None`` if the body is not exactly ``{"idempotency_key": <key>}``."""
    try:
        body = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        return None
    key = (
        body.get("idempotency_key")
        if isinstance(body, dict) and set(body) == {"idempotency_key"}
        else None
    )
    if (
        not isinstance(key, str)
        or not 1 <= len(key) <= _MAX_KEY_CHARS
        or not key.isascii()
        or not key.isprintable()
        or key != key.strip()
    ):
        return None
    return key


def _resolve_callback_proposal(
    session: Session, binding: CallbackBinding, key: str
) -> ProposalView | None:
    """The proposal this connector may report on: its own, in the bound project. Anything else is simply not found, so a
    callback cannot learn whether a key exists elsewhere."""
    proposal_id = session.execute(
        select(CaliberEffectProposal.proposal_id)
        .join(
            CaliberWorkflowRun,
            CaliberWorkflowRun.workflow_run_id == CaliberEffectProposal.workflow_run_id,
        )
        .where(
            CaliberEffectProposal.logical_key == key,
            CaliberEffectProposal.connector == binding.connector,
            CaliberWorkflowRun.project_id == binding.project_id,
        )
    ).scalar_one_or_none()
    return None if proposal_id is None else proposal_view(session, proposal_id)


def _commit_from_callback(session: Session, proposal_id: str, facts: ReceiptFacts) -> str:
    """Record the verified receipt and commit (``unknown`` goes through ``reconciled`` first); the receipt id."""
    state = proposal_state(session, proposal_id)
    if state.state == COMMITTED and state.receipt_id is not None:
        return state.receipt_id
    receipt = record_receipt(session, proposal_id, facts)
    version = state.version
    if state.state == UNKNOWN:
        version = transition(
            session, proposal_id, RECONCILED, expected_version=version, reason="receipt_callback"
        ).version
    transition(
        session, proposal_id, COMMITTED, expected_version=version, receipt_id=receipt.receipt_id
    )
    return receipt.receipt_id


def _callback_state_reply(view: ProposalView) -> JSONResponse | None:
    """The answer for a proposal that is not waiting on a receipt, or ``None`` if it is (``unknown`` or ``reconciled``)."""
    if view.state == COMMITTED:
        return _callback_reply(200, status="already_committed")
    if view.state == FAILED:
        return _callback_reply(409, reason="proposal_failed")
    if (
        view.state == DISPATCHED
    ):  # the dispatcher (or the reconciler, once the dispatch is abandoned) still owns it
        return _callback_reply(202, status="in_flight")
    if view.state not in (UNKNOWN, RECONCILED):
        return _callback_reply(409, reason="not_dispatched")
    return None


def _process_receipt_callback(  # noqa: PLR0911 - one distinct, documented answer per refusal
    factory: sessionmaker[Session], config: CaliberConfig, headers: dict[str, str], raw: bytes
) -> JSONResponse:
    authenticated = _authenticated_binding(config, headers, raw, now=time.time())
    if authenticated is None:
        return _callback_rejected()
    binding, delivery = authenticated
    key = _callback_key(raw)
    if key is None:
        return _callback_reply(400, detail='the body must be exactly {"idempotency_key": <key>}')
    with factory() as session:
        view = _resolve_callback_proposal(session, binding, key)
    if view is None:
        return _callback_reply(404, detail="no such proposal")
    refused = _callback_state_reply(view)
    if refused is not None:
        return refused
    # The authoritative query runs outside any transaction: nothing is held open across the network call.
    try:
        found = execute_openapi_http_tool(
            execution_config=lookup_config(binding.base_url),
            input_schema=None,
            input_data=lookup_input(key),
        )
    except OpenApiExecutionError:
        return _callback_reply(502, status="lookup_failed")
    facts = facts_from_lookup(view, lookup_status=found["status_code"], lookup_body=found["json"])
    if facts is None:
        # The downstream does not (yet) hold this effect, or holds something else: not a receipt, and not a failure. The
        # delivery is not consumed, so the connector can call again.
        return _callback_reply(202, status="unknown")
    with factory() as session:
        try:
            outcome = process_once(
                session,
                consumer=_CALLBACK_CONSUMER,
                source=binding.connector,
                event_id=delivery,
                payload_hash=payload_hash(
                    {"project_id": binding.project_id, "idempotency_key": key}
                ),
                apply=lambda: _commit_from_callback(session, view.proposal_id, facts),
            )
            if outcome.applied:
                audit_record(
                    session,
                    actor=f"connector:{binding.connector}",
                    action="effect_receipt_callback",
                    entity_type="effect_proposal",
                    entity_id=view.proposal_id,
                    details={"delivery": delivery, "receipt_id": outcome.value},
                )
            session.commit()
        except InboxPayloadMismatch:
            session.rollback()
            return _callback_reply(409, reason="inbox_payload_mismatch")
        except ProposalError as exc:
            session.rollback()  # the reservation went with it: the delivery is not consumed and can be retried
            return _callback_reply(409, reason=exc.code)
    if outcome.outcome == "duplicate":
        return _callback_reply(200, status="duplicate")
    return _callback_reply(200, status="committed", receipt_id=outcome.value)


async def receive_effect_receipt(request: Request) -> JSONResponse:
    """A connector's signed "the effect for this key is done" (B05.4b, #725).

    Not session-authenticated: the connector is the caller, and its signature over the connector, project, timestamp, delivery id
    and body (``caliber.effects.callbacks``) is the admission control. The callback is a *notification*: it names an
    idempotency key and nothing else, and the platform asks the downstream itself before it records anything. A downstream that
    has no such request yet leaves the proposal ``unknown`` (202) and the delivery unconsumed.
    """
    config = request.app.state.config
    raw = await read_request_body(request, max_body_bytes=_CALLBACK_MAX_BODY_BYTES)
    return await run_in_threadpool(
        _process_receipt_callback, get_session_factory(request), config, dict(request.headers), raw
    )


def register(app: Starlette) -> None:
    app.routes.append(Route(EFFECTS_PATH, list_system_effects, methods=["GET"]))
    app.routes.append(Route(EFFECT_RESOLVE_PATH, resolve_system_effect, methods=["POST"]))
    app.routes.append(Route(DEAD_LETTERS_PATH, list_webhook_dead_letters, methods=["GET"]))
    app.routes.append(
        Route(DEAD_LETTER_ACK_PATH, acknowledge_webhook_dead_letter, methods=["POST"])
    )
    app.routes.append(Route(DEAD_LETTER_REPLAY_PATH, replay_webhook_dead_letter, methods=["POST"]))
    app.routes.append(Route(RECEIPT_CALLBACK_PATH, receive_effect_receipt, methods=["POST"]))
