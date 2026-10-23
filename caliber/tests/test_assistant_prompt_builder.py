"""Shared assistant prompt builder tests."""

from __future__ import annotations

from dataclasses import asdict

from caliber.assistant.models import AssistantTurnRequest
from caliber.assistant.prompt_builder import (
    build_assistant_system_prompt,
    parse_assistant_response,
)
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
    assert (
        "File formats such as csv, json, pdf, and markdown are not CALIBER artifact types" in prompt
    )


def test_response_parser_ignores_non_list_structured_fields() -> None:
    result = parse_assistant_response(
        '{"reply":"usable","questions":null,"draft_deltas":{"artifact_type":"csv"}}'
    )

    assert result.reply == "usable"
    assert result.questions == []
    assert result.draft_deltas == []


def test_response_parser_ignores_non_object_draft_entries() -> None:
    result = parse_assistant_response(
        '{"reply":"usable","draft_deltas":[null,"csv",42,["prompt"]]}'
    )

    assert result.reply == "usable"
    assert result.draft_deltas == []


def test_response_parser_fails_closed_for_malformed_reply_and_questions() -> None:
    content = '{"reply":null,"questions":[null,42,"question?"],"done":false}'
    result = parse_assistant_response(content)

    assert result.reply == content
    assert result.questions == []
    assert result.done is False


def test_response_parser_does_not_treat_string_false_as_done() -> None:
    result = parse_assistant_response('{"reply":"continue","done":"false"}')

    assert result.reply == "continue"
    assert result.done is False


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


def test_prompt_playground_runs_the_prompt_instead_of_authoring() -> None:
    prompt = build_assistant_system_prompt(
        AssistantTurnRequest(
            session_id="ASST-test",
            user_message="Analyze this CSV",
            artifact_type="prompt",
            goal="You are a financial analyst. Return one JSON object.",
            prompt_playground=True,
        )
    )

    assert "CALIBER PLATFORM POLICY" in prompt
    assert "PROMPT PLAYGROUND" in prompt
    assert "You are a financial analyst. Return one JSON object." in prompt
    # None of the authoring machinery: that is what made the model "create a prompt" instead of
    # answering with it.
    assert "CALIBER ASSISTANT CORE" not in prompt
    assert "draft_deltas" not in prompt


def test_prompt_authoring_does_not_enter_playground_mode() -> None:
    for kwargs in ({}, {"prompt_playground": True, "goal": ""}):
        prompt = build_assistant_system_prompt(
            AssistantTurnRequest(
                session_id="ASST-test",
                user_message="create a prompt",
                artifact_type="prompt",
                **({"goal": "Draft a support prompt."} | kwargs),
            )
        )
        assert "PROMPT PLAYGROUND" not in prompt
        assert "CALIBER ASSISTANT CORE" in prompt


def test_prompt_playground_is_only_for_prompts() -> None:
    prompt = build_assistant_system_prompt(
        AssistantTurnRequest(
            session_id="ASST-test",
            user_message="try this",
            artifact_type="skill",
            goal="Answer in bullets.",
            prompt_playground=True,
        )
    )

    assert "PROMPT PLAYGROUND" not in prompt


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


def test_system_prompt_tells_the_model_the_draft_delta_shape() -> None:
    """A real model only produces a usable draft if the prompt names the fields CALIBER reads.

    Without this block a hosted model invented ``content``/``instructions`` keys and every
    draft arrived empty (found by driving Aria with a real OpenAI key).
    """
    prompt = build_assistant_system_prompt(AssistantTurnRequest(session_id="s", user_message="hi"))
    assert "DRAFT DELTA FORMAT" in prompt
    for needle in (
        '"artifact"',
        '"template"',
        '"variables"',
        '"source"',
        '"manifest"',
        '"transport"',
    ):
        assert needle in prompt, needle


def test_playground_prompts_do_not_carry_the_draft_format() -> None:
    prompt = build_assistant_system_prompt(
        AssistantTurnRequest(
            session_id="s",
            user_message="hi",
            artifact_type="prompt",
            goal="Answer briefly.",
            prompt_playground=True,
        )
    )
    assert "DRAFT DELTA FORMAT" not in prompt


def test_a_conforming_prompt_draft_envelope_parses_into_a_usable_delta() -> None:
    result = parse_assistant_response(
        '{"reply": "ok", "questions": [], "draft_deltas": [{"artifact_type": "prompt", '
        '"title": "Classifier", "summary": "s", "spec": {}, "artifact": {"name": "classifier", '
        '"template": "Label: {{ticket}}", "variables": ["ticket"]}}], "done": true}'
    )
    assert result.draft_deltas[0].artifact["template"] == "Label: {{ticket}}"
    assert result.draft_deltas[0].artifact["variables"] == ["ticket"]
