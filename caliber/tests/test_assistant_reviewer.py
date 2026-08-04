"""Strict parsing and isolation tests for the reviewer-agent adapter."""

from __future__ import annotations

import json

import pytest

from caliber.assistant.models import AssistantTurnRequest, AssistantTurnResult
from caliber.assistant.reviewer import DraftReviewRequest, EngineDraftReviewer


class _Engine:
    def __init__(self, result: AssistantTurnResult) -> None:
        self.result = result
        self.request: AssistantTurnRequest | None = None
        self.toolset: object | None = object()

    def run_turn(
        self,
        request: AssistantTurnRequest,
        *,
        toolset: object | None = None,
    ) -> AssistantTurnResult:
        self.request = request
        self.toolset = toolset
        return self.result


def _request() -> DraftReviewRequest:
    return DraftReviewRequest(
        draft_id="ADRF-1",
        session_id="ASST-1",
        artifact_type="tool",
        candidate_version=2,
        candidate_hash="a" * 64,
        title="Tool",
        summary="Summary",
        spec={},
        artifact={"name": "tool"},
        validation_report={"valid": True},
        test_report={"passed": True},
        author_user="@author",
        reviewer_user="@reviewer",
        policy_version="policy-v1",
    )


def test_engine_reviewer_uses_isolated_chat_turn_without_tools() -> None:
    response = {
        "decision": "approve",
        "rationale": "All supplied gates passed.",
        "confidence": 0.95,
        "evidence_ids": ["validation_report", "test_report"],
        "policy_version": "policy-v1",
        "model": "review-model",
    }
    engine = _Engine(AssistantTurnResult(reply=json.dumps(response)))
    decision = EngineDraftReviewer(engine).review(_request())  # type: ignore[arg-type]
    assert decision.decision == "approve"
    assert engine.toolset is None
    assert engine.request is not None
    assert engine.request.history == []
    assert engine.request.mode == "chat"
    assert engine.request.user == "@reviewer"
    assert "candidate_hash" in engine.request.user_message


@pytest.mark.parametrize(
    "result",
    [
        AssistantTurnResult(reply="not-json"),
        AssistantTurnResult(reply='{"decision":"approve"}'),
        AssistantTurnResult(reply="{}", error="provider unavailable"),
        AssistantTurnResult(
            reply=json.dumps(
                {
                    "decision": "approve",
                    "rationale": "ok",
                    "confidence": 0.9,
                    "evidence_ids": [],
                    "policy_version": "wrong-policy",
                    "model": "review-model",
                }
            )
        ),
    ],
)
def test_engine_reviewer_rejects_invalid_or_failed_results(result: AssistantTurnResult) -> None:
    with pytest.raises((RuntimeError, ValueError)):
        EngineDraftReviewer(_Engine(result)).review(_request())  # type: ignore[arg-type]


def test_engine_reviewer_accepts_fenced_json() -> None:
    raw = """```json
{"decision":"reject","rationale":"unsafe","confidence":0.99,"evidence_ids":[],"policy_version":"policy-v1","model":"review-model"}
```"""
    decision = EngineDraftReviewer(_Engine(AssistantTurnResult(reply=raw))).review(  # type: ignore[arg-type]
        _request()
    )
    assert decision.decision == "reject"
