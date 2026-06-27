"""Build normalized assistant task context from request + session state."""

from __future__ import annotations

from collections.abc import Sequence

from caliber.assistant.models import MessageSendRequest
from caliber.assistant.task_context import (
    AssistantTaskContext,
    TaskKind,
    merge_task_context,
    task_context_from_session_metadata,
)


class AssistantContextBuilder:
    """Compose the per-turn task envelope from stored and request-local state."""

    def build_turn_task_context(
        self,
        *,
        session_metadata: dict[str, object],
        body: MessageSendRequest,
        project_id: str | None,
        scopes: Sequence[str] | None,
        current_surface: str,
        task_kind: TaskKind,
    ) -> AssistantTaskContext:
        stored = task_context_from_session_metadata(session_metadata)
        fields_set = set(body.model_fields_set)
        requested_surface = (
            body.current_surface if "current_surface" in fields_set else current_surface
        )
        return merge_task_context(
            stored,
            project_id=project_id,
            project_id_set=True,
            scopes=scopes,
            current_surface=requested_surface,
            task_kind=body.task_kind if "task_kind" in fields_set else task_kind,
            constraints=body.constraints,
            constraints_set="constraints" in fields_set,
            done_when=body.done_when,
            done_when_set="done_when" in fields_set,
            context_refs=body.context_refs,
            context_refs_set="context_refs" in fields_set,
            selected_resources=body.selected_resources,
            selected_resources_set="selected_resources" in fields_set,
            resume_from_plan_id=body.resume_from_plan_id,
            resume_from_plan_id_set="resume_from_plan_id" in fields_set,
        )
