"""Typed models for the operational and agentic surfaces (beta tier)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Job:
    """A durable background job (refinement, calibration, reporting)."""

    job_id: str = ""
    status: str = ""
    kind: str | None = None
    agent_id: str | None = None
    optimizer: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    error: str | None = None
    result: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            "succeeded",
            "completed",
            "failed",
            "error",
            "cancelled",
            "rejected",
            "applied",
        }

    @property
    def awaits_human(self) -> bool:
        """Whether the job stopped for a person rather than finishing.

        A refinement job that reaches ``candidate_ready`` is not done — it is
        waiting for an operator to apply it. Treating that as terminal is how a
        script silently drops the human decision the loop exists for.
        """
        return self.status in {"candidate_ready", "awaiting_approval", "paused"}


@dataclass
class ReviewQueue:
    """A structured human-review queue."""

    queue_id: str = ""
    name: str = ""
    description: str | None = None
    owner: str | None = None
    status: str = ""
    review_questions: list[dict[str, Any]] = field(default_factory=list)
    item_count: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AriaPlan:
    """An Aria goal-plan: a sequence of steps awaiting approval or execution."""

    plan_id: str = ""
    session_id: str | None = None
    goal: str = ""
    status: str = ""
    autonomy: str | None = None
    owner: str | None = None
    step_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_you(self) -> bool:
        """Paused awaiting a human decision — a gate, approval, or confirm.

        The state the SPA badges, and the one a script must not poll past: a
        paused plan makes no further progress until someone answers.
        """
        return self.status == "paused"


@dataclass
class AriaPlanStep:
    """One step inside an Aria plan detail response."""

    step_id: str = ""
    plan_id: str = ""
    title: str = ""
    capability_key: str | None = None
    depends_on: list[str] = field(default_factory=list)
    status: str = ""
    result: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    draft_id: str | None = None
    job_id: str | None = None
    approval_id: str | None = None
    checkpoint_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AriaPlanDetail:
    """A plan plus its steps."""

    plan: AriaPlan = field(default_factory=AriaPlan)
    steps: list[AriaPlanStep] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AriaInteraction:
    """One pause/question inside an Aria plan."""

    interaction_id: str = ""
    plan_id: str = ""
    step_id: str = ""
    kind: str = ""
    prompt: str = ""
    options: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    required_scope: str | None = None
    status: str = ""
    response: dict[str, Any] = field(default_factory=dict)
    responded_by: str | None = None
    responded_at: str | None = None
    created_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReleaseCandidate:
    """A release candidate with weighted criteria and signoff."""

    candidate_id: str = ""
    name: str = ""
    artifact_type: str | None = None
    artifact_ref: str | None = None
    version_ref: str | None = None
    status: str = ""
    weighted_score: float | None = None
    criteria: list[dict[str, Any]] = field(default_factory=list)
    created_by: str | None = None
    created_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Trace:
    """One MLflow trace as observability reports it."""

    trace_id: str = ""
    request_id: str | None = None
    status: str | None = None
    timestamp_ms: int | None = None
    execution_time_ms: float | None = None
    tags: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class AuditEntry:
    """One audit-log row."""

    audit_id: str = ""
    actor: str = ""
    action: str = ""
    entity_type: str | None = None
    entity_id: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    created_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CookbookRecipe:
    """A built-in, installable example."""

    id: str = ""
    slug: str = ""
    title: str = ""
    summary: str = ""
    icon: str | None = None
    capabilities: list[str] = field(default_factory=list)
    prerequisites: list[str] = field(default_factory=list)
    activation_requires_review: bool = True
    steps: list[dict[str, Any]] = field(default_factory=list)
    readiness: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_ready(self) -> bool:
        return str(self.readiness.get("status", "")) == "ready"

    @property
    def unmet_checks(self) -> list[dict[str, Any]]:
        """Checks standing between this recipe and a clean install."""
        checks = self.readiness.get("checks")
        if not isinstance(checks, list):
            return []
        return [c for c in checks if isinstance(c, dict) and c.get("status") != "ready"]


__all__ = [
    "AriaInteraction",
    "AriaPlan",
    "AriaPlanDetail",
    "AriaPlanStep",
    "AuditEntry",
    "CookbookRecipe",
    "Job",
    "ReleaseCandidate",
    "ReviewQueue",
    "Trace",
]
