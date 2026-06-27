"""``/caliber/agents/{agent_id}/rollback*`` endpoints — undo a promotion.

Every successful promotion writes a :class:`CaliberRollbackCheckpoint` row
with enough state to restore the artifact to its pre-promotion form. The
rollback endpoint reads the most recent unused checkpoint for an agent
and calls back into the promoter to perform the actual artifact-level
restore (an alias rotation, for prompts).

Routes:

* ``GET /caliber/agents/{agent_id}/checkpoints`` — list rollback
  checkpoints for an agent, newest first. The Settings / Agent History
  UI uses this to render the "Roll back to v3" affordance.
* ``POST /caliber/agents/{agent_id}/rollback`` — perform the rollback.
  Without a body, rolls back to the most recent unused checkpoint;
  with ``{"checkpoint_id": "CK-..."}`` rolls back to a specific one.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_OPERATOR, require_scopes, require_user
from caliber.db.models import CaliberAgentConfig, CaliberRollbackCheckpoint, CaliberSkill
from caliber.events.bus import EventBus
from caliber.observability import metrics
from caliber.promoter import Promoter, PromoterError, RollbackRequest
from caliber.routes._deps import envelope_response, get_session_factory, parse_json_object
from caliber.schemas import RollbackCheckpointSchema, RollbackResponse

logger = logging.getLogger("caliber.routes.rollback")

CHECKPOINTS_PATH = "/ajax-api/2.0/mlflow/caliber/agents/{agent_id}/checkpoints"
ROLLBACK_PATH = "/ajax-api/2.0/mlflow/caliber/agents/{agent_id}/rollback"


def _get_promoter(request: Request) -> Promoter:
    """Return the per-app promoter (parked on ``app.state`` by ``create_app``)."""
    promoter: Promoter = request.app.state.promoter
    return promoter


def _rollback_target_exists(session: object, agent_id: str) -> bool:
    """Whether the ``{agent_id}`` path param names a rollback-able artifact.

    The route is nominally agent-scoped, but skill promotions write checkpoints
    keyed by the skill name (skills have no ``CaliberAgentConfig`` row), so a
    skill of that name is an equally valid target.
    """
    from sqlalchemy import select as _select  # noqa: PLC0415
    from sqlalchemy.orm import Session  # noqa: PLC0415

    assert isinstance(session, Session)
    if session.get(CaliberAgentConfig, agent_id) is not None:
        return True
    return (
        session.execute(
            _select(CaliberSkill.skill_id).where(CaliberSkill.name == agent_id).limit(1)
        )
        .scalars()
        .first()
        is not None
    )


async def list_checkpoints(request: Request) -> JSONResponse:
    """Return rollback checkpoints for an agent, newest first."""
    require_user(request)
    agent_id = request.path_params["agent_id"]
    factory = get_session_factory(request)
    with factory() as session:
        if not _rollback_target_exists(session, agent_id):
            raise HTTPException(status_code=404, detail=f"agent or skill {agent_id!r} not found")
        rows = (
            session.execute(
                select(CaliberRollbackCheckpoint)
                .where(CaliberRollbackCheckpoint.agent_id == agent_id)
                .order_by(CaliberRollbackCheckpoint.created_at.desc())
            )
            .scalars()
            .all()
        )
    items = [RollbackCheckpointSchema.model_validate(row) for row in rows]
    return envelope_response(items)


async def rollback_agent(request: Request) -> JSONResponse:
    """Roll an agent's active artifact back to the prior version.

    Picks the most recent unused checkpoint for the agent by default; the
    caller can target a specific one by sending ``{"checkpoint_id": "CK-..."}``.
    Rolling back a checkpoint that has already been rolled back is a 409 —
    re-rolling would point the alias at the same version twice and produce
    a misleading "rotated" timestamp.
    """
    agent_id = request.path_params["agent_id"]
    body = await parse_json_object(request, allow_empty=True)
    # ``checkpoint_id`` is optional, but if the caller did supply it
    # we validate the shape eagerly. Before this guard, ``{"checkpoint_id": 123}``
    # or ``{"checkpoint_id": ""}`` silently fell through to the
    # "latest unused checkpoint" path — i.e. a destructive operation
    # ran against a different target than the request claimed
    # (V2 review Finding 1).
    raw_checkpoint = body.get("checkpoint_id")
    if "checkpoint_id" in body and (not isinstance(raw_checkpoint, str) or not raw_checkpoint):
        raise HTTPException(
            status_code=400,
            detail=(
                f"checkpoint_id, if provided, must be a non-empty string; got {raw_checkpoint!r}"
            ),
        )
    checkpoint_id = raw_checkpoint if isinstance(raw_checkpoint, str) else None
    actor = require_scopes(request, [SCOPE_OPERATOR])
    promoter = _get_promoter(request)

    factory = get_session_factory(request)
    with factory() as session:
        if not _rollback_target_exists(session, agent_id):
            raise HTTPException(status_code=404, detail=f"agent or skill {agent_id!r} not found")

        checkpoint = _select_checkpoint(session, agent_id, checkpoint_id)
        if checkpoint is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    "no eligible rollback checkpoint found for agent "
                    f"{agent_id!r} (a checkpoint is created on every successful "
                    "promotion; rollback consumes it)."
                ),
            )
        if checkpoint.rolled_back_at is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"checkpoint {checkpoint.checkpoint_id!r} was already rolled back at "
                    f"{checkpoint.rolled_back_at.isoformat()}"
                ),
            )

        rollback_req = RollbackRequest(
            agent_id=checkpoint.agent_id,
            artifact_type=checkpoint.artifact_type,
            version_before=checkpoint.version_before,
            checkpoint_id=checkpoint.checkpoint_id,
        )
        try:
            result = promoter.rollback(rollback_req)
        except PromoterError as exc:
            # No DB writes have happened yet. Surface 502 like the approve
            # path does on promoter failure.
            raise HTTPException(status_code=502, detail=f"rollback failed: {exc}") from exc

        checkpoint.rolled_back_at = datetime.now(timezone.utc)
        checkpoint.rolled_back_by = actor

        audit_record(
            session,
            actor=actor,
            action="rollback",
            entity_type="agent",
            entity_id=agent_id,
            details={
                "checkpoint_id": checkpoint.checkpoint_id,
                "artifact_ref": result.artifact_ref,
                "version_before": checkpoint.version_before,
                "version_after": checkpoint.version_after,
            },
        )
        session.commit()

        response = RollbackResponse(
            checkpoint=RollbackCheckpointSchema.model_validate(checkpoint),
            rotated_to=result.artifact_ref,
            rotated_at=result.rotated_at,
        )

    metrics.record_rollback(agent_id=agent_id)
    bus: EventBus | None = getattr(request.app.state, "event_bus", None)
    if bus is not None:
        bus.publish(
            {
                "type": "agent.rolled_back",
                "agent_id": agent_id,
                "checkpoint_id": checkpoint.checkpoint_id,
                "rotated_to": result.artifact_ref,
            }
        )
    return envelope_response(response)


def _select_checkpoint(
    session: object, agent_id: str, checkpoint_id: object
) -> CaliberRollbackCheckpoint | None:
    """Pick the checkpoint to roll back to.

    Explicit ``checkpoint_id`` wins (and the caller is responsible for
    ordering correctness). Without one, we use the most recent unused
    checkpoint — the canonical "undo the last thing" semantic the UI
    button maps onto.

    Cross-agent guard: when an explicit ``checkpoint_id`` is supplied,
    we verify it belongs to the URL's ``agent_id`` before returning.
    A request to ``/agents/A/rollback`` with a ``checkpoint_id`` from
    agent ``B`` would otherwise silently roll back ``B`` while the
    audit row claimed ``A`` — a cross-resource authorization defect.
    Returning ``None`` here lets the caller surface a clean 404.
    """
    from sqlalchemy.orm import Session  # noqa: PLC0415  — local import to keep signature loose

    assert isinstance(session, Session)
    if isinstance(checkpoint_id, str) and checkpoint_id:
        checkpoint = session.get(CaliberRollbackCheckpoint, checkpoint_id)
        if checkpoint is None:
            return None
        if checkpoint.agent_id != agent_id:
            return None
        return checkpoint
    return (
        session.execute(
            select(CaliberRollbackCheckpoint)
            .where(CaliberRollbackCheckpoint.agent_id == agent_id)
            .where(CaliberRollbackCheckpoint.rolled_back_at.is_(None))
            .order_by(CaliberRollbackCheckpoint.created_at.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )


def register(app: Starlette) -> None:
    """Add the rollback routes to the given Starlette application."""
    app.routes.append(Route(CHECKPOINTS_PATH, list_checkpoints, methods=["GET"]))
    app.routes.append(Route(ROLLBACK_PATH, rollback_agent, methods=["POST"]))
