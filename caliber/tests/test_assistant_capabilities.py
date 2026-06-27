"""Phase 0 — capability registry + its projection into Aria's agent toolset.

Verifies the registry is populated, that capabilities project into the toolset
under the same tier gate as the hand-written tools (read always; mutate only in
build + auto_all; gated never auto-exposed), and that dispatching a capability
performs a real, validated, audited operation reusing the route's core helper.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from caliber.assistant.capabilities import (
    TIER_GATED,
    Capability,
    capability_by_tool_name,
    get_capability,
    registered_capabilities,
)
from caliber.assistant.fake import FakeAssistantEngine
from caliber.assistant.models import SessionCreateRequest
from caliber.assistant.service import AssistantService
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberAuditLog,
    CaliberEvalDataset,
    CaliberJudge,
    CaliberReviewItem,
    CaliberReviewQueue,
)

USER = "@test"


@pytest.fixture
def svc() -> AssistantService:
    return AssistantService(engine=FakeAssistantEngine(), runtime_config=CaliberConfig())


def _session(svc: AssistantService, factory: sessionmaker[Session]) -> str:
    return svc.create_session(
        SessionCreateRequest(title="S"), session_factory=factory, user=USER
    ).session_id


def _toolset(
    svc: AssistantService,
    factory: sessionmaker[Session],
    sid: str,
    *,
    mode: str,
    approval: str,
    project_id: str | None = None,
):
    return svc._build_agent_toolset(
        session_factory=factory,
        user=USER,
        session_id=sid,
        mode=mode,
        approval_mode=approval,
        project_id=project_id,
    )


def _names(toolset) -> set[str]:
    return {s["function"]["name"] for s in toolset.specs()}


# --- registry ---------------------------------------------------------------


def test_registry_has_builtin_capabilities() -> None:
    keys = {c.key for c in registered_capabilities()}
    assert {"judge.list", "judge.create", "review_queue.list", "review_queue.create"} <= keys
    assert get_capability("judge.create").tier == "mutate"
    assert get_capability("judge.list").tier == "read"
    # dotted keys map to underscore tool names (provider tool-name rules).
    assert capability_by_tool_name("judge_create").key == "judge.create"


# --- projection / tier gate --------------------------------------------------


def test_read_capability_always_exposed(svc, session_factory) -> None:
    sid = _session(svc, session_factory)
    names = _names(_toolset(svc, session_factory, sid, mode="chat", approval="manual"))
    assert "judge_list" in names  # read tier — available everywhere
    assert "judge_create" not in names  # mutate — not in chat/manual


def test_mutate_capability_only_in_auto_all_build(svc, session_factory) -> None:
    sid = _session(svc, session_factory)
    assert "judge_create" not in _names(
        _toolset(svc, session_factory, sid, mode="build", approval="manual")
    )
    assert "judge_create" not in _names(
        _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
    )
    auto_all = _names(_toolset(svc, session_factory, sid, mode="build", approval="auto_all"))
    assert {"judge_create", "review_queue_create"} <= auto_all


def test_gated_tier_never_exposed(svc, session_factory) -> None:
    sid = _session(svc, session_factory)
    ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_all")
    # The most permissive mode still refuses the gated tier.
    assert ts._tier_allowed(TIER_GATED) is False
    # A gated capability would not appear in specs even in build+auto_all.
    gated = Capability(
        key="judge.promote_test",
        title="x",
        description="x",
        tier=TIER_GATED,
        handler=lambda _c, _a: None,
    )
    assert gated.to_spec()["function"]["name"] == "judge_promote_test"
    assert ts._tier_allowed(gated.tier) is False


# --- dispatch ----------------------------------------------------------------


def test_dispatch_creates_judge(svc, session_factory) -> None:
    sid = _session(svc, session_factory)
    ts = _toolset(
        svc,
        session_factory,
        sid,
        mode="build",
        approval="auto_all",
        project_id="PRJ-judge",
    )
    out = json.loads(
        ts.dispatch(
            "judge_create",
            {
                "name": "cap-faithfulness",
                "instructions": "Is {{ outputs }} faithful to {{ expectations }}?",
                "model": "openai:/gpt-4o-mini",
                "feedback_value_type": "bool",
            },
        )
    )
    assert out["ok"] is True
    assert out["data"]["name"] == "cap-faithfulness"
    with session_factory() as db:
        row = (
            db.execute(select(CaliberJudge).where(CaliberJudge.name == "cap-faithfulness"))
            .scalars()
            .first()
        )
        assert row is not None and row.owner == USER
        assert row.project_id == "PRJ-judge"
        # Same audit trail as the REST route (shared create_judge_record helper).
        actions = {
            r.action
            for r in db.execute(
                select(CaliberAuditLog).where(CaliberAuditLog.entity_id == row.judge_id)
            )
            .scalars()
            .all()
        }
        assert "create_judge" in actions


def test_dispatch_judge_rejects_bad_instructions(svc, session_factory) -> None:
    sid = _session(svc, session_factory)
    ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_all")
    out = json.loads(ts.dispatch("judge_create", {"name": "bad", "instructions": "no vars"}))
    assert "error" in out  # schema validation (reused from the route) rejects it
    with session_factory() as db:
        assert (
            db.execute(select(CaliberJudge).where(CaliberJudge.name == "bad")).scalars().first()
            is None
        )


def test_dispatch_denied_in_manual_mode(svc, session_factory) -> None:
    sid = _session(svc, session_factory)
    ts = _toolset(svc, session_factory, sid, mode="build", approval="manual")
    out = json.loads(
        ts.dispatch("judge_create", {"name": "nope", "instructions": "Rate {{ outputs }}"})
    )
    assert "error" in out and "not permitted" in out["error"]
    with session_factory() as db:
        assert (
            db.execute(select(CaliberJudge).where(CaliberJudge.name == "nope")).scalars().first()
            is None
        )


def test_dispatch_creates_review_queue(svc, session_factory) -> None:
    sid = _session(svc, session_factory)
    ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_all")
    out = json.loads(
        ts.dispatch(
            "review_queue_create",
            {
                "name": "cap-review",
                "questions": [{"key": "correct", "title": "Correct?", "type": "pass_fail"}],
            },
        )
    )
    assert out["ok"] is True and out["data"]["questions"] == 1
    with session_factory() as db:
        assert (
            db.execute(select(CaliberReviewQueue).where(CaliberReviewQueue.name == "cap-review"))
            .scalars()
            .first()
            is not None
        )


def test_dispatch_creates_eval_dataset(svc, session_factory) -> None:
    sid = _session(svc, session_factory)
    ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_all")
    out = json.loads(
        ts.dispatch("eval_dataset_create", {"name": "cap-testset", "description": "d"})
    )
    assert out["ok"] is True and out["data"]["name"] == "cap-testset"
    with session_factory() as db:
        row = (
            db.execute(select(CaliberEvalDataset).where(CaliberEvalDataset.name == "cap-testset"))
            .scalars()
            .first()
        )
        # Owner is the acting Aria user, not a caller-supplied value.
        assert row is not None and row.owner == USER


def test_dispatch_enqueues_review_items(svc, session_factory) -> None:
    sid = _session(svc, session_factory)
    ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_all")
    queue = json.loads(
        ts.dispatch(
            "review_queue_create",
            {"name": "cap-q2", "questions": [{"key": "ok", "title": "OK?", "type": "pass_fail"}]},
        )
    )
    queue_id = queue["data"]["queue_id"]
    out = json.loads(
        ts.dispatch("review_queue_add_items", {"queue_id": queue_id, "trace_ids": ["tr-a", "tr-b"]})
    )
    assert out["ok"] is True and out["data"]["added"] == 2
    with session_factory() as db:
        items = (
            db.execute(select(CaliberReviewItem).where(CaliberReviewItem.queue_id == queue_id))
            .scalars()
            .all()
        )
        assert {i.trace_id for i in items} == {"tr-a", "tr-b"}
