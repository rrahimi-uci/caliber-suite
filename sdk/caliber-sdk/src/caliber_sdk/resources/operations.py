"""Jobs, review queues, Aria, releases, observability, audit, events, cookbooks.

All beta. Several carry a distinction the type system alone cannot express: a
job or plan can *stop* without being *finished*, because it is waiting for a
person. The properties on the models say which, and the waiters here respect it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from ..models._decode import decode, decode_list
from ..models.operations import (
    AriaInteraction,
    AriaPlan,
    AriaPlanDetail,
    AriaPlanStep,
    AuditEntry,
    CookbookRecipe,
    Job,
    ReleaseCandidate,
    ReviewQueue,
    Trace,
)
from ..waiters import wait_for
from ._base import Resource

_List = list


def _decode_plan_detail(payload: Any) -> AriaPlanDetail:
    if not isinstance(payload, dict):
        return AriaPlanDetail()
    detail = decode(AriaPlanDetail, payload)
    detail.plan = decode(AriaPlan, payload.get("plan"))
    detail.steps = decode_list(AriaPlanStep, payload.get("steps"))
    return detail


class JobsAPI(Resource):
    """Durable background jobs — refinement, calibration, reporting."""

    def list(self, *, status: str | None = None) -> _List[Job]:
        params = {"status": status} if status else None
        return decode_list(Job, self._get("/jobs", params=params))

    def get(self, job_id: str) -> Job:
        return decode(Job, self._get(f"/jobs/{job_id}"))

    def targets(self, job_id: str) -> Any:
        """What applying this job would change."""
        return self._get(f"/jobs/{job_id}/targets")

    def apply(self, job_id: str, **options: Any) -> Any:
        """Apply a job's candidate. This is the human decision, made explicit."""
        return self._post(f"/jobs/{job_id}/apply", json=options)

    def wait(self, job_id: str, *, timeout: float = 900.0, **options: Any) -> Job:
        """Poll until the job stops *or* stops for a person.

        ``awaits_human`` counts as done here on purpose. A refinement job that
        reaches ``candidate_ready`` will never advance on its own, so a waiter
        that only accepted terminal states would block until timeout on the
        expected outcome.
        """
        return wait_for(
            lambda: self.get(job_id),
            is_done=lambda job: job.is_terminal or job.awaits_human,
            timeout=timeout,
            **options,
        )


class ReviewQueuesAPI(Resource):
    """Structured human review."""

    def list(self) -> _List[ReviewQueue]:
        return decode_list(ReviewQueue, self._get("/review-queues"))

    def get(self, queue_id: str) -> ReviewQueue:
        return decode(ReviewQueue, self._get(f"/review-queues/{queue_id}"))

    def create(self, name: str, **options: Any) -> ReviewQueue:
        return decode(ReviewQueue, self._post("/review-queues", json={"name": name, **options}))

    def update(self, queue_id: str, **changes: Any) -> ReviewQueue:
        return decode(ReviewQueue, self._patch(f"/review-queues/{queue_id}", json=changes))

    def enqueue(self, queue_id: str, **payload: Any) -> Any:
        return self._post(f"/review-queues/{queue_id}/items", json=payload)

    def submit(self, queue_id: str, item_id: str, **answers: Any) -> Any:
        """Answer a queued item. The write-back that turns review into evidence."""
        return self._post(f"/review-queues/{queue_id}/items/{item_id}/submit", json=answers)

    def alignment_examples(self, queue_id: str) -> Any:
        """Human labels usable for judge-alignment scoring."""
        return self._get(f"/review-queues/{queue_id}/alignment-examples")


class AriaAPI(Resource):
    """Aria goal-plans: the permissioned agentic loop."""

    def capabilities(self) -> Any:
        """What Aria is allowed to do in this deployment."""
        return self._get("/aria/capabilities")

    def plans(
        self,
        *,
        session_id: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> _List[AriaPlan]:
        params: dict[str, Any] = {}
        for key, value in (("session_id", session_id), ("limit", limit), ("offset", offset)):
            if value is not None:
                params[key] = value
        return decode_list(AriaPlan, self._get("/aria/plans", params=params))

    def get_plan(self, plan_id: str) -> AriaPlanDetail:
        return _decode_plan_detail(self._get(f"/aria/plans/{plan_id}"))

    def create_plan(self, goal: str, **options: Any) -> AriaPlanDetail:
        """State an intent. Aria plans the steps; you approve them."""
        return _decode_plan_detail(self._post("/aria/plans", json={"goal": goal, **options}))

    def update_plan(self, plan_id: str, **changes: Any) -> AriaPlanDetail:
        return _decode_plan_detail(self._patch(f"/aria/plans/{plan_id}", json=changes))

    def approve_plan(self, plan_id: str, **options: Any) -> AriaPlanDetail:
        return _decode_plan_detail(self._post(f"/aria/plans/{plan_id}/approve", json=options))

    def execute_plan(self, plan_id: str, **options: Any) -> AriaPlanDetail:
        return _decode_plan_detail(self._post(f"/aria/plans/{plan_id}/execute", json=options))

    def poll_plan(self, plan_id: str, **options: Any) -> AriaPlanDetail:
        return _decode_plan_detail(self._post(f"/aria/plans/{plan_id}/poll", json=options))

    def interactions(
        self, plan_id: str, *, limit: int | None = None, offset: int | None = None
    ) -> _List[AriaInteraction]:
        params: dict[str, Any] = {}
        for key, value in (("limit", limit), ("offset", offset)):
            if value is not None:
                params[key] = value
        return decode_list(
            AriaInteraction,
            self._get(f"/aria/plans/{plan_id}/interactions", params=params or None),
        )

    def answer(self, interaction_id: str, **payload: Any) -> AriaPlanDetail:
        """Answer a question Aria paused to ask."""
        return _decode_plan_detail(
            self._post(f"/aria/interactions/{interaction_id}/answer", json=payload)
        )

    def wait_for_plan(
        self, plan_id: str, *, timeout: float = 900.0, **options: Any
    ) -> AriaPlanDetail:
        """Poll until the plan finishes or pauses for you.

        ``paused`` is a resting state, not a transient one: the plan makes no
        further progress until a person answers, so polling past it would burn
        the whole timeout waiting for something that cannot happen.
        """
        return wait_for(
            lambda: self.get_plan(plan_id),
            is_done=lambda detail: (
                detail.plan.needs_you
                or detail.plan.status
                in {"completed", "succeeded", "failed", "cancelled", "rejected"}
            ),
            timeout=timeout,
            **options,
        )


class ReleasesAPI(Resource):
    """Release candidates, evidence, waivers, and signoff."""

    def candidates(self) -> _List[ReleaseCandidate]:
        return decode_list(ReleaseCandidate, self._get("/releases/candidates"))

    def get_candidate(self, candidate_id: str) -> ReleaseCandidate:
        return decode(ReleaseCandidate, self._get(f"/releases/candidates/{candidate_id}"))

    def create_candidate(self, name: str, **options: Any) -> ReleaseCandidate:
        return decode(
            ReleaseCandidate, self._post("/releases/candidates", json={"name": name, **options})
        )

    def evaluate(self, candidate_id: str, **options: Any) -> ReleaseCandidate:
        """Recompute the weighted score from current evidence."""
        return decode(
            ReleaseCandidate,
            self._post(f"/releases/candidates/{candidate_id}/evaluate", json=options),
        )

    def add_waiver(self, candidate_id: str, **payload: Any) -> Any:
        """Record an exception. Admin-only, and audited — a waiver is a
        decision someone owns, not a way to raise a score."""
        return self._post(f"/releases/candidates/{candidate_id}/waivers", json=payload)

    def generate_report(self, candidate_id: str, **options: Any) -> Any:
        return self._post(f"/releases/candidates/{candidate_id}/reports", json=options)

    def sign(self, candidate_id: str, *, decision: str, rationale: str, **options: Any) -> Any:
        """Record go / no-go. ``rationale`` is required by design: a signoff
        without a reason is not evidence of a decision."""
        return self._post(
            f"/releases/candidates/{candidate_id}/signoffs",
            json={"decision": decision, "rationale": rationale, **options},
        )

    def report_job(self, report_job_id: str) -> Any:
        """Fetch a generated Allure-format report job by id."""
        return self._get(f"/releases/report-jobs/{report_job_id}")

    def timeline(self, **params: Any) -> Any:
        """Recent promotion/rollback/activation events, newest first.

        ``params``: ``limit`` (default 50, server-capped at 200) and
        ``entity_type`` (``prompt`` / ``workflow`` / ``knowledge_base`` /
        ``skill``).
        """
        return self._get("/releases/timeline", params=params or None)

    def live(self) -> Any:
        """What is currently live across artifact types.

        Prompt ``@prod`` liveness lives in the MLflow registry, not here — see
        the server route's own docstring; this reports DB-backed liveness
        (workflow deployments, knowledge-base active versions).
        """
        return self._get("/releases/live")

    def operations(self, **params: Any) -> Any:
        """Durable release intents, including any with an incomplete external
        effect. ``params``: ``status``, ``limit`` (default 100, capped 500)."""
        return self._get("/releases/operations", params=params or None)

    def reconcile(self) -> Any:
        """Observe provider alias state and settle incomplete prompt release
        intents. The operator-triggered half of the intent-first release
        protocol described in ``ARCHITECTURE.md`` §8."""
        return self._post("/releases/operations/reconcile")

    def resolve_operation(self, operation_id: str, *, action: str, **params: Any) -> Any:
        """Retry or abandon a ``prepared`` (pre-effect) release intent.

        ``action`` is ``"retry"`` or ``"abandon"``. Only ``prepared`` rows are
        accepted — once a row reaches ``applying`` the provider may already
        have changed, and reconciliation (:meth:`reconcile`), not a blind
        retry, is the safe next step.
        """
        return self._post(
            f"/releases/operations/{operation_id}/resolve",
            json={"action": action, **params},
        )


class GateVerdictsAPI(Resource):
    """Advisory per-version evaluation verdicts (prompt/workflow/skill).

    Advisory in v1: a verdict never blocks alias rotation on its own (see
    ``ARCHITECTURE.md`` §4) — it is release evidence for the Version panel,
    not a gate.
    """

    def get(self, artifact_type: str, version_key: str) -> Any:
        """The latest verdict, or ``{"state": "none"}`` if none was recorded."""
        return self._get(f"/gate-verdicts/{artifact_type}/{version_key}")

    def record(self, artifact_type: str, version_key: str, *, state: str, **params: Any) -> Any:
        """Upsert a verdict. ``state`` is ``"pass"``, ``"fail"``, or
        ``"none"``; ``params`` may carry ``score``, ``baseline_score``,
        ``min_aggregate_score``, ``worst_regression``, ``max_regression_delta``,
        and ``eval_run_id``."""
        return self._post(
            f"/gate-verdicts/{artifact_type}/{version_key}",
            json={"state": state, **params},
        )


class SystemAPI(Resource):
    """Operational recovery surfaces: effects that need a human decision.

    Two related recoveries, both "CALIBER could not complete something
    outward-facing, and only a person can decide what happens next" — see the
    server module's own docstring (``routes/system_effects.py``) for the full
    rationale. Only these two are covered so far; ``/system/services``,
    ``/system/queue``, ``/system/alerts``, and ``/system/incidents`` land in a
    follow-up wave onto this same class.
    """

    def effects(self, **params: Any) -> Any:
        """Indeterminate effect-ledger claims. Defaults to ``status=in_progress``
        server-side -- the set that actually needs a decision. ``params``:
        ``status``, ``workflow_run_id``, ``limit``."""
        return self._get("/system/effects", params=params or None)

    def resolve_effect(self, effect_key: str, *, resolution: str, **params: Any) -> Any:
        """Record whether an indeterminate effect happened. ``resolution`` is
        ``"skip"`` (it did happen; do not repeat it) or ``"retry"`` (it did
        not). Admin-scoped and audited -- this asserts something about the
        outside world CALIBER cannot verify on its own."""
        return self._post(
            f"/system/effects/{effect_key}/resolve",
            json={"resolution": resolution, **params},
        )

    def webhook_dead_letters(self, **params: Any) -> Any:
        """Outbound events that were never delivered. Defaults to
        ``status=open`` server-side. ``params``: ``status``, ``kind``, ``limit``."""
        return self._get("/system/webhook-dead-letters", params=params or None)

    def acknowledge_dead_letter(self, dead_letter_id: str, **params: Any) -> Any:
        """Mark a dead letter handled without resending it."""
        return self._post(f"/system/webhook-dead-letters/{dead_letter_id}/acknowledge", json=params)

    def replay_dead_letter(self, dead_letter_id: str) -> Any:
        """Re-send a lost event. A failed replay leaves the row open (not
        acknowledged) and records why, so a failed recovery never looks like
        a completed one."""
        return self._post(f"/system/webhook-dead-letters/{dead_letter_id}/replay")


class ObservabilityAPI(Resource):
    """Traces, experiments, and metrics."""

    def traces(self, **params: Any) -> _List[Trace]:
        payload = self._get("/observability/traces", params=params or None)
        items = payload.get("items") if isinstance(payload, dict) else payload
        return decode_list(Trace, items)

    def trace(self, trace_id: str) -> Any:
        """One trace with its full span tree, left untyped: the node structure
        is MLflow's, and re-declaring it here would drift from it."""
        return self._get(f"/observability/traces/{trace_id}")

    def experiments(self) -> Any:
        return self._get("/observability/experiments")

    def metrics(self, **params: Any) -> Any:
        return self._get("/observability/metrics", params=params or None)


class AuditAPI(Resource):
    """The audit log."""

    def list(self, **params: Any) -> _List[AuditEntry]:
        payload = self._get("/audit-log", params=params or None)
        items = payload.get("items") if isinstance(payload, dict) else payload
        return decode_list(AuditEntry, items)

    def export(self, *, format: str = "csv", **params: Any) -> bytes:
        """Raw export bytes. CSV by default; JSON is admin-only on the server."""
        return self._transport.download("/audit-log/export", params={"format": format, **params})


class EventsAPI(Resource):
    """Server-sent events."""

    def stream(self, **params: Any) -> Iterator[str]:
        """Yield raw SSE lines.

        Deliberately unparsed. The event vocabulary is a live surface, and a
        client that decoded into fixed types would reject events added after it
        shipped — the opposite of what a stream consumer wants.
        """
        return self._transport.stream_lines("/events/stream", params=params or None)


class CookbooksAPI(Resource):
    """Built-in, installable examples."""

    def list(self) -> _List[CookbookRecipe]:
        payload = self._get("/cookbooks")
        items = payload.get("recipes") if isinstance(payload, dict) else payload
        return decode_list(CookbookRecipe, items)

    def install(self, cookbook_id: str, *, name: str | None = None, **options: Any) -> Any:
        """Install a recipe as a paused workflow plus an editable draft.

        Paused on purpose: an example manifest can carry model, connector, or
        side-effect bindings that an operator should review before anything
        runs.
        """
        body: dict[str, Any] = {**options}
        if name is not None:
            body["name"] = name
        return self._post(f"/cookbooks/{cookbook_id}/install", json=body)


class SecretsAPI(Resource):
    """Secret references. Write-only: values are never returned."""

    def list(self) -> Any:
        """Names and metadata only — no values, by design."""
        return self._get("/secrets")

    def put(self, name: str, value: str) -> Any:
        return self._put(f"/secrets/{name}", json={"value": value})

    def revoke(self, name: str) -> Any:
        return self._post(f"/secrets/{name}/revoke")

    def delete(self, name: str) -> Any:
        return self._delete(f"/secrets/{name}")


__all__ = [
    "AriaAPI",
    "AuditAPI",
    "CookbooksAPI",
    "EventsAPI",
    "GateVerdictsAPI",
    "JobsAPI",
    "ObservabilityAPI",
    "ReleasesAPI",
    "ReviewQueuesAPI",
    "SecretsAPI",
    "SystemAPI",
]
