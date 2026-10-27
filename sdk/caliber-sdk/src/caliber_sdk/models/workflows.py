"""Typed models for workflows, versions, runs, deployments, and services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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
