"""Coverage for the shared assistant prompt builder branches.

Targets the recently added attachment aggregate-budget logic, the drafts
compaction summary, the steer / mode / task-context branch arms, and the
malformed-skill-payload guard.
"""

from __future__ import annotations

from caliber.assistant.models import AssistantTurnRequest
from caliber.assistant.prompt_builder import (
    _ATTACHMENT_BLOCK_BUDGET,
    _DRAFTS_SUMMARY_MAX,
    _drafts_summary,
    build_assistant_system_prompt,
)
from caliber.assistant.task_context import AssistantTaskContext, TaskContextRef


def _request(**kwargs: object) -> AssistantTurnRequest:
    base: dict[str, object] = {"session_id": "ASST-cov", "user_message": "hello"}
    base.update(kwargs)
    return AssistantTurnRequest(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# steer course-correction block (line 54-63)
# ---------------------------------------------------------------------------


def test_steer_adds_course_correction_block() -> None:
    prompt = build_assistant_system_prompt(_request(steer=True))
    assert "COURSE CORRECTION" in prompt
    assert "re-prioritize around it" in prompt


def test_no_steer_omits_course_correction_block() -> None:
    prompt = build_assistant_system_prompt(_request(steer=False))
    assert "COURSE CORRECTION" not in prompt


# ---------------------------------------------------------------------------
# per-mode guidance branches (lines 137-164)
# ---------------------------------------------------------------------------


def test_chat_mode_guidance_present() -> None:
    prompt = build_assistant_system_prompt(_request(mode="chat"))
    assert "INTERACTION MODE: CHAT" in prompt
    assert "INTERACTION MODE: PLAN" not in prompt
    assert "INTERACTION MODE: BUILD" not in prompt


def test_plan_mode_guidance_present() -> None:
    prompt = build_assistant_system_prompt(_request(mode="plan"))
    assert "INTERACTION MODE: PLAN" in prompt
    assert "do NOT emit draft_deltas" in prompt


def test_build_mode_guidance_present() -> None:
    prompt = build_assistant_system_prompt(_request(mode="build"))
    assert "INTERACTION MODE: BUILD" in prompt


# ---------------------------------------------------------------------------
# attachment block: budget exhaustion + omitted note + truncation (171-196)
# ---------------------------------------------------------------------------


def test_attachment_block_small_attachments_render_fully() -> None:
    prompt = build_assistant_system_prompt(
        _request(
            attachments=[
                {
                    "name": "spec.md",
                    "kind": "object_file",
                    "ref_type": "prompt",
                    "content_text": "alpha content",
                },
                {
                    "name": "notes",
                    "kind": "text_snippet",
                    "content_text": "beta content",
                },
            ]
        )
    )
    assert "ATTACHED CONTEXT" in prompt
    # ref_type present -> "kind/ref_type" label.
    assert "--- [1] spec.md (object_file/prompt) ---" in prompt
    # no ref_type -> bare kind label.
    assert "--- [2] notes (text_snippet) ---" in prompt
    assert "alpha content" in prompt
    assert "beta content" in prompt
    assert "omitted to fit the context budget" not in prompt


def test_attachment_block_truncates_when_clip_shorter_than_text() -> None:
    # One attachment slightly larger than the whole budget: it is clipped so
    # the rendered slice is shorter than the source text -> "(truncated)".
    big = "x" * (_ATTACHMENT_BLOCK_BUDGET + 500)
    prompt = build_assistant_system_prompt(
        _request(
            attachments=[
                {"name": "huge.txt", "kind": "upload", "content_text": big},
            ]
        )
    )
    header = "--- [1] huge.txt (upload) (truncated) ---"
    assert header in prompt
    # The injected slice is clipped to exactly the aggregate budget: the run of
    # "x" immediately after the header is _ATTACHMENT_BLOCK_BUDGET long.
    clipped = prompt.split(header + "\n", 1)[1]
    run = len(clipped) - len(clipped.lstrip("x"))
    assert run == _ATTACHMENT_BLOCK_BUDGET


def test_attachment_block_explicit_truncated_flag_marks_header() -> None:
    prompt = build_assistant_system_prompt(
        _request(
            attachments=[
                {
                    "name": "clip.txt",
                    "kind": "upload",
                    "content_text": "short",
                    "truncated": True,
                },
            ]
        )
    )
    assert "--- [1] clip.txt (upload) (truncated) ---" in prompt


def test_attachment_block_budget_exhausted_emits_omitted_note() -> None:
    # First attachment consumes the entire budget; the remaining two are
    # counted (omitted) rather than concatenated.
    first = "y" * _ATTACHMENT_BLOCK_BUDGET
    prompt = build_assistant_system_prompt(
        _request(
            attachments=[
                {"name": "first.txt", "kind": "upload", "content_text": first},
                {"name": "second.txt", "kind": "upload", "content_text": "later"},
                {"name": "third.txt", "kind": "upload", "content_text": "later2"},
            ]
        )
    )
    assert "--- [1] first.txt (upload) ---" in prompt
    # Budget gone before rendering the rest.
    assert "second.txt" not in prompt
    assert "third.txt" not in prompt
    assert "... and 2 more attachment(s) omitted to fit the context budget." in prompt


def test_attachment_block_default_name_when_missing() -> None:
    prompt = build_assistant_system_prompt(
        _request(attachments=[{"kind": "upload", "content_text": "data"}])
    )
    assert "--- [1] attachment 1 (upload) ---" in prompt


def test_no_attachments_omits_block() -> None:
    prompt = build_assistant_system_prompt(_request())
    assert "ATTACHED CONTEXT" not in prompt


# ---------------------------------------------------------------------------
# drafts compaction summary (lines 78-79, 99-107)
# ---------------------------------------------------------------------------


def test_drafts_summary_projects_only_identifying_fields() -> None:
    prompt = build_assistant_system_prompt(
        _request(
            drafts=[
                {
                    "draft_id": "D-1",
                    "artifact_type": "tool",
                    "status": "draft",
                    "title": "My Tool",
                    "name": "my_tool",
                    # noise that must be dropped from the compact summary.
                    "spec": {"big": "x" * 5000},
                    "artifact": {"more": "noise"},
                },
            ]
        )
    )
    assert "Current drafts:" in prompt
    assert '"draft_id": "D-1"' in prompt
    assert '"artifact_type": "tool"' in prompt
    assert '"title": "My Tool"' in prompt
    assert '"name": "my_tool"' in prompt
    # Non-identifying fields must not leak into the summary.
    assert "big" not in prompt
    assert "noise" not in prompt
    assert "more)" not in prompt


def test_drafts_summary_caps_and_counts_overflow() -> None:
    drafts = [
        {"draft_id": f"D-{i}", "artifact_type": "prompt", "status": "draft"}
        for i in range(_DRAFTS_SUMMARY_MAX + 5)
    ]
    prompt = build_assistant_system_prompt(_request(drafts=drafts))
    assert "Current drafts:" in prompt
    # Only the first _DRAFTS_SUMMARY_MAX are enumerated; the tail is summarized.
    assert "(+5 more)" in prompt
    assert '"draft_id": "D-0"' in prompt
    assert f'"draft_id": "D-{_DRAFTS_SUMMARY_MAX - 1}"' in prompt
    # The 21st draft (index 20) is beyond the cap and must not be enumerated.
    assert f'"draft_id": "D-{_DRAFTS_SUMMARY_MAX}"' not in prompt


def test_drafts_summary_skips_non_dict_entries() -> None:
    # The pydantic model coerces ``drafts`` to ``list[dict]``, so the defensive
    # isinstance guard is only reachable by calling the helper directly with a
    # mixed list (e.g. a malformed persisted draft row).
    summary = _drafts_summary(["not-a-dict", {"draft_id": "D-real", "status": "validated"}])  # type: ignore[list-item]
    assert '"draft_id": "D-real"' in summary
    assert "not-a-dict" not in summary


def test_no_drafts_omits_drafts_line() -> None:
    prompt = build_assistant_system_prompt(_request())
    assert "Current drafts:" not in prompt


# ---------------------------------------------------------------------------
# task-context branch arms (lines 207-228)
# ---------------------------------------------------------------------------


def test_task_context_constraints_and_refs_branches() -> None:
    prompt = build_assistant_system_prompt(
        _request(
            task_context=AssistantTaskContext(
                task_kind="build",
                current_surface="workflows",
                project_id="PRJ-9",
                resume_from_plan_id="PLAN-3",
                scopes=["caliber.operator"],
                done_when=["all green", "docs updated"],
                constraints={"must_test": True, "max_cost": 5},
                context_refs=[
                    TaskContextRef(
                        ref_type="tool",
                        ref_id="T-1",
                        label="My Tool",
                        metadata_={"version": 2},
                    ),
                ],
                selected_resources=[
                    TaskContextRef(ref_type="prompt", ref_id="P-1"),
                ],
            )
        )
    )
    assert "TASK CONTEXT" in prompt
    assert "Task kind: build" in prompt
    assert "Current surface: workflows" in prompt
    assert "Active project: PRJ-9" in prompt
    assert "Resume plan: PLAN-3" in prompt
    assert "Caller scopes: caliber.operator" in prompt
    assert "Done when:" in prompt
    assert "- all green" in prompt
    assert "- docs updated" in prompt
    # constraints rendered as JSON.
    assert '"must_test": true' in prompt
    assert '"max_cost": 5' in prompt
    # context_refs: label + metadata suffix.
    assert "Context refs:" in prompt
    assert '- tool:T-1 (My Tool) metadata={"version": 2}' in prompt
    # selected_resources: no label, no metadata.
    assert "Selected resources:" in prompt
    assert "- prompt:P-1" in prompt


def test_task_context_single_arm_only_renders_that_arm() -> None:
    # Exercise the block when only one optional field is set.
    prompt = build_assistant_system_prompt(
        _request(task_context=AssistantTaskContext(task_kind="answer"))
    )
    assert "TASK CONTEXT" in prompt
    assert "Task kind: answer" in prompt
    assert "Done when:" not in prompt
    assert "Constraints:" not in prompt
    assert "Context refs:" not in prompt
    assert "Selected resources:" not in prompt
    assert "Active project:" not in prompt


def test_empty_task_context_omits_block() -> None:
    prompt = build_assistant_system_prompt(_request(task_context=AssistantTaskContext()))
    assert "TASK CONTEXT" not in prompt


# ---------------------------------------------------------------------------
# malformed skill payload guard (lines 253-271)
# ---------------------------------------------------------------------------


def test_malformed_skill_payload_is_dropped() -> None:
    # Missing required "skill_id"/"name" -> KeyError -> None -> no skill block.
    prompt = build_assistant_system_prompt(
        _request(selected_skills=[{"summary": "broken, no skill_id"}])
    )
    assert "CALIBER ASSISTANT SKILLS" not in prompt
    assert "broken, no skill_id" not in prompt


def test_skill_payload_bad_version_type_is_dropped() -> None:
    # version that cannot be coerced to int -> ValueError -> None.
    prompt = build_assistant_system_prompt(
        _request(
            selected_skills=[
                {"skill_id": "SK-x", "name": "x", "version": "not-an-int"},
            ]
        )
    )
    assert "CALIBER ASSISTANT SKILLS" not in prompt


def test_valid_skill_payload_renders_block() -> None:
    prompt = build_assistant_system_prompt(
        _request(
            selected_skills=[
                {
                    "skill_id": "SK-ok",
                    "name": "ok-skill",
                    "version": 3,
                    "summary": "does things",
                    "content": "instructions here",
                    "category": "custom",
                    "selection_reason": "manual",
                    "content_included": True,
                },
            ]
        )
    )
    assert "CALIBER ASSISTANT SKILLS" in prompt
    assert "Skill: ok-skill" in prompt
