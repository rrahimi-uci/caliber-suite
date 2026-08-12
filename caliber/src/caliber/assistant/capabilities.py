"""Capability registry — the single definition of an operation Aria can perform.

This is Phase 0 of the agentic-orchestration plan
(see ``docs/12-assistant/aria-agentic-orchestration.md``). A *capability* declares
one platform operation once — its key, risk tier, RBAC scopes, LLM-facing input
schema, and a handler that performs the work through the existing service/route
code. Aria's agent toolset then becomes a *projection* of this registry, so a
newly-registered capability is reachable by Aria without hand-writing a bespoke
tool — closing the parity gap where features like Judges and Review Queues were
invisible to the assistant.

Risk tiers (a superset of the three the toolset has used):

* ``read``   — no side effects; always available.
* ``safe``   — reversible / sandboxed; build mode + every non-manual policy.
* ``mutate`` — real domain write; build mode + ``auto_all``/``full_autonomy``.
* ``gated``  — **new**: irreversible (publish/promote/deploy/spend). NEVER
  auto-executed in a synchronous turn — it always requires a human decision, so
  the toolset does not advertise or dispatch gated capabilities. Plans (a later
  phase) drive gated steps through an explicit human-approval interaction.

This module is import-light and deliberately does **not** import the assistant
service or the agent toolset (they import it), so handlers use lazy imports.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

# Risk tiers — single-sourced here; the agent toolset imports them.
TIER_READ = "read"
TIER_SAFE = "safe"
TIER_MUTATE = "mutate"
TIER_GATED = "gated"

_VALID_TIERS = frozenset({TIER_READ, TIER_SAFE, TIER_MUTATE, TIER_GATED})


@dataclass(frozen=True)
class CapabilityContext:
    """What a capability handler needs to do its work, injected per call.

    Mirrors the dependencies the existing ``_t_*`` agent tools rely on
    (``session_factory`` + ``config``) plus the acting identity, so handlers can
    perform real, audited operations without a Starlette ``Request``.
    """

    session_factory: Any
    config: Any
    actor: str
    project_id: str | None = None

    def identity(self) -> Any:
        """The acting identity, resolved for visibility filtering.

        Capability handlers run without a Starlette ``Request``, so they cannot use
        ``resolve_identity``. Building the same :class:`~caliber.auth.CaliberIdentity`
        here is what lets them apply the *same* visibility predicate the REST routes
        do — the review found these handlers listing judges and review queues
        globally, so Aria could enumerate another project's artifacts.
        """
        from caliber.auth import CaliberIdentity, scopes_for_user  # noqa: PLC0415

        return CaliberIdentity(
            user_id=self.actor,
            scopes=scopes_for_user(self.config, self.actor),
            active_project_id=self.project_id,
        )


@dataclass(frozen=True)
class Capability:
    """One platform operation, defined once."""

    key: str  # dotted, e.g. "judge.create"
    title: str
    description: str  # LLM-facing
    tier: str
    handler: Callable[[CapabilityContext, dict[str, Any]], Any]
    required_scopes: tuple[str, ...] = ()
    properties: Mapping[str, Any] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    # Names this capability guarantees in its result.  The deterministic plan
    # builder uses these to wire a later input to a prior step without guessing
    # from prose (for example review_queue.create.queue_id -> add_items.queue_id).
    result_properties: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.tier not in _VALID_TIERS:
            raise ValueError(f"capability {self.key!r} has invalid tier {self.tier!r}")

    @property
    def tool_name(self) -> str:
        """OpenAI/Anthropic tool names disallow dots; map ``judge.create`` -> ``judge_create``."""
        return self.key.replace(".", "_").replace("-", "_")

    def to_spec(self) -> dict[str, Any]:
        """Render the OpenAI-style function spec the engines advertise."""
        from caliber.assistant.tools import _fn  # noqa: PLC0415

        return _fn(self.tool_name, self.description, dict(self.properties), list(self.required))

    @property
    def input_schema(self) -> dict[str, Any]:
        """JSON Schema used by both engines and the human plan-input form."""
        return {
            "type": "object",
            "properties": dict(self.properties),
            "required": list(self.required),
            "additionalProperties": False,
        }


# --- registry ---------------------------------------------------------------

_REGISTRY: dict[str, Capability] = {}
_BY_TOOL_NAME: dict[str, str] = {}


def register(cap: Capability) -> None:
    """Register a capability (idempotent by key — re-registration replaces)."""
    _REGISTRY[cap.key] = cap
    _BY_TOOL_NAME[cap.tool_name] = cap.key


def unregister(key: str) -> None:
    """Remove a capability (mainly for tests that register a transient one)."""
    cap = _REGISTRY.pop(key, None)
    if cap is not None:
        _BY_TOOL_NAME.pop(cap.tool_name, None)


def registered_capabilities() -> list[Capability]:
    return list(_REGISTRY.values())


def get_capability(key: str) -> Capability | None:
    return _REGISTRY.get(key)


def capability_by_tool_name(tool_name: str) -> Capability | None:
    key = _BY_TOOL_NAME.get(tool_name)
    return _REGISTRY.get(key) if key is not None else None


# --- built-in capabilities ---------------------------------------------------
#
# Phase 0 registers the operations Aria could not previously reach. Handlers call
# the SAME core helpers the REST routes use (extracted in routes/judges.py and
# routes/review_queues.py), so there is one definition of the business logic.


def _judge_list(ctx: CapabilityContext, _args: dict[str, Any]) -> Any:
    from sqlalchemy import select  # noqa: PLC0415

    from caliber.db.models import CaliberJudge  # noqa: PLC0415
    from caliber.db.scoping import apply_visibility_filter  # noqa: PLC0415

    identity = ctx.identity()
    with ctx.session_factory() as session:
        # Visibility-scoped: this listed every active judge in the database, so Aria
        # could enumerate another project's judges (including their names and models)
        # through a capability the REST route would have filtered.
        stmt = apply_visibility_filter(
            select(CaliberJudge).where(CaliberJudge.status == "active"),
            CaliberJudge,
            identity,
            identity.active_project_id,
        ).order_by(CaliberJudge.name)
        rows = session.execute(stmt).scalars().all()
        return [
            {"judge_id": r.judge_id, "name": r.name, "model": r.model, "description": r.description}
            for r in rows
        ]


def _judge_create(ctx: CapabilityContext, args: dict[str, Any]) -> Any:
    from caliber.routes.judges import create_judge_record  # noqa: PLC0415
    from caliber.schemas import JudgeCreateRequest  # noqa: PLC0415

    payload = JudgeCreateRequest.model_validate(args)
    with ctx.session_factory() as session:
        judge = create_judge_record(
            session, payload=payload, actor=ctx.actor, project_id=ctx.project_id
        )
        session.commit()
        return {"judge_id": judge.judge_id, "name": judge.name, "status": judge.status}


def _review_queue_list(ctx: CapabilityContext, _args: dict[str, Any]) -> Any:
    from sqlalchemy import select  # noqa: PLC0415

    from caliber.db.models import CaliberReviewQueue  # noqa: PLC0415
    from caliber.db.scoping import apply_visibility_filter  # noqa: PLC0415

    identity = ctx.identity()
    with ctx.session_factory() as session:
        # Same defect as the judge list: globally unscoped.
        stmt = apply_visibility_filter(
            select(CaliberReviewQueue).where(CaliberReviewQueue.status == "active"),
            CaliberReviewQueue,
            identity,
            identity.active_project_id,
        ).order_by(CaliberReviewQueue.name)
        rows = session.execute(stmt).scalars().all()
        return [
            {"queue_id": r.queue_id, "name": r.name, "questions": len(r.questions or [])}
            for r in rows
        ]


def _review_queue_create(ctx: CapabilityContext, args: dict[str, Any]) -> Any:
    from caliber.routes.review_queues import create_review_queue_record  # noqa: PLC0415
    from caliber.schemas import ReviewQueueCreateRequest  # noqa: PLC0415

    payload = ReviewQueueCreateRequest.model_validate(args)
    with ctx.session_factory() as session:
        queue = create_review_queue_record(
            session, payload=payload, actor=ctx.actor, project_id=ctx.project_id
        )
        session.commit()
        return {"queue_id": queue.queue_id, "name": queue.name, "questions": len(queue.questions)}


def _eval_dataset_create(ctx: CapabilityContext, args: dict[str, Any]) -> Any:
    from caliber.routes.eval_datasets import create_eval_dataset_record  # noqa: PLC0415
    from caliber.schemas import EvalDatasetCreateRequest  # noqa: PLC0415

    # Owner is always the acting identity (the schema requires the field but the
    # record helper ignores any caller-supplied owner).
    payload = EvalDatasetCreateRequest.model_validate({**args, "owner": ctx.actor})
    with ctx.session_factory() as session:
        dataset = create_eval_dataset_record(
            session, payload=payload, actor=ctx.actor, project_id=ctx.project_id
        )
        session.commit()
        return {"dataset_id": dataset.dataset_id, "name": dataset.name}


def _review_queue_add_items(ctx: CapabilityContext, args: dict[str, Any]) -> Any:
    from caliber.db.models import CaliberReviewQueue  # noqa: PLC0415
    from caliber.db.scoping import get_visible  # noqa: PLC0415
    from caliber.routes.review_queues import add_review_items_records  # noqa: PLC0415

    queue_id = str(args.get("queue_id") or "")
    trace_ids = [str(t) for t in (args.get("trace_ids") or []) if str(t).strip()]
    if not queue_id or not trace_ids:
        raise ValueError("queue_id and a non-empty trace_ids list are required")
    identity = ctx.identity()
    with ctx.session_factory() as session:
        # Resolve the queue through the caller's visibility before writing to it: the
        # handler accepted an unscoped queue id, so Aria could add items to another
        # project's review queue.
        if (
            get_visible(
                session, CaliberReviewQueue, CaliberReviewQueue.queue_id, queue_id, identity
            )
            is None
        ):
            raise ValueError(f"review queue {queue_id!r} not found")
        created = add_review_items_records(
            session,
            queue_id=queue_id,
            trace_ids=trace_ids,
            experiment_id=args.get("experiment_id"),
            assigned_to=args.get("assigned_to"),
            actor=ctx.actor,
        )
        session.commit()
        return {"queue_id": queue_id, "added": len(created)}


def _workflow_calibrate(ctx: CapabilityContext, args: dict[str, Any]) -> Any:
    # The first ASYNC capability: enqueues a real workflow-calibration job and
    # returns an AsyncJobHandle so the plan parks (waiting_job) until the job
    # finishes (the MLflow resolver maps kind="refinement_job" -> done/failed).
    from caliber.assistant.executor import AsyncJobHandle  # noqa: PLC0415
    from caliber.routes.workflow_calibration import (  # noqa: PLC0415
        enqueue_workflow_calibration_run,
    )
    from caliber.schemas import WorkflowCalibrationRunRequest  # noqa: PLC0415

    workflow_id = str(args.get("workflow_id") or "")
    agent_id = str(args.get("agent_id") or "")
    if not workflow_id or not agent_id:
        raise ValueError("workflow_id and agent_id are required")
    payload = WorkflowCalibrationRunRequest.model_validate({"agent_id": agent_id})
    with ctx.session_factory() as session:
        resp = enqueue_workflow_calibration_run(
            session=session,
            workflow_id=workflow_id,
            payload=payload,
            actor=ctx.actor,
            config=ctx.config,
        )
        session.commit()
        return AsyncJobHandle(
            job_id=resp.job.job_id,
            kind="refinement_job",
            evidence={"item_id": resp.item.item_id},
        )


_QUESTION_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "key": {"type": "string"},
        "title": {"type": "string"},
        "type": {"type": "string", "enum": ["pass_fail", "categorical", "numeric", "text"]},
        "options": {"type": "array", "items": {"type": "string"}},
        "required": {"type": "boolean"},
        "target": {"type": "string", "enum": ["feedback", "expectation"]},
    },
    "required": ["key", "title"],
}


def _register_builtins() -> None:
    register(
        Capability(
            key="judge.list",
            title="List judges",
            description="List active custom LLM judges (judge_id, name, model).",
            tier=TIER_READ,
            handler=_judge_list,
            required_scopes=("viewer",),
            result_properties=("judges",),
        )
    )
    register(
        Capability(
            key="judge.create",
            title="Create judge",
            description=(
                "Create a custom LLM judge (MLflow make_judge). 'instructions' must "
                "reference at least one of {{ inputs }}, {{ outputs }}, {{ expectations }}."
            ),
            tier=TIER_MUTATE,
            handler=_judge_create,
            required_scopes=("operator",),
            properties={
                "name": {"type": "string"},
                "instructions": {"type": "string"},
                "description": {"type": "string"},
                "model": {"type": "string"},
                "feedback_value_type": {"type": "string", "enum": ["bool", "int", "float", "str"]},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            required=("name", "instructions"),
            result_properties=("judge_id", "name", "status"),
        )
    )
    register(
        Capability(
            key="review_queue.list",
            title="List review queues",
            description="List active structured human-review queues (queue_id, name, question count).",
            tier=TIER_READ,
            handler=_review_queue_list,
            required_scopes=("viewer",),
            result_properties=("review_queues",),
        )
    )
    register(
        Capability(
            key="review_queue.create",
            title="Create review queue",
            description=(
                "Create a structured human-review queue with a label schema of questions "
                "(pass_fail | categorical | numeric | text; target feedback or expectation)."
            ),
            tier=TIER_MUTATE,
            handler=_review_queue_create,
            required_scopes=("operator",),
            properties={
                "name": {"type": "string"},
                "description": {"type": "string"},
                "questions": {"type": "array", "items": _QUESTION_ITEM_SCHEMA},
                "reviewers": {"type": "array", "items": {"type": "string"}},
            },
            required=("name", "questions"),
            result_properties=("queue_id", "name", "questions"),
        )
    )
    register(
        Capability(
            key="eval_dataset.create",
            title="Create test set",
            description="Create a versioned evaluation test set (input/expected examples are added separately).",
            tier=TIER_MUTATE,
            handler=_eval_dataset_create,
            required_scopes=("operator",),
            properties={
                "name": {"type": "string"},
                "description": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            required=("name",),
            result_properties=("dataset_id", "name"),
        )
    )
    register(
        Capability(
            key="review_queue.add_items",
            title="Enqueue traces for review",
            description="Add traces (by id) to a review queue so reviewers can score them.",
            tier=TIER_MUTATE,
            handler=_review_queue_add_items,
            required_scopes=("operator",),
            properties={
                "queue_id": {"type": "string"},
                "trace_ids": {"type": "array", "items": {"type": "string"}},
                "experiment_id": {"type": "string"},
                "assigned_to": {"type": "string"},
            },
            required=("queue_id", "trace_ids"),
            result_properties=("queue_id", "added"),
        )
    )
    register(
        Capability(
            key="workflow.calibrate",
            title="Calibrate workflow",
            description=(
                "Enqueue an asynchronous workflow-calibration job (optimizes the "
                "workflow against its dataset). The plan parks until the job finishes."
            ),
            tier=TIER_MUTATE,
            handler=_workflow_calibrate,
            required_scopes=("operator",),
            properties={
                "workflow_id": {"type": "string"},
                "agent_id": {"type": "string"},
            },
            required=("workflow_id", "agent_id"),
            result_properties=("job_id",),
        )
    )


def _openapi_tool_list(ctx: CapabilityContext, _args: dict[str, Any]) -> Any:
    """List published OpenAPI-derived tools Aria may discover.

    This is the "explicit follow-on adapter" the OpenAPI Integrations proposal
    calls for in §4.4 and §10 — Aria's callable surface is not "all published
    tools," so an OpenAPI-backed tool is reachable here only because this
    capability exists, not automatically. Every published tool is listed
    (including write/``external_action`` ones) so Aria can discover what exists
    even though :func:`_openapi_tool_invoke` refuses to call anything but a read.
    """

    from sqlalchemy import select  # noqa: PLC0415

    from caliber.db.models import CaliberToolRegistry  # noqa: PLC0415
    from caliber.db.scoping import apply_visibility_filter  # noqa: PLC0415

    identity = ctx.identity()
    with ctx.session_factory() as session:
        stmt = apply_visibility_filter(
            select(CaliberToolRegistry).where(
                CaliberToolRegistry.execution_backend == "openapi_http",
                CaliberToolRegistry.status == "active",
            ),
            CaliberToolRegistry,
            identity,
            identity.active_project_id,
        ).order_by(CaliberToolRegistry.name)
        rows = session.execute(stmt).scalars().all()
        return [
            {
                "tool_id": row.tool_id,
                "name": row.name,
                "version": row.version,
                "description": row.description,
                "side_effect_level": row.side_effect_level,
                "requires_approval": row.requires_approval,
                "input_schema": row.input_schema,
            }
            for row in rows
        ]


def invoke_openapi_published_tool(
    ctx: CapabilityContext,
    *,
    tool_id: str,
    tool_input: dict[str, Any],
    allowed_side_effect_levels: set[str] | None = None,
) -> dict[str, Any]:
    """Run one published OpenAPI-backed tool through Aria's governed surface."""

    from sqlalchemy import select  # noqa: PLC0415

    from caliber.audit import record as audit_record  # noqa: PLC0415
    from caliber.db.models import CaliberToolRegistry  # noqa: PLC0415
    from caliber.db.scoping import apply_visibility_filter  # noqa: PLC0415
    from caliber.egress import EgressPolicy  # noqa: PLC0415
    from caliber.integrations.openapi.executor import execute_openapi_http_tool  # noqa: PLC0415

    identity = ctx.identity()
    policy = EgressPolicy.from_config(ctx.config)
    allowed = allowed_side_effect_levels or {"read"}
    with ctx.session_factory() as session:
        stmt = apply_visibility_filter(
            select(CaliberToolRegistry).where(CaliberToolRegistry.tool_id == tool_id),
            CaliberToolRegistry,
            identity,
            identity.active_project_id,
        )
        tool = session.execute(stmt).scalars().first()
        if tool is None or tool.execution_backend != "openapi_http":
            raise ValueError(f"published OpenAPI tool {tool_id!r} not found")
        if tool.requires_approval:
            raise ValueError(
                f"tool {tool.name!r} requires approval and cannot be invoked through Aria"
            )
        if tool.side_effect_level not in allowed:
            raise ValueError(
                f"tool {tool.name!r} is {tool.side_effect_level!r} and cannot be invoked "
                "through this Aria surface"
            )
        error: str | None = None
        result: dict[str, Any] | None = None
        try:
            result = execute_openapi_http_tool(
                execution_config=dict(tool.backend_config or {}),
                input_schema=dict(tool.input_schema or {})
                if isinstance(tool.input_schema, dict)
                else None,
                input_data=tool_input,
                egress_policy=policy,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to audit then re-raised
            error = str(exc)
        audit_record(
            session,
            actor=ctx.actor,
            action="invoke_openapi_tool_via_aria",
            entity_type="tool",
            entity_id=tool.tool_id,
            details={
                "tool_name": tool.name,
                "side_effect_level": tool.side_effect_level,
                "status_code": result.get("status_code") if result else None,
                "error": error,
            },
        )
        session.commit()
    if error is not None:
        raise ValueError(error)
    assert result is not None
    return result


def _openapi_tool_invoke(ctx: CapabilityContext, args: dict[str, Any]) -> Any:
    """Invoke one published, read-only OpenAPI-derived tool.

    Deliberately conservative, matching §10's "defer... full dynamic Aria parity
    for every published OpenAPI tool": this refuses anything whose
    ``side_effect_level`` is not ``read`` or that ``requires_approval``, rather
    than inventing autonomous-write semantics for an arbitrary external HTTP call
    the proposal never asked for. A write/external-action tool remains reachable
    through the tool registry, workflow runtime, and SDK/API — just not from here.

    Execution goes through the same guarded executor and egress policy as every
    other OpenAPI-backed invocation, and is audited the same way the preview
    route is: shape of the call, never the request/response payload or a
    resolved credential.
    """

    tool_id = str(args.get("tool_id") or "").strip()
    if not tool_id:
        raise ValueError("tool_id is required")
    tool_input = args.get("input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    return invoke_openapi_published_tool(
        ctx,
        tool_id=tool_id,
        tool_input=tool_input,
        allowed_side_effect_levels={"read"},
    )


_register_builtins()

register(
    Capability(
        key="openapi_tool.list",
        title="List OpenAPI tools",
        description=(
            "List published OpenAPI-derived tools (tool_id, name, side_effect_level, "
            "whether they require approval). Includes write-capable tools for "
            "discovery even though only read tools can be invoked here."
        ),
        tier=TIER_READ,
        handler=_openapi_tool_list,
        required_scopes=("viewer",),
        result_properties=("tools",),
    )
)
register(
    Capability(
        key="openapi_tool.invoke",
        title="Invoke a read-only OpenAPI tool",
        description=(
            "Invoke one published, read-only OpenAPI-derived tool by tool_id with "
            "structured input. Refuses write or approval-gated tools — use a "
            "workflow or the SDK for those."
        ),
        tier=TIER_READ,
        handler=_openapi_tool_invoke,
        required_scopes=("viewer",),
        properties={
            "tool_id": {"type": "string"},
            "input": {"type": "object"},
        },
        required=("tool_id",),
        result_properties=("status_code", "json", "text"),
    )
)
