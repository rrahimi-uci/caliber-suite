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
        """Answer a queued item. The write-back that turns review into evidence.

        ``answers`` is wrapped in ``{"answers": ...}`` because that is the
        server's actual field (``ReviewItemSubmitRequest.answers``); the
        request schema forbids extra fields, so posting the answer keys
        unwrapped at the top level used to 422 against a real server despite
        matching every mocked test.
        """
        return self._post(
            f"/review-queues/{queue_id}/items/{item_id}/submit", json={"answers": answers}
        )

    def alignment_examples(self, queue_id: str, **params: Any) -> Any:
        """Human labels usable for judge-alignment scoring.

        ``question_key`` (required by the server) belongs in ``params`` --
        without it the route always 400s, which this method used to make
        impossible to avoid since it accepted no query parameters at all.
        """
        return self._get(f"/review-queues/{queue_id}/alignment-examples", params=params or None)


class AriaSessionsAPI(Resource):
    """Aria conversational sessions: messages, attachments, the message
    queue, intent resolution, and the session-scoped plan lifecycle.

    Distinct from ``client.aria.plans``/``.create_plan``/etc: those are
    Aria's durable **goal-plans** (``/aria/plans/*``), while this is the
    **chat session** surface (``/assistant/*``) a goal-plan is created from.
    Both are "Aria" to a user; they are different route families on the
    server, which is why they are separate classes under one ``client.aria``
    tree rather than one flat namespace.
    """

    def list(self, *, owner: str | None = None) -> Any:
        return self._get("/assistant/sessions", params={"owner": owner} if owner else None)

    def create(self, **options: Any) -> Any:
        """``options`` may carry ``title``, ``goal``, ``metadata_``,
        ``artifact_type``, ``skill_mode``, ``pinned_skill_names``."""
        return self._post("/assistant/sessions", json=options)

    def get(self, session_id: str) -> Any:
        return self._get(f"/assistant/sessions/{session_id}")

    def update(self, session_id: str, **changes: Any) -> Any:
        return self._patch(f"/assistant/sessions/{session_id}", json=changes)

    def messages(self, session_id: str) -> Any:
        return self._get(f"/assistant/sessions/{session_id}/messages")

    def send_message(self, session_id: str, content: str, **params: Any) -> Any:
        """``params`` may carry ``artifact_type``, ``skill_mode``,
        ``skill_names``, ``mode``, ``steer``."""
        return self._post(
            f"/assistant/sessions/{session_id}/messages",
            json={"content": content, **params},
        )

    def queue(self, session_id: str) -> Any:
        """Messages queued to send once the current turn finishes."""
        return self._get(f"/assistant/sessions/{session_id}/queue")

    def enqueue_message(self, session_id: str, content: str, **params: Any) -> Any:
        """``params`` may carry ``mode`` (queue vs. steer) and ``kind``."""
        return self._post(
            f"/assistant/sessions/{session_id}/queue",
            json={"content": content, **params},
        )

    def cancel_queued(self, queue_id: str) -> bool:
        """204 on success; the queued message is gone either way once this
        returns without raising."""
        self._delete(f"/assistant/queue/{queue_id}")
        return True

    def attachments(self, session_id: str) -> Any:
        return self._get(f"/assistant/sessions/{session_id}/attachments")

    def create_attachment(self, session_id: str, *, kind: str, **params: Any) -> Any:
        """Attach by reference rather than uploading bytes. ``kind`` is
        ``"text_snippet"`` (requires ``text=``), ``"library_resource"``
        (requires ``resource_type=``, ``resource_id=``), or
        ``"object_file"`` (requires ``bucket=``, ``key=``). For raw file
        bytes see :meth:`upload_attachment`."""
        return self._post(
            f"/assistant/sessions/{session_id}/attachments",
            json={"kind": kind, **params},
        )

    def upload_attachment(
        self,
        session_id: str,
        filename: str,
        content: bytes,
        *,
        bucket: str | None = None,
    ) -> Any:
        """Upload a file's bytes as an attachment. Multipart, so it does not
        go through the JSON path. ``bucket``, if given, also persists the
        raw file to that object-store bucket."""
        files = {"file": (filename, content, "application/octet-stream")}
        data: dict[str, str] = {"bucket": bucket} if bucket else {}
        response = self._transport.request(
            "POST", f"/assistant/sessions/{session_id}/attachments/upload", files=files, data=data
        )
        return response.data

    def delete_attachment(self, attachment_id: str) -> bool:
        self._delete(f"/assistant/attachments/{attachment_id}")
        return True

    def resolve_intent(self, session_id: str, content: str, **params: Any) -> Any:
        """Classify a free-text message into a known intent + slots before
        committing to a plan. ``params`` may carry ``context``."""
        return self._post(
            f"/assistant/sessions/{session_id}/intent/resolve",
            json={"content": content, **params},
        )

    def create_plan(self, session_id: str, **params: Any) -> Any:
        """``params`` may carry ``content``, ``intent_name``,
        ``slot_overrides``, ``context``."""
        return self._post(f"/assistant/sessions/{session_id}/plans", json=params)

    def latest_plan(self, session_id: str) -> Any:
        return self._get(f"/assistant/sessions/{session_id}/plans/latest")

    def execute_plan(self, session_id: str, **params: Any) -> Any:
        return self._post(f"/assistant/sessions/{session_id}/plans/execute", json=params)

    def operation(self, session_id: str, operation_id: str) -> Any:
        """Poll a long-running plan-execution operation."""
        return self._get(f"/assistant/sessions/{session_id}/operations/{operation_id}")

    def drafts(self, session_id: str) -> Any:
        """Artifact drafts this session has produced. To act on one, see
        ``client.aria.drafts``."""
        return self._get(f"/assistant/sessions/{session_id}/drafts")


class AriaDraftsAPI(Resource):
    """An artifact draft's validate -> test -> approve -> publish lifecycle.

    ``approve``/``publish`` are ``gated`` in Aria's own tool projection --
    the autonomous loop cannot call them regardless of build mode -- but
    both remain ordinary, human-driven HTTP operations reachable here.
    """

    def get(self, draft_id: str) -> Any:
        return self._get(f"/assistant/drafts/{draft_id}")

    def update(self, draft_id: str, **changes: Any) -> Any:
        return self._patch(f"/assistant/drafts/{draft_id}", json=changes)

    def validate(self, draft_id: str) -> Any:
        return self._post(f"/assistant/drafts/{draft_id}/validate")

    def test(self, draft_id: str) -> Any:
        return self._post(f"/assistant/drafts/{draft_id}/test")

    def approve(self, draft_id: str) -> Any:
        return self._post(f"/assistant/drafts/{draft_id}/approve")

    def publish(self, draft_id: str) -> Any:
        """A failed publish reports ``success: false`` in the body (400)
        rather than raising for every failure mode -- check the report."""
        return self._post(f"/assistant/drafts/{draft_id}/publish")


class AriaAPI(Resource):
    """Aria: conversational sessions (``.sessions``), artifact drafts
    (``.drafts``), durable goal-plans, and deployment-wide config."""

    def __init__(self, transport: Any) -> None:
        super().__init__(transport)
        self.sessions = AriaSessionsAPI(transport)
        self.drafts = AriaDraftsAPI(transport)

    def capabilities(self) -> Any:
        """What Aria is allowed to do in this deployment."""
        return self._get("/aria/capabilities")

    def config(self) -> Any:
        """Deployment-wide assistant settings: engine, model, reasoning
        effort, enabled intents/domains, autonomy status."""
        return self._get("/assistant/config")

    def update_config(self, **changes: Any) -> Any:
        return self._patch("/assistant/config", json=changes)

    def prompt_draft(self, description: str) -> Any:
        """One-shot prompt draft from a free-text task description -- feeds
        the manual prompt builder's "Describe it" on-ramp. Not session-
        scoped and nothing is persisted."""
        return self._post("/assistant/prompt-draft", json={"description": description})

    def run(self, run_id: str) -> Any:
        """One Aria-driven run's detail (a message turn or plan-execution
        run), for the session transcript's expandable trace."""
        return self._get(f"/assistant/runs/{run_id}")

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
    """Operational surfaces: effect-ledger and webhook recovery (both
    "CALIBER could not complete something outward-facing, and only a person
    can decide what happens next" -- see ``routes/system_effects.py`` for
    the full rationale), plus service health, the background-loop queue,
    and the incident/alert surface.
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

    def services(self) -> Any:
        """Live health probes for CALIBER's own backing services
        (database, object storage, MLflow, ...) -- the Settings page's
        service-status panel."""
        return self._get("/system/services")

    def queue(self) -> Any:
        """Depth and health of the in-process background-loop queues
        (refinement, calibration, workflow runs, ...)."""
        return self._get("/system/queue")

    def alerts(self) -> Any:
        """Active operational alerts (a queue backed up, a loop stalled)."""
        return self._get("/system/alerts")

    def incidents(self) -> Any:
        """Durable incident records -- distinct from :meth:`alerts`, which
        is live/transient; an incident persists once opened."""
        return self._get("/system/incidents")

    def acknowledge_incident(self, incident_id: str) -> Any:
        """Take ownership. Deliberately not the same as resolving: "someone
        is looking at this" and "it stopped" are different facts."""
        return self._post(f"/system/incidents/{incident_id}/acknowledge")

    def silence_incident(self, incident_id: str, *, minutes: int = 60) -> Any:
        """Mute routing for ``minutes`` (default 60) while keeping the
        record -- the incident is not resolved, just not paging anyone."""
        return self._post(f"/system/incidents/{incident_id}/silence", json={"minutes": minutes})


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

    def record_feedback(self, trace_id: str, *, value: Any, **params: Any) -> Any:
        """Attach a human feedback assessment to a trace
        (``mlflow.log_feedback``). ``value`` is a bool, number, or string;
        ``params`` may carry ``name`` (default ``"feedback"``) and
        ``rationale``. Returns the trace's refreshed assessments."""
        return self._post(
            f"/observability/traces/{trace_id}/feedback",
            json={"value": value, **params},
        )


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


class LlmPricingAPI(Resource):
    """Per-model token pricing (USD per 1K), used to cost out gateway
    usage."""

    def list(self, *, status: str | None = None) -> Any:
        return self._get("/llm-pricing", params={"status": status} if status else None)

    def get(self, pricing_id: str) -> Any:
        return self._get(f"/llm-pricing/{pricing_id}")

    def create(
        self,
        *,
        provider: str,
        model_id: str,
        prompt_price: float,
        completion_price: float,
        **options: Any,
    ) -> Any:
        """``options`` may carry ``cached_prompt_price``, ``tags``."""
        return self._post(
            "/llm-pricing",
            json={
                "provider": provider,
                "model_id": model_id,
                "prompt_price": prompt_price,
                "completion_price": completion_price,
                **options,
            },
        )

    def update(self, pricing_id: str, **changes: Any) -> Any:
        return self._patch(f"/llm-pricing/{pricing_id}", json=changes)


class MemoryAPI(Resource):
    """Direct add/search/list/delete over agent long-term memory (mem0).

    Every operation is scoped by ``agent_id``/``user_id``/``run_id`` -- at
    least one is required -- so the shared team store stays partitioned.
    503s when memory is disabled or the ``[memory]`` extra is absent on the
    server.
    """

    def add(self, text: str, *, agent_id: str | None = None, **params: Any) -> Any:
        """``params`` may carry ``user_id``, ``run_id``, ``metadata``,
        ``infer``. At least one of ``agent_id``/``user_id``/``run_id`` is
        required."""
        body: dict[str, Any] = {"text": text, **params}
        if agent_id is not None:
            body["agent_id"] = agent_id
        return self._post("/memory", json=body)

    def search(self, query: str, *, agent_id: str | None = None, **params: Any) -> Any:
        """``params`` may carry ``user_id``, ``run_id``, ``top_k`` (default
        10 server-side)."""
        body: dict[str, Any] = {"query": query, **params}
        if agent_id is not None:
            body["agent_id"] = agent_id
        return self._post("/memory/search", json=body)

    def list(self, *, agent_id: str | None = None, **params: Any) -> Any:
        """``params`` may carry ``user_id``, ``run_id``, ``top_k`` (default
        50 server-side). Sent as query parameters, not a JSON body."""
        query: dict[str, Any] = {**params}
        if agent_id is not None:
            query["agent_id"] = agent_id
        return self._get("/memory", params=query or None)

    def delete_all(self, *, agent_id: str | None = None, **params: Any) -> Any:
        """Delete every memory in the given scope. Irreversible."""
        query: dict[str, Any] = {**params}
        if agent_id is not None:
            query["agent_id"] = agent_id
        return self._delete("/memory", params=query or None)


__all__ = [
    "AriaAPI",
    "AriaDraftsAPI",
    "AriaSessionsAPI",
    "AuditAPI",
    "CookbooksAPI",
    "EventsAPI",
    "GateVerdictsAPI",
    "JobsAPI",
    "LlmPricingAPI",
    "MemoryAPI",
    "ObservabilityAPI",
    "ReleasesAPI",
    "ReviewQueuesAPI",
    "SecretsAPI",
    "SystemAPI",
]
