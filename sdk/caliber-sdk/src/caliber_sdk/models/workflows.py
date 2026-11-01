"""Typed models for workflows, versions, runs, deployments, and services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ._decode import decode

#: Run states that mean the work has stopped.
#:
#: The server's authoritative vocabulary for a workflow run's ``status``
#: (``caliber/src/caliber/workflows/run_state.py``) is
#: ``{queued, running, waiting_approval, waiting_event, completed, failed,
#: cancelled, expired}``, with ``{completed, failed, cancelled, expired}`` as
#: its own ``TERMINAL_RUN_STATUSES``. ``"completed"`` and ``"expired"`` were
#: both missing here even though the server can report either one: a run's
#: default status (``CaliberWorkflowRun.status`` defaults to ``"completed"``
#: in ``caliber/src/caliber/db/models.py``) and the outcome of a run whose
#: lease lapsed (``RUN_STATUS_EXPIRED``, set by
#: ``caliber/src/caliber/orchestrator/workflow_run_worker.py``) would each
#: poll forever in :meth:`WorkflowRunsAPI.wait`/`AsyncWorkflowRunsAPI.wait`
#: until :class:`~caliber_sdk.waiters.WaitTimeout`, even though the run had
#: already stopped. ``"succeeded"``, ``"canceled"`` (single L), and
#: ``"timed_out"`` are kept for backward compatibility even though they are
#: not values the current server emits for this field -- being lenient about
#: recognizing a terminal state costs nothing, and removing them would be a
#: breaking change for any caller relying on them today. ``"error"`` is added
#: for the same reason it is already a terminal/failure spelling in the
#: sibling ``CalibrationJob.is_terminal`` (``models/assets.py``) and
#: ``Evaluation.is_terminal`` (``models/quality.py``) sets, and in this
#: package's own generic ``waiters.FAILURE_STATES`` -- the interpreter-level
#: "error" outcome is normalized to ``RUN_STATUS_FAILED`` before it is
#: persisted as a run's ``status`` today (see
#: ``RUNTIME_RESULT_TO_RUN_STATUS`` in the same server module), so the server
#: does not currently emit it here either, but recognizing it is free and
#: keeps this set consistent with the rest of the SDK's vocabulary.
TERMINAL_RUN_STATES = frozenset(
    {"succeeded", "completed", "failed", "cancelled", "canceled", "timed_out", "expired", "error"}
)
#: Terminal states that did not succeed -- i.e. :data:`TERMINAL_RUN_STATES`
#: minus the success spellings (``"succeeded"``, ``"completed"``).
FAILED_RUN_STATES = frozenset({"failed", "cancelled", "canceled", "timed_out", "expired", "error"})


@dataclass
class Workflow:
    """The container. Versions hold the manifest; this holds identity and status."""

    workflow_id: str = ""
    project_id: str | None = None
    name: str = ""
    description: str | None = None
    owner: str | None = None
    status: str = ""
    default_experiment_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowVersion:
    """One immutable manifest snapshot.

    ``manifest`` and ``validation_report`` stay open: the manifest is a
    structured-but-extensible document the server validates, and the report is
    produced by the validator rather than defined here.
    """

    version_id: str = ""
    workflow_id: str = ""
    version_number: int = 0
    status: str = ""
    manifest: dict[str, Any] = field(default_factory=dict)
    manifest_hash: str | None = None
    compiler_version: str | None = None
    compiled_artifact_uri: str | None = None
    validation_report: dict[str, Any] | None = None
    compiled_bundle: Any = None
    created_by: str | None = None
    created_at: str | None = None
    published_by: str | None = None
    published_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_draft(self) -> bool:
        return self.status == "draft"


@dataclass
class WorkflowRun:
    """One execution."""

    workflow_run_id: str = ""
    workflow_id: str = ""
    project_id: str | None = None
    workflow_version_id: str | None = None
    deployment_alias: str | None = None
    mlflow_run_id: str | None = None
    trace_id: str | None = None
    session_id: str | None = None
    status: str = ""
    source: str | None = None
    priority: int | None = None
    queued_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    claimed_by: str | None = None
    error: str | None = None
    output: Any = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # The server keeps a run's final text on ``summary.output`` and its failure reason on
        # ``error_summary``; it has no top-level ``output``/``error``. Reading them from there
        # means ``run.output`` is what a caller expects instead of always ``None``.
        summary = self.extra.get("summary")
        if self.output is None and isinstance(summary, dict) and summary.get("output") is not None:
            self.output = summary["output"]
        if self.error is None and self.extra.get("error_summary"):
            self.error = str(self.extra["error_summary"])

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_RUN_STATES

    @property
    def succeeded(self) -> bool:
        return self.status in {"succeeded", "completed"}


@dataclass
class AdmissionManifest:
    """The immutable manifest a run was admitted with: what it was pinned to and who started it.

    Not the workflow manifest the run executed (that is ``runs.manifest``). A legacy run has none:
    ``legacy`` is true and ``run_manifest`` and ``manifest_digest`` are ``None``.
    ``manifest_digest`` is the SHA-256 of the canonical bytes of ``run_manifest``.
    """

    workflow_run_id: str = ""
    #: ``True`` for a run with no admission manifest, ``False`` for an admitted one;
    #: ``None`` when the server did not say (never read as admitted).
    legacy: bool | None = None
    schema_version: str | None = None
    manifest_id: str | None = None
    run_manifest: dict[str, Any] | None = None
    manifest_digest: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CausalRunManifestLink:
    """The run's admission manifest by id and digest (never its body).

    ``status`` is ``linked``, ``legacy`` (a run admitted before manifests has none, and never
    will) or ``missing`` (an admitted run with no stored row); ``code`` says why when it is not
    ``linked``.
    """

    status: str | None = None
    code: str | None = None
    manifest_id: str | None = None
    schema_version: str | None = None
    manifest_digest: str | None = None
    created_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CausalAbsentRecord:
    """A record kind the platform does not have yet (acceptance, evidence package): stated, not
    synthesized."""

    status: str | None = None
    code: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CausalContextManifest:
    """One persisted context manifest of a step, by id and digest."""

    context_manifest_id: str = ""
    step_request_id: str = ""
    node_step_id: str | None = None
    occurrence_id: str | None = None
    attempt: int | None = None
    revision: int | None = None
    schema_version: str | None = None
    manifest_digest: str | None = None
    created_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CausalPolicyDecision:
    """One persisted policy decision of the run: the answer and its digests, never who asked or
    why in prose."""

    decision_id: str = ""
    grant_id: str | None = None
    action: str | None = None
    resource: str | None = None
    result: str | None = None
    reason_codes: list[str] = field(default_factory=list)
    policy_version: str | None = None
    input_hash: str | None = None
    evaluated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CausalContextLink:
    """The context manifest of the step that proposed an effect (latest attempt, then highest
    revision)."""

    status: str | None = None
    code: str | None = None
    context_manifest_id: str | None = None
    revision: int | None = None
    attempt: int | None = None
    manifest_digest: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CausalApprovalLink:
    """The runtime approval request behind an effect: ids, digests, states and timestamps,
    never who decided."""

    status: str | None = None
    code: str | None = None
    approval_id: str | None = None
    request_status: str | None = None
    superseded_by: str | None = None
    intent_hash: str | None = None
    evidence_hash: str | None = None
    policy_digest: str | None = None
    requested_at: str | None = None
    decided_at: str | None = None
    expires_at: str | None = None
    consumed_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CausalDecisionLink:
    """The policy decision that authorised an effect, when the effect records it.

    A policy-approved effect names it; an exact-intent effect names the commit-time decision stored
    on its consumed approval request. An exact-intent effect consumed before that id was stored is
    ``unrecorded`` (``code="decision_recorded_per_run_only"``): the run's decisions are listed in
    the ``policy_decisions`` section, and the pairing is never guessed.
    """

    status: str | None = None
    code: str | None = None
    decision_id: str | None = None
    action: str | None = None
    result: str | None = None
    reason_codes: list[str] = field(default_factory=list)
    policy_version: str | None = None
    input_hash: str | None = None
    evaluated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CausalEffectOutcomeLink:
    """The verified receipt of an effect; ``effect_id`` is the id its spans carry
    (``caliber.effect_id``)."""

    status: str | None = None
    code: str | None = None
    effect_id: str | None = None
    attempt: int | None = None
    receipt_digest: str | None = None
    response_digest: str | None = None
    verified_at: str | None = None
    committed: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CausalEffect:
    """One effect proposal and the persisted records that authorised and settled it.

    Each link has a ``status``: ``linked`` (a persisted row of this run), ``not_required``,
    ``pending`` (the effect is in flight and the row is expected to appear), ``missing`` (the
    chain needs a row, the effect is settled and there is none: a broken chain) or
    ``unrecorded`` (the platform does not store this link against the effect, so it is neither
    followable nor broken). ``missing_links`` names the ``missing`` links and
    ``unrecorded_links`` the ``unrecorded`` ones. ``span_attributes`` is a union derived from
    the resolved rows, not what any one span carries.
    """

    proposal_id: str = ""
    node_id: str = ""
    occurrence_id: str = ""
    node_step_id: str = ""
    state: str = ""
    connector: str = ""
    connector_version: str = ""
    effect_class: str = ""
    intent_hash: str = ""
    created_at: str | None = None
    settled_at: str | None = None
    context_manifest: CausalContextLink = field(default_factory=CausalContextLink)
    approval: CausalApprovalLink = field(default_factory=CausalApprovalLink)
    decision: CausalDecisionLink = field(default_factory=CausalDecisionLink)
    effect: CausalEffectOutcomeLink = field(default_factory=CausalEffectOutcomeLink)
    span_attributes: dict[str, str] = field(default_factory=dict)
    missing_links: list[str] = field(default_factory=list)
    unrecorded_links: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CausalLinks:
    """One page of one section of a run's persisted causal links (identifiers, digests, states
    and timestamps only).

    ``section`` is ``effects``, ``context_manifests`` or ``policy_decisions``; only that list is
    filled. Pass ``next_cursor`` back as ``cursor`` for the next page while ``has_more``.
    """

    workflow_run_id: str = ""
    project_id: str | None = None
    trace_id: str | None = None
    section: str = ""
    run_manifest: CausalRunManifestLink = field(default_factory=CausalRunManifestLink)
    acceptance: CausalAbsentRecord = field(default_factory=CausalAbsentRecord)
    evidence_package: CausalAbsentRecord = field(default_factory=CausalAbsentRecord)
    effects: list[CausalEffect] = field(default_factory=list)
    context_manifests: list[CausalContextManifest] = field(default_factory=list)
    policy_decisions: list[CausalPolicyDecision] = field(default_factory=list)
    limit: int | None = None
    #: ``False`` unless the server said there is more (a missing field is never read as
    #: "there is more").
    has_more: bool = False
    next_cursor: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: Any) -> CausalLinks:
        """Decode a ``/causal-links`` payload, nested records included (shared by the sync and
        async clients)."""
        data = payload if isinstance(payload, dict) else {}
        nested = {
            "run_manifest",
            "acceptance",
            "evidence_package",
            "effects",
            "context_manifests",
            "policy_decisions",
        }
        page = decode(cls, {key: value for key, value in data.items() if key not in nested})
        page.run_manifest = decode(CausalRunManifestLink, data.get("run_manifest"))
        page.acceptance = decode(CausalAbsentRecord, data.get("acceptance"))
        page.evidence_package = decode(CausalAbsentRecord, data.get("evidence_package"))
        page.effects = [_decode_effect(item) for item in _items(data.get("effects"))]
        page.context_manifests = [
            decode(CausalContextManifest, item) for item in _items(data.get("context_manifests"))
        ]
        page.policy_decisions = [
            decode(CausalPolicyDecision, item) for item in _items(data.get("policy_decisions"))
        ]
        return page


def _items(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _decode_effect(payload: dict[str, Any]) -> CausalEffect:
    links = {"context_manifest", "approval", "decision", "effect"}
    effect = decode(
        CausalEffect, {key: value for key, value in payload.items() if key not in links}
    )
    effect.context_manifest = decode(CausalContextLink, payload.get("context_manifest"))
    effect.approval = decode(CausalApprovalLink, payload.get("approval"))
    effect.decision = decode(CausalDecisionLink, payload.get("decision"))
    effect.effect = decode(CausalEffectOutcomeLink, payload.get("effect"))
    return effect


@dataclass
class WorkflowService:
    """A workflow published as an externally invocable HTTP service."""

    service_id: str = ""
    workflow_id: str = ""
    alias: str | None = None
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    enabled: bool = False
    auth_required: bool = True
    rate_limit_per_minute: int | None = None
    cors_allowed_origins: list[str] = field(default_factory=list)
    endpoint: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    token_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "FAILED_RUN_STATES",
    "TERMINAL_RUN_STATES",
    "Workflow",
    "WorkflowRun",
    "WorkflowService",
    "WorkflowVersion",
]
