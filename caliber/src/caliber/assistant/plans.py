"""Aria goal-plans — decompose a goal into capability steps, persist, retrieve.

Phase 1 of the agentic-orchestration plan
(``docs/12-assistant/aria-agentic-orchestration.md``). A :class:`Planner` turns a
goal into an ordered list of capability invocations; :class:`PlanService`
persists that as a :class:`~caliber.db.models.CaliberAriaPlan` + its steps and
serves it back. The planner is a boundary (Protocol) with a deterministic,
registry-driven default (:class:`HeuristicPlanner`) so the pipeline is fully
testable without an LLM; an LLM-backed planner slots in behind the same Protocol.

The *executor* that walks an approved plan (running capability steps, handling
async waits + interactions) is a later phase — this module only owns
decomposition, persistence, and retrieval.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from caliber.assistant.capabilities import (
    TIER_MUTATE,
    TIER_SAFE,
    Capability,
    registered_capabilities,
)
from caliber.audit import record as audit_record
from caliber.auth import CaliberIdentity
from caliber.db.models import CaliberAriaPlan, CaliberAriaPlanStep
from caliber.db.scoping import get_visible
from caliber.ids import new_aria_plan_id, new_aria_plan_step_id
from caliber.schemas import AriaPlanSchema, AriaPlanStepSchema


@dataclass(frozen=True)
class PlannedStep:
    """A planner's proposed step — a capability + its (possibly partial) inputs."""

    capability_key: str
    title: str
    inputs: dict[str, Any] = field(default_factory=dict)
    depends_on: list[int] = field(default_factory=list)  # indices of prior steps
    gate: dict[str, Any] | None = None  # optional quality gate, e.g. {"metric","min"}


class Planner(Protocol):
    """Boundary: turn a goal into an ordered list of capability steps."""

    def plan(self, goal: str, *, capabilities: Sequence[Capability]) -> list[PlannedStep]: ...


class HeuristicPlanner:
    """Deterministic, registry-driven planner (default; no LLM required).

    Includes a step for every non-read capability whose domain (the part of the
    key before the dot, e.g. ``judge`` in ``judge.create``) is mentioned in the
    goal. Registry-driven on purpose: registering a new capability extends what
    the planner can propose without touching this code. Steps run in registry
    order. Required inputs that are declared outputs of an earlier selected
    capability are linked explicitly; remaining fields are collected through a
    typed interaction before execution.
    """

    def plan(self, goal: str, *, capabilities: Sequence[Capability]) -> list[PlannedStep]:
        goal_l = goal.lower()
        steps: list[PlannedStep] = []
        produced_by: dict[str, int] = {}
        for cap in capabilities:
            if cap.tier not in (TIER_SAFE, TIER_MUTATE):
                continue
            domain = cap.key.split(".", 1)[0]
            needle = domain.replace("_", " ")
            if needle in goal_l or domain in goal_l or cap.key in goal_l:
                inputs: dict[str, Any] = {}
                dependencies: list[int] = []
                for field_name in cap.required:
                    producer_index = produced_by.get(field_name)
                    if producer_index is None:
                        continue
                    inputs[field_name] = {
                        "$from_step_index": producer_index,
                        "path": field_name,
                    }
                    dependencies.append(producer_index)
                step_index = len(steps)
                steps.append(
                    PlannedStep(
                        capability_key=cap.key,
                        title=cap.title,
                        inputs=inputs,
                        depends_on=sorted(set(dependencies)),
                    )
                )
                for result_name in cap.result_properties:
                    produced_by[result_name] = step_index
        return steps


def build_default_planner() -> Planner:
    return HeuristicPlanner()


class PlanService:
    """Create, retrieve, and lifecycle-manage Aria goal-plans."""

    def __init__(self, planner: Planner | None = None) -> None:
        self._planner = planner or build_default_planner()

    def create_plan(
        self,
        *,
        session_factory: Any,
        goal: str,
        owner: str,
        session_id: str | None = None,
        project_id: str | None = None,
        autonomy: str = "approve_plan",
        constraints: dict[str, Any] | None = None,
        done_when: Sequence[str] | None = None,
        context_refs: Sequence[Any] | None = None,
    ) -> dict[str, Any]:
        planned = self._planner.plan(goal, capabilities=registered_capabilities())
        with session_factory() as session:
            plan = CaliberAriaPlan(
                plan_id=new_aria_plan_id(),
                session_id=session_id,
                goal=goal,
                status="draft",
                autonomy=autonomy,
                constraints=dict(constraints or {}),
                done_when=[str(item) for item in (done_when or []) if str(item).strip()],
                context_refs=[
                    ref.model_dump(mode="python") if hasattr(ref, "model_dump") else dict(ref)
                    for ref in (context_refs or [])
                ],
                owner=owner,
                project_id=project_id,
                visibility="project" if project_id else "user",
            )
            session.add(plan)
            session.flush()
            # Materialize steps; translate planner depends_on (indices) to step ids.
            step_ids: list[str] = [new_aria_plan_step_id() for _ in planned]
            steps: list[CaliberAriaPlanStep] = []
            for seq, (sid, ps) in enumerate(zip(step_ids, planned, strict=True)):
                inputs = _translate_step_input_refs(ps.inputs, step_ids)
                steps.append(
                    CaliberAriaPlanStep(
                        step_id=sid,
                        plan_id=plan.plan_id,
                        seq=seq,
                        capability_key=ps.capability_key,
                        title=ps.title,
                        inputs=inputs,
                        depends_on=[step_ids[i] for i in ps.depends_on if 0 <= i < len(step_ids)],
                        gate=dict(ps.gate) if ps.gate else None,
                        status="pending",
                    )
                )
            session.add_all(steps)
            audit_record(
                session,
                actor=owner,
                action="create_aria_plan",
                entity_type="aria_plan",
                entity_id=plan.plan_id,
                details={"goal": goal[:200], "steps": len(steps)},
            )
            session.commit()
            return self._detail(plan, steps)

    def get_plan(
        self,
        *,
        session_factory: Any,
        plan_id: str,
        identity: CaliberIdentity | None = None,
    ) -> dict[str, Any] | None:
        with session_factory() as session:
            # Detail/action routes pass ``identity`` so an out-of-scope plan
            # returns None (the route 404s) rather than leaking another user's
            # plan by a guessed id. Internal callers (executor/worker) pass None
            # and get the unscoped lookup, since they run post-authorization.
            if identity is not None:
                plan = get_visible(
                    session, CaliberAriaPlan, CaliberAriaPlan.plan_id, plan_id, identity
                )
            else:
                plan = session.get(CaliberAriaPlan, plan_id)
            if plan is None:
                return None
            steps = self._steps_for(session, plan_id)
            return self._detail(plan, steps)

    def list_plans(
        self,
        *,
        session_factory: Any,
        owner: str | None = None,
        session_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[AriaPlanSchema]:
        from sqlalchemy import func, select  # noqa: PLC0415

        with session_factory() as session:
            stmt = select(CaliberAriaPlan).order_by(CaliberAriaPlan.created_at.desc())
            if owner is not None:
                stmt = stmt.where(CaliberAriaPlan.owner == owner)
            if session_id is not None:
                stmt = stmt.where(CaliberAriaPlan.session_id == session_id)
            if limit is not None:
                stmt = stmt.limit(limit).offset(offset)
            plans = session.execute(stmt).scalars().all()
            # Step counts for all listed plans in one grouped query rather than a
            # COUNT(*) per plan (was an N+1 over the plan list).
            plan_ids = [plan.plan_id for plan in plans]
            counts: dict[str, int] = {}
            if plan_ids:
                rows = session.execute(
                    select(CaliberAriaPlanStep.plan_id, func.count())
                    .where(CaliberAriaPlanStep.plan_id.in_(plan_ids))
                    .group_by(CaliberAriaPlanStep.plan_id)
                )
                counts = {pid: int(n) for pid, n in rows}
            out: list[AriaPlanSchema] = []
            for plan in plans:
                schema = AriaPlanSchema.model_validate(plan)
                schema.step_count = counts.get(plan.plan_id, 0)
                out.append(schema)
            return out

    def set_status(
        self, *, session_factory: Any, plan_id: str, status: str, actor: str
    ) -> dict[str, Any] | None:
        with session_factory() as session:
            plan = session.get(CaliberAriaPlan, plan_id)
            if plan is None:
                return None
            plan.status = status
            audit_record(
                session,
                actor=actor,
                action=f"aria_plan_{status}",
                entity_type="aria_plan",
                entity_id=plan_id,
                details={},
            )
            session.commit()
            steps = self._steps_for(session, plan_id)
            return self._detail(plan, steps)

    def set_autonomy(
        self, *, session_factory: Any, plan_id: str, autonomy: str, actor: str
    ) -> dict[str, Any] | None:
        """Edit a plan's autonomy dial, audited. Returns ``None`` if it's gone.

        Relaxing the autonomy (e.g. to ``auto_guarded``) changes how much runs
        without a human, so it is a state change worth recording — unlike a bare
        in-place column write, this emits an audit row and tolerates a delete
        race (``None`` → the route 404s) instead of raising.
        """
        with session_factory() as session:
            plan = session.get(CaliberAriaPlan, plan_id)
            if plan is None:
                return None
            before = plan.autonomy
            plan.autonomy = autonomy
            audit_record(
                session,
                actor=actor,
                action="aria_plan_set_autonomy",
                entity_type="aria_plan",
                entity_id=plan_id,
                details={"from": before, "to": autonomy},
            )
            session.commit()
            steps = self._steps_for(session, plan_id)
            return self._detail(plan, steps)

    # -- helpers ---------------------------------------------------------

    @staticmethod
    def _steps_for(session: Any, plan_id: str) -> list[CaliberAriaPlanStep]:
        from sqlalchemy import select  # noqa: PLC0415

        return list(
            session.execute(
                select(CaliberAriaPlanStep)
                .where(CaliberAriaPlanStep.plan_id == plan_id)
                .order_by(CaliberAriaPlanStep.seq)
            )
            .scalars()
            .all()
        )

    @staticmethod
    def _detail(plan: CaliberAriaPlan, steps: Sequence[CaliberAriaPlanStep]) -> dict[str, Any]:
        plan_schema = AriaPlanSchema.model_validate(plan)
        plan_schema.step_count = len(steps)
        return {
            "plan": plan_schema.model_dump(mode="json"),
            "steps": [_step_detail(s) for s in steps],
        }


def _translate_step_input_refs(value: Any, step_ids: Sequence[str]) -> Any:
    """Replace planner-local step indices with durable sibling step ids."""
    if isinstance(value, list):
        return [_translate_step_input_refs(item, step_ids) for item in value]
    if not isinstance(value, dict):
        return value
    index = value.get("$from_step_index")
    if isinstance(index, int) and 0 <= index < len(step_ids):
        return {
            "$from_step": step_ids[index],
            "path": str(value.get("path") or ""),
        }
    return {key: _translate_step_input_refs(item, step_ids) for key, item in value.items()}


def _step_detail(step: CaliberAriaPlanStep) -> dict[str, Any]:
    schema = AriaPlanStepSchema.model_validate(step)
    capability = next(
        (item for item in registered_capabilities() if item.key == step.capability_key),
        None,
    )
    schema.input_schema = capability.input_schema if capability is not None else {}
    return schema.model_dump(mode="json")
