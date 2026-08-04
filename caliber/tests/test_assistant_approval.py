"""Tests for Aria's approval mode — how far Aria auto-advances a draft.

The fake author engine produces a draft on its second turn, so each test sends
two messages. Reviewer-agent decisions use a separate deterministic test double.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.assistant.fake import FakeAssistantEngine
from caliber.assistant.models import (
    AssistantTurnResult,
    DraftDelta,
    DraftUpdateRequest,
    MessageSendRequest,
    SessionCreateRequest,
)
from caliber.assistant.reviewer import DraftReviewDecision, DraftReviewRequest
from caliber.assistant.service import AssistantRuntimeSettings, AssistantService
from caliber.config import CaliberConfig
from caliber.db.models import CaliberAssistantPublishEvent, CaliberAssistantReview

USER = "@test"
PREFIX = "/ajax-api/2.0/mlflow/caliber/assistant"


class _Reviewer:
    def __init__(self, decision: str = "approve", confidence: float = 0.99) -> None:
        self.decision = decision
        self.confidence = confidence
        self.requests: list[DraftReviewRequest] = []

    def review(self, request: DraftReviewRequest) -> DraftReviewDecision:
        self.requests.append(request)
        return DraftReviewDecision(
            decision=self.decision,
            rationale="Independent evidence review completed.",
            confidence=self.confidence,
            evidence_ids=["validation_report", "test_report"],
            policy_version=request.policy_version,
            model="test-reviewer-v1",
        )


class _Publisher:
    def publish(self, **kwargs: object) -> dict[str, object]:
        return {
            "success": True,
            "registry_id": "tool:foo",
            "version_id": "1",
            "published_by": kwargs.get("user"),
        }


class _PromptEngine:
    def run_turn(self, request: object, *, toolset: object | None = None) -> AssistantTurnResult:
        del request, toolset
        return AssistantTurnResult(
            reply="Prompt draft ready.",
            draft_deltas=[
                DraftDelta(
                    artifact_type="prompt",
                    title="Support prompt",
                    artifact={
                        "name": "support-prompt",
                        "template": "Answer {{question}}",
                        "variables": ["question"],
                        "target_alias": "prod",
                    },
                )
            ],
        )


def _autonomous_service(
    *,
    decision: str = "approve",
    confidence: float = 0.99,
    reviewer_user: str = "@reviewer",
    release_user: str = "@release",
) -> tuple[AssistantService, _Reviewer]:
    reviewer = _Reviewer(decision=decision, confidence=confidence)
    config = CaliberConfig(
        approver_users="@reviewer",
        operator_users="@test,@release",
    )
    service = AssistantService(
        engine=FakeAssistantEngine(),
        reviewer=reviewer,
        publisher=_Publisher(),  # type: ignore[arg-type]
        runtime_config=config,
        settings=AssistantRuntimeSettings(
            reviewer_user=reviewer_user,
            release_user=release_user,
            reviewer_min_confidence=0.8,
        ),
    )
    return service, reviewer


@pytest.fixture
def svc() -> AssistantService:
    return AssistantService(engine=FakeAssistantEngine())


def _drive_to_draft(
    svc: AssistantService,
    factory: sessionmaker[Session],
    approval_mode: str,
):
    """Run the fake engine to its draft turn and return the final TurnResponse."""
    sid = svc.create_session(
        SessionCreateRequest(title="S"), session_factory=factory, user=USER
    ).session_id
    svc.send_message(
        sid,
        MessageSendRequest(content="make a tool", mode="build", approval_mode=approval_mode),
        session_factory=factory,
        user=USER,
    )
    return sid, svc.send_message(
        sid,
        MessageSendRequest(content="call it foo", mode="build", approval_mode=approval_mode),
        session_factory=factory,
        user=USER,
    )


class TestApprovalMode:
    def test_manual_leaves_draft_untouched(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        _sid, turn = _drive_to_draft(svc, session_factory, "manual")
        assert turn.draft_updates
        assert turn.draft_updates[0].status == "draft"
        labels = [step["label"] for step in turn.assistant_message.metadata_["process_steps"]]
        assert labels == ["Thinking", "Drafted", "Review required"]

    def test_auto_safe_validates_and_tests(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        _sid, turn = _drive_to_draft(svc, session_factory, "auto_safe")
        assert turn.draft_updates
        # Auto-advance stops at "tested" — approve/publish remain manual.
        assert turn.draft_updates[0].status == "tested"
        labels = [step["label"] for step in turn.assistant_message.metadata_["process_steps"]]
        assert labels == ["Thinking", "Drafted", "Validated", "Tested", "Review required"]

    def test_legacy_auto_all_no_longer_self_approves(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        _sid, turn = _drive_to_draft(svc, session_factory, "auto_all")
        assert turn.draft_updates
        assert turn.draft_updates[0].status == "tested"
        labels = [step["label"] for step in turn.assistant_message.metadata_["process_steps"]]
        assert labels == ["Thinking", "Drafted", "Validated", "Tested", "Review required"]

    def test_agent_review_approves_but_does_not_publish(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        service, reviewer = _autonomous_service()
        _sid, turn = _drive_to_draft(service, session_factory, "agent_review")
        draft = turn.draft_updates[0]
        assert draft.status == "approved"
        assert draft.review_report is not None
        assert draft.review_report["reviewer_user"] == "@reviewer"
        assert draft.target_registry_id is None
        assert reviewer.requests[0].author_user == USER
        assert reviewer.requests[0].candidate_hash == draft.review_report["candidate_hash"]

        edited = service.update_draft(
            draft.draft_id,
            DraftUpdateRequest(version=draft.version, summary="Changed after review"),
            session_factory=session_factory,
            user=USER,
        )
        assert edited is not None
        assert edited.status == "draft"
        assert edited.review_report is None

    def test_full_autonomy_uses_distinct_reviewer_and_release_identities(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        service, _reviewer = _autonomous_service()
        _sid, turn = _drive_to_draft(service, session_factory, "full_autonomy")
        draft = turn.draft_updates[0]
        assert draft.status == "published"
        with session_factory() as db:
            review = db.query(CaliberAssistantReview).one()
            event = db.query(CaliberAssistantPublishEvent).one()
            assert review.author_user == USER
            assert review.reviewer_user == "@reviewer"
            assert event.approved_by == "@reviewer"
            assert event.published_by == "@release"

    @pytest.mark.parametrize(
        ("reviewer_user", "release_user", "expected"),
        [
            (USER, "@release", "distinct from the draft author"),
            ("@unknown", "@release", "does not carry caliber.approver"),
            ("@reviewer", "@reviewer", "distinct from author and reviewer"),
            ("@reviewer", "@unknown", "does not carry caliber.operator"),
        ],
    )
    def test_full_autonomy_fails_closed_on_identity_or_scope_error(
        self,
        session_factory: sessionmaker[Session],
        reviewer_user: str,
        release_user: str,
        expected: str,
    ) -> None:
        service, reviewer = _autonomous_service(
            reviewer_user=reviewer_user,
            release_user=release_user,
        )
        _sid, turn = _drive_to_draft(service, session_factory, "full_autonomy")
        draft = turn.draft_updates[0]
        assert draft.status == "review_failed"
        assert expected in str(draft.review_report["rationale"])
        assert reviewer.requests == []

    def test_agent_rejection_is_durable_and_not_published(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        service, _reviewer = _autonomous_service(decision="reject")
        _sid, turn = _drive_to_draft(service, session_factory, "full_autonomy")
        assert turn.draft_updates[0].status == "review_rejected"
        with session_factory() as db:
            assert db.query(CaliberAssistantReview).one().decision == "reject"
            assert db.query(CaliberAssistantPublishEvent).count() == 0

    def test_low_confidence_approval_is_rejected(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        service, _reviewer = _autonomous_service(confidence=0.5)
        _sid, turn = _drive_to_draft(service, session_factory, "agent_review")
        assert turn.draft_updates[0].status == "review_rejected"
        assert "below the configured minimum" in str(
            turn.draft_updates[0].review_report["rationale"]
        )

    def test_expired_agent_review_cannot_be_published(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        service, _reviewer = _autonomous_service()
        _sid, turn = _drive_to_draft(service, session_factory, "agent_review")
        draft = turn.draft_updates[0]
        with session_factory() as db:
            review = db.query(CaliberAssistantReview).one()
            review.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
        report = service.publish_draft(
            draft.draft_id,
            session_factory=session_factory,
            user=USER,
        )
        assert report == {"success": False, "error": "Reviewer decision has expired."}

    def test_full_autonomy_review_satisfies_prompt_alias_provenance(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        reviewer = _Reviewer()
        service = AssistantService(
            engine=_PromptEngine(),  # type: ignore[arg-type]
            reviewer=reviewer,
            publisher=_Publisher(),  # type: ignore[arg-type]
            runtime_config=CaliberConfig(
                approver_users="@reviewer",
                operator_users="@test,@release",
            ),
            settings=AssistantRuntimeSettings(
                reviewer_user="@reviewer",
                release_user="@release",
            ),
        )
        sid = service.create_session(
            SessionCreateRequest(title="Prompt"),
            session_factory=session_factory,
            user=USER,
        ).session_id
        turn = service.send_message(
            sid,
            MessageSendRequest(
                content="Create the prompt",
                mode="build",
                approval_mode="full_autonomy",
            ),
            session_factory=session_factory,
            user=USER,
        )
        assert turn.draft_updates[0].status == "published"
        with session_factory() as db:
            event = db.query(CaliberAssistantPublishEvent).one()
            assert event.publish_report["policy"]["approval_kind"] == "agent_review"

    def test_approval_mode_persisted(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        sid, _turn = _drive_to_draft(svc, session_factory, "auto_safe")
        session = svc.get_session(sid, session_factory=session_factory, user=USER)
        assert session is not None
        assert session.metadata_["assistant_approval_mode"] == "auto_safe"

    def test_chat_mode_ignores_approval(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        # In chat mode no drafts are produced, so auto-advance has nothing to do.
        sid = svc.create_session(
            SessionCreateRequest(title="S"), session_factory=session_factory, user=USER
        ).session_id
        turn = svc.send_message(
            sid,
            MessageSendRequest(content="hi", mode="chat", approval_mode="auto_all"),
            session_factory=session_factory,
            user=USER,
        )
        assert turn.draft_updates == []


class TestApprovalRoutes:
    def test_auto_safe_over_http(self, client: TestClient) -> None:
        sid = client.post(f"{PREFIX}/sessions", json={"title": "S"}).json()["data"]["session_id"]
        client.post(
            f"{PREFIX}/sessions/{sid}/messages",
            json={"content": "make a tool", "mode": "build", "approval_mode": "auto_safe"},
        )
        resp = client.post(
            f"{PREFIX}/sessions/{sid}/messages",
            json={"content": "call it foo", "mode": "build", "approval_mode": "auto_safe"},
        )
        assert resp.status_code == 201
        drafts = resp.json()["data"]["draft_updates"]
        assert drafts and drafts[0]["status"] == "tested"
