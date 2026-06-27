"""Shared assistant prompt builder tests."""

from __future__ import annotations

from dataclasses import asdict

from caliber.assistant.models import AssistantTurnRequest
from caliber.assistant.prompt_builder import build_assistant_system_prompt
from caliber.assistant.skill_runtime import AssistantResolvedSkill
from caliber.assistant.task_context import AssistantTaskContext, TaskContextRef


def _skill_payload() -> dict[str, object]:
    return asdict(
        AssistantResolvedSkill(
            skill_id="SK-workflow-calibration-review",
            name="workflow-calibration-review",
            version=2,
            summary="Review workflow calibration runs.",
            content="Check eval datasets before approving winners.",
            allowed_tools="read-only review tools",
            depends_on=[],
            tags=["assistant-runtime"],
            category="workflow_automation",
            selection_reason="artifact_type:workflow",
            content_included=True,
        )
    )


def test_prompt_includes_selected_skill_after_platform_policy() -> None:
    prompt = build_assistant_system_prompt(
        AssistantTurnRequest(
            session_id="ASST-test",
            user_message="calibrate workflow",
            selected_skills=[_skill_payload()],
            artifact_type="workflow",
        )
    )

    assert prompt.index("CALIBER PLATFORM POLICY") < prompt.index("CALIBER ASSISTANT SKILLS")
    assert "Skill: workflow-calibration-review" in prompt
    assert "Version: 2" in prompt
    assert "Why selected: artifact_type:workflow" in prompt
    assert "Check eval datasets before approving winners." in prompt
    assert (
        '{"reply": "...", "questions": [...], "draft_deltas": [...], "done": false/true}' in prompt
    )


def test_normal_skill_authoring_does_not_enter_playground_mode() -> None:
    prompt = build_assistant_system_prompt(
        AssistantTurnRequest(
            session_id="ASST-test",
            user_message="create a skill",
            artifact_type="skill",
            goal="These are candidate skill instructions.",
        )
    )

    assert "SKILL PLAYGROUND" not in prompt
    assert "CALIBER ASSISTANT CORE" in prompt
    assert "The user's goal: These are candidate skill instructions." in prompt


def test_explicit_skill_playground_mode_still_works() -> None:
    prompt = build_assistant_system_prompt(
        AssistantTurnRequest(
            session_id="ASST-test",
            user_message="try this",
            artifact_type="skill",
            goal="Always answer in short bullets.",
            skill_playground=True,
        )
    )

    assert "CALIBER PLATFORM POLICY" in prompt
    assert "SKILL PLAYGROUND" in prompt
    assert "Always answer in short bullets." in prompt


def test_prompt_includes_task_context_block() -> None:
    prompt = build_assistant_system_prompt(
        AssistantTurnRequest(
            session_id="ASST-test",
            user_message="resume the plan",
            task_context=AssistantTaskContext(
                project_id="PRJ-7",
                scopes=["caliber.operator", "caliber.viewer"],
                constraints={"must_test": True},
                done_when=["all tests pass"],
                current_surface="assistant_drawer",
                task_kind="resume",
                context_refs=[
                    TaskContextRef(ref_type="workflow", ref_id="WF-1", label="Support Flow")
                ],
                selected_resources=[
                    TaskContextRef(
                        ref_type="knowledge_base",
                        ref_id="KB-1",
                        label="Support KB",
                    )
                ],
                resume_from_plan_id="PLAN-42",
            ),
        )
    )

    assert "TASK CONTEXT" in prompt
    assert "Task kind: resume" in prompt
    assert "Current surface: assistant_drawer" in prompt
    assert "Active project: PRJ-7" in prompt
    assert "Resume plan: PLAN-42" in prompt
    assert "Caller scopes: caliber.operator, caliber.viewer" in prompt
    assert "Done when:" in prompt
    assert "- workflow:WF-1 (Support Flow)" in prompt
    assert "- knowledge_base:KB-1 (Support KB)" in prompt
