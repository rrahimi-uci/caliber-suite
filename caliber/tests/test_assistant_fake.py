"""Tests for caliber.assistant.fake — the FakeAssistantEngine."""

from __future__ import annotations

import pytest

from caliber.assistant.fake import FakeAssistantEngine
from caliber.assistant.models import AssistantTurnRequest


class TestFakeAssistantEngine:
    def setup_method(self):
        self.engine = FakeAssistantEngine()
        self.session_id = "ASST-test0001"

    def _request(self, *, artifact_type: str = "tool", message: str = "hi") -> AssistantTurnRequest:
        return AssistantTurnRequest(
            session_id=self.session_id,
            user_message=message,
            artifact_type=artifact_type,  # type: ignore[arg-type]
        )

    def test_first_turn_asks_questions(self):
        result = self.engine.run_turn(self._request())
        assert result.reply
        assert len(result.questions) > 0
        assert result.questions[0].question
        assert not result.done
        assert len(result.draft_deltas) == 0

    def test_second_turn_produces_draft(self):
        self.engine.run_turn(self._request())
        result = self.engine.run_turn(self._request(message="Call it greet"))
        assert len(result.draft_deltas) == 1
        delta = result.draft_deltas[0]
        assert delta.artifact_type == "tool"
        assert delta.artifact

    def test_third_turn_is_done(self):
        self.engine.run_turn(self._request())
        self.engine.run_turn(self._request())
        result = self.engine.run_turn(self._request())
        assert result.done

    def test_different_sessions_independent(self):
        r1 = self.engine.run_turn(self._request())
        other = AssistantTurnRequest(
            session_id="ASST-other001",
            user_message="hello",
            artifact_type="skill",
        )
        r2 = self.engine.run_turn(other)
        # Both should be first-turn (questions).
        assert len(r1.questions) > 0
        assert len(r2.questions) > 0

    def test_greeting_without_intent_is_conversational(self):
        # No artifact_type and no build keywords → converse, never author.
        req = AssistantTurnRequest(session_id="ASST-greet01", user_message="hello")
        result = self.engine.run_turn(req)
        assert result.reply
        assert "Aria" in result.reply
        assert len(result.questions) == 0
        assert len(result.draft_deltas) == 0
        assert not result.done

    def test_build_intent_starts_authoring(self):
        # An explicit build request enters the authoring flow (asks a question).
        req = AssistantTurnRequest(
            session_id="ASST-bi01",
            user_message="create a tool that validates email addresses",
        )
        result = self.engine.run_turn(req)
        assert len(result.questions) > 0
        assert len(result.draft_deltas) == 0

    def test_greeting_then_build_request(self):
        # Greeting first stays conversational; a follow-up build request authors.
        sid = "ASST-flow01"
        greet = self.engine.run_turn(AssistantTurnRequest(session_id=sid, user_message="hi there"))
        assert len(greet.questions) == 0 and len(greet.draft_deltas) == 0
        build = self.engine.run_turn(
            AssistantTurnRequest(session_id=sid, user_message="build me a skill")
        )
        assert len(build.questions) > 0

    @pytest.mark.parametrize("artifact_type", ["tool", "skill", "prompt", "workflow", "mcp_server"])
    def test_all_artifact_types_produce_draft(self, artifact_type: str):
        self.engine.run_turn(self._request(artifact_type=artifact_type))
        result = self.engine.run_turn(self._request(artifact_type=artifact_type))
        assert len(result.draft_deltas) == 1
        assert result.draft_deltas[0].artifact
