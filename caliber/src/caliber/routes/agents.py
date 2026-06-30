"""``/caliber/agents`` endpoints — list, read, register, update.

The agent record is the linchpin everything else hangs off: a verification
item belongs to an agent, a refinement job runs on an agent, an approval
applies to an agent's artifact. Register-and-pause are the two operator
levers that keep CALIBER's pipeline scope correct over time.

* ``POST /caliber/agents`` — register a new agent (typically once per
  agent at deploy time, via an infra script).
* ``PATCH /caliber/agents/{agent_id}`` — partial update; the
  ``enabled`` field is the pause/resume toggle the worker reads when
  deciding whether to claim a queued job for this agent.
* ``DELETE /caliber/agents/{agent_id}`` — remove an agent and cascade
  its dependent verification/refinement/approval/checkpoint/regression
  rows in one transaction.

Every write goes through ``audit_record`` in the same transaction as the
mutation, mirroring the convention used by the verification-queue endpoints.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from caliber.audit import record as audit_record
from caliber.auth import SCOPE_ADMIN, require_scopes, require_user, resolve_identity
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberApprovalRequest,
    CaliberRefinementJob,
    CaliberRegressionRun,
    CaliberRollbackCheckpoint,
    CaliberSkill,
    CaliberVerificationItem,
)
from caliber.db.scoping import apply_visibility_filter, get_visible
from caliber.prompt_targets import is_hidden_prompt_target
from caliber.routes._deps import (
    envelope_response,
    get_session_factory,
    list_limit,
    parse_json_object,
    visibility_param,
)
from caliber.schemas import (
    AgentConfigSchema,
    AgentRegisterRequest,
    AgentSkillsResponse,
    AgentUpdateRequest,
    SkillSchema,
)
from caliber.skill_targets import is_hidden_skill_target

LIST_PATH = "/ajax-api/2.0/mlflow/caliber/agents"
DETAIL_PATH = "/ajax-api/2.0/mlflow/caliber/agents/{agent_id}"
SKILLS_PATH = "/ajax-api/2.0/mlflow/caliber/agents/{agent_id}/skills"


def _extract_skill_refs(optimizer_config: object) -> list[str]:
    """Return the list of skill names cited in ``optimizer_config.skills``.

    Convention: agents reference skills by *name* under a top-level
    ``skills`` key in their ``optimizer_config``. Cited names are
    resolved server-side on demand — we don't denormalize the
    skill_id into the agent config because skills can be archived /
    replaced without rewriting every agent that cites them.
    """
    if not isinstance(optimizer_config, dict):
        return []
    raw = optimizer_config.get("skills", [])
    if not isinstance(raw, list):
        return []
    return [str(s) for s in raw if isinstance(s, str) and s]


async def list_agents(request: Request) -> JSONResponse:
    """Return registered agents (visible to the caller), bounded by ``?limit``/``?offset``."""
    require_user(request)
    identity = resolve_identity(request)
    factory = get_session_factory(request)
    limit, offset = list_limit(request)
    with factory() as session:
        stmt = apply_visibility_filter(
            select(CaliberAgentConfig),
            CaliberAgentConfig,
            identity,
            identity.active_project_id,
            only=visibility_param(request),
        )
        rows = session.execute(stmt.limit(limit).offset(offset)).scalars().all()
    # Auto-provisioned hidden prompt/skill targets are a runtime-identity
    # implementation detail (see :mod:`caliber.prompt_targets` and
    # :mod:`caliber.skill_targets`); they must never appear as agents.
    items = [
        AgentConfigSchema.model_validate(row)
        for row in rows
        if not is_hidden_prompt_target(row) and not is_hidden_skill_target(row)
    ]
    return envelope_response(items)


async def get_agent(request: Request) -> JSONResponse:
    """Return a single agent by ``agent_id``.

    404s with a structured error payload when the agent does not exist. The
    ``ok`` / ``not_found`` / ``invalid_request`` error-code vocabulary tracks
    the convention.
    """
    require_user(request)
    identity = resolve_identity(request)
    agent_id = request.path_params["agent_id"]
    factory = get_session_factory(request)
    with factory() as session:
        row = get_visible(
            session, CaliberAgentConfig, CaliberAgentConfig.agent_id, agent_id, identity
        )
    if row is None:
        raise HTTPException(status_code=404, detail=f"agent {agent_id!r} not found")
    return envelope_response(AgentConfigSchema.model_validate(row))


async def register_agent(request: Request) -> JSONResponse:
    """Create a new agent record.

    409 if the ``agent_id`` already exists — registration is a one-shot
    operation; further changes go through ``PATCH``. The ``experiment_id``
    column also has a unique constraint, so re-using one is also a 409.
    """
    body = await parse_json_object(request)
    payload = AgentRegisterRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN])
    identity = resolve_identity(request)

    factory = get_session_factory(request)
    with factory() as session:
        if session.get(CaliberAgentConfig, payload.agent_id) is not None:
            raise HTTPException(
                status_code=409,
                detail=f"agent {payload.agent_id!r} is already registered",
            )

        existing_experiment = (
            session.execute(
                select(CaliberAgentConfig).where(
                    CaliberAgentConfig.experiment_id == payload.experiment_id
                )
            )
            .scalars()
            .first()
        )
        if existing_experiment is not None:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"experiment_id {payload.experiment_id!r} is already bound to "
                    f"agent {existing_experiment.agent_id!r}"
                ),
            )

        agent = CaliberAgentConfig(
            agent_id=payload.agent_id,
            experiment_id=payload.experiment_id,
            name=payload.name,
            # Owner is the authenticated actor (payload.owner is ignored).
            owner=actor,
            project_id=identity.active_project_id,
            visibility="project" if identity.active_project_id else "user",
            artifact_types=payload.artifact_types,
            eval_thresholds=payload.eval_thresholds,
            optimizer_config=payload.optimizer_config,
            approval_policy=payload.approval_policy,
            optimize_for=payload.optimize_for,
            collaboration_mode=payload.collaboration_mode,
            enabled=payload.enabled,
            required_approvals=payload.required_approvals,
        )
        session.add(agent)
        session.flush()

        audit_record(
            session,
            actor=actor,
            action="register_agent",
            entity_type="agent",
            entity_id=agent.agent_id,
            details={
                "experiment_id": agent.experiment_id,
                "enabled": agent.enabled,
                "required_approvals": agent.required_approvals,
            },
        )
        session.commit()
        agent_data = AgentConfigSchema.model_validate(agent)

    return envelope_response(agent_data, status_code=201)


_UPDATABLE_FIELDS = (
    "name",
    "owner",
    "artifact_types",
    "eval_thresholds",
    "optimizer_config",
    "approval_policy",
    "optimize_for",
    "collaboration_mode",
    "enabled",
    "required_approvals",
)


async def update_agent(request: Request) -> JSONResponse:
    """Partial-update an agent. Pass only the fields you want to change.

    The audit row records the diff (which fields changed and to what) so
    the operator history is recoverable from the audit log alone — handy
    when investigating "who paused this agent last Friday?"
    """
    agent_id = request.path_params["agent_id"]
    body = await parse_json_object(request)
    payload = AgentUpdateRequest.model_validate(body)
    actor = require_scopes(request, [SCOPE_ADMIN])

    # Pydantic ``exclude_unset=True`` is the difference between "field omitted
    # from the request" and "field explicitly set to its default value." We
    # only want to mutate the former.
    changes = payload.model_dump(exclude_unset=True)
    if not changes:
        raise HTTPException(status_code=400, detail="request body must include at least one field")

    factory = get_session_factory(request)
    with factory() as session:
        agent = session.get(CaliberAgentConfig, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"agent {agent_id!r} not found")

        diff: dict[str, dict[str, object]] = {}
        for field in _UPDATABLE_FIELDS:
            if field not in changes:
                continue
            new_value = changes[field]
            old_value = getattr(agent, field)
            if new_value != old_value:
                diff[field] = {"from": old_value, "to": new_value}
                setattr(agent, field, new_value)

        if not diff:
            # All supplied fields match current state — no-op.
            return envelope_response(AgentConfigSchema.model_validate(agent))

        audit_record(
            session,
            actor=actor,
            action="update_agent",
            entity_type="agent",
            entity_id=agent.agent_id,
            details={"changes": diff},
        )
        session.commit()
        agent_data = AgentConfigSchema.model_validate(agent)

    return envelope_response(agent_data)


async def delete_agent(request: Request) -> JSONResponse:
    """``DELETE /caliber/agents/{agent_id}`` — remove an agent and its history.

    The agent record is the linchpin a verification item, refinement job,
    approval, rollback checkpoint, and regression run all hang off of, so a
    plain delete would trip those foreign keys. We clear the dependent rows in
    FK-safe order (checkpoints/regression runs reference an approval row, so
    they go before the approvals they hang off of) inside one transaction —
    the same cascade ``delete_workflow`` runs for its fleet agents.

    Returns the deleted ``agent_id``; 404 if the agent doesn't exist.
    """
    agent_id = request.path_params["agent_id"]
    actor = require_scopes(request, [SCOPE_ADMIN])

    factory = get_session_factory(request)
    with factory() as session:
        agent = session.get(CaliberAgentConfig, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"agent {agent_id!r} not found")
        agent_name = agent.name

        # Rollback checkpoints and regression runs reference an approval row;
        # clear them before the approval requests they hang off of.
        approval_ids_stmt = select(CaliberApprovalRequest.approval_id).where(
            CaliberApprovalRequest.agent_id == agent_id
        )
        session.execute(
            delete(CaliberRollbackCheckpoint).where(
                CaliberRollbackCheckpoint.approval_id.in_(approval_ids_stmt)
            )
        )
        session.execute(
            delete(CaliberRegressionRun).where(
                CaliberRegressionRun.approval_id.in_(approval_ids_stmt)
            )
        )
        for model in (
            CaliberApprovalRequest,
            CaliberVerificationItem,
            CaliberRefinementJob,
            CaliberRollbackCheckpoint,
            CaliberRegressionRun,
        ):
            session.execute(delete(model).where(model.agent_id == agent_id))
        session.delete(agent)

        audit_record(
            session,
            actor=actor,
            action="delete_agent",
            entity_type="agent",
            entity_id=agent_id,
            details={"name": agent_name},
        )
        session.commit()

    return JSONResponse({"data": {"agent_id": agent_id, "deleted": True}})


async def get_agent_skills(request: Request) -> JSONResponse:
    """Return the skills an agent references in its ``optimizer_config``.

    Resolves skill *names* (the agent-cited handle) into full skill
    records — including archived skills, so an agent that still cites
    an old skill name surfaces the dependency clearly. The response
    also reports any *missing* references (names cited by the agent
    that don't resolve to a skill row) so the UI can flag them as
    "broken reference" rather than silently dropping.
    """
    require_user(request)
    agent_id = request.path_params["agent_id"]
    factory = get_session_factory(request)
    with factory() as session:
        agent = session.get(CaliberAgentConfig, agent_id)
        if agent is None:
            raise HTTPException(status_code=404, detail=f"agent {agent_id!r} not found")
        cited = _extract_skill_refs(agent.optimizer_config)
        if not cited:
            return envelope_response(AgentSkillsResponse(skills=[], missing=[]))
        rows = (
            session.execute(select(CaliberSkill).where(CaliberSkill.name.in_(cited)))
            .scalars()
            .all()
        )
        found_by_name = {row.name: row for row in rows}
        missing = [name for name in cited if name not in found_by_name]
        skills = [
            SkillSchema.model_validate(found_by_name[name])
            for name in cited
            if name in found_by_name
        ]
    return envelope_response(AgentSkillsResponse(skills=skills, missing=missing))


def register(app: Starlette) -> None:
    """Add the agent routes to the given Starlette application."""
    app.routes.append(Route(LIST_PATH, list_agents, methods=["GET"]))
    app.routes.append(Route(LIST_PATH, register_agent, methods=["POST"]))
    app.routes.append(Route(DETAIL_PATH, get_agent, methods=["GET"]))
    app.routes.append(Route(DETAIL_PATH, update_agent, methods=["PATCH"]))
    app.routes.append(Route(DETAIL_PATH, delete_agent, methods=["DELETE"]))
    app.routes.append(Route(SKILLS_PATH, get_agent_skills, methods=["GET"]))
