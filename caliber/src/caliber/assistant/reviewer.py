"""Independent, fail-closed review agent contract for assistant drafts."""

from __future__ import annotations

import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from caliber.assistant.engine import AssistantEngine
from caliber.assistant.models import AssistantTurnRequest


class DraftReviewRequest(BaseModel):
    """Immutable candidate and evidence presented to the reviewer agent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    draft_id: str
    session_id: str
    artifact_type: str
    candidate_version: int
    candidate_hash: str
    title: str
    summary: str
    spec: dict[str, Any] = Field(default_factory=dict)
    artifact: dict[str, Any] = Field(default_factory=dict)
    validation_report: dict[str, Any]
    test_report: dict[str, Any]
    author_user: str
    reviewer_user: str
    policy_version: str


class DraftReviewDecision(BaseModel):
    """Strict structured result accepted from a reviewer agent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: Literal["approve", "reject"]
    rationale: str = Field(min_length=1, max_length=8_000)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    policy_version: str = Field(min_length=1, max_length=256)
    model: str = Field(default="", max_length=256)


class DraftReviewer(Protocol):
    """A reviewer is deliberately separate from the authoring tool surface."""

    def review(self, request: DraftReviewRequest) -> DraftReviewDecision: ...


class EngineDraftReviewer:
    """Run an isolated reviewer turn through a configured assistant engine.

    The reviewer receives no author conversation history and no mutation tools.
    It must return a strict JSON decision. Invalid or provider-error responses
    raise and are handled by the service as fail-closed review failures.
    """

    def __init__(self, engine: AssistantEngine) -> None:
        self._engine = engine

    def review(self, request: DraftReviewRequest) -> DraftReviewDecision:
        payload = request.model_dump(mode="json")
        prompt = (
            "You are CALIBER's independent release reviewer. Evaluate only the immutable "
            "candidate and gate evidence below. Reject if evidence is insufficient, inconsistent, "
            "unsafe, or policy-noncompliant. Return JSON only with exactly these keys: "
            "decision ('approve' or 'reject'), rationale, confidence (0..1), evidence_ids "
            "(array of strings), policy_version (exactly the supplied policy_version), and model.\n\n"
            f"REVIEW_INPUT={json.dumps(payload, sort_keys=True, separators=(',', ':'))}"
        )
        result = self._engine.run_turn(
            AssistantTurnRequest(
                session_id=f"review:{request.draft_id}",
                user_message=prompt,
                mode="chat",
                approval_mode="manual",
                user=request.reviewer_user,
            ),
            toolset=None,
        )
        if result.error:
            raise RuntimeError(f"reviewer engine failed: {result.error}")
        raw = result.reply.strip()
        if raw.startswith("```"):
            lines = raw.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw = "\n".join(lines).strip()
        try:
            value = json.loads(raw)
            decision = DraftReviewDecision.model_validate(value)
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            raise ValueError("reviewer returned an invalid structured decision") from exc
        if decision.policy_version != request.policy_version:
            raise ValueError("reviewer policy version does not match the requested policy")
        return decision
