"""Tests for caliber.assistant.models — Pydantic request/response schemas."""

from __future__ import annotations

import pytest

from caliber.assistant.models import (
    ARTIFACT_TYPES,
    AssistantTurnResult,
    ClarifyingQuestion,
    DraftDelta,
    DraftUpdateRequest,
    MessageSendRequest,
    SessionCreateRequest,
    ValidationReport,
)
from caliber.assistant.models import (
    TestReport as _TestReport,
)


class TestArtifactTypes:
    def test_all_types_present(self):
        assert {"tool", "skill", "prompt", "workflow", "mcp_server"} == ARTIFACT_TYPES


class TestSessionCreateRequest:
    def test_defaults(self):
        req = SessionCreateRequest()
        assert req.title == ""
        assert req.goal == ""
        assert req.artifact_type is None

    def test_with_values(self):
        req = SessionCreateRequest(title="My session", goal="Build tool", artifact_type="tool")
        assert req.title == "My session"
        assert req.artifact_type == "tool"

    def test_rejects_extra_fields(self):
        with pytest.raises(Exception):
            SessionCreateRequest(title="x", unknown_field="bad")  # type: ignore[call-arg]

    def test_skill_runtime_fields(self):
        req = SessionCreateRequest(
            skill_mode="manual",
            pinned_skill_names=["workflow-calibration-review"],
        )
        assert req.skill_mode == "manual"
        assert req.pinned_skill_names == ["workflow-calibration-review"]

    def test_invalid_skill_runtime_mode_rejected(self):
        with pytest.raises(Exception):
            SessionCreateRequest(skill_mode="sometimes")  # type: ignore[arg-type]


class TestMessageSendRequest:
    def test_requires_content(self):
        with pytest.raises(Exception):
            MessageSendRequest(content="")  # min_length=1

    def test_valid(self):
        req = MessageSendRequest(content="hello")
        assert req.content == "hello"

    def test_skill_runtime_turn_fields(self):
        req = MessageSendRequest(
            content="hello",
            skill_mode="off",
            skill_names=["backend-route-safety-review"],
        )
        assert req.skill_mode == "off"
        assert req.skill_names == ["backend-route-safety-review"]


class TestDraftUpdateRequest:
    def test_requires_version(self):
        with pytest.raises(Exception):
            DraftUpdateRequest()  # type: ignore[call-arg]

    def test_valid(self):
        req = DraftUpdateRequest(version=1)
        assert req.version == 1


class TestAssistantTurnResult:
    def test_defaults(self):
        result = AssistantTurnResult()
        assert result.reply == ""
        assert result.questions == []
        assert result.draft_deltas == []
        assert not result.done
        assert result.error is None

    def test_with_questions(self):
        result = AssistantTurnResult(
            reply="Here's a question.",
            questions=[ClarifyingQuestion(question="What name?")],
        )
        assert len(result.questions) == 1

    def test_with_draft_deltas(self):
        result = AssistantTurnResult(
            draft_deltas=[DraftDelta(artifact_type="tool", title="my_tool")],
        )
        assert len(result.draft_deltas) == 1
        assert result.draft_deltas[0].title == "my_tool"


class TestValidationReport:
    def test_defaults(self):
        r = ValidationReport()
        assert not r.valid
        assert r.errors == []

    def test_valid_report(self):
        r = ValidationReport(valid=True)
        assert r.valid


class TestTestReportSchema:
    def test_defaults(self):
        r = _TestReport()
        assert not r.passed
        assert r.total == 0
