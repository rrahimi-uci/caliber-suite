"""Shared task-context types and helpers for Aria turns and durable plans."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

TaskKind = Literal["answer", "clarify", "build", "plan", "resume"]

_TASK_CONTEXT_KEY = "assistant_task_context"


class TaskContextRef(BaseModel):
    """One referenced resource or object relevant to the current task."""

    model_config = ConfigDict(extra="forbid")

    ref_type: str = Field(min_length=1, max_length=64)
    ref_id: str = Field(min_length=1, max_length=256)
    label: str = Field(default="", max_length=256)
    metadata_: dict[str, Any] = Field(default_factory=dict)


class AssistantTaskContext(BaseModel):
    """Normalized task envelope shared by assistant turns and durable plans."""

    model_config = ConfigDict(extra="forbid")

    project_id: str | None = Field(default=None, max_length=64)
    scopes: list[str] = Field(default_factory=list)
    context_refs: list[TaskContextRef] = Field(default_factory=list)
    constraints: dict[str, Any] = Field(default_factory=dict)
    done_when: list[str] = Field(default_factory=list)
    current_surface: str = Field(default="", max_length=64)
    task_kind: TaskKind | None = None
    selected_resources: list[TaskContextRef] = Field(default_factory=list)
    resume_from_plan_id: str | None = Field(default=None, max_length=64)


def task_context_from_session_metadata(metadata_raw: Any) -> AssistantTaskContext:
    """Best-effort read of the stable task envelope stored on a session."""
    metadata = copy.deepcopy(metadata_raw) if isinstance(metadata_raw, dict) else {}
    raw = metadata.get(_TASK_CONTEXT_KEY)
    if not isinstance(raw, dict):
        return AssistantTaskContext()
    try:
        return AssistantTaskContext.model_validate(raw)
    except ValidationError:
        return AssistantTaskContext()


def update_session_task_context_metadata(
    metadata_raw: Any,
    task_context: AssistantTaskContext,
) -> dict[str, Any]:
    """Persist only the stable subset of the task envelope on the session."""
    metadata = copy.deepcopy(metadata_raw) if isinstance(metadata_raw, dict) else {}
    stable = task_context.model_dump(
        mode="python",
        exclude={"scopes", "task_kind"},
    )
    metadata[_TASK_CONTEXT_KEY] = stable
    return metadata


def merge_task_context(
    base: AssistantTaskContext | None = None,
    *,
    project_id: str | None = None,
    project_id_set: bool = False,
    scopes: Sequence[str] | None = None,
    current_surface: str | None = None,
    task_kind: TaskKind | None = None,
    constraints: dict[str, Any] | None = None,
    constraints_set: bool = False,
    done_when: Sequence[str] | None = None,
    done_when_set: bool = False,
    context_refs: Sequence[TaskContextRef | dict[str, Any]] | None = None,
    context_refs_set: bool = False,
    selected_resources: Sequence[TaskContextRef | dict[str, Any]] | None = None,
    selected_resources_set: bool = False,
    resume_from_plan_id: str | None = None,
    resume_from_plan_id_set: bool = False,
) -> AssistantTaskContext:
    """Overlay per-request task data onto the stored stable task envelope."""
    payload = (
        base.model_dump(mode="python")
        if isinstance(base, AssistantTaskContext)
        else AssistantTaskContext().model_dump(mode="python")
    )
    if project_id_set:
        payload["project_id"] = project_id
    payload["scopes"] = sorted({str(scope) for scope in (scopes or ()) if str(scope).strip()})
    if current_surface is not None:
        payload["current_surface"] = current_surface
    if task_kind is not None:
        payload["task_kind"] = task_kind
    if constraints_set:
        payload["constraints"] = dict(constraints or {})
    if done_when_set:
        payload["done_when"] = [str(item) for item in (done_when or []) if str(item).strip()]
    if context_refs_set:
        payload["context_refs"] = [
            ref.model_dump(mode="python") if isinstance(ref, TaskContextRef) else dict(ref)
            for ref in (context_refs or [])
        ]
    if selected_resources_set:
        payload["selected_resources"] = [
            ref.model_dump(mode="python") if isinstance(ref, TaskContextRef) else dict(ref)
            for ref in (selected_resources or [])
        ]
    if resume_from_plan_id_set:
        payload["resume_from_plan_id"] = resume_from_plan_id
    return AssistantTaskContext.model_validate(payload)
