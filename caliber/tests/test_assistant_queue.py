"""Tests for Aria's message queue ("add to queue") and steer features."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.assistant.fake import FakeAssistantEngine
from caliber.assistant.models import MessageSendRequest, SessionCreateRequest
from caliber.assistant.service import AssistantRuntimeSettings, AssistantService, ConflictError

USER = "@test"
PREFIX = "/ajax-api/2.0/mlflow/caliber/assistant"


@pytest.fixture
def svc() -> AssistantService:
    return AssistantService(engine=FakeAssistantEngine())


def _new_session(svc: AssistantService, factory: sessionmaker[Session]) -> str:
    return svc.create_session(
        SessionCreateRequest(title="S"), session_factory=factory, user=USER
    ).session_id


# ---------------------------------------------------------------------------
# Service: queue
# ---------------------------------------------------------------------------


class TestQueueService:
    def test_enqueue_and_list_in_order(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        sid = _new_session(svc, session_factory)
        svc.enqueue_message(sid, content="first", session_factory=session_factory, user=USER)
        svc.enqueue_message(sid, content="second", session_factory=session_factory, user=USER)
        queue = svc.list_queue(sid, session_factory=session_factory, user=USER)
        assert [q.content for q in queue] == ["first", "second"]
        assert all(q.kind == "queued" and q.status == "pending" for q in queue)

    def test_steer_jumps_to_front(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        sid = _new_session(svc, session_factory)
        svc.enqueue_message(sid, content="first", session_factory=session_factory, user=USER)
        svc.enqueue_message(sid, content="second", session_factory=session_factory, user=USER)
        svc.enqueue_message(
            sid, content="redirect", kind="steer", session_factory=session_factory, user=USER
        )
        queue = svc.list_queue(sid, session_factory=session_factory, user=USER)
        assert queue[0].content == "redirect"
        assert queue[0].kind == "steer"

    def test_empty_content_rejected(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        sid = _new_session(svc, session_factory)
        with pytest.raises(ValueError, match="empty"):
            svc.enqueue_message(sid, content="   ", session_factory=session_factory, user=USER)

    def test_unknown_kind_rejected(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        sid = _new_session(svc, session_factory)
        with pytest.raises(ValueError, match="kind"):
            svc.enqueue_message(
                sid, content="x", kind="bogus", session_factory=session_factory, user=USER
            )

    def test_cancel_queued(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        sid = _new_session(svc, session_factory)
        item = svc.enqueue_message(sid, content="x", session_factory=session_factory, user=USER)
        assert svc.cancel_queued(item.queue_id, session_factory=session_factory, user=USER)
        assert svc.list_queue(sid, session_factory=session_factory, user=USER) == []

    def test_cancel_cross_owner_denied(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        sid = _new_session(svc, session_factory)
        item = svc.enqueue_message(sid, content="x", session_factory=session_factory, user=USER)
        assert not svc.cancel_queued(item.queue_id, session_factory=session_factory, user="@other")

    def test_queue_limit_enforced(self, session_factory: sessionmaker[Session]) -> None:
        svc = AssistantService(
            engine=FakeAssistantEngine(),
            settings=AssistantRuntimeSettings(max_queued_per_session=2),
        )
        sid = _new_session(svc, session_factory)
        svc.enqueue_message(sid, content="a", session_factory=session_factory, user=USER)
        svc.enqueue_message(sid, content="b", session_factory=session_factory, user=USER)
        with pytest.raises(ConflictError):
            svc.enqueue_message(sid, content="c", session_factory=session_factory, user=USER)


# ---------------------------------------------------------------------------
# Service: steer in send_message
# ---------------------------------------------------------------------------


class TestSteerTurn:
    def test_steer_forwarded_to_engine(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        sid = _new_session(svc, session_factory)
        turn = svc.send_message(
            sid,
            MessageSendRequest(content="go left instead", steer=True, mode="build"),
            session_factory=session_factory,
            user=USER,
        )
        # The fake engine returns a steering acknowledgement and no drafts.
        assert "steering" in turn.assistant_message.content.lower()
        assert turn.draft_updates == []

    def test_steer_recorded_on_user_message(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        sid = _new_session(svc, session_factory)
        svc.send_message(
            sid,
            MessageSendRequest(content="redirect", steer=True),
            session_factory=session_factory,
            user=USER,
        )
        messages = svc.list_messages(sid, session_factory=session_factory, user=USER)
        user_msg = next(m for m in messages if m.role == "user")
        assert user_msg.metadata_.get("steer") is True


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class TestQueueRoutes:
    def _create_session(self, client: TestClient) -> str:
        return client.post(f"{PREFIX}/sessions", json={"title": "S"}).json()["data"]["session_id"]

    def test_enqueue_list_cancel(self, client: TestClient) -> None:
        sid = self._create_session(client)
        created = client.post(
            f"{PREFIX}/sessions/{sid}/queue",
            json={"content": "do this next", "mode": "build"},
        )
        assert created.status_code == 201
        queue_id = created.json()["data"]["queue_id"]

        listed = client.get(f"{PREFIX}/sessions/{sid}/queue")
        assert listed.status_code == 200
        assert len(listed.json()["data"]) == 1

        deleted = client.delete(f"{PREFIX}/queue/{queue_id}")
        assert deleted.status_code == 204
        assert client.delete(f"{PREFIX}/queue/{queue_id}").status_code == 404

    def test_enqueue_steer_kind(self, client: TestClient) -> None:
        sid = self._create_session(client)
        resp = client.post(
            f"{PREFIX}/sessions/{sid}/queue",
            json={"content": "actually, change direction", "kind": "steer"},
        )
        assert resp.status_code == 201
        assert resp.json()["data"]["kind"] == "steer"

    def test_send_message_with_steer(self, client: TestClient) -> None:
        sid = self._create_session(client)
        resp = client.post(
            f"{PREFIX}/sessions/{sid}/messages",
            json={"content": "redirect now", "steer": True},
        )
        assert resp.status_code == 201
        assert "steering" in resp.json()["data"]["assistant_message"]["content"].lower()

    def test_queue_unknown_session_404(self, client: TestClient) -> None:
        assert client.get(f"{PREFIX}/sessions/ASST-00000000/queue").status_code == 404
