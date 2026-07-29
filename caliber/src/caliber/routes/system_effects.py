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

from datetime import datetime, timezone

from sqlalchemy import func, select
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_ADMIN, SCOPE_OPERATOR, require_scopes
from caliber.db.models import CaliberWebhookDeadLetter
from caliber.routes._deps import (
    envelope_response_dict,
    get_session_factory,
    parse_json_object,
)
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

_STATUS_ANY = "any"
_DEAD_LETTER_OPEN = "open"
_DEAD_LETTER_ACKNOWLEDGED = "acknowledged"
#: A replay that actually reached the receiver. Distinct from "acknowledged":
#: one means the event was delivered after all, the other means a human decided
#: it no longer needs to be.
_DEAD_LETTER_REPLAYED = "replayed"


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
    if dispatcher is None or not hasattr(dispatcher, "replay_event"):
        raise HTTPException(status_code=503, detail="no webhook dispatcher is configured")

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
        url, event = row.url, dict(row.event)

    delivered, detail = dispatcher.replay_event(url, event)

    with factory() as session:
        row = session.get(CaliberWebhookDeadLetter, dead_letter_id)
        if row is not None:
            if delivered:
                row.status = _DEAD_LETTER_REPLAYED
                row.acknowledged_by = actor
                row.acknowledged_at = datetime.now(timezone.utc)
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


def register(app: Starlette) -> None:
    app.routes.append(Route(EFFECTS_PATH, list_system_effects, methods=["GET"]))
    app.routes.append(Route(EFFECT_RESOLVE_PATH, resolve_system_effect, methods=["POST"]))
    app.routes.append(Route(DEAD_LETTERS_PATH, list_webhook_dead_letters, methods=["GET"]))
    app.routes.append(
        Route(DEAD_LETTER_ACK_PATH, acknowledge_webhook_dead_letter, methods=["POST"])
    )
    app.routes.append(Route(DEAD_LETTER_REPLAY_PATH, replay_webhook_dead_letter, methods=["POST"]))
