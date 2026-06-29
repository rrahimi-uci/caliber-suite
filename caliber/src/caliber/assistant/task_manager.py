"""Minimal typed task routing for assistant turns."""

from __future__ import annotations

from dataclasses import dataclass

from caliber.assistant.models import AssistantMode
from caliber.assistant.task_context import TaskKind


@dataclass(frozen=True)
class TaskManagerDecision:
    task_kind: TaskKind


class TaskManager:
    """Choose the high-level task lane for a turn.

    This is intentionally conservative in the first implementation: it does not
    change product behavior, it just makes the lane explicit for prompts,
    tracing, and future planner routing.
    """

    def choose(
        self,
        *,
        mode: AssistantMode,
        explicit_task_kind: TaskKind | None = None,
        resume_from_plan_id: str | None = None,
    ) -> TaskManagerDecision:
        if explicit_task_kind is not None:
            return TaskManagerDecision(task_kind=explicit_task_kind)
        if resume_from_plan_id:
            return TaskManagerDecision(task_kind="resume")
        if mode == "plan":
            return TaskManagerDecision(task_kind="plan")
        if mode == "build":
            return TaskManagerDecision(task_kind="build")
        return TaskManagerDecision(task_kind="answer")
