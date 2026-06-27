"""Tests for Aria context attachments ("+ add files") and interaction modes.

Covers the AssistantService attachment helpers + mode wiring at the service
layer, and the HTTP round-trip for the attachment routes.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker
from starlette.testclient import TestClient

from caliber.assistant.models import (
    AssistantTurnRequest,
    AssistantTurnResult,
    MessageSendRequest,
    SessionCreateRequest,
    SessionUpdateRequest,
)
from caliber.assistant.service import AssistantRuntimeSettings, AssistantService, ConflictError
from caliber.db.models import CaliberSkill

USER = "@test"
PREFIX = "/ajax-api/2.0/mlflow/caliber/assistant"


class CapturingEngine:
    """Engine that records turn requests and returns a draft only in build mode."""

    def __init__(self) -> None:
        self.requests: list[AssistantTurnRequest] = []

    def run_turn(self, request: AssistantTurnRequest, *, toolset: object | None = None) -> AssistantTurnResult:
        self.requests.append(request)
        from caliber.assistant.models import DraftDelta

        return AssistantTurnResult(
            reply="ok",
            draft_deltas=[DraftDelta(artifact_type="tool", title="t", summary="s")],
        )


@pytest.fixture
def svc() -> AssistantService:
    from caliber.assistant.fake import FakeAssistantEngine

    return AssistantService(engine=FakeAssistantEngine())


def _new_session(svc: AssistantService, factory: sessionmaker[Session]) -> str:
    return svc.create_session(
        SessionCreateRequest(title="S"), session_factory=factory, user=USER
    ).session_id


def _seed_skill(factory: sessionmaker[Session]) -> str:
    skill_id = "SK-attach01"
    with factory() as db:
        db.add(
            CaliberSkill(
                skill_id=skill_id,
                name="billing-helper",
                summary="Handles billing questions",
                content="Always cite the policy doc.",
                owner=USER,
                category="custom",
            )
        )
        db.commit()
    return skill_id


# ---------------------------------------------------------------------------
# Service: attachments
# ---------------------------------------------------------------------------


class TestAttachmentService:
    def test_add_and_list_text_snippet(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        sid = _new_session(svc, session_factory)
        att = svc.add_text_attachment(
            sid, name="Notes", text="Refund within 30 days.", session_factory=session_factory, user=USER
        )
        assert att.attachment_id.startswith("AATT-")
        assert att.kind == "text_snippet"
        assert att.name == "Notes"
        assert "Refund" in att.content_text

        listed = svc.list_attachments(sid, session_factory=session_factory, user=USER)
        assert [a.attachment_id for a in listed] == [att.attachment_id]

    def test_empty_text_snippet_rejected(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        sid = _new_session(svc, session_factory)
        with pytest.raises(ValueError, match="empty"):
            svc.add_text_attachment(
                sid, name="x", text="   ", session_factory=session_factory, user=USER
            )

    def test_add_library_resource_skill(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        sid = _new_session(svc, session_factory)
        skill_id = _seed_skill(session_factory)
        att = svc.add_library_attachment(
            sid,
            resource_type="skill",
            resource_id=skill_id,
            session_factory=session_factory,
            user=USER,
        )
        assert att.kind == "library_resource"
        assert att.ref_type == "skill"
        assert att.name == "billing-helper"
        assert "policy doc" in att.content_text
        assert att.metadata_["resource_id"] == skill_id

    def test_library_resource_missing_raises(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        sid = _new_session(svc, session_factory)
        with pytest.raises(ValueError, match="not found"):
            svc.add_library_attachment(
                sid,
                resource_type="skill",
                resource_id="SK-nope",
                session_factory=session_factory,
                user=USER,
            )

    def test_text_is_capped(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        from caliber.assistant.models import ATTACHMENT_TEXT_MAX_CHARS

        sid = _new_session(svc, session_factory)
        att = svc.add_text_attachment(
            sid,
            name="big",
            text="x" * (ATTACHMENT_TEXT_MAX_CHARS + 100),
            session_factory=session_factory,
            user=USER,
        )
        assert att.truncated is True
        assert len(att.content_text) == ATTACHMENT_TEXT_MAX_CHARS

    def test_attachment_limit_enforced(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        from caliber.assistant.fake import FakeAssistantEngine

        svc = AssistantService(
            engine=FakeAssistantEngine(),
            settings=AssistantRuntimeSettings(max_attachments_per_session=2),
        )
        sid = _new_session(svc, session_factory)
        for i in range(2):
            svc.add_text_attachment(
                sid, name=f"n{i}", text="hi", session_factory=session_factory, user=USER
            )
        with pytest.raises(ConflictError):
            svc.add_text_attachment(
                sid, name="overflow", text="hi", session_factory=session_factory, user=USER
            )

    def test_delete_attachment(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        sid = _new_session(svc, session_factory)
        att = svc.add_text_attachment(
            sid, name="n", text="hi", session_factory=session_factory, user=USER
        )
        assert svc.delete_attachment(att.attachment_id, session_factory=session_factory, user=USER)
        assert svc.list_attachments(sid, session_factory=session_factory, user=USER) == []

    def test_delete_cross_owner_denied(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        sid = _new_session(svc, session_factory)
        att = svc.add_text_attachment(
            sid, name="n", text="hi", session_factory=session_factory, user=USER
        )
        assert not svc.delete_attachment(
            att.attachment_id, session_factory=session_factory, user="@other"
        )

    def test_cross_owner_list_empty(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        sid = _new_session(svc, session_factory)
        svc.add_text_attachment(
            sid, name="n", text="hi", session_factory=session_factory, user=USER
        )
        assert svc.list_attachments(sid, session_factory=session_factory, user="@other") == []


# ---------------------------------------------------------------------------
# Service: modes + attachment context plumbing
# ---------------------------------------------------------------------------


class TestModeAndContext:
    def test_attachments_forwarded_to_engine(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        engine = CapturingEngine()
        svc = AssistantService(engine=engine)
        sid = _new_session(svc, session_factory)
        svc.add_text_attachment(
            sid, name="ctx", text="grounding text", session_factory=session_factory, user=USER
        )
        svc.send_message(
            sid,
            MessageSendRequest(content="hello", mode="build"),
            session_factory=session_factory,
            user=USER,
        )
        req = engine.requests[-1]
        assert req.mode == "build"
        assert len(req.attachments) == 1
        assert req.attachments[0]["content_text"] == "grounding text"

    def test_chat_mode_suppresses_drafts(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        engine = CapturingEngine()  # always returns a draft delta
        svc = AssistantService(engine=engine)
        sid = _new_session(svc, session_factory)
        turn = svc.send_message(
            sid,
            MessageSendRequest(content="hi", mode="chat"),
            session_factory=session_factory,
            user=USER,
        )
        assert turn.draft_updates == []

    def test_plan_mode_suppresses_drafts(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        # Drafts are a build-mode authoring artifact; plan mode must not
        # materialize them (a plan chat shouldn't leak authoring drafts).
        engine = CapturingEngine()
        svc = AssistantService(engine=engine)
        sid = _new_session(svc, session_factory)
        turn = svc.send_message(
            sid,
            MessageSendRequest(content="plan it", mode="plan"),
            session_factory=session_factory,
            user=USER,
        )
        assert turn.draft_updates == []

    def test_build_mode_materializes_drafts(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        engine = CapturingEngine()
        svc = AssistantService(engine=engine)
        sid = _new_session(svc, session_factory)
        turn = svc.send_message(
            sid,
            MessageSendRequest(content="hi", mode="build"),
            session_factory=session_factory,
            user=USER,
        )
        assert len(turn.draft_updates) == 1

    def test_engine_handled_error_marks_run_failed(
        self, session_factory: sessionmaker[Session]
    ) -> None:
        # An engine that *returns* (not raises) a result carrying ``error`` must
        # not be persisted as a clean success — the run is marked failed and the
        # message metadata flags the error.
        class _ErroringEngine:
            def run_turn(
                self, request: AssistantTurnRequest, *, toolset: object | None = None
            ) -> AssistantTurnResult:
                return AssistantTurnResult(reply="I encountered an error: boom", error="boom")

        svc = AssistantService(engine=_ErroringEngine())
        sid = _new_session(svc, session_factory)
        turn = svc.send_message(
            sid,
            MessageSendRequest(content="hi", mode="chat"),
            session_factory=session_factory,
            user=USER,
        )
        assert turn.run is not None
        assert turn.run.status == "failed"
        assert turn.assistant_message.metadata_.get("error") is True

    def test_mode_persisted_in_session_metadata(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        sid = _new_session(svc, session_factory)
        svc.send_message(
            sid,
            MessageSendRequest(content="hi", mode="plan"),
            session_factory=session_factory,
            user=USER,
        )
        session = svc.get_session(sid, session_factory=session_factory, user=USER)
        assert session is not None
        assert session.metadata_["assistant_mode"] == "plan"

    def test_update_session_sets_mode(
        self, svc: AssistantService, session_factory: sessionmaker[Session]
    ) -> None:
        sid = _new_session(svc, session_factory)
        updated = svc.update_session(
            sid, SessionUpdateRequest(mode="chat"), session_factory=session_factory, user=USER
        )
        assert updated is not None
        assert updated.metadata_["assistant_mode"] == "chat"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


class TestAttachmentRoutes:
    def _create_session(self, client: TestClient) -> str:
        resp = client.post(f"{PREFIX}/sessions", json={"title": "S"})
        return resp.json()["data"]["session_id"]

    def test_create_text_snippet_and_list(self, client: TestClient) -> None:
        sid = self._create_session(client)
        resp = client.post(
            f"{PREFIX}/sessions/{sid}/attachments",
            json={"kind": "text_snippet", "name": "Notes", "text": "policy text"},
        )
        assert resp.status_code == 201
        att = resp.json()["data"]
        assert att["kind"] == "text_snippet"

        listed = client.get(f"{PREFIX}/sessions/{sid}/attachments")
        assert listed.status_code == 200
        assert len(listed.json()["data"]) == 1

    def test_text_snippet_requires_text(self, client: TestClient) -> None:
        sid = self._create_session(client)
        resp = client.post(
            f"{PREFIX}/sessions/{sid}/attachments",
            json={"kind": "text_snippet", "name": "x"},
        )
        assert resp.status_code == 400

    def test_delete_attachment_route(self, client: TestClient) -> None:
        sid = self._create_session(client)
        created = client.post(
            f"{PREFIX}/sessions/{sid}/attachments",
            json={"kind": "text_snippet", "text": "t"},
        )
        att_id = created.json()["data"]["attachment_id"]
        delete = client.delete(f"{PREFIX}/attachments/{att_id}")
        assert delete.status_code == 204
        assert client.delete(f"{PREFIX}/attachments/{att_id}").status_code == 404

    def test_upload_attachment_route(self, client: TestClient) -> None:
        sid = self._create_session(client)
        resp = client.post(
            f"{PREFIX}/sessions/{sid}/attachments/upload",
            files={"file": ("notes.txt", b"hello from upload", "text/plain")},
        )
        assert resp.status_code == 201
        att = resp.json()["data"]
        assert att["kind"] == "upload"
        assert "hello from upload" in att["content_text"]

    def test_attachments_on_unknown_session_404(self, client: TestClient) -> None:
        resp = client.get(f"{PREFIX}/sessions/ASST-00000000/attachments")
        assert resp.status_code == 404
