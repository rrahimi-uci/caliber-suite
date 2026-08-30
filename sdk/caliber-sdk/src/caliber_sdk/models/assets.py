"""Typed models for the governed asset families: agents, prompts, skills, tools.

Field sets mirror the server (``caliber.schemas`` and the prompt route
serializers). Where the server keeps a payload open — a tool's JSON Schema, a
skill's metadata — so does this, because those shapes are the caller's data
rather than CALIBER's contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Agent:
    """The agent record — the anchor a verification item, refinement job,
    approval, and rollback checkpoint all hang off of.

    Mirrors ``caliber.schemas.AgentConfigSchema``. ``optimizer_config``,
    ``eval_thresholds``, and ``approval_policy`` stay open dicts because the
    server itself keeps them open -- they're policy payloads whose shape
    varies by optimizer/artifact type, not a fixed CALIBER contract.
    """

    agent_id: str = ""
    experiment_id: str = ""
    name: str = ""
    owner: str = ""
    artifact_types: list[str] = field(default_factory=list)
    eval_thresholds: dict[str, Any] = field(default_factory=dict)
    optimizer_config: dict[str, Any] = field(default_factory=dict)
    approval_policy: dict[str, Any] = field(default_factory=dict)
    optimize_for: str = ""
    collaboration_mode: str | None = None
    enabled: bool = True
    required_approvals: int = 1
    created_at: str | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Prompt:
    """A prompt as the list/detail routes report it.

    Prompts live in MLflow's registry, not CALIBER's database, so this carries
    a registry coordinate (``prompt_name``, ``version``, ``alias``) rather than
    a CALIBER row id. ``template_preview`` is truncated by the server; fetch
    the version to read the whole template.
    """

    agent_id: str = ""
    prompt_name: str = ""
    version: int | None = None
    alias: str | None = None
    template_preview: str | None = None
    template_length: int = 0
    approval_id: str | None = None
    artifact_ref: str | None = None
    agent_name: str | None = None
    agent_enabled: bool | None = None
    has_prompt: bool | None = None
    source: str | None = None
    description: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Skill:
    """A reusable instruction asset."""

    skill_id: str = ""
    name: str = ""
    description: str | None = None
    summary: str | None = None
    content: str | None = None
    owner: str | None = None
    category: str | None = None
    tags: list[str] = field(default_factory=list)
    skill_metadata: dict[str, Any] = field(default_factory=dict)
    allowed_tools: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    status: str = ""
    version: int | None = None
    created_at: str | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillRender:
    """A skill's content with variables substituted."""

    skill_id: str = ""
    skill_name: str = ""
    rendered_content: str = ""
    original_content: str = ""
    detected_variables: list[str] = field(default_factory=list)
    unresolved_variables: list[str] = field(default_factory=list)
    variables_applied: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    word_count: int = 0
    char_count: int = 0
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillSelection:
    """Whether a skill would be auto-selected for a query, and why."""

    skill_id: str = ""
    skill_name: str = ""
    is_selected: bool = False
    selection_score: float = 0.0
    selection_reason: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillVersion:
    """One immutable skill snapshot."""

    skill_id: str = ""
    version_number: int = 0
    content: str | None = None
    summary: str | None = None
    created_by: str | None = None
    created_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Tool:
    """A registered callable.

    ``input_schema`` and ``output_schema`` stay open mappings: they are JSON
    Schema documents describing the caller's own function, and CALIBER stores
    them rather than defining them.
    """

    tool_id: str = ""
    name: str = ""
    version: str | None = None
    description: str | None = None
    module_path: str | None = None
    callable_name: str | None = None
    input_schema: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    side_effect_level: str | None = None
    requires_approval: bool = False
    allow_in_preview: bool = True
    secret_refs: list[str] = field(default_factory=list)
    owner: str | None = None
    status: str = ""
    deprecated_at: str | None = None
    successor_tool_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class CalibrationJob:
    """One tool calibration job.

    ``result`` is left open: it carries scorer output whose keys vary by suite,
    and the server is the authority on what a run measured.
    """

    job_id: str = ""
    tool_id: str | None = None
    status: str = ""
    requested_by: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    created_at: str | None = None
    claimed_at: str | None = None
    claimed_by: str | None = None
    finished_at: str | None = None
    pass_rate: float | None = None
    retry_of_job_id: str | None = None
    resolution: str | None = None
    resolution_reason: str | None = None
    resolved_by: str | None = None
    resolved_at: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        """Whether the job has stopped, successfully or not."""
        return self.status in {"succeeded", "failed", "cancelled", "error", "completed"}


__all__ = [
    "CalibrationJob",
    "Prompt",
    "Skill",
    "SkillRender",
    "SkillSelection",
    "SkillVersion",
    "Tool",
]
