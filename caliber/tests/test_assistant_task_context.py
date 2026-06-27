"""Unit tests for Aria's shared task-context helpers."""

from __future__ import annotations

from caliber.assistant.context_builder import AssistantContextBuilder
from caliber.assistant.models import MessageSendRequest
from caliber.assistant.task_context import (
    AssistantTaskContext,
    TaskContextRef,
    task_context_from_session_metadata,
    update_session_task_context_metadata,
)
from caliber.assistant.task_manager import TaskManager


def test_task_context_from_session_metadata_falls_back_cleanly() -> None:
    assert task_context_from_session_metadata(None) == AssistantTaskContext()
    assert (
        task_context_from_session_metadata({"assistant_task_context": "bad"})
        == AssistantTaskContext()
    )
    assert (
        task_context_from_session_metadata(
            {"assistant_task_context": {"project_id": ["not-a-string"]}}
        )
        == AssistantTaskContext()
    )


def test_update_session_task_context_metadata_omits_ephemeral_fields() -> None:
    metadata = update_session_task_context_metadata(
        {"other": "value"},
        AssistantTaskContext(
            project_id="PRJ-1",
            scopes=["caliber.operator"],
            task_kind="build",
            done_when=["validated"],
        ),
    )

    stored = metadata["assistant_task_context"]
    assert stored["project_id"] == "PRJ-1"
    assert stored["done_when"] == ["validated"]
    assert "scopes" not in stored
    assert "task_kind" not in stored


def test_task_manager_prefers_resume_over_mode_default() -> None:
    decision = TaskManager().choose(mode="build", resume_from_plan_id="PLAN-9")
    assert decision.task_kind == "resume"
    assert TaskManager().choose(mode="plan").task_kind == "plan"
    assert TaskManager().choose(mode="chat").task_kind == "answer"


def test_context_builder_overlays_request_fields_and_clears_project() -> None:
    builder = AssistantContextBuilder()
    stored_metadata = update_session_task_context_metadata(
        {},
        AssistantTaskContext(
            project_id="PRJ-OLD",
            done_when=["old target"],
            current_surface="old_surface",
        ),
    )
    body = MessageSendRequest(
        content="resume this",
        constraints={"must_test": True},
        done_when=["all checks green"],
        context_refs=[
            {
                "ref_type": "workflow",
                "ref_id": "WF-1",
                "label": "Support Flow",
            }
        ],
        selected_resources=[
            {
                "ref_type": "knowledge_base",
                "ref_id": "KB-1",
                "label": "Support KB",
            }
        ],
        resume_from_plan_id="PLAN-9",
    )

    task_context = builder.build_turn_task_context(
        session_metadata=stored_metadata,
        body=body,
        project_id=None,
        scopes=["caliber.viewer", "caliber.operator"],
        current_surface="assistant_drawer",
        task_kind="resume",
    )

    assert task_context.project_id is None
    assert task_context.scopes == ["caliber.operator", "caliber.viewer"]
    assert task_context.current_surface == "assistant_drawer"
    assert task_context.task_kind == "resume"
    assert task_context.constraints == {"must_test": True}
    assert task_context.done_when == ["all checks green"]
    assert task_context.context_refs == [
        TaskContextRef(ref_type="workflow", ref_id="WF-1", label="Support Flow")
    ]
    assert task_context.selected_resources == [
        TaskContextRef(ref_type="knowledge_base", ref_id="KB-1", label="Support KB")
    ]
    assert task_context.resume_from_plan_id == "PLAN-9"
