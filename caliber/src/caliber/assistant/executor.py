"""Aria plan executor — walk an approved goal-plan, supervised between steps.

Phase 2 of the agentic-orchestration plan. The executor advances a plan's steps
in dependency order, dispatching each step's capability through the registry. It
is **supervised**: the autonomy dial (per step tier) decides whether a step runs
autonomously or pauses for a human via a :class:`CaliberAriaInteraction`:

* ``read``   → always run.
* ``gated``  → always pause for a human (the non-negotiable floor).
* ``safe`` / ``mutate`` → run under ``approve_plan`` / ``auto_guarded``; pause
  under ``ask_each``.

When a step pauses, the plan goes ``paused`` and one interaction is created.
Answering the interaction (approve/deny) resumes execution from where it stopped.

This is the synchronous executor: every capability registered today completes
in-process. The durable, worker-backed variant that waits on long async jobs
(``waiting_job`` + DB polling) lands with the first async capability (Phase 4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, ClassVar, Protocol

from caliber.assistant.capabilities import (
    TIER_GATED,
    TIER_READ,
    CapabilityContext,
    get_capability,
)
from caliber.assistant.plans import PlanService
from caliber.audit import record as audit_record
from caliber.db.models import CaliberAriaInteraction, CaliberAriaPlan, CaliberAriaPlanStep
from caliber.ids import new_aria_interaction_id

# Plan states from which execution may (re)start.
_EXECUTABLE = frozenset({"approved", "running", "paused"})
_STEP_TERMINAL = frozenset({"done", "skipped"})

# Max plans polled per worker tick — bounds the fan-out of full plan
# re-executions so one tick can't run unboundedly long (the rest drain next tick).
_POLL_BATCH = 100


@dataclass(frozen=True)
class AsyncJobHandle:
    """An async capability returns this instead of a result.

    The executor parks the step in ``waiting_job`` (storing ``job_id`` + ``kind``)
    and leaves the plan ``running``; a later poll resolves the job and resumes.
    """

    job_id: str
    kind: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class JobStatus:
    state: str  # "pending" | "done" | "failed"
    result: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


class JobStatusResolver(Protocol):
    """Boundary: resolve the terminal state of an async job a step is waiting on."""

    def resolve(self, *, kind: str, job_id: str, session_factory: Any) -> JobStatus: ...


class FakeJobStatusResolver:
    """In-memory resolver for tests; jobs are ``pending`` unless told otherwise."""

    def __init__(self, statuses: dict[str, JobStatus] | None = None) -> None:
        self._statuses = dict(statuses or {})

    def set(self, job_id: str, status: JobStatus) -> None:
        self._statuses[job_id] = status

    # kind / session_factory kept for Protocol conformance; the fake ignores them.
    def resolve(self, *, kind: str, job_id: str, session_factory: Any) -> JobStatus:  # noqa: ARG002
        return self._statuses.get(job_id, JobStatus(state="pending"))


class MLflowJobStatusResolver:
    """Production resolver — maps a job ``kind`` to its backing table's status.

    Each backing table uses its OWN terminal vocabulary, so the done/failed
    mapping is keyed by ``kind``. A ``CaliberRefinementJob`` never reaches
    ``completed`` — its success terminals are ``candidate_ready`` / ``applied``
    and its failure terminals are ``rejected`` / ``failed`` — whereas a
    ``CaliberEvalRun`` uses ``completed``. Mapping only ``completed``→done made
    every ``workflow.calibrate`` plan hang forever in ``waiting_job``. Unknown
    kinds resolve to ``pending`` (never falsely terminal).
    """

    # Success terminals per kind.
    _DONE_BY_KIND: ClassVar[dict[str, frozenset[str]]] = {
        "refinement_job": frozenset({"candidate_ready", "applied"}),
        "eval_run": frozenset({"completed"}),
    }
    # Failure terminals per kind (a rejected candidate finishes the step, not hangs).
    _FAILED_BY_KIND: ClassVar[dict[str, frozenset[str]]] = {
        "refinement_job": frozenset({"rejected", "failed", "error"}),
        "eval_run": frozenset({"failed", "error"}),
    }

    def resolve(self, *, kind: str, job_id: str, session_factory: Any) -> JobStatus:
        from caliber.db.models import (  # noqa: PLC0415
            CaliberEvalRun,
            CaliberRefinementJob,
        )

        model = {"refinement_job": CaliberRefinementJob, "eval_run": CaliberEvalRun}.get(kind)
        if model is None:
            return JobStatus(state="pending")
        done = self._DONE_BY_KIND.get(kind, frozenset({"completed"}))
        failed = self._FAILED_BY_KIND.get(kind, frozenset({"failed", "error"}))
        with session_factory() as session:
            row = session.get(model, job_id)
            if row is None:
                return JobStatus(state="failed", error=f"{kind} {job_id!r} not found")
            status = str(getattr(row, "status", "") or "")
            if status in failed:
                return JobStatus(state="failed", error=status)
            if status in done:
                return JobStatus(state="done", evidence={"status": status})
            return JobStatus(state="pending", evidence={"status": status})


def gate_decision(autonomy: str, tier: str) -> str:
    """Return ``"run"`` or ``"ask"`` for a step of ``tier`` under ``autonomy``."""
    if tier == TIER_READ:
        return "run"
    if tier == TIER_GATED:
        return "ask"  # irreversible — always a human decision
    if autonomy == "ask_each":
        return "ask"
    return "run"  # approve_plan / auto_guarded auto-run safe + mutate


class PlanExecutionError(Exception):
    pass


class PlanForbiddenError(PlanExecutionError):
    """Answering an interaction violated authority / separation-of-duties rules."""


# A gated interaction's ``required_scope`` label maps to a CALIBER scope.
_REQUIRED_SCOPE_MAP = {"approver": "caliber.approver"}

_STEP_REF_KEY = "$from_step"


def _is_step_reference(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get(_STEP_REF_KEY), str)
        and isinstance(value.get("path"), str)
    )


def _missing_capability_inputs(capability: Any, inputs: dict[str, Any]) -> list[str]:
    return [name for name in capability.required if name not in inputs or inputs[name] is None]


def _validate_schema_value(  # noqa: PLR0912 - compact JSON-Schema subset interpreter
    value: Any, schema: dict[str, Any], *, path: str
) -> None:
    """Validate the JSON-Schema subset emitted by the capability registry."""
    if _is_step_reference(value):
        return
    allowed = schema.get("enum")
    if isinstance(allowed, list) and value not in allowed:
        raise PlanExecutionError(f"{path} must be one of {allowed!r}")
    expected = schema.get("type")
    valid = True
    if expected == "string":
        valid = isinstance(value, str)
    elif expected == "boolean":
        valid = isinstance(value, bool)
    elif expected == "integer":
        valid = isinstance(value, int) and not isinstance(value, bool)
    elif expected == "number":
        valid = isinstance(value, (int, float)) and not isinstance(value, bool)
    elif expected == "array":
        valid = isinstance(value, list)
    elif expected == "object":
        valid = isinstance(value, dict)
    if not valid:
        raise PlanExecutionError(f"{path} must be {expected}")
    if expected == "string" and isinstance(value, str):
        minimum = schema.get("minLength")
        if isinstance(minimum, int) and len(value) < minimum:
            raise PlanExecutionError(f"{path} must contain at least {minimum} character(s)")
    if expected == "array" and isinstance(value, list):
        minimum = schema.get("minItems")
        if isinstance(minimum, int) and len(value) < minimum:
            raise PlanExecutionError(f"{path} must contain at least {minimum} item(s)")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                _validate_schema_value(item, item_schema, path=f"{path}[{index}]")
    if expected == "object" and isinstance(value, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            required_value = schema.get("required")
            required = required_value if isinstance(required_value, list) else []
            missing = [name for name in required if name not in value or value[name] is None]
            if missing:
                raise PlanExecutionError(
                    f"{path} is missing required field(s): {', '.join(missing)}"
                )
            for key, item in value.items():
                field_schema = properties.get(key)
                if isinstance(field_schema, dict):
                    _validate_schema_value(item, field_schema, path=f"{path}.{key}")


def _validate_capability_inputs(capability: Any, inputs: dict[str, Any]) -> None:
    unknown = sorted(set(inputs) - set(capability.properties))
    if unknown:
        raise PlanExecutionError(
            f"{capability.key} received unknown input field(s): {', '.join(unknown)}"
        )
    missing = _missing_capability_inputs(capability, inputs)
    if missing:
        raise PlanExecutionError(
            f"{capability.key} is missing required input field(s): {', '.join(missing)}"
        )
    for key, value in inputs.items():
        schema = capability.properties.get(key)
        if isinstance(schema, dict):
            _validate_schema_value(value, schema, path=key)


def _result_path(value: Any, path: str) -> Any:
    current = value
    for part in [item for item in path.split(".") if item]:
        if not isinstance(current, dict) or part not in current:
            raise PlanExecutionError(f"dependency result does not contain path {path!r}")
        current = current[part]
    return current


def _resolve_step_input_value(session: Any, plan_id: str, value: Any) -> Any:
    if _is_step_reference(value):
        source_id = value[_STEP_REF_KEY]
        source = session.get(CaliberAriaPlanStep, source_id)
        if source is None or source.plan_id != plan_id:
            raise PlanExecutionError(f"dependency step {source_id!r} was not found in this plan")
        if source.status != "done":
            raise PlanExecutionError(f"dependency step {source_id!r} is not complete")
        return _result_path(dict(source.result or {}), str(value.get("path") or ""))
    if isinstance(value, list):
        return [_resolve_step_input_value(session, plan_id, item) for item in value]
    if isinstance(value, dict):
        return {
            key: _resolve_step_input_value(session, plan_id, item) for key, item in value.items()
        }
    return value


class PlanExecutor:
    """Drives a plan to its next pause point (interaction) or to completion."""

    def __init__(self, plans: PlanService | None = None) -> None:
        self._plans = plans or PlanService()

    # -- public ----------------------------------------------------------

    def execute(
        self,
        *,
        session_factory: Any,
        config: Any,
        actor: str,
        plan_id: str,
        project_id: str | None = None,
        approved_step_ids: set[str] | None = None,
    ) -> dict[str, Any] | None:
        """Advance the plan until it pauses (interaction), fails, or completes."""
        approved = set(approved_step_ids or ())
        # Bound the loop by the step count (+slack) so a logic bug can't spin.
        for _ in range(self._step_budget(session_factory, plan_id) + 2):
            action = self._plan_next_action(
                session_factory=session_factory,
                config=config,
                plan_id=plan_id,
                approved=approved,
            )
            if action is None:
                break  # terminal: paused (asked), completed, failed, or not executable
            step_id, cap_key = action
            self._run_step(
                session_factory=session_factory,
                config=config,
                actor=actor,
                project_id=project_id,
                plan_id=plan_id,
                step_id=step_id,
                cap_key=cap_key,
            )
            approved.discard(step_id)
        return self._plans.get_plan(session_factory=session_factory, plan_id=plan_id)

    def list_interactions(
        self,
        *,
        session_factory: Any,
        plan_id: str,
        pending_only: bool = True,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[CaliberAriaInteraction]:
        from sqlalchemy import select  # noqa: PLC0415

        with session_factory() as session:
            stmt = select(CaliberAriaInteraction).where(CaliberAriaInteraction.plan_id == plan_id)
            if pending_only:
                stmt = stmt.where(CaliberAriaInteraction.status == "pending")
            stmt = stmt.order_by(CaliberAriaInteraction.created_at)
            if limit is not None:
                stmt = stmt.limit(limit).offset(offset)
            return list(session.execute(stmt).scalars())

    def answer(
        self,
        *,
        session_factory: Any,
        config: Any,
        actor: str,
        interaction_id: str,
        approved: bool,
        response: dict[str, Any] | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Record a human answer, then resume the plan.

        ``approved`` True → the gated/asked step is pre-approved and runs on resume;
        False → the step is skipped. Returns the refreshed plan detail.
        """
        with session_factory() as session:
            interaction = session.get(CaliberAriaInteraction, interaction_id)
            if interaction is None:
                raise PlanExecutionError(f"interaction {interaction_id!r} not found")
            if interaction.status != "pending":
                raise PlanExecutionError(f"interaction {interaction_id!r} already answered")
            plan = session.get(CaliberAriaPlan, interaction.plan_id)
            # A "confirm" interaction is a post-execution gate escalation (accept or
            # reject an already-produced below-gate result); "permission" is a
            # pre-execution gate (run the step or not).
            is_gate = interaction.kind == "confirm"
            is_input = interaction.kind == "input"
            # Authorize the answerer: only the plan owner (or an admin) may answer
            # their own asks/denials; a scoped gated approval goes through the
            # separation-of-duties path. Closes the IDOR where any authenticated
            # user could approve/deny a plan they don't own.
            self._authorize_answer(
                config=config,
                actor=actor,
                interaction=interaction,
                plan=plan,
                approved=approved,
                is_gate=is_gate,
            )
            interaction.status = "answered"
            interaction.response = {"approved": approved, **(response or {})}
            interaction.responded_by = actor
            interaction.responded_at = datetime.now(timezone.utc)
            plan_id = interaction.plan_id
            step_id = interaction.step_id
            step = session.get(CaliberAriaPlanStep, step_id)
            if step is not None:
                if is_gate:
                    # Accept → keep the already-computed result (done); reject → skip.
                    step.status = "done" if approved else "skipped"
                elif not approved:
                    step.status = "skipped"
                elif is_input:
                    supplied = (response or {}).get("inputs")
                    if not isinstance(supplied, dict):
                        raise PlanExecutionError("an input interaction requires an 'inputs' object")
                    capability = get_capability(step.capability_key)
                    if capability is None:
                        raise PlanExecutionError(f"unknown capability {step.capability_key!r}")
                    merged = {**dict(step.inputs or {}), **dict(supplied)}
                    _validate_capability_inputs(capability, merged)
                    step.inputs = merged
                    # Input collection is not permission approval. Return the
                    # step to pending so the normal autonomy/risk gate runs next.
                    step.status = "pending"
            audit_record(
                session,
                actor=actor,
                action="answer_aria_interaction",
                entity_type="aria_plan",
                entity_id=plan_id,
                details={
                    "interaction_id": interaction_id,
                    "approved": approved,
                    "input_fields": sorted((response or {}).get("inputs", {}))
                    if isinstance((response or {}).get("inputs"), dict)
                    else [],
                },
            )
            session.commit()

        return self.execute(
            session_factory=session_factory,
            config=config,
            actor=actor,
            plan_id=plan_id,
            project_id=project_id,
            # A pre-execution permission approval pre-clears the step to run; a
            # gate (confirm) acceptance already marked it done — don't re-run it.
            approved_step_ids={step_id} if (approved and not is_gate and not is_input) else set(),
        )

    def poll(
        self,
        *,
        session_factory: Any,
        config: Any,
        plan_id: str,
        resolver: JobStatusResolver,
        actor: str = "@system",
        project_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Resolve the plan's in-flight async jobs; resume on completion.

        For each ``waiting_job`` step, ask the resolver for the job's state: a
        ``done`` job marks the step done (with its evidence) and lets execution
        continue; a ``failed`` job fails the step + plan; ``pending`` is left
        alone for the next poll.
        """
        advanced = failed = False
        for step_id, job_id, kind in self._waiting_job_steps(session_factory, plan_id):
            status = resolver.resolve(kind=kind, job_id=job_id, session_factory=session_factory)
            if status.state == "done":
                self._complete_or_escalate(
                    session_factory,
                    plan_id,
                    step_id,
                    result=status.result or {"job_id": job_id},
                    evidence=status.evidence,
                )
                advanced = True
            elif status.state == "failed":
                self._finish_step(
                    session_factory,
                    plan_id,
                    step_id,
                    status="failed",
                    error=status.error or "async job failed",
                    plan_status="failed",
                )
                failed = True
        if advanced and not failed:
            return self.execute(
                session_factory=session_factory,
                config=config,
                actor=actor,
                plan_id=plan_id,
                project_id=project_id,
            )
        return self._plans.get_plan(session_factory=session_factory, plan_id=plan_id)

    def poll_waiting_plans(
        self, *, session_factory: Any, config: Any, resolver: JobStatusResolver
    ) -> list[str]:
        """Poll plans with an in-flight async step (the worker entrypoint).

        Bounded to ``_POLL_BATCH`` plans per tick: each ``poll`` re-walks a plan's
        steps, so an unbounded fan-out could run a single tick well past the poll
        interval and starve the loop. Remaining plans drain on the next tick.
        """
        from sqlalchemy import select  # noqa: PLC0415

        with session_factory() as session:
            plan_ids = list(
                session.execute(
                    select(CaliberAriaPlanStep.plan_id)
                    .where(CaliberAriaPlanStep.status == "waiting_job")
                    .distinct()
                    .limit(_POLL_BATCH)
                ).scalars()
            )
        for plan_id in plan_ids:
            self.poll(
                session_factory=session_factory, config=config, plan_id=plan_id, resolver=resolver
            )
        return plan_ids

    # -- internals -------------------------------------------------------

    @staticmethod
    def _waiting_job_steps(session_factory: Any, plan_id: str) -> list[tuple[str, str, str]]:
        """Return ``(step_id, job_id, kind)`` for each waiting_job step (detached)."""
        from sqlalchemy import select  # noqa: PLC0415

        with session_factory() as session:
            rows = (
                session.execute(
                    select(CaliberAriaPlanStep)
                    .where(CaliberAriaPlanStep.plan_id == plan_id)
                    .where(CaliberAriaPlanStep.status == "waiting_job")
                )
                .scalars()
                .all()
            )
            return [
                (s.step_id, s.job_id or "", str((s.result or {}).get("__job_kind__", "")))
                for s in rows
            ]

    @staticmethod
    def _authorize_answer(
        *,
        config: Any,
        actor: str,
        interaction: CaliberAriaInteraction,
        plan: CaliberAriaPlan | None,
        approved: bool,
        is_gate: bool,
    ) -> None:
        """Authorize ``actor`` to answer ``interaction`` (raises on denial).

        A scoped, pre-execution gated *approval* goes through separation-of-duties
        (a distinct identity holding the named authority). Every other answer —
        the owner's own permission/choice/input asks, denials, and confirm-gate
        decisions — is the plan owner's (or an admin's) to make. This closes the
        IDOR where any authenticated user could approve/deny another user's plan.
        """
        from caliber.auth import SCOPE_ADMIN, scopes_for_user  # noqa: PLC0415

        owner = plan.owner if plan is not None else None
        if approved and interaction.required_scope and not is_gate:
            PlanExecutor._enforce_sod(
                config=config,
                actor=actor,
                required_scope=interaction.required_scope,
                plan_owner=owner,
            )
            return
        if owner is None or actor == owner:
            return
        scopes = scopes_for_user(config, actor) if config is not None else frozenset()
        if SCOPE_ADMIN in scopes:
            return
        raise PlanForbiddenError("only the plan owner may answer this interaction")

    @staticmethod
    def _enforce_sod(
        *, config: Any, actor: str, required_scope: str, plan_owner: str | None
    ) -> None:
        """Raise :class:`PlanForbiddenError` unless ``actor`` may approve a gated step.

        Two independent checks: the actor must hold the required authority, and
        (separation of duties) must not be the plan's own owner/proposer.
        """
        from caliber.auth import scopes_for_user  # noqa: PLC0415

        if plan_owner is not None and actor == plan_owner:
            raise PlanForbiddenError(
                "separation of duties: the plan owner cannot approve its own gated step"
            )
        needed = _REQUIRED_SCOPE_MAP.get(required_scope)
        # An unmapped ``required_scope`` must deny, not silently authorize anyone:
        # a label that doesn't resolve to a known CALIBER scope is a misconfig, and
        # treating it as "no check" would let any non-owner approve a gated step.
        if needed is None:
            raise PlanForbiddenError(
                f"approving this step requires unknown authority {required_scope!r}"
            )
        scopes = scopes_for_user(config, actor) if config is not None else frozenset()
        if needed not in scopes:
            raise PlanForbiddenError(f"approving this step requires {required_scope!r} authority")

    @staticmethod
    def _step_budget(session_factory: Any, plan_id: str) -> int:
        from sqlalchemy import func, select  # noqa: PLC0415

        with session_factory() as session:
            return int(
                session.execute(
                    select(func.count())
                    .select_from(CaliberAriaPlanStep)
                    .where(CaliberAriaPlanStep.plan_id == plan_id)
                ).scalar_one()
            )

    def _plan_next_action(  # noqa: PLR0911 - explicit persisted plan-state outcomes
        self, *, session_factory: Any, config: Any, plan_id: str, approved: set[str]
    ) -> tuple[str, str] | None:
        """Pick the next runnable step, or settle a terminal plan state.

        Returns ``(step_id, capability_key)`` to run, or ``None`` when the plan is
        paused (an interaction was created), completed, failed, or not executable.
        Commits any status / interaction changes it makes.
        """
        from sqlalchemy import select  # noqa: PLC0415

        with session_factory() as session:
            plan = session.get(CaliberAriaPlan, plan_id)
            if plan is None or plan.status not in _EXECUTABLE:
                return None
            steps = list(
                session.execute(
                    select(CaliberAriaPlanStep)
                    .where(CaliberAriaPlanStep.plan_id == plan_id)
                    .order_by(CaliberAriaPlanStep.seq)
                ).scalars()
            )
            by_id = {s.step_id: s for s in steps}

            for step in steps:
                if step.status in _STEP_TERMINAL:
                    continue
                preapproved = step.step_id in approved
                readiness = self._readiness(step, by_id, preapproved=preapproved)
                if readiness != "ready":
                    if readiness == "blocked":
                        step.status = "blocked"
                    # A job in flight keeps the plan running; awaiting a human (or a
                    # dead dependency) pauses it.
                    self._ensure_status(plan, "running" if readiness == "wait_job" else "paused")
                    session.commit()
                    return None

                cap = get_capability(step.capability_key)
                if cap is None:
                    step.status = "failed"
                    step.error = f"unknown capability {step.capability_key!r}"
                    plan.status = "failed"
                    session.commit()
                    return None

                if cap.required_scopes and config is not None:
                    from caliber.auth import scopes_for_user  # noqa: PLC0415

                    have = scopes_for_user(config, plan.owner)
                    missing_scopes = [
                        scope for scope in cap.required_scopes if f"caliber.{scope}" not in have
                    ]
                    if missing_scopes:
                        step.status = "failed"
                        step.error = "plan owner lacks required scope(s): " + ", ".join(
                            sorted(missing_scopes)
                        )
                        plan.status = "failed"
                        session.commit()
                        return None

                missing_inputs = _missing_capability_inputs(cap, dict(step.inputs or {}))
                if missing_inputs:
                    self._create_input_interaction(
                        session,
                        plan,
                        step,
                        cap.input_schema,
                        missing_inputs,
                    )
                    step.status = "waiting_input"
                    plan.status = "paused"
                    session.commit()
                    return None

                if not preapproved and gate_decision(plan.autonomy, cap.tier) == "ask":
                    self._create_interaction(session, plan, step, cap.tier)
                    step.status = "waiting_input"
                    plan.status = "paused"
                    session.commit()
                    return None

                self._ensure_status(plan, "running")
                session.commit()
                return (step.step_id, step.capability_key)

            return self._settle_terminal(session, plan, steps)

    @staticmethod
    def _readiness(
        step: CaliberAriaPlanStep, by_id: dict[str, CaliberAriaPlanStep], *, preapproved: bool
    ) -> str:
        """``ready`` | ``blocked`` (dead dep) | ``wait_job`` (in flight) | ``wait``."""
        if step.status == "waiting_job":
            return "wait_job"  # an async job is in flight; a poll will advance it
        if step.status == "waiting_input" and not preapproved:
            return "wait"  # still awaiting the human's answer
        deps = [by_id.get(d) for d in (step.depends_on or [])]
        if any(d is None or d.status == "failed" for d in deps):
            return "blocked"
        if not all(d is not None and d.status == "done" for d in deps):
            return "wait"
        return "ready"

    @staticmethod
    def _settle_terminal(
        session: Any, plan: CaliberAriaPlan, steps: list[CaliberAriaPlanStep]
    ) -> None:
        if all(s.status in _STEP_TERMINAL for s in steps):
            plan.status = "completed"
        elif plan.status == "running":
            plan.status = "paused"
        session.commit()

    def _run_step(
        self,
        *,
        session_factory: Any,
        config: Any,
        actor: str,
        project_id: str | None,
        plan_id: str,
        step_id: str,
        cap_key: str,
    ) -> None:
        """Run one capability handler outside any open plan transaction."""
        cap = get_capability(cap_key)
        resolution_error: str | None = None
        with session_factory() as session:
            step = session.get(CaliberAriaPlanStep, step_id)
            raw_inputs = dict(step.inputs or {}) if step is not None else {}
            try:
                inputs = {
                    key: _resolve_step_input_value(session, plan_id, value)
                    for key, value in raw_inputs.items()
                }
                if cap is not None:
                    _validate_capability_inputs(cap, inputs)
            except PlanExecutionError as exc:
                inputs = {}
                resolution_error = str(exc)
            plan = session.get(CaliberAriaPlan, plan_id)
            owner = plan.owner if plan is not None else actor
        if resolution_error is not None:
            self._finish_step(
                session_factory,
                plan_id,
                step_id,
                status="failed",
                error=resolution_error,
                plan_status="failed",
            )
            return
        # RBAC floor: the autonomy dial gates on a capability's risk *tier*, but a
        # capability also declares the *scopes* it needs. Enforce them against the
        # plan **owner** — the principal the plan runs on behalf of — so an
        # under-privileged owner's auto-running plan can't invoke a capability
        # they lack the scope for (e.g. a viewer's auto_guarded plan calling an
        # operator-only mutate), while a separate approver unblocking a gate (or
        # the ``@system`` async poller resuming a parked step) isn't required to
        # personally hold the capability's scope. Skipped when ``config`` is None
        # (internal/test callers can't resolve scopes; the route always supplies it).
        if cap is not None and cap.required_scopes and config is not None:
            from caliber.auth import scopes_for_user  # noqa: PLC0415

            have = scopes_for_user(config, owner)
            missing = [s for s in cap.required_scopes if f"caliber.{s}" not in have]
            if missing:
                self._finish_step(
                    session_factory,
                    plan_id,
                    step_id,
                    status="failed",
                    error=f"plan owner lacks required scope(s): {', '.join(sorted(missing))}",
                    plan_status="failed",
                )
                return
        ctx = CapabilityContext(
            session_factory=session_factory, config=config, actor=actor, project_id=project_id
        )
        try:
            result = cap.handler(ctx, inputs) if cap is not None else None
        except Exception as exc:  # capability failed — surface on the step + plan
            self._finish_step(
                session_factory,
                plan_id,
                step_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
                plan_status="failed",
            )
            return
        if isinstance(result, AsyncJobHandle):
            # The capability enqueued long-running work — park the step until a
            # poll resolves the job. The plan stays "running" (work in flight).
            self._mark_waiting_job(session_factory, step_id, result)
            return
        self._complete_or_escalate(
            session_factory, plan_id, step_id, result=_jsonable(result), evidence={}
        )

    @staticmethod
    def _mark_waiting_job(session_factory: Any, step_id: str, handle: AsyncJobHandle) -> None:
        with session_factory() as session:
            step = session.get(CaliberAriaPlanStep, step_id)
            if step is not None:
                step.status = "waiting_job"
                step.job_id = handle.job_id
                step.result = {"__job_kind__": handle.kind}
                if handle.evidence:
                    step.evidence = dict(handle.evidence)
            session.commit()

    @staticmethod
    def _evaluate_gate(gate: dict[str, Any], data: dict[str, Any]) -> bool:
        """True when the step passes its gate (or there's nothing to judge)."""
        metric = gate.get("metric")
        minimum = gate.get("min")
        if not metric or minimum is None:
            return True
        value = data.get(metric)
        if not isinstance(value, (int, float)):
            return True  # no comparable score → can't fail the gate
        return float(value) >= float(minimum)

    def _complete_or_escalate(
        self,
        session_factory: Any,
        plan_id: str,
        step_id: str,
        *,
        result: dict[str, Any],
        evidence: dict[str, Any],
    ) -> None:
        """Finish a step done, unless its gate fails → escalate a confirm interaction.

        Self-correction: a completed step whose evidence/result is below its gate
        is never silently accepted — it parks for human accept/reject with the
        numbers attached.
        """
        with session_factory() as session:
            step = session.get(CaliberAriaPlanStep, step_id)
            if step is None:
                return
            gate = step.gate or {}
            data = {**(result or {}), **(evidence or {})}
            if gate and not self._evaluate_gate(gate, data):
                step.result = result or {}
                step.evidence = evidence or {}
                step.status = "waiting_input"
                metric = str(gate.get("metric") or "")
                minimum = gate.get("min")
                session.add(
                    CaliberAriaInteraction(
                        interaction_id=new_aria_interaction_id(),
                        plan_id=plan_id,
                        step_id=step_id,
                        kind="confirm",
                        prompt=(
                            f"Step {step.seq + 1} ({step.capability_key}) scored "
                            f"{data.get(metric)} below the gate ({metric} >= {minimum}). "
                            "Accept the result anyway?"
                        ),
                        options=[
                            {"label": "Accept", "value": True},
                            {"label": "Reject", "value": False},
                        ],
                        evidence={"metric": metric, "min": minimum, "value": data.get(metric)},
                        status="pending",
                    )
                )
                plan = session.get(CaliberAriaPlan, plan_id)
                if plan is not None and plan.status != "failed":
                    plan.status = "paused"
                session.commit()
                return
            step.status = "done"
            if result is not None:
                step.result = result
            if evidence:
                step.evidence = evidence
            session.commit()

    @staticmethod
    def _finish_step(
        session_factory: Any,
        plan_id: str,
        step_id: str,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        evidence: dict[str, Any] | None = None,
        error: str | None = None,
        plan_status: str | None = None,
    ) -> None:
        with session_factory() as session:
            step = session.get(CaliberAriaPlanStep, step_id)
            if step is not None:
                step.status = status
                if result is not None:
                    step.result = result
                if evidence:
                    step.evidence = evidence
                if error is not None:
                    step.error = error
            if plan_status is not None:
                plan = session.get(CaliberAriaPlan, plan_id)
                if plan is not None:
                    plan.status = plan_status
            session.commit()

    @staticmethod
    def _ensure_status(plan: CaliberAriaPlan, status: str) -> None:
        if plan.status != status:
            plan.status = status

    @staticmethod
    def _create_interaction(
        session: Any, plan: CaliberAriaPlan, step: CaliberAriaPlanStep, tier: str
    ) -> None:
        gated = tier == TIER_GATED
        session.add(
            CaliberAriaInteraction(
                interaction_id=new_aria_interaction_id(),
                plan_id=plan.plan_id,
                step_id=step.step_id,
                kind="permission",
                prompt=(
                    f"Approve step {step.seq + 1}: {step.title or step.capability_key} "
                    f"({step.capability_key})?"
                ),
                options=[
                    {"label": "Approve", "value": True},
                    {"label": "Deny", "value": False},
                ],
                # Gated steps demand an approver authority (distinct-identity check
                # is enforced in Phase 3).
                required_scope="approver" if gated else None,
                status="pending",
            )
        )

    @staticmethod
    def _create_input_interaction(
        session: Any,
        plan: CaliberAriaPlan,
        step: CaliberAriaPlanStep,
        input_schema: dict[str, Any],
        missing: list[str],
    ) -> None:
        """Ask once for all unresolved capability fields using its JSON Schema."""
        session.add(
            CaliberAriaInteraction(
                interaction_id=new_aria_interaction_id(),
                plan_id=plan.plan_id,
                step_id=step.step_id,
                kind="input",
                prompt=(
                    f"Provide the required inputs for step {step.seq + 1}: "
                    f"{step.title or step.capability_key}."
                ),
                evidence={
                    "capability_key": step.capability_key,
                    "input_schema": input_schema,
                    "missing": list(missing),
                    "current_inputs": dict(step.inputs or {}),
                },
                status="pending",
            )
        )


def _jsonable(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    return {"value": value}
