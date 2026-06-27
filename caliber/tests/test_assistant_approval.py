"""Tests for Aria's approval mode — how far Aria auto-advances a draft.

``manual`` leaves a new draft untouched; ``auto_safe`` validates and tests it;
``auto_all`` additionally approves and publishes. The fake engine produces a
draft on its second turn, so each test sends two messages.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.assistant.fake import FakeAssistantEngine
from caliber.assistant.models import MessageSendRequest, SessionCreateRequest
from caliber.assistant.service import AssistantService

USER = "@test"
PREFIX = "/ajax-api/2.0/mlflow/caliber/assistant"


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

    def test_auto_all_approves_and_publishes(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        _sid, turn = _drive_to_draft(svc, session_factory, "auto_all")
        assert turn.draft_updates
        # The draft is taken past "tested" to at least "approved".
        assert turn.draft_updates[0].status in {
            "approved",
            "publishing",
            "published",
            "publish_failed",
        }
        labels = [step["label"] for step in turn.assistant_message.metadata_["process_steps"]]
        assert labels[:4] == ["Thinking", "Drafted", "Validated", "Tested"]
        assert "Approved" in labels

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
