"""Focused Aria mode / approval propagation coverage for AssistantService."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session, sessionmaker

from caliber.assistant.models import (
    AssistantTurnRequest,
    AssistantTurnResult,
    MessageSendRequest,
    SessionCreateRequest,
    SessionUpdateRequest,
)
from caliber.assistant.service import AssistantService

USER = "@test"


class CapturingAssistantEngine:
    def __init__(self) -> None:
        self.requests: list[AssistantTurnRequest] = []

    def run_turn(
        self,
        request: AssistantTurnRequest,
        *,
        toolset: object | None = None,
    ) -> AssistantTurnResult:
        self.requests.append(request)
        return AssistantTurnResult(reply="captured")


def _create_service() -> tuple[AssistantService, CapturingAssistantEngine]:
    engine = CapturingAssistantEngine()
    return AssistantService(engine=engine), engine


def _create_session(
    svc: AssistantService,
    session_factory: sessionmaker[Session],
    **kwargs: object,
) -> str:
    return svc.create_session(
        SessionCreateRequest(title="Aria", **kwargs),
        session_factory=session_factory,
        user=USER,
    ).session_id


@pytest.mark.parametrize("mode", ["chat", "build", "plan"])
def test_create_session_persists_requested_mode(
    session_factory: sessionmaker[Session],
    mode: str,
) -> None:
    svc, _engine = _create_service()
    session = svc.create_session(
        SessionCreateRequest(title="Aria", mode=mode),
        session_factory=session_factory,
        user=USER,
    )
    assert session.metadata_["assistant_mode"] == mode


@pytest.mark.parametrize("approval_mode", ["manual", "auto_safe", "auto_all"])
def test_create_session_persists_requested_approval_mode(
    session_factory: sessionmaker[Session],
    approval_mode: str,
) -> None:
    svc, _engine = _create_service()
    session = svc.create_session(
        SessionCreateRequest(title="Aria", approval_mode=approval_mode),
        session_factory=session_factory,
        user=USER,
    )
    assert session.metadata_["assistant_approval_mode"] == approval_mode


@pytest.mark.parametrize("mode", ["chat", "build", "plan"])
def test_update_session_persists_requested_mode(
    session_factory: sessionmaker[Session],
    mode: str,
) -> None:
    svc, _engine = _create_service()
    sid = _create_session(svc, session_factory)
    updated = svc.update_session(
        sid,
        SessionUpdateRequest(mode=mode),
        session_factory=session_factory,
        user=USER,
    )
    assert updated is not None
    assert updated.metadata_["assistant_mode"] == mode


@pytest.mark.parametrize("approval_mode", ["manual", "auto_safe", "auto_all"])
def test_update_session_persists_requested_approval_mode(
    session_factory: sessionmaker[Session],
    approval_mode: str,
) -> None:
    svc, _engine = _create_service()
    sid = _create_session(svc, session_factory)
    updated = svc.update_session(
        sid,
        SessionUpdateRequest(approval_mode=approval_mode),
        session_factory=session_factory,
        user=USER,
    )
    assert updated is not None
    assert updated.metadata_["assistant_approval_mode"] == approval_mode


@pytest.mark.parametrize("mode", ["chat", "build", "plan"])
def test_send_message_propagates_request_level_mode_to_engine_and_session(
    session_factory: sessionmaker[Session],
    mode: str,
) -> None:
    svc, engine = _create_service()
    sid = _create_session(svc, session_factory)

    turn = svc.send_message(
        sid,
        MessageSendRequest(content="hello", mode=mode),
        session_factory=session_factory,
        user=USER,
    )

    assert engine.requests[-1].mode == mode
    assert turn.assistant_message.content == "captured"
    session = svc.get_session(sid, session_factory=session_factory, user=USER)
    assert session is not None
    assert session.metadata_["assistant_mode"] == mode


@pytest.mark.parametrize("approval_mode", ["manual", "auto_safe", "auto_all"])
def test_send_message_propagates_request_level_approval_mode_to_engine_and_session(
    session_factory: sessionmaker[Session],
    approval_mode: str,
) -> None:
    svc, engine = _create_service()
    sid = _create_session(svc, session_factory)

    turn = svc.send_message(
        sid,
        MessageSendRequest(content="hello", approval_mode=approval_mode),
        session_factory=session_factory,
        user=USER,
    )

    assert engine.requests[-1].approval_mode == approval_mode
    assert turn.assistant_message.content == "captured"
    session = svc.get_session(sid, session_factory=session_factory, user=USER)
    assert session is not None
    assert session.metadata_["assistant_approval_mode"] == approval_mode


@pytest.mark.parametrize(
    ("mode", "approval_mode"),
    [
        ("chat", "manual"),
        ("build", "auto_safe"),
        ("plan", "auto_all"),
    ],
)
def test_send_message_inherits_mode_and_approval_from_session_metadata(
    session_factory: sessionmaker[Session],
    mode: str,
    approval_mode: str,
) -> None:
    svc, engine = _create_service()
    sid = _create_session(
        svc,
        session_factory,
        mode=mode,
        approval_mode=approval_mode,
    )

    turn = svc.send_message(
        sid,
        MessageSendRequest(content="hello"),
        session_factory=session_factory,
        user=USER,
    )

    assert engine.requests[-1].mode == mode
    assert engine.requests[-1].approval_mode == approval_mode
    assert turn.assistant_message.content == "captured"


def test_send_message_falls_back_to_defaults_for_invalid_persisted_mode_and_approval(
    session_factory: sessionmaker[Session],
) -> None:
    svc, engine = _create_service()
    sid = _create_session(svc, session_factory)
    updated = svc.update_session(
        sid,
        SessionUpdateRequest(
            metadata_={"assistant_mode": "review", "assistant_approval_mode": "always"}
        ),
        session_factory=session_factory,
        user=USER,
    )
    assert updated is not None

    turn = svc.send_message(
        sid,
        MessageSendRequest(content="hello"),
        session_factory=session_factory,
        user=USER,
    )

    assert engine.requests[-1].mode == "build"
    assert engine.requests[-1].approval_mode == "manual"
    assert turn.assistant_message.content == "captured"


def test_send_message_does_not_overwrite_existing_session_mode_when_request_omits_it(
    session_factory: sessionmaker[Session],
) -> None:
    svc, engine = _create_service()
    sid = _create_session(svc, session_factory, mode="plan")

    svc.send_message(
        sid,
        MessageSendRequest(content="hello"),
        session_factory=session_factory,
        user=USER,
    )

    assert engine.requests[-1].mode == "plan"
    session = svc.get_session(sid, session_factory=session_factory, user=USER)
    assert session is not None
    assert session.metadata_["assistant_mode"] == "plan"


def test_send_message_does_not_overwrite_existing_session_approval_when_request_omits_it(
    session_factory: sessionmaker[Session],
) -> None:
    svc, engine = _create_service()
    sid = _create_session(svc, session_factory, approval_mode="auto_safe")

    svc.send_message(
        sid,
        MessageSendRequest(content="hello"),
        session_factory=session_factory,
        user=USER,
    )

    assert engine.requests[-1].approval_mode == "auto_safe"
    session = svc.get_session(sid, session_factory=session_factory, user=USER)
    assert session is not None
    assert session.metadata_["assistant_approval_mode"] == "auto_safe"
