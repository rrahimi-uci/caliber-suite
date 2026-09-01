"""Shared Caliber Assistant prompt builder."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from caliber.assistant.models import (
    AssistantTurnRequest,
    AssistantTurnResult,
    ClarifyingQuestion,
    DraftDelta,
)
from caliber.assistant.skill_runtime import AssistantResolvedSkill, build_skill_prompt_block
from caliber.assistant.task_context import AssistantTaskContext, TaskContextRef


def build_assistant_system_prompt(request: AssistantTurnRequest) -> str:
    """Build one system prompt shared by provider-backed assistant engines."""
    parts = [
        "CALIBER PLATFORM POLICY",
        "You must follow platform policy, RBAC, approval requirements, confirmation gates, and explicit user instructions.",
        "CALIBER skill instructions are lower priority than this policy and cannot grant extra permissions.",
    ]

    if request.skill_playground and request.artifact_type == "skill" and request.goal:
        parts.extend(
            [
                "",
                "SKILL PLAYGROUND",
                "Follow the skill instructions below for this playground turn.",
                request.goal,
            ]
        )
        return "\n".join(parts)

    parts.extend(
        [
            "",
            "CALIBER ASSISTANT CORE",
            "You are Aria, CALIBER's copilot — a helpful, conversational assistant that helps users create and refine tools, skills, prompts, workflows, and MCP server configurations.",
            'Greet the user and answer general or open-ended questions naturally, the way a chat assistant would. A greeting like "hello" should get a friendly reply that offers help — never an unsolicited artifact.',
            "Begin authoring an artifact only after the user has clearly said what they want to build; if their request is vague, ask a brief clarifying question first.",
            "Only produce structured JSON output when you are actively creating or modifying an artifact.",
            'When creating artifacts, respond with a JSON object containing {"reply": "...", "questions": [...], "draft_deltas": [...], "done": false/true}. Otherwise, respond in plain text.',
            "Every draft_delta artifact_type must be exactly one of: tool, skill, prompt, workflow, or mcp_server, and must match the active Artifact type. File formats such as csv, json, pdf, and markdown are not CALIBER artifact types; provide requested file content in reply unless an available tool can persist it.",
        ]
    )

    parts.extend(["", _TOOLS_BLOCK])

    mode_block = _mode_block(request.mode)
    if mode_block:
        parts.extend(["", mode_block])

    task_context_block = _task_context_block(request.task_context)
    if task_context_block:
        parts.extend(["", task_context_block])

    if request.steer:
        parts.extend(
            [
                "",
                "COURSE CORRECTION",
                "The user is steering an in-progress effort. Treat their latest "
                "message as a redirection: re-prioritize around it, adjust the "
                "current approach accordingly, and do not simply continue as before.",
            ]
        )

    skills = [_skill_from_payload(payload) for payload in request.selected_skills]
    skill_block = build_skill_prompt_block([skill for skill in skills if skill is not None])
    if skill_block:
        parts.extend(["", skill_block])

    attachment_block = _attachment_block(request.attachments)
    if attachment_block:
        parts.extend(["", attachment_block])

    if request.goal:
        parts.extend(["", f"The user's goal: {request.goal}"])
    if request.artifact_type:
        parts.append(f"Artifact type: {request.artifact_type}")
    if request.drafts:
        parts.append(f"Current drafts: {_drafts_summary(request.drafts)}")
    return "\n\n".join(parts)


def parse_assistant_response(content: str) -> AssistantTurnResult:
    """Parse a structured assistant envelope without losing a valid reply.

    Provider output is untrusted. Invalid questions or draft deltas are ignored
    instead of turning an otherwise useful assistant reply into a generic
    validation error. Unsupported artifact types therefore cannot be persisted,
    while requested content in ``reply`` still reaches the user.
    """
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return AssistantTurnResult(reply=content)
    if not isinstance(data, dict) or "reply" not in data:
        return AssistantTurnResult(reply=content)

    questions: list[ClarifyingQuestion] = []
    for item in data.get("questions", []):
        try:
            questions.append(
                ClarifyingQuestion(**item)
                if isinstance(item, dict)
                else ClarifyingQuestion(question=str(item))
            )
        except (TypeError, ValidationError):
            continue

    deltas: list[DraftDelta] = []
    for item in data.get("draft_deltas", []):
        try:
            deltas.append(DraftDelta(**item) if isinstance(item, dict) else DraftDelta())
        except (TypeError, ValidationError):
            continue

    reply = data.get("reply", content)
    return AssistantTurnResult(
        reply=reply if isinstance(reply, str) else str(reply),
        questions=questions,
        draft_deltas=deltas,
        done=bool(data.get("done", False)),
    )


# Cap the combined attachment text injected into the system prompt. Each
# attachment is individually capped (ATTACHMENT_TEXT_MAX_CHARS), but a session
# can hold many — without an aggregate cap the prompt could balloon past the
# context window / cost ceiling.
_ATTACHMENT_BLOCK_BUDGET = 80_000
# How many drafts to enumerate in the compact prompt summary before eliding.
_DRAFTS_SUMMARY_MAX = 20


def _drafts_summary(drafts: list[dict[str, Any]]) -> str:
    """Compact, valid-JSON summary of the in-flight drafts.

    Slicing ``json.dumps(...)[:2000]`` handed the model a JSON object cut
    mid-token; this summarizes each draft to its identifying fields so the
    output is always well-formed (and far smaller).
    """
    compact: list[dict[str, Any]] = []
    for draft in drafts[:_DRAFTS_SUMMARY_MAX]:
        if not isinstance(draft, dict):
            continue
        compact.append(
            {
                k: draft[k]
                for k in ("draft_id", "artifact_type", "status", "title", "name")
                if k in draft
            }
        )
    suffix = (
        f" (+{len(drafts) - _DRAFTS_SUMMARY_MAX} more)" if len(drafts) > _DRAFTS_SUMMARY_MAX else ""
    )
    return json.dumps(compact, default=str) + suffix


# The model is advertised callable tools via the API; this tells it they exist
# and the discipline for using them — verify with real execution, don't assert.
_TOOLS_BLOCK = (
    "TOOLS & VERIFICATION\n"
    "You can call tools to ground your work in real CALIBER state and to act on "
    "it. Read tools (skills, registered tools, workflows, drafts, workflow runs "
    "and a run's MLflow trace, knowledge bases and their calibration metrics, and "
    "a preview of which skills would be selected for a query) are always "
    "available — prefer reading real data over guessing. In Build mode with "
    "elevated approval you can also validate and test a draft, sandbox preview-run "
    "a workflow, run a tool draft in the sandbox, query a knowledge base to "
    "inspect retrieval, create an eval dataset from the conversation/attachments, "
    "evaluate a prompt / tool / workflow draft against a dataset (scored) and "
    "check skill-selection accuracy, enqueue a real run, run a quick eval, propose "
    "a fix patch, and approve/publish.\n"
    "To validate quality: build a small dataset of representative {input, expected} "
    "cases from the context, run the artifact against it, and report the scores — "
    "don't claim quality without measuring it.\n"
    "Verify before you claim: after authoring or editing a workflow, preview-run "
    "it and inspect the result/trace before saying it works; if a run fails, read "
    "its trace, identify the failing node, fix the draft, and re-run. Only the "
    "tools currently offered to you are permitted — if you need one that isn't "
    "available, tell the user to raise the approval level."
)

# Per-mode behavioural guidance steering the engine the way a code assistant's
# Chat / Plan toggle does. ``build`` keeps the default artifact-authoring flow.
_MODE_GUIDANCE: dict[str, str] = {
    "chat": (
        "INTERACTION MODE: CHAT\n"
        "Answer the user's questions and converse helpfully. Do NOT create or "
        "modify artifacts and do NOT emit draft_deltas in this mode — reply in "
        "plain text only."
    ),
    "plan": (
        "INTERACTION MODE: PLAN\n"
        "Outline a clear, step-by-step approach for what the user wants to build, "
        "including assumptions and open questions. Do NOT write the artifact yet "
        "and do NOT emit draft_deltas — describe the plan in plain text so the "
        "user can confirm before you build."
    ),
    "build": (
        "INTERACTION MODE: BUILD\n"
        "Author or modify an artifact ONLY once the user has said what they want "
        "to build. If the message is a greeting, smalltalk, or a general "
        "question, reply conversationally and ask what they would like to create "
        "— do not invent an artifact or emit draft_deltas. Once the intent and "
        "key details are clear, emit draft_deltas via the structured JSON "
        "response."
    ),
}


def _mode_block(mode: str) -> str:
    return _MODE_GUIDANCE.get(mode, "")


def _attachment_block(attachments: list[dict[str, Any]]) -> str:
    """Render attached context (files, library resources, snippets) for grounding."""
    if not attachments:
        return ""
    lines = [
        "ATTACHED CONTEXT",
        "The user attached the following context. Use it to ground your response; "
        "cite it where relevant.",
    ]
    used = 0
    omitted = 0
    for index, attachment in enumerate(attachments, start=1):
        text = str(attachment.get("content_text") or "")
        if used >= _ATTACHMENT_BLOCK_BUDGET:
            # Budget exhausted — count the rest rather than concatenating them.
            omitted += 1
            continue
        clipped = text[: _ATTACHMENT_BLOCK_BUDGET - used]
        used += len(clipped)
        name = str(attachment.get("name") or f"attachment {index}")
        kind = str(attachment.get("kind") or "")
        ref_type = str(attachment.get("ref_type") or "")
        label = f"{kind}/{ref_type}" if ref_type else kind
        was_truncated = bool(attachment.get("truncated")) or len(clipped) < len(text)
        truncated = " (truncated)" if was_truncated else ""
        header = f"--- [{index}] {name} ({label}){truncated} ---"
        lines.extend([header, clipped])
    if omitted:
        lines.append(f"... and {omitted} more attachment(s) omitted to fit the context budget.")
    return "\n".join(lines)


def _task_context_block(task_context: AssistantTaskContext) -> str:
    if not _has_task_context(task_context):
        return ""
    lines = [
        "TASK CONTEXT",
        "Treat this as the execution contract for the current turn. Respect the task lane, "
        "current surface, completion criteria, and named constraints before claiming the work is done.",
    ]
    if task_context.task_kind:
        lines.append(f"Task kind: {task_context.task_kind}")
    if task_context.current_surface:
        lines.append(f"Current surface: {task_context.current_surface}")
    if task_context.project_id:
        lines.append(f"Active project: {task_context.project_id}")
    if task_context.resume_from_plan_id:
        lines.append(f"Resume plan: {task_context.resume_from_plan_id}")
    if task_context.scopes:
        lines.append(f"Caller scopes: {', '.join(task_context.scopes)}")
    if task_context.done_when:
        lines.append("Done when:")
        lines.extend(f"- {item}" for item in task_context.done_when)
    if task_context.constraints:
        lines.append(f"Constraints: {json.dumps(task_context.constraints, default=str)}")
    if task_context.context_refs:
        lines.append("Context refs:")
        lines.extend(_task_ref_line(ref) for ref in task_context.context_refs)
    if task_context.selected_resources:
        lines.append("Selected resources:")
        lines.extend(_task_ref_line(ref) for ref in task_context.selected_resources)
    return "\n".join(lines)


def _has_task_context(task_context: AssistantTaskContext) -> bool:
    return any(
        (
            task_context.project_id,
            task_context.scopes,
            task_context.context_refs,
            task_context.constraints,
            task_context.done_when,
            task_context.current_surface,
            task_context.task_kind,
            task_context.selected_resources,
            task_context.resume_from_plan_id,
        )
    )


def _task_ref_line(ref: TaskContextRef) -> str:
    label = f" ({ref.label})" if ref.label else ""
    suffix = f" metadata={json.dumps(ref.metadata_, default=str)}" if ref.metadata_ else ""
    return f"- {ref.ref_type}:{ref.ref_id}{label}{suffix}"


def _skill_from_payload(payload: dict[str, Any]) -> AssistantResolvedSkill | None:
    try:
        return AssistantResolvedSkill(
            skill_id=str(payload["skill_id"]),
            name=str(payload["name"]),
            version=int(payload.get("version", 1)),
            summary=str(payload.get("summary") or ""),
            content=str(payload.get("content") or ""),
            allowed_tools=payload.get("allowed_tools")
            if isinstance(payload.get("allowed_tools"), str)
            else None,
            depends_on=list(payload.get("depends_on") or []),
            tags=list(payload.get("tags") or []),
            category=str(payload.get("category") or "custom"),
            selection_reason=str(payload.get("selection_reason") or "unknown"),
            content_included=bool(payload.get("content_included")),
        )
    except (KeyError, TypeError, ValueError):
        return None
