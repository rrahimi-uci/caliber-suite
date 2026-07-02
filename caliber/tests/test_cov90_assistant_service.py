"""Coverage-focused tests for :mod:`caliber.assistant.service`.

These exercise the uncovered slot-extraction, intent-execution adapter, publish
policy, attachment/queue, and send-message error/timeout paths of
``AssistantService`` using the deterministic ``FakeAssistantEngine`` and direct
calls into the service's helper methods (asserting real return values / DB
side-effects, never assertion-free calls).
"""

from __future__ import annotations

import hashlib
import time
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session, sessionmaker

from caliber.assistant.fake import FakeAssistantEngine
from caliber.assistant.models import (
    ATTACHMENT_TEXT_MAX_CHARS,
    AssistantTurnResult,
    DraftDelta,
    DraftUpdateRequest,
    IntentCandidate,
    IntentExecuteRequest,
    IntentPlanRequest,
    IntentPlanResponse,
    IntentResolveRequest,
    IntentSlot,
    MessageSendRequest,
    SessionCreateRequest,
    SessionUpdateRequest,
)
from caliber.assistant.service import (
    AssistantRuntimeSettings,
    AssistantService,
    ConflictError,
    _assistant_process_steps,
    _content_hash,
    _extract_template_from_text,
    _parse_prompt_ref_name,
    default_prompt_fetcher,
    normalize_disabled_domains,
    normalize_disabled_intents,
)
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberApprovalRequest,
    CaliberAssistantDraft,
    CaliberKnowledgeBase,
    CaliberRefinementJob,
    CaliberSkill,
    CaliberToolRegistry,
    CaliberWorkflow,
    CaliberWorkflowVersion,
)
from caliber.db.models import CaliberAssistantRun as _RunRow
from caliber.ids import new_assistant_draft_id
from caliber.tool_sandbox.models import ToolSandboxTestSuiteResult
from tests.workflow_helpers import make_manifest, make_support_manifest, seed_eval_dataset

USER = "@test"


@pytest.fixture
def svc() -> AssistantService:
    return AssistantService(engine=FakeAssistantEngine())


def _new_session(svc: AssistantService, sf: sessionmaker[Session], **kw: object) -> str:
    return svc.create_session(
        SessionCreateRequest(title="cov", **kw),  # type: ignore[arg-type]
        session_factory=sf,
        user=USER,
    ).session_id


def _plan(intent_name: str, slots: dict[str, object]) -> IntentPlanResponse:
    """Hand-build a ready plan for direct ``_execute_*`` calls."""
    return IntentPlanResponse(
        plan_id="APLN-cov-test",
        intent=IntentCandidate(name=intent_name, confidence=0.9, rationale="cov"),
        slots=[
            IntentSlot(name=name, value=value, required=True, source="user")
            for name, value in slots.items()
        ],
        ready=True,
        requires_confirmation=True,
    )


# ---------------------------------------------------------------------------
# Module-level pure helpers
# ---------------------------------------------------------------------------


def test_parse_prompt_ref_name() -> None:
    assert _parse_prompt_ref_name("") is None
    assert _parse_prompt_ref_name("prompts:/support-agent@prod") == "support-agent"
    assert _parse_prompt_ref_name("not-a-ref") is None


def test_extract_template_from_text() -> None:
    assert _extract_template_from_text("template: You are helpful.") == "You are helpful."
    assert _extract_template_from_text("nothing to see here") is None
    # match with an empty capture group -> falls through to None
    assert _extract_template_from_text("template:   ") is None


def test_content_hash() -> None:
    assert _content_hash("abc") == hashlib.sha256(b"abc").hexdigest()


def test_process_steps_validation_failed() -> None:
    steps = _assistant_process_steps(
        questions=[],
        tool_calls=[],
        draft_updates=[SimpleNamespace(status="validation_failed")],
        approval_mode="manual",
    )
    keys = [s["key"] for s in steps]
    assert keys == ["thinking", "drafted", "validation_failed"]


def test_process_steps_test_failed() -> None:
    steps = _assistant_process_steps(
        questions=[],
        tool_calls=[],
        draft_updates=[SimpleNamespace(status="test_failed")],
        approval_mode="manual",
    )
    keys = [s["key"] for s in steps]
    assert keys == ["thinking", "drafted", "validated", "test_failed"]


def test_process_steps_publish_failed() -> None:
    steps = _assistant_process_steps(
        questions=[],
        tool_calls=[],
        draft_updates=[SimpleNamespace(status="publish_failed")],
        approval_mode="manual",
    )
    keys = [s["key"] for s in steps]
    assert keys == ["thinking", "drafted", "validated", "tested", "approved", "publish_failed"]


def test_process_steps_review_required_and_actions() -> None:
    steps = _assistant_process_steps(
        questions=[],
        tool_calls=[SimpleNamespace(ok=False)],
        draft_updates=[SimpleNamespace(status="draft")],
        approval_mode="manual",
        error=True,
    )
    keys = [s["key"] for s in steps]
    assert keys == ["thinking", "actions", "drafted", "review", "error"]
    action_step = next(s for s in steps if s["key"] == "actions")
    assert action_step["label"] == "1 action"
    assert action_step["tone"] == "warning"


def test_normalize_disabled_intents() -> None:
    assert normalize_disabled_intents(None) == ()
    assert normalize_disabled_intents("generate_test_cases,bogus") == ("generate_test_cases",)
    assert normalize_disabled_intents(["generate_test_cases", "generate_test_cases"]) == (
        "generate_test_cases",
    )
    assert normalize_disabled_intents(123) == ()
    with pytest.raises(ValueError):
        normalize_disabled_intents(123, strict=True)
    with pytest.raises(ValueError):
        normalize_disabled_intents("bogus_intent", strict=True)


def test_normalize_disabled_domains() -> None:
    assert normalize_disabled_domains(None) == ()
    assert normalize_disabled_domains("prompt,bogus") == ("prompt",)
    assert normalize_disabled_domains({"prompt"}) == ("prompt",)
    assert normalize_disabled_domains(123) == ()
    with pytest.raises(ValueError):
        normalize_disabled_domains(123, strict=True)
    with pytest.raises(ValueError):
        normalize_disabled_domains("bogus_domain", strict=True)


def test_default_prompt_fetcher_no_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    import mlflow

    monkeypatch.setattr(mlflow, "load_prompt", None, raising=False)
    assert default_prompt_fetcher("x") is None


def test_default_prompt_fetcher_returns_template(monkeypatch: pytest.MonkeyPatch) -> None:
    import mlflow

    class _P:
        template = "You are helpful."

    def _loader(ref: str) -> object:
        if ref.endswith("@prod"):
            raise RuntimeError("no prod alias")
        return _P()

    monkeypatch.setattr(mlflow, "load_prompt", _loader, raising=False)
    assert default_prompt_fetcher("my-prompt") == "You are helpful."


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def test_list_sessions_user_filter_and_cursor(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    a = svc.create_session(
        SessionCreateRequest(title="A"), session_factory=session_factory, user=USER
    )
    b = svc.create_session(
        SessionCreateRequest(title="B"), session_factory=session_factory, user=USER
    )
    both = svc.list_sessions(session_factory=session_factory, user=USER)
    assert len(both) == 2
    ids = sorted([a.session_id, b.session_id])
    filtered = svc.list_sessions(session_factory=session_factory, user=USER, cursor=ids[1])
    assert len(filtered) == 1
    assert all(s.session_id < ids[1] for s in filtered)


def test_update_session_status(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    updated = svc.update_session(
        sid, SessionUpdateRequest(status="archived"), session_factory=session_factory, user=USER
    )
    assert updated is not None
    assert updated.status == "archived"


# ---------------------------------------------------------------------------
# Intent workbench: resolve / plan / execute guard rails
# ---------------------------------------------------------------------------


def test_resolve_intent_session_not_found(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    with pytest.raises(ValueError, match="not found"):
        svc.resolve_intent(
            "ASST-none",
            IntentResolveRequest(content="hi"),
            session_factory=session_factory,
            user=USER,
        )


def test_create_plan_session_not_found(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    with pytest.raises(ValueError, match="not found"):
        svc.create_intent_plan(
            "ASST-none", IntentPlanRequest(content="hi"), session_factory=session_factory, user=USER
        )


def test_create_plan_no_context_raises(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    with pytest.raises(ValueError, match="No intent context"):
        svc.create_intent_plan(sid, IntentPlanRequest(), session_factory=session_factory, user=USER)


def test_create_plan_content_with_intent_override(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    plan = svc.create_intent_plan(
        sid,
        IntentPlanRequest(content="optimize prompt foo", intent_name="create_prompt"),
        session_factory=session_factory,
        user=USER,
    )
    assert plan.intent.name == "create_prompt"
    assert plan.intent.rationale == "Intent explicitly provided by user."


def test_create_plan_discards_bad_stored_intent(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    with session_factory() as db:
        from caliber.db.models import CaliberAssistantSession

        row = db.get(CaliberAssistantSession, sid)
        assert row is not None
        md = dict(row.metadata_)
        md["intent_workbench"] = {
            "latest_intent": {"intent": {"name": "x", "confidence": 5.0}},
            "plans": {},
            "operations": {},
        }
        row.metadata_ = md
        db.commit()
    plan = svc.create_intent_plan(
        sid,
        IntentPlanRequest(intent_name="create_prompt"),
        session_factory=session_factory,
        user=USER,
    )
    assert plan.intent.name == "create_prompt"


def test_build_plan_missing_slot(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    plan = svc.create_intent_plan(
        sid,
        IntentPlanRequest(intent_name="create_prompt", slot_overrides={"prompt_name": "p"}),
        session_factory=session_factory,
        user=USER,
    )
    assert plan.ready is False
    assert "template" in plan.missing_slots
    assert "template" in {s.name for s in plan.slots}


def test_get_latest_plan_variants(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    assert svc.get_latest_plan("ASST-none", session_factory=session_factory) is None
    sid = _new_session(svc, session_factory)
    assert svc.get_latest_plan(sid, session_factory=session_factory) is None
    with session_factory() as db:
        from caliber.db.models import CaliberAssistantSession

        row = db.get(CaliberAssistantSession, sid)
        assert row is not None
        md = dict(row.metadata_)
        md["intent_workbench"] = {
            "latest_plan": {"plan_id": "x", "intent": {"garbage": True}},
            "plans": {},
            "operations": {},
        }
        row.metadata_ = md
        db.commit()
    assert svc.get_latest_plan(sid, session_factory=session_factory) is None


def test_execute_plan_guards(svc: AssistantService, session_factory: sessionmaker[Session]) -> None:
    with pytest.raises(ValueError, match="not found"):
        svc.execute_intent_plan(
            "ASST-none",
            IntentExecuteRequest(plan_id="p"),
            session_factory=session_factory,
            user=USER,
        )
    sid = _new_session(svc, session_factory)
    with pytest.raises(ValueError, match="No plan found"):
        svc.execute_intent_plan(
            sid, IntentExecuteRequest(), session_factory=session_factory, user=USER
        )


def test_execute_plan_id_mismatch_falls_back_to_latest(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    svc.create_intent_plan(
        sid,
        IntentPlanRequest(intent_name="generate_test_cases", slot_overrides={"prompt_name": "p"}),
        session_factory=session_factory,
        user=USER,
    )
    with pytest.raises(ValueError, match="not found"):
        svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id="APLN-wrong"),
            session_factory=session_factory,
            user=USER,
        )


def test_execute_plan_not_ready(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    plan = svc.create_intent_plan(
        sid,
        IntentPlanRequest(intent_name="create_prompt"),
        session_factory=session_factory,
        user=USER,
    )
    assert plan.ready is False
    with pytest.raises(ValueError, match="not ready"):
        svc.execute_intent_plan(
            sid,
            IntentExecuteRequest(plan_id=plan.plan_id),
            session_factory=session_factory,
            user=USER,
        )


def test_get_operation_status_session_not_found(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    assert svc.get_operation_status("ASST-none", "op", session_factory=session_factory) is None


def test_execute_optimization_failure_records_error(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    plan = svc.create_intent_plan(
        sid,
        IntentPlanRequest(
            intent_name="run_prompt_optimization",
            slot_overrides={
                "agent_id": "cov-agent",
                "eval_dataset_id": "ED-missing",
                "optimizer_type": "MetaPrompt",
                "scorers": ["Correctness"],
                "gate": {"min_aggregate_score": 0.7, "max_regression_delta": 0.05},
            },
        ),
        session_factory=session_factory,
        user=USER,
    )
    assert plan.ready
    executed = svc.execute_intent_plan(
        sid,
        IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
        session_factory=session_factory,
        user=USER,
    )
    assert executed.status == "failed"
    assert executed.result["result_type"] == "error"
    assert executed.result["warnings"]


def test_execute_optimization_real_success(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as db:
        dataset_id = seed_eval_dataset(db)
    sid = _new_session(svc, session_factory)
    plan = svc.create_intent_plan(
        sid,
        IntentPlanRequest(
            intent_name="run_prompt_optimization",
            slot_overrides={
                "agent_id": "support-agent",
                "eval_dataset_id": dataset_id,
                "optimizer_type": "MetaPrompt",
                "scorers": [{"name": "Correctness", "weight": 1.0, "config": {}}],
                "gate": {"min_aggregate_score": 0.7, "max_regression_delta": 0.05},
            },
        ),
        session_factory=session_factory,
        user=USER,
    )
    executed = svc.execute_intent_plan(
        sid,
        IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
        session_factory=session_factory,
        user=USER,
    )
    assert executed.status == "completed"
    assert executed.result["result_type"] == "optimization_run"
    job_id = executed.result["ids"]["job_id"]
    assert job_id
    with session_factory() as db:
        assert db.get(CaliberRefinementJob, job_id) is not None


def test_execute_edit_prompt(
    svc: AssistantService, session_factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    from caliber.routes import prompts as prompt_routes

    captured: dict[str, object] = {}

    def _fake(**kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"name": kwargs["name"], "version": 9, "uri": "prompts:/x/9", "alias_changed": False}

    monkeypatch.setattr(prompt_routes, "register_prompt_version", _fake)
    sid = _new_session(svc, session_factory)
    plan = svc.create_intent_plan(
        sid,
        IntentPlanRequest(
            intent_name="edit_prompt",
            slot_overrides={"prompt_name": "support-agent", "template": "Be strict."},
        ),
        session_factory=session_factory,
        user=USER,
    )
    executed = svc.execute_intent_plan(
        sid,
        IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
        session_factory=session_factory,
        user=USER,
    )
    assert executed.executed_action == "register_prompt_version"
    assert executed.result["result_type"] == "prompt_version"
    assert captured["commit_message"] == "edited via CALIBER assistant"


# ---------------------------------------------------------------------------
# Intent classification + slot extraction (resolve_intent with free text)
# ---------------------------------------------------------------------------


def test_resolve_intent_default_fallback(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(content="hello there friend"),
        session_factory=session_factory,
        user=USER,
    )
    assert resolved.intent.name == "create_prompt"
    assert resolved.intent.confidence <= 0.34


def test_resolve_create_prompt_new_prompt(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(
            content="create a new prompt named greeter with template: Hello {{n}}"
        ),
        session_factory=session_factory,
        user=USER,
    )
    assert resolved.intent.name == "create_prompt"
    slots = {s.name: s.value for s in resolved.slots}
    assert slots.get("prompt_name") == "greeter"
    assert "Hello" in str(slots.get("template"))


def test_resolve_edit_prompt(svc: AssistantService, session_factory: sessionmaker[Session]) -> None:
    sid = _new_session(svc, session_factory)
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(
            content="rewrite the prompt support-agent to be stricter\ntemplate: Be very strict.\ncommit message: tighten"
        ),
        session_factory=session_factory,
        user=USER,
    )
    assert resolved.intent.name == "edit_prompt"
    slots = {s.name: s.value for s in resolved.slots}
    assert slots.get("commit_message") == "tighten"


def test_resolve_create_tool_from_text(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    content = "build a tool named double_tool with callable double_tool\nsource: def double_tool(x):\n    return x"
    resolved = svc.resolve_intent(
        sid, IntentResolveRequest(content=content), session_factory=session_factory, user=USER
    )
    assert resolved.intent.name == "create_tool"
    slots = {s.name: s.value for s in resolved.slots}
    assert slots.get("tool_name") == "double_tool"
    assert "def double_tool" in str(slots.get("source"))
    assert slots.get("callable_name") == "double_tool"


def test_resolve_create_tool_from_context(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(
            content="create a tool please",
            context={
                "tool_name": "t1",
                "source": "def t1():\n    return 1",
                "callable_name": "t1",
                "tests": [{"name": "a", "input": {}, "expected": {}}],
                "description": "does a thing",
                "input_schema": {"type": "object"},
            },
        ),
        session_factory=session_factory,
        user=USER,
    )
    slots = {s.name: s.value for s in resolved.slots}
    assert slots["tool_name"] == "t1"
    assert slots["callable_name"] == "t1"
    assert slots["tests"]
    assert slots["description"] == "does a thing"


def test_resolve_create_tool_name_and_function_context(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(
            content="create a tool",
            context={"name": "named_tool", "function_name": "fn_name"},
        ),
        session_factory=session_factory,
        user=USER,
    )
    slots = {s.name: s.value for s in resolved.slots}
    assert slots["tool_name"] == "named_tool"
    assert slots["callable_name"] == "fn_name"


def test_resolve_create_skill_from_text(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    content = (
        "create a skill named support-triage as a skill package\n"
        "description: Triage tickets fast.\ncontent: Follow the runbook."
    )
    resolved = svc.resolve_intent(
        sid, IntentResolveRequest(content=content), session_factory=session_factory, user=USER
    )
    assert resolved.intent.name == "create_skill"
    slots = {s.name: s.value for s in resolved.slots}
    assert slots.get("skill_name") == "support-triage"
    assert slots.get("description") == "Triage tickets fast."
    assert slots.get("content") == "Follow the runbook."


def test_resolve_create_skill_from_context(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(
            content="build a skill",
            context={
                "skill_name": "s1",
                "description": "D",
                "content": "C",
                "summary": "sm",
                "category": "custom",
                "tags": ["x"],
            },
        ),
        session_factory=session_factory,
        user=USER,
    )
    slots = {s.name: s.value for s in resolved.slots}
    assert slots["skill_name"] == "s1"
    assert slots["summary"] == "sm"


def test_resolve_create_workflow(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(
            content="draft a workflow named support-routing with a workflow manifest"
        ),
        session_factory=session_factory,
        user=USER,
    )
    assert resolved.intent.name == "create_workflow"
    slots = {s.name: s.value for s in resolved.slots}
    assert str(slots.get("workflow_name")).startswith("support-routing")


def test_resolve_create_workflow_from_context(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(
            content="create a workflow",
            context={
                "workflow_name": "wf1",
                "manifest": {"schema_version": 1},
                "description": "desc",
            },
        ),
        session_factory=session_factory,
        user=USER,
    )
    slots = {s.name: s.value for s in resolved.slots}
    assert slots["workflow_name"] == "wf1"
    assert slots["manifest"] == {"schema_version": 1}


def test_resolve_create_mcp_transport_variants(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    stdio = svc.resolve_intent(
        sid,
        IntentResolveRequest(content="set up an MCP server named filesystem over stdio"),
        session_factory=session_factory,
        user=USER,
    )
    assert stdio.intent.name == "create_mcp_server"
    assert {s.name: s.value for s in stdio.slots}.get("transport") == "stdio"

    sse = svc.resolve_intent(
        sid,
        IntentResolveRequest(content="configure an mcp server named events using sse"),
        session_factory=session_factory,
        user=USER,
    )
    assert {s.name: s.value for s in sse.slots}.get("transport") == "sse"

    http = svc.resolve_intent(
        sid,
        IntentResolveRequest(content="register an mcp server named api over streamable-http"),
        session_factory=session_factory,
        user=USER,
    )
    assert {s.name: s.value for s in http.slots}.get("transport") == "streamable-http"


def test_resolve_create_mcp_from_context(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(
            content="create an mcp server",
            context={
                "server_name": "srv",
                "transport": "sse",
                "uri": "https://example.com",
                "description": "d",
            },
        ),
        session_factory=session_factory,
        user=USER,
    )
    slots = {s.name: s.value for s in resolved.slots}
    assert slots["server_name"] == "srv"
    assert slots["transport"] == "sse"
    assert slots["uri"] == "https://example.com"


def test_resolve_run_workflow_calibration_from_text(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(
            content="calibrate workflow WF-1 with objective tool adherence epsilon 0.1 max candidates 3"
        ),
        session_factory=session_factory,
        user=USER,
    )
    assert resolved.intent.name == "run_workflow_calibration"
    slots = {s.name: s.value for s in resolved.slots}
    assert slots.get("workflow_id") == "WF-1"
    assert slots.get("objective") == "tool_adherence"
    assert slots.get("epsilon") == 0.1
    assert slots.get("max_candidates") == 3


def test_resolve_run_workflow_calibration_from_context(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(
            content="calibrate this workflow",
            context={
                "workflow_id": "WF-9",
                "agent_id": "a",
                "objective": {"maximize": "quality", "epsilon": 0.05},
                "budget": {"max_candidates": 2},
            },
        ),
        session_factory=session_factory,
        user=USER,
    )
    slots = {s.name: s.value for s in resolved.slots}
    assert slots.get("workflow_id") == "WF-9"
    assert slots.get("agent_id") == "a"
    assert slots.get("objective") == "quality"
    assert slots.get("epsilon") == 0.05
    assert slots.get("max_candidates") == 2


def test_resolve_run_prompt_optimization_from_text(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(
            content="optimize prompt support-agent using dataset EDS-qa1 with faithfulness and correctness scorers via gepa"
        ),
        session_factory=session_factory,
        user=USER,
    )
    assert resolved.intent.name == "run_prompt_optimization"
    slots = {s.name: s.value for s in resolved.slots}
    assert slots.get("eval_dataset_id") == "EDS-qa1"
    assert slots.get("optimizer_type") == "GEPA"
    scorer_names = {s["name"] for s in slots["scorers"]}
    assert "DeepEval.Faithfulness" in scorer_names
    assert "Correctness" in scorer_names


def test_resolve_run_prompt_optimization_from_context(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(
            content="optimize this prompt",
            context={
                "agent_id": "a",
                "eval_dataset_id": "ED-1",
                "optimizer_type": "MetaPrompt",
                "scorers": [{"name": "Correctness", "weight": 2.0}, "Safety"],
                "gate": {"min_aggregate_score": 0.7, "max_regression_delta": 0.05},
            },
        ),
        session_factory=session_factory,
        user=USER,
    )
    slots = {s.name: s.value for s in resolved.slots}
    assert slots.get("agent_id") == "a"
    assert slots.get("eval_dataset_id") == "ED-1"
    assert slots.get("optimizer_type") == "MetaPrompt"
    assert slots.get("gate.min_aggregate_score") == 0.7
    scorer_names = {s["name"] for s in slots["scorers"]}
    assert {"Correctness", "Safety"} <= scorer_names


def test_resolve_save_eval_dataset(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(content="save the dataset named qa_eval_set"),
        session_factory=session_factory,
        user=USER,
    )
    assert resolved.intent.name == "save_eval_dataset"
    slots = {s.name: s.value for s in resolved.slots}
    assert slots.get("dataset_name") == "qa_eval_set"


def test_resolve_review_optimization(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(content="review the optimization result for job RFN-abc123"),
        session_factory=session_factory,
        user=USER,
    )
    assert resolved.intent.name == "review_optimization_result"
    assert {s.name: s.value for s in resolved.slots}.get("job_id") == "RFN-abc123"


def test_resolve_review_workflow_calibration(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(content="review the workflow candidate winner for job RFN-wf01"),
        session_factory=session_factory,
        user=USER,
    )
    assert resolved.intent.name == "review_workflow_calibration_result"
    assert {s.name: s.value for s in resolved.slots}.get("job_id") == "RFN-wf01"


def test_resolve_propose_promotion(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(content="promote prompt support-agent to prod version 5"),
        session_factory=session_factory,
        user=USER,
    )
    assert resolved.intent.name == "propose_promotion"
    slots = {s.name: s.value for s in resolved.slots}
    assert slots.get("target_alias") == "prod"
    assert slots.get("source_version") == 5
    assert slots.get("prompt_name") == "support-agent"


def test_resolve_prompt_name_from_context(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(content="generate test cases", context={"prompt_name": "ctx-prompt"}),
        session_factory=session_factory,
        user=USER,
    )
    assert {s.name: s.value for s in resolved.slots}.get("prompt_name") == "ctx-prompt"


def test_resolve_prompt_name_from_agent_id_context(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(content="generate test cases", context={"agent_id": "agent-x"}),
        session_factory=session_factory,
        user=USER,
    )
    assert {s.name: s.value for s in resolved.slots}.get("prompt_name") == "agent-x"


def test_resolve_prompt_name_from_session_metadata(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = svc.create_session(
        SessionCreateRequest(title="s", metadata_={"prompt_name": "meta-prompt"}),
        session_factory=session_factory,
        user=USER,
    ).session_id
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(content="generate test cases"),
        session_factory=session_factory,
        user=USER,
    )
    assert {s.name: s.value for s in resolved.slots}.get("prompt_name") == "meta-prompt"


def test_resolve_prompt_name_from_prompt_ref(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = svc.create_session(
        SessionCreateRequest(title="s", metadata_={"prompt_ref": "prompts:/ref-prompt@prod"}),
        session_factory=session_factory,
        user=USER,
    ).session_id
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(content="generate test cases"),
        session_factory=session_factory,
        user=USER,
    )
    assert {s.name: s.value for s in resolved.slots}.get("prompt_name") == "ref-prompt"


def test_save_eval_dataset_plan_reuses_generated(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    gplan = svc.create_intent_plan(
        sid,
        IntentPlanRequest(intent_name="generate_test_cases", slot_overrides={"prompt_name": "p1"}),
        session_factory=session_factory,
        user=USER,
    )
    gexec = svc.execute_intent_plan(
        sid,
        IntentExecuteRequest(plan_id=gplan.plan_id, confirm=True),
        session_factory=session_factory,
        user=USER,
    )
    assert gexec.result["result_type"] == "test_cases"

    splan = svc.create_intent_plan(
        sid,
        IntentPlanRequest(intent_name="save_eval_dataset"),
        session_factory=session_factory,
        user=USER,
    )
    slots = {s.name: s.value for s in splan.slots}
    assert slots.get("examples")
    assert slots.get("dataset_name") == "p1-generated-tests"


# ---------------------------------------------------------------------------
# Direct _execute_* adapter validation branches
# ---------------------------------------------------------------------------


def test_execute_create_tool_errors(svc: AssistantService) -> None:
    with pytest.raises(ValueError, match="tool_name"):
        svc._execute_create_tool(
            _plan("create_tool", {"tool_name": "  ", "source": "s", "callable_name": "c"}),
            session_id="S",
            session_factory=None,
            user=USER,
        )
    with pytest.raises(ValueError, match="source"):
        svc._execute_create_tool(
            _plan("create_tool", {"tool_name": "t", "source": "", "callable_name": "c"}),
            session_id="S",
            session_factory=None,
            user=USER,
        )
    with pytest.raises(ValueError, match="callable_name"):
        svc._execute_create_tool(
            _plan("create_tool", {"tool_name": "t", "source": "s", "callable_name": ""}),
            session_id="S",
            session_factory=None,
            user=USER,
        )


def test_coerce_tool_tests(svc: AssistantService) -> None:
    with pytest.raises(ValueError, match="at least one"):
        svc._coerce_tool_tests([])
    with pytest.raises(ValueError, match="at least one"):
        svc._coerce_tool_tests("notalist")
    with pytest.raises(ValueError, match="must be an object"):
        svc._coerce_tool_tests(["notadict"])
    good = svc._coerce_tool_tests([{"name": "n", "input": {}, "expected": {}}])
    assert len(good) == 1


def test_run_tool_sandbox_missing_fields(svc: AssistantService) -> None:
    no_source = svc._run_tool_sandbox_tests(
        {"tests": [{"name": "n", "input": {}, "expected": {}}], "source": "", "callable_name": "c"}
    )
    assert no_source.passed is False
    assert "source" in (no_source.error or "")

    no_callable = svc._run_tool_sandbox_tests(
        {
            "tests": [{"name": "n", "input": {}, "expected": {}}],
            "source": "def f(): pass",
            "callable_name": "",
        }
    )
    assert no_callable.passed is False
    assert "callable_name" in (no_callable.error or "")


def test_tool_sandbox_report_forces_failure_on_timeout(svc: AssistantService) -> None:
    result = ToolSandboxTestSuiteResult(
        status="timed_out", tests=[], error="timeout", duration_ms=1
    )
    report = svc._tool_sandbox_report(result)
    assert report.passed is False
    assert report.failures == 1


def test_execute_create_skill_errors(svc: AssistantService) -> None:
    with pytest.raises(ValueError, match="skill_name"):
        svc._execute_create_skill(
            _plan("create_skill", {"skill_name": "", "description": "d", "content": "c"}),
            session_id="S",
            session_factory=None,
            user=USER,
        )
    with pytest.raises(ValueError, match="description"):
        svc._execute_create_skill(
            _plan("create_skill", {"skill_name": "s", "description": "", "content": "c"}),
            session_id="S",
            session_factory=None,
            user=USER,
        )
    with pytest.raises(ValueError, match="content"):
        svc._execute_create_skill(
            _plan("create_skill", {"skill_name": "s", "description": "d", "content": ""}),
            session_id="S",
            session_factory=None,
            user=USER,
        )


def test_execute_create_skill_success(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    result = svc._execute_create_skill(
        _plan(
            "create_skill",
            {
                "skill_name": "my-skill",
                "description": "A helpful skill.",
                "content": "Do the thing carefully.",
            },
        ),
        session_id=sid,
        session_factory=session_factory,
        user=USER,
    )
    assert result["result_type"] == "skill_draft"
    assert result["ids"]["draft_id"]
    with session_factory() as db:
        assert db.get(CaliberAssistantDraft, result["ids"]["draft_id"]) is not None


def test_execute_create_skill_invalid_schema(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    result = svc._execute_create_skill(
        _plan("create_skill", {"skill_name": "Bad Name!!", "description": "d", "content": "c"}),
        session_id=sid,
        session_factory=session_factory,
        user=USER,
    )
    assert result["result_type"] == "skill_draft"
    assert result["status"] == "blocked"
    assert result["validation_report"]["valid"] is False


def test_run_skill_package_tests_bad_schema(svc: AssistantService) -> None:
    report = svc._run_skill_package_tests({})
    assert report.passed is False
    assert report.error


def test_execute_create_workflow_errors(svc: AssistantService) -> None:
    with pytest.raises(ValueError, match="workflow_name"):
        svc._execute_create_workflow(
            _plan("create_workflow", {"workflow_name": "", "manifest": {}}),
            session_id="S",
            session_factory=None,
            user=USER,
        )
    with pytest.raises(ValueError, match="manifest"):
        svc._execute_create_workflow(
            _plan("create_workflow", {"workflow_name": "w", "manifest": "notadict"}),
            session_id="S",
            session_factory=None,
            user=USER,
        )


def test_execute_create_workflow_invalid_manifest(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    result = svc._execute_create_workflow(
        _plan("create_workflow", {"workflow_name": "My WF", "manifest": {"foo": "bar"}}),
        session_id=sid,
        session_factory=session_factory,
        user=USER,
    )
    assert result["result_type"] == "workflow_draft"
    assert result["validation_report"]["valid"] is False


def test_run_workflow_compile_tests_manifest_not_dict(svc: AssistantService) -> None:
    report = svc._run_workflow_compile_tests({"manifest": "nope"})
    assert report.passed is False
    assert "structured object" in (report.error or "")


def test_execute_create_mcp_errors(svc: AssistantService) -> None:
    with pytest.raises(ValueError, match="server_name"):
        svc._execute_create_mcp_server(
            _plan("create_mcp_server", {"server_name": "", "transport": "stdio"}),
            session_id="S",
            session_factory=None,
            user=USER,
        )
    with pytest.raises(ValueError, match="transport"):
        svc._execute_create_mcp_server(
            _plan("create_mcp_server", {"server_name": "s", "transport": "  "}),
            session_id="S",
            session_factory=None,
            user=USER,
        )


def test_execute_create_mcp_invalid_schema(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    result = svc._execute_create_mcp_server(
        _plan("create_mcp_server", {"server_name": "srv", "transport": "badtransport"}),
        session_id=sid,
        session_factory=session_factory,
        user=USER,
    )
    assert result["result_type"] == "mcp_server_draft"
    assert result["validation_report"]["valid"] is False


def test_run_mcp_connection_tests(svc: AssistantService) -> None:
    stdio_missing = svc._run_mcp_connection_tests({"transport": "stdio", "command": ""})
    assert stdio_missing.passed is False
    assert "command" in (stdio_missing.error or "")

    http_missing = svc._run_mcp_connection_tests({"transport": "sse", "uri": ""})
    assert http_missing.passed is False
    assert "uri" in (http_missing.error or "")

    ok = svc._run_mcp_connection_tests({"transport": "stdio", "command": "echo"})
    assert ok.passed is True
    assert ok.details[0]["tool_count"] == 0


def test_execute_prompt_write_errors(svc: AssistantService) -> None:
    with pytest.raises(ValueError, match="prompt_name"):
        svc._execute_prompt_write(
            _plan("create_prompt", {"prompt_name": "", "template": "t"}), is_edit=False, user=USER
        )
    with pytest.raises(ValueError, match="template"):
        svc._execute_prompt_write(
            _plan("create_prompt", {"prompt_name": "p", "template": ""}), is_edit=False, user=USER
        )


def test_execute_prompt_optimization_errors(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    base = {
        "agent_id": "a",
        "eval_dataset_id": "d",
        "optimizer_type": "MetaPrompt",
        "scorers": [{"name": "Correctness"}],
    }
    with pytest.raises(ValueError, match="agent_id"):
        svc._execute_prompt_optimization(
            _plan("run_prompt_optimization", {**base, "agent_id": ""}),
            session_factory=session_factory,
            user=USER,
        )
    with pytest.raises(ValueError, match="eval_dataset_id"):
        svc._execute_prompt_optimization(
            _plan("run_prompt_optimization", {**base, "eval_dataset_id": ""}),
            session_factory=session_factory,
            user=USER,
        )
    with pytest.raises(ValueError, match="optimizer_type"):
        svc._execute_prompt_optimization(
            _plan("run_prompt_optimization", {**base, "optimizer_type": ""}),
            session_factory=session_factory,
            user=USER,
        )
    with pytest.raises(ValueError, match="at least one scorer"):
        svc._execute_prompt_optimization(
            _plan("run_prompt_optimization", {**base, "scorers": []}),
            session_factory=session_factory,
            user=USER,
        )


def test_execute_generate_test_cases_error(svc: AssistantService) -> None:
    with pytest.raises(ValueError, match="prompt_name"):
        svc._execute_generate_test_cases(_plan("generate_test_cases", {"prompt_name": ""}))


def test_parse_test_cases_variants() -> None:
    fenced = '```json\n[{"input":"q1","expected":"be helpful","tags":["a"]}]\n```'
    cases = AssistantService._parse_test_cases(fenced)
    assert cases[0]["input"] == {"query": "q1"}
    assert cases[0]["expected"] == {"behavior": "be helpful"}

    defaults = AssistantService._parse_test_cases('[{"input":{"query":"x"},"expected":"do y"}]')
    assert defaults[0]["expected"] == {"behavior": "do y"}
    assert defaults[0]["tags"] == ["generated"]

    assert AssistantService._parse_test_cases("") == []
    assert AssistantService._parse_test_cases('["notadict"]') == []
    assert AssistantService._parse_test_cases('[{"expected":{"b":1}}]') == []

    prose = 'Here you go: [{"input":{"query":"x"},"expected":{"behavior":"y"}}] all done'
    from_prose = AssistantService._parse_test_cases(prose)
    assert from_prose[0]["input"] == {"query": "x"}


def test_execute_save_eval_dataset_errors_and_dup(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    with pytest.raises(ValueError, match="dataset_name"):
        svc._execute_save_eval_dataset(
            _plan("save_eval_dataset", {"dataset_name": "", "examples": [{}]}),
            session_factory=session_factory,
            user=USER,
        )
    with pytest.raises(ValueError, match="at least one example"):
        svc._execute_save_eval_dataset(
            _plan("save_eval_dataset", {"dataset_name": "n", "examples": []}),
            session_factory=session_factory,
            user=USER,
        )
    with pytest.raises(ValueError, match="must be an object"):
        svc._execute_save_eval_dataset(
            _plan("save_eval_dataset", {"dataset_name": "n2", "examples": ["notadict"]}),
            session_factory=session_factory,
            user=USER,
        )

    examples = [{"input": {"query": "x"}, "expected": {"behavior": "y"}}]
    result = svc._execute_save_eval_dataset(
        _plan("save_eval_dataset", {"dataset_name": "cov-ds", "examples": examples}),
        session_factory=session_factory,
        user=USER,
    )
    assert result["result_type"] == "eval_dataset"
    assert result["ids"]["example_ids"]
    with pytest.raises(ValueError, match="already in use"):
        svc._execute_save_eval_dataset(
            _plan("save_eval_dataset", {"dataset_name": "cov-ds", "examples": examples}),
            session_factory=session_factory,
            user=USER,
        )


def test_execute_review_optimization_errors(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    with pytest.raises(ValueError, match="job_id"):
        svc._execute_review_optimization_result(
            _plan("review_optimization_result", {"job_id": ""}), session_factory=session_factory
        )
    with pytest.raises(ValueError, match="not found"):
        svc._execute_review_optimization_result(
            _plan("review_optimization_result", {"job_id": "RFN-none"}),
            session_factory=session_factory,
        )


def test_execute_review_workflow_errors_and_eval_results(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    with pytest.raises(ValueError, match="job_id"):
        svc._execute_review_workflow_calibration_result(
            _plan("review_workflow_calibration_result", {"job_id": ""}),
            session_factory=session_factory,
        )
    with pytest.raises(ValueError, match="not found"):
        svc._execute_review_workflow_calibration_result(
            _plan("review_workflow_calibration_result", {"job_id": "RFN-none"}),
            session_factory=session_factory,
        )

    with session_factory() as db:
        db.add(
            CaliberRefinementJob(
                job_id="RFN-evalcands",
                agent_id="a",
                primary_item_id="FB-1",
                artifact_type="workflow_manifest",
                status="completed",
                current_stage="done",
                candidate={},
                eval_results={
                    "calibration_candidates": [
                        {"candidate_id": "c1", "accepted": True, "scores": {"q": 0.9}}
                    ],
                    "calibration_winner_id": "c1",
                },
            )
        )
        db.commit()
    result = svc._execute_review_workflow_calibration_result(
        _plan("review_workflow_calibration_result", {"job_id": "RFN-evalcands"}),
        session_factory=session_factory,
    )
    assert result["ids"]["winner_id"] == "c1"
    assert result["score_table"][0]["candidate_id"] == "c1"


def test_execute_review_workflow_no_candidates(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as db:
        db.add(
            CaliberRefinementJob(
                job_id="RFN-empty",
                agent_id="a",
                primary_item_id="FB-2",
                artifact_type="workflow_manifest",
                status="completed",
                current_stage="done",
                candidate={},
                eval_results={},
            )
        )
        db.commit()
    result = svc._execute_review_workflow_calibration_result(
        _plan("review_workflow_calibration_result", {"job_id": "RFN-empty"}),
        session_factory=session_factory,
    )
    assert result["score_table"] == []


# ---------------------------------------------------------------------------
# propose_promotion adapter
# ---------------------------------------------------------------------------


def _promotion_kwargs(sf: sessionmaker[Session], sid: str) -> dict[str, object]:
    return {
        "session_id": sid,
        "operation_id": "OP-1",
        "trace_id": "T-1",
        "correlation_id": "C-1",
        "session_factory": sf,
        "user": USER,
    }


def test_execute_propose_promotion_errors(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    with pytest.raises(ValueError, match="prompt_name"):
        svc._execute_propose_promotion(
            _plan(
                "propose_promotion",
                {"prompt_name": "", "target_alias": "prod", "source_version": 1},
            ),
            **_promotion_kwargs(session_factory, "S"),
        )
    with pytest.raises(ValueError, match="target_alias"):
        svc._execute_propose_promotion(
            _plan(
                "propose_promotion", {"prompt_name": "p", "target_alias": "", "source_version": 1}
            ),
            **_promotion_kwargs(session_factory, "S"),
        )
    with pytest.raises(ValueError, match="integer"):
        svc._execute_propose_promotion(
            _plan(
                "propose_promotion",
                {"prompt_name": "p", "target_alias": "prod", "source_version": "notint"},
            ),
            **_promotion_kwargs(session_factory, "S"),
        )
    with pytest.raises(ValueError, match="positive"):
        svc._execute_propose_promotion(
            _plan(
                "propose_promotion",
                {"prompt_name": "p", "target_alias": "prod", "source_version": 0},
            ),
            **_promotion_kwargs(session_factory, "S"),
        )


def test_propose_promotion_needs_source_version(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    result = svc._execute_propose_promotion(
        _plan("propose_promotion", {"prompt_name": "p", "target_alias": "prod"}),
        **_promotion_kwargs(session_factory, "S"),
    )
    assert result["status"] == "blocked"
    assert "source_version" in result["warnings"][0]


def test_propose_promotion_agent_missing(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    result = svc._execute_propose_promotion(
        _plan(
            "propose_promotion",
            {"prompt_name": "no-agent", "target_alias": "prod", "source_version": 2},
        ),
        **_promotion_kwargs(session_factory, "S"),
    )
    assert result["status"] == "blocked"
    assert "was not found" in result["warnings"][0]


def test_propose_promotion_creates_and_reuses(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as db:
        db.add(
            CaliberAgentConfig(
                agent_id="promo-agent", experiment_id="exp-promo", name="Promo", owner=USER
            )
        )
        db.commit()
    sid = _new_session(svc, session_factory)
    r1 = svc._execute_propose_promotion(
        _plan(
            "propose_promotion",
            {"prompt_name": "promo-agent", "target_alias": "prod", "source_version": 3},
        ),
        **_promotion_kwargs(session_factory, sid),
    )
    assert r1["result_type"] == "promotion_proposal"
    approval_id = r1["ids"]["approval_id"]
    assert approval_id
    r2 = svc._execute_propose_promotion(
        _plan(
            "propose_promotion",
            {"prompt_name": "promo-agent", "target_alias": "prod", "source_version": 3},
        ),
        **_promotion_kwargs(session_factory, sid),
    )
    assert r2["ids"]["approval_id"] == approval_id


# ---------------------------------------------------------------------------
# Workflow calibration adapter (defaults + guard rails)
# ---------------------------------------------------------------------------


def _seed_wf_calibration(sf: sessionmaker[Session]) -> None:
    with sf() as db:
        db.add(
            CaliberAgentConfig(
                agent_id="wf-agent",
                experiment_id="exp-wf",
                name="WF Agent",
                owner=USER,
                enabled=True,
            )
        )
        db.add(CaliberWorkflow(workflow_id="WF-CAL", name="Cal WF", owner=USER))
        db.add(
            CaliberWorkflowVersion(
                version_id="WFV-CAL",
                workflow_id="WF-CAL",
                version_number=1,
                status="published",
                manifest=make_support_manifest(
                    "WF-CAL",
                    deploy_gates={
                        "support_eval_gate": {
                            "type": "deploy_gate",
                            "dataset_ref": "support_eval",
                            "required_for_aliases": ["dev"],
                            "thresholds": {},
                        }
                    },
                ),
                manifest_hash="hash-wfv-cal",
            )
        )
        db.commit()
        seed_eval_dataset(db)


def test_execute_workflow_calibration_no_workflow_id(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    with pytest.raises(ValueError, match="workflow_id"):
        svc._execute_workflow_calibration(
            _plan("run_workflow_calibration", {"workflow_id": ""}),
            session_factory=session_factory,
            user=USER,
        )


def test_execute_workflow_calibration_no_enabled_agent(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    with pytest.raises(ValueError, match="No enabled agent"):
        svc._execute_workflow_calibration(
            _plan("run_workflow_calibration", {"workflow_id": "WF-X"}),
            session_factory=session_factory,
            user=USER,
        )


def test_execute_workflow_calibration_defaults(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    _seed_wf_calibration(session_factory)
    plan = _plan(
        "run_workflow_calibration",
        {
            "workflow_id": "WF-CAL",
            "objective": "bogus_objective",
            "epsilon": "notnum",
            "max_candidates": "notnum",
        },
    )
    result = svc._execute_workflow_calibration(plan, session_factory=session_factory, user=USER)
    assert result["result_type"] == "workflow_calibration_run"
    assert result["ids"]["job_id"]
    assert any("Defaulted agent_id" in w for w in result["warnings"])
    assert any("quality" in w for w in result["warnings"])
    with session_factory() as db:
        job = db.get(CaliberRefinementJob, result["ids"]["job_id"])
        assert job is not None
        assert job.calibration_spec["objective"]["maximize"] == "quality"


# ---------------------------------------------------------------------------
# Attachments + library resource resolution
# ---------------------------------------------------------------------------


def test_resolve_library_prompt(session_factory: sessionmaker[Session]) -> None:
    svc = AssistantService(engine=FakeAssistantEngine(), prompt_fetcher=lambda n: "TEMPLATE-BODY")
    sid = _new_session(svc, session_factory)
    att = svc.add_library_attachment(
        sid, resource_type="prompt", resource_id="p1", session_factory=session_factory, user=USER
    )
    assert "TEMPLATE-BODY" in att.content_text

    svc2 = AssistantService(engine=FakeAssistantEngine(), prompt_fetcher=lambda n: None)
    att2 = svc2.add_library_attachment(
        sid, resource_type="prompt", resource_id="p2", session_factory=session_factory, user=USER
    )
    assert "Prompt reference: p2" in att2.content_text


def test_resolve_library_all_types(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    with session_factory() as db:
        db.add(
            CaliberSkill(
                skill_id="SK-1",
                name="s1",
                description="d",
                summary="sum",
                content="body",
                owner=USER,
                category="custom",
                status="active",
                version=1,
            )
        )
        db.add(
            CaliberToolRegistry(
                tool_id="TL-1",
                name="t1",
                version="1.0.0",
                description="desc",
                module_path="m",
                callable_name="f",
                input_schema={"type": "object"},
            )
        )
        db.add(CaliberWorkflow(workflow_id="WF-A", name="wfa", description="wd", owner=USER))
        db.add(
            CaliberKnowledgeBase(
                knowledge_base_id="KB-1", name="kb1", description="kd", source_bucket="bkt"
            )
        )
        db.commit()

    assert (
        "Skill: s1"
        in svc.add_library_attachment(
            sid,
            resource_type="skill",
            resource_id="SK-1",
            session_factory=session_factory,
            user=USER,
        ).content_text
    )
    assert (
        "Tool: t1"
        in svc.add_library_attachment(
            sid,
            resource_type="tool",
            resource_id="TL-1",
            session_factory=session_factory,
            user=USER,
        ).content_text
    )
    assert (
        "Workflow: wfa"
        in svc.add_library_attachment(
            sid,
            resource_type="workflow",
            resource_id="WF-A",
            session_factory=session_factory,
            user=USER,
        ).content_text
    )
    assert (
        "Knowledge base: kb1"
        in svc.add_library_attachment(
            sid,
            resource_type="knowledge_base",
            resource_id="KB-1",
            session_factory=session_factory,
            user=USER,
        ).content_text
    )


def test_resolve_library_not_found_and_unsupported(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    for resource_type in ("skill", "tool", "workflow", "knowledge_base"):
        with pytest.raises(ValueError, match="not found"):
            svc.add_library_attachment(
                sid,
                resource_type=resource_type,
                resource_id="MISSING",
                session_factory=session_factory,
                user=USER,
            )
    with pytest.raises(ValueError, match="Unsupported"):
        svc._resolve_library_resource("bogus", "x", session_factory=session_factory)


def test_attachment_helpers(svc: AssistantService, session_factory: sessionmaker[Session]) -> None:
    assert svc.list_attachments("ASST-none", session_factory=session_factory) == []
    sid = _new_session(svc, session_factory)
    with pytest.raises(ValueError, match="empty"):
        svc.add_text_attachment(
            sid, name="n", text="   ", session_factory=session_factory, user=USER
        )
    with pytest.raises(ValueError, match="not found"):
        svc.create_attachment_record(
            "ASST-none",
            kind="text_snippet",
            session_factory=session_factory,
            user=USER,
            content_text="x",
        )
    att = svc.add_text_attachment(
        sid, name="note", text="hello", session_factory=session_factory, user=USER
    )
    assert att.content_text == "hello"
    assert (
        svc.delete_attachment(att.attachment_id, session_factory=session_factory, user=USER) is True
    )
    assert svc.delete_attachment("AATT-none", session_factory=session_factory, user=USER) is False


# ---------------------------------------------------------------------------
# Queue + messages
# ---------------------------------------------------------------------------


def test_queue_lifecycle(svc: AssistantService, session_factory: sessionmaker[Session]) -> None:
    assert svc.list_queue("ASST-none", session_factory=session_factory) == []
    sid = _new_session(svc, session_factory)
    with pytest.raises(ValueError, match="empty"):
        svc.enqueue_message(sid, content="  ", session_factory=session_factory, user=USER)
    with pytest.raises(ValueError, match="kind"):
        svc.enqueue_message(
            sid, content="x", session_factory=session_factory, user=USER, kind="bogus"
        )
    with pytest.raises(ValueError, match="not found"):
        svc.enqueue_message("ASST-none", content="x", session_factory=session_factory, user=USER)

    q1 = svc.enqueue_message(sid, content="first", session_factory=session_factory, user=USER)
    svc.enqueue_message(
        sid, content="steer me", session_factory=session_factory, user=USER, kind="steer"
    )
    queue = svc.list_queue(sid, session_factory=session_factory, user=USER)
    assert queue[0].content == "steer me"
    assert svc.cancel_queued(q1.queue_id, session_factory=session_factory, user=USER) is True
    assert svc.cancel_queued("AQMSG-none", session_factory=session_factory, user=USER) is False


def test_list_messages_session_none(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    assert svc.list_messages("ASST-none", session_factory=session_factory) == []


# ---------------------------------------------------------------------------
# draft_prompt_from_description
# ---------------------------------------------------------------------------


def test_draft_prompt_engine_raises() -> None:
    class _Boom:
        def run_turn(
            self, request: object, *, toolset: object | None = None
        ) -> AssistantTurnResult:
            raise RuntimeError("boom")

    svc = AssistantService(engine=_Boom())  # type: ignore[arg-type]
    result = svc.draft_prompt_from_description("do a thing")
    assert result["template"] == ""


def test_draft_prompt_ignores_non_prompt_delta() -> None:
    class _ToolEngine:
        def run_turn(
            self, request: object, *, toolset: object | None = None
        ) -> AssistantTurnResult:
            return AssistantTurnResult(
                reply="ok",
                draft_deltas=[DraftDelta(artifact_type="tool", artifact={"template": "x"})],
                done=True,
            )

    svc = AssistantService(engine=_ToolEngine())  # type: ignore[arg-type]
    result = svc.draft_prompt_from_description("do a thing")
    assert result["template"] == ""


# ---------------------------------------------------------------------------
# send_message: limits, skill-off, timeout, engine-error, draft update paths
# ---------------------------------------------------------------------------


def test_send_message_turn_limit(session_factory: sessionmaker[Session]) -> None:
    svc = AssistantService(
        engine=FakeAssistantEngine(), settings=AssistantRuntimeSettings(max_turns=1)
    )
    sid = _new_session(svc, session_factory)
    svc.send_message(
        sid, MessageSendRequest(content="one"), session_factory=session_factory, user=USER
    )
    with pytest.raises(ValueError, match="turn limit"):
        svc.send_message(
            sid, MessageSendRequest(content="two"), session_factory=session_factory, user=USER
        )


def test_send_message_skill_runtime_disabled(session_factory: sessionmaker[Session]) -> None:
    svc = AssistantService(
        engine=FakeAssistantEngine(), settings=AssistantRuntimeSettings(skill_runtime_enabled=False)
    )
    sid = _new_session(svc, session_factory)
    turn = svc.send_message(
        sid, MessageSendRequest(content="hi"), session_factory=session_factory, user=USER
    )
    assert turn.assistant_message.metadata_["skill_runtime_mode"] == "off"


def test_send_message_timeout(session_factory: sessionmaker[Session]) -> None:
    class _SlowEngine:
        def run_turn(
            self, request: object, *, toolset: object | None = None
        ) -> AssistantTurnResult:
            time.sleep(0.5)
            return AssistantTurnResult(reply="late")

    svc = AssistantService(
        engine=_SlowEngine(), settings=AssistantRuntimeSettings(run_timeout_seconds=0.05)
    )  # type: ignore[arg-type]
    sid = _new_session(svc, session_factory)
    turn = svc.send_message(
        sid, MessageSendRequest(content="hi"), session_factory=session_factory, user=USER
    )
    assert turn.run is not None
    assert turn.run.status == "failed"
    assert "error occurred" in turn.assistant_message.content.lower()
    with session_factory() as db:
        run = db.get(_RunRow, turn.run.run_id)
        assert run is not None
        assert run.status == "failed"


def test_send_message_engine_returns_error(session_factory: sessionmaker[Session]) -> None:
    class _ErrEngine:
        def run_turn(
            self, request: object, *, toolset: object | None = None
        ) -> AssistantTurnResult:
            return AssistantTurnResult(reply="handled internally", error="something broke")

    svc = AssistantService(engine=_ErrEngine())  # type: ignore[arg-type]
    sid = _new_session(svc, session_factory)
    turn = svc.send_message(
        sid, MessageSendRequest(content="hi"), session_factory=session_factory, user=USER
    )
    assert turn.run is not None
    assert turn.run.status == "failed"
    assert turn.assistant_message.metadata_["error"] is True


def test_send_message_updates_existing_draft(session_factory: sessionmaker[Session]) -> None:
    seeded: dict[str, str] = {}

    class _UpdateEngine:
        def run_turn(
            self, request: object, *, toolset: object | None = None
        ) -> AssistantTurnResult:
            return AssistantTurnResult(
                reply="updated",
                draft_deltas=[
                    DraftDelta(
                        draft_id=seeded["id"],
                        title="new title",
                        summary="new summary",
                        spec={"k": "v"},
                        artifact={"a": "b"},
                    )
                ],
            )

    svc = AssistantService(engine=_UpdateEngine())  # type: ignore[arg-type]
    sid = _new_session(svc, session_factory)
    with session_factory() as db:
        draft = CaliberAssistantDraft(
            draft_id=new_assistant_draft_id(),
            session_id=sid,
            artifact_type="tool",
            status="draft",
            title="old",
            spec={},
            artifact={},
            created_by=USER,
            updated_by=USER,
        )
        db.add(draft)
        db.commit()
        seeded["id"] = draft.draft_id

    turn = svc.send_message(
        sid,
        MessageSendRequest(content="update it", mode="build", approval_mode="manual"),
        session_factory=session_factory,
        user=USER,
    )
    assert turn.draft_updates
    updated = turn.draft_updates[0]
    assert updated.draft_id == seeded["id"]
    assert updated.title == "new title"
    assert updated.version >= 2


def test_send_message_auto_advance(session_factory: sessionmaker[Session]) -> None:
    svc = AssistantService(engine=FakeAssistantEngine())
    sid = svc.create_session(
        SessionCreateRequest(title="auto", approval_mode="auto_all"),
        session_factory=session_factory,
        user=USER,
    ).session_id
    svc.send_message(
        sid, MessageSendRequest(content="create a tool"), session_factory=session_factory, user=USER
    )
    turn = svc.send_message(
        sid, MessageSendRequest(content="name it foo"), session_factory=session_factory, user=USER
    )
    assert turn.draft_updates
    assert turn.draft_updates[0].status != "draft"


# ---------------------------------------------------------------------------
# Drafts: cross-owner / not-found / update field branches
# ---------------------------------------------------------------------------


def test_draft_access_branches(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    other = "@other"
    sid = _new_session(svc, session_factory)
    with session_factory() as db:
        draft = CaliberAssistantDraft(
            draft_id=new_assistant_draft_id(),
            session_id=sid,
            artifact_type="tool",
            status="draft",
            title="t",
            spec={},
            artifact={"source": "def f(): pass"},
            created_by=USER,
            updated_by=USER,
        )
        db.add(draft)
        db.commit()
        did = draft.draft_id

    assert svc.list_drafts(sid, session_factory=session_factory, user=other) == []
    assert svc.get_draft(did, session_factory=session_factory, user=other) is None
    assert (
        svc.update_draft(
            "ADRF-none", DraftUpdateRequest(version=1), session_factory=session_factory, user=USER
        )
        is None
    )
    assert (
        svc.update_draft(
            did, DraftUpdateRequest(version=1), session_factory=session_factory, user=other
        )
        is None
    )

    updated = svc.update_draft(
        did,
        DraftUpdateRequest(version=1, summary="s", spec={"a": 1}, artifact={"b": 2}),
        session_factory=session_factory,
        user=USER,
    )
    assert updated is not None
    assert updated.summary == "s"
    assert updated.spec == {"a": 1}
    assert updated.artifact == {"b": 2}

    assert svc.validate_draft(did, session_factory=session_factory, user=other).valid is False
    assert svc.test_draft("ADRF-none", session_factory=session_factory).passed is False
    assert svc.test_draft(did, session_factory=session_factory, user=other).passed is False
    assert svc.approve_draft(did, session_factory=session_factory, user=other) is None


def test_update_draft_version_conflict(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    with session_factory() as db:
        draft = CaliberAssistantDraft(
            draft_id=new_assistant_draft_id(),
            session_id=sid,
            artifact_type="tool",
            status="draft",
            title="t",
            spec={},
            artifact={},
            created_by=USER,
            updated_by=USER,
        )
        db.add(draft)
        db.commit()
        did = draft.draft_id
    with pytest.raises(ConflictError):
        svc.update_draft(
            did, DraftUpdateRequest(version=999), session_factory=session_factory, user=USER
        )


def test_run_structural_tests(svc: AssistantService) -> None:
    assert svc._run_structural_tests("tool", {}).passed is False
    assert svc._run_structural_tests("tool", {"source": "def ("}).passed is False
    assert svc._run_structural_tests("prompt", {"template": "x"}).passed is True


def test_get_run_cross_owner(svc: AssistantService, session_factory: sessionmaker[Session]) -> None:
    sid = _new_session(svc, session_factory)
    turn = svc.send_message(
        sid, MessageSendRequest(content="hi"), session_factory=session_factory, user=USER
    )
    assert turn.run is not None
    assert svc.get_run(turn.run.run_id, session_factory=session_factory, user="@other") is None
    assert svc.get_run("ARN-none", session_factory=session_factory) is None


# ---------------------------------------------------------------------------
# Prompt-alias publish policy + publish_draft outcomes
# ---------------------------------------------------------------------------


def test_prompt_alias_publish_policy_branches(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as db:
        ok, rep = svc._prompt_alias_publish_policy(db, draft_id="D", artifact={})
        assert ok is True
        assert rep["requires_approval"] is False

        ok2, rep2 = svc._prompt_alias_publish_policy(
            db, draft_id="D", artifact={"target_alias": "prod", "name": "p", "template": "t"}
        )
        assert ok2 is False
        assert "requires an approved" in rep2["reason"]

        ok3, rep3 = svc._prompt_alias_publish_policy(
            db,
            draft_id="D",
            artifact={
                "target_alias": "prod",
                "name": "p",
                "template": "t",
                "approval_id": "AP-none",
            },
        )
        assert ok3 is False
        assert "was not found" in rep3["reason"]

        db.add(
            CaliberApprovalRequest(
                approval_id="AP-pending",
                job_id="J",
                agent_id="p",
                status="pending",
                candidate_snapshot={},
            )
        )
        db.add(
            CaliberApprovalRequest(
                approval_id="AP-name",
                job_id="J",
                agent_id="other",
                status="approved",
                candidate_snapshot={"prompt_name": "other", "target_alias": "prod"},
            )
        )
        db.add(
            CaliberApprovalRequest(
                approval_id="AP-alias",
                job_id="J",
                agent_id="p",
                status="approved",
                candidate_snapshot={"prompt_name": "p", "target_alias": "dev"},
            )
        )
        db.add(
            CaliberApprovalRequest(
                approval_id="AP-content",
                job_id="J",
                agent_id="p",
                status="approved",
                candidate_snapshot={
                    "prompt_name": "p",
                    "target_alias": "prod",
                    "assistant_draft_id": "OTHER",
                },
            )
        )
        db.add(
            CaliberApprovalRequest(
                approval_id="AP-pass",
                job_id="J",
                agent_id="p",
                status="approved",
                candidate_snapshot={
                    "prompt_name": "p",
                    "target_alias": "prod",
                    "assistant_draft_id": "D",
                },
            )
        )
        db.commit()

        base = {"target_alias": "prod", "name": "p", "template": "t"}
        assert (
            svc._prompt_alias_publish_policy(
                db, draft_id="D", artifact={**base, "approval_id": "AP-pending"}
            )[0]
            is False
        )
        assert (
            svc._prompt_alias_publish_policy(
                db, draft_id="D", artifact={**base, "approval_id": "AP-name"}
            )[0]
            is False
        )
        assert (
            svc._prompt_alias_publish_policy(
                db, draft_id="D", artifact={**base, "approval_id": "AP-alias"}
            )[0]
            is False
        )
        ok_content, rep_content = svc._prompt_alias_publish_policy(
            db, draft_id="D", artifact={**base, "approval_id": "AP-content"}
        )
        assert ok_content is False
        assert "does not match" in rep_content["reason"]
        ok_pass, rep_pass = svc._prompt_alias_publish_policy(
            db, draft_id="D", artifact={**base, "approval_id": "AP-pass"}
        )
        assert ok_pass is True
        assert rep_pass["passed"] is True


def test_prompt_alias_publish_policy_disabled(session_factory: sessionmaker[Session]) -> None:
    svc = AssistantService(
        engine=FakeAssistantEngine(),
        settings=AssistantRuntimeSettings(publish_requires_approval=False),
    )
    with session_factory() as db:
        ok, rep = svc._prompt_alias_publish_policy(
            db, draft_id="D", artifact={"target_alias": "prod", "name": "p", "template": "t"}
        )
    assert ok is True
    assert "approval_policy_disabled" in rep["checks"]


def test_publish_draft_cross_owner(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    with session_factory() as db:
        draft = CaliberAssistantDraft(
            draft_id=new_assistant_draft_id(),
            session_id=sid,
            artifact_type="tool",
            status="approved",
            title="t",
            spec={},
            artifact={},
            created_by=USER,
            updated_by=USER,
        )
        db.add(draft)
        db.commit()
        did = draft.draft_id
    result = svc.publish_draft(did, session_factory=session_factory, user="@other")
    assert result["success"] is False


def test_publish_draft_not_approved(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    with session_factory() as db:
        draft = CaliberAssistantDraft(
            draft_id=new_assistant_draft_id(),
            session_id=sid,
            artifact_type="tool",
            status="draft",
            title="t",
            spec={},
            artifact={},
            created_by=USER,
            updated_by=USER,
        )
        db.add(draft)
        db.commit()
        did = draft.draft_id
    result = svc.publish_draft(did, session_factory=session_factory, user=USER)
    assert result["success"] is False
    assert "approved" in result["error"].lower()


class _FailPublisher:
    def publish(self, **kwargs: object) -> dict[str, object]:
        return {"success": False, "error": "publisher refused"}


class _OkPublisher:
    def publish(self, **kwargs: object) -> dict[str, object]:
        return {"success": True, "registry_id": "REG-1", "version_id": "v1"}


def test_publish_draft_publisher_failure(session_factory: sessionmaker[Session]) -> None:
    svc = AssistantService(engine=FakeAssistantEngine(), publisher=_FailPublisher())  # type: ignore[arg-type]
    sid = _new_session(svc, session_factory)
    with session_factory() as db:
        draft = CaliberAssistantDraft(
            draft_id=new_assistant_draft_id(),
            session_id=sid,
            artifact_type="tool",
            status="approved",
            title="t",
            spec={},
            artifact={},
            created_by=USER,
            updated_by=USER,
        )
        db.add(draft)
        db.commit()
        did = draft.draft_id
    result = svc.publish_draft(did, session_factory=session_factory, user=USER)
    assert result["success"] is False
    with session_factory() as db:
        row = db.get(CaliberAssistantDraft, did)
        assert row is not None
        assert row.status == "publish_failed"


def test_publish_draft_publisher_success(session_factory: sessionmaker[Session]) -> None:
    svc = AssistantService(engine=FakeAssistantEngine(), publisher=_OkPublisher())  # type: ignore[arg-type]
    sid = _new_session(svc, session_factory)
    with session_factory() as db:
        draft = CaliberAssistantDraft(
            draft_id=new_assistant_draft_id(),
            session_id=sid,
            artifact_type="tool",
            status="approved",
            title="t",
            spec={},
            artifact={},
            created_by=USER,
            updated_by=USER,
        )
        db.add(draft)
        db.commit()
        did = draft.draft_id
    result = svc.publish_draft(did, session_factory=session_factory, user=USER)
    assert result["success"] is True
    with session_factory() as db:
        row = db.get(CaliberAssistantDraft, did)
        assert row is not None
        assert row.status == "published"
        assert row.target_registry_id == "REG-1"


# ---------------------------------------------------------------------------
# Adapter happy paths (full execution bodies)
# ---------------------------------------------------------------------------


def test_execute_create_tool_success(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    plan = svc.create_intent_plan(
        sid,
        IntentPlanRequest(
            intent_name="create_tool",
            slot_overrides={
                "tool_name": "double_tool",
                "source": "def double_tool(x: int) -> dict:\n    return {'value': x * 2}\n",
                "callable_name": "double_tool",
                "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}}},
                "tests": [{"name": "doubles", "input": {"x": 2}, "expected": {"value": 4}}],
            },
        ),
        session_factory=session_factory,
        user=USER,
    )
    executed = svc.execute_intent_plan(
        sid,
        IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
        session_factory=session_factory,
        user=USER,
    )
    assert executed.executed_action == "create_validate_test_tool_draft"
    assert executed.result["result_type"] == "tool_draft"
    assert executed.result["status"] == "completed"
    assert executed.result["test_report"]["passed"] is True
    assert executed.result["next_actions"][0]["intent_name"] == "approve_draft"


def test_execute_create_workflow_success(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    plan = svc.create_intent_plan(
        sid,
        IntentPlanRequest(
            intent_name="create_workflow",
            slot_overrides={"workflow_name": "Cov WF", "manifest": make_manifest("cov_wf")},
        ),
        session_factory=session_factory,
        user=USER,
    )
    executed = svc.execute_intent_plan(
        sid,
        IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
        session_factory=session_factory,
        user=USER,
    )
    assert executed.executed_action == "create_validate_compile_workflow_draft"
    assert executed.result["result_type"] == "workflow_draft"
    assert executed.result["test_report"]["passed"] is True


def test_execute_create_mcp_success(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    plan = svc.create_intent_plan(
        sid,
        IntentPlanRequest(
            intent_name="create_mcp_server",
            slot_overrides={
                "server_name": "filesystem",
                "transport": "stdio",
                "command": "echo",
                "args": ["hello"],
                "discovered_tools": [{"name": "read_file"}],
            },
        ),
        session_factory=session_factory,
        user=USER,
    )
    executed = svc.execute_intent_plan(
        sid,
        IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
        session_factory=session_factory,
        user=USER,
    )
    assert executed.executed_action == "create_validate_test_mcp_server_draft"
    assert executed.result["result_type"] == "mcp_server_draft"
    assert executed.result["status"] == "completed"


def test_execute_review_optimization_success(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as db:
        db.add(
            CaliberRefinementJob(
                job_id="RFN-revok",
                agent_id="a",
                primary_item_id="FB-r",
                artifact_type="prompt",
                optimizer_type="MetaPrompt",
                status="awaiting_approval",
                current_stage="approval",
                eval_results={"gate": {"passed": True}},
                candidate={"prompt_version": 4},
            )
        )
        db.commit()
    result = svc._execute_review_optimization_result(
        _plan("review_optimization_result", {"job_id": "RFN-revok"}),
        session_factory=session_factory,
    )
    assert result["result_type"] == "optimization_review"
    assert result["ids"]["job_id"] == "RFN-revok"
    assert result["eval_results"] == {"gate": {"passed": True}}


def test_create_optimization_plan_fills_defaults(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    plan = svc.create_intent_plan(
        sid,
        IntentPlanRequest(content="optimize prompt support-agent with dataset ED-qa123"),
        session_factory=session_factory,
        user=USER,
    )
    assert plan.intent.name == "run_prompt_optimization"
    names = {s.name for s in plan.slots}
    assert {
        "optimizer_type",
        "scorers",
        "gate.min_aggregate_score",
        "gate.max_regression_delta",
    } <= names


# ---------------------------------------------------------------------------
# test_draft dispatch across artifact types
# ---------------------------------------------------------------------------


def test_test_draft_dispatch(svc: AssistantService, session_factory: sessionmaker[Session]) -> None:
    sid = _new_session(svc, session_factory)
    with session_factory() as db:
        db.add(
            CaliberAssistantDraft(
                draft_id="ADRF-skill",
                session_id=sid,
                artifact_type="skill",
                status="draft",
                title="s",
                spec={},
                artifact={
                    "name": "s1",
                    "description": "d",
                    "summary": "",
                    "content": "c",
                    "owner": USER,
                    "category": "custom",
                    "tags": [],
                    "skill_metadata": {},
                    "allowed_tools": None,
                    "depends_on": [],
                },
                created_by=USER,
                updated_by=USER,
            )
        )
        db.add(
            CaliberAssistantDraft(
                draft_id="ADRF-wf",
                session_id=sid,
                artifact_type="workflow",
                status="draft",
                title="w",
                spec={},
                artifact={"manifest": make_manifest("cov_test_wf")},
                created_by=USER,
                updated_by=USER,
            )
        )
        db.add(
            CaliberAssistantDraft(
                draft_id="ADRF-mcp",
                session_id=sid,
                artifact_type="mcp_server",
                status="draft",
                title="m",
                spec={},
                artifact={"transport": "stdio", "command": "echo"},
                created_by=USER,
                updated_by=USER,
            )
        )
        db.add(
            CaliberAssistantDraft(
                draft_id="ADRF-prompt",
                session_id=sid,
                artifact_type="prompt",
                status="draft",
                title="p",
                spec={},
                artifact={"name": "p", "template": "hi"},
                created_by=USER,
                updated_by=USER,
            )
        )
        db.commit()
    assert svc.test_draft("ADRF-skill", session_factory=session_factory, user=USER).passed is True
    assert svc.test_draft("ADRF-wf", session_factory=session_factory, user=USER).passed is True
    assert svc.test_draft("ADRF-mcp", session_factory=session_factory, user=USER).passed is True
    assert svc.test_draft("ADRF-prompt", session_factory=session_factory, user=USER).passed is True


# ---------------------------------------------------------------------------
# publish_draft: prompt-alias policy block, not-found, approve/get_run misc
# ---------------------------------------------------------------------------


def test_publish_prompt_alias_blocked(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    with session_factory() as db:
        db.add(
            CaliberAssistantDraft(
                draft_id="ADRF-promptblock",
                session_id=sid,
                artifact_type="prompt",
                status="approved",
                title="p",
                spec={"plan_id": "APLN-x"},
                artifact={"name": "p", "template": "Hi {{n}}", "target_alias": "prod"},
                created_by=USER,
                updated_by=USER,
            )
        )
        db.commit()
    result = svc.publish_draft("ADRF-promptblock", session_factory=session_factory, user=USER)
    assert result["success"] is False
    assert result["policy"]["requires_approval"] is True
    assert result["policy"]["passed"] is False
    with session_factory() as db:
        row = db.get(CaliberAssistantDraft, "ADRF-promptblock")
        assert row is not None
        assert row.status == "approved"


def test_publish_and_approve_not_found(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    assert (
        svc.publish_draft("ADRF-none", session_factory=session_factory, user=USER)["success"]
        is False
    )
    assert svc.approve_draft("ADRF-none", session_factory=session_factory, user=USER) is None


def test_get_run_success(svc: AssistantService, session_factory: sessionmaker[Session]) -> None:
    sid = _new_session(svc, session_factory)
    turn = svc.send_message(
        sid, MessageSendRequest(content="hi"), session_factory=session_factory, user=USER
    )
    assert turn.run is not None
    run = svc.get_run(turn.run.run_id, session_factory=session_factory, user=USER)
    assert run is not None
    assert run.run_id == turn.run.run_id


def test_send_message_session_not_found(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    with pytest.raises(ValueError, match="not found"):
        svc.send_message(
            "ASST-none",
            MessageSendRequest(content="hi"),
            session_factory=session_factory,
            user=USER,
        )


# ---------------------------------------------------------------------------
# Additional context / branch coverage
# ---------------------------------------------------------------------------


def test_latest_generated_test_cases_skips(svc: AssistantService) -> None:
    assert svc._latest_generated_test_cases({}) is None
    assert svc._latest_generated_test_cases({"intent_workbench": {"operations": "x"}}) is None
    assert (
        svc._latest_generated_test_cases(
            {"intent_workbench": {"operations": {"op": {"intent_name": "other"}}}}
        )
        is None
    )
    # Ordering matters: the loop walks ``reversed(operations)``, so the matching
    # entry is inserted first (visited last) to force every skip branch to run.
    metadata = {
        "intent_workbench": {
            "operations": {
                "op_match": {
                    "intent_name": "generate_test_cases",
                    "result": {
                        "result_type": "test_cases",
                        "examples": [{"input": {"query": "x"}}],
                        "next_actions": ["notadict", {"intent_name": "other"}],
                    },
                },
                "op_nondict": "notadict",
                "op_wrong_intent": {"intent_name": "other"},
                "op_wrong_type": {
                    "intent_name": "generate_test_cases",
                    "result": {"result_type": "other"},
                },
                "op_empty": {
                    "intent_name": "generate_test_cases",
                    "result": {"result_type": "test_cases", "examples": []},
                },
            }
        }
    }
    result = svc._latest_generated_test_cases(metadata)
    assert result is not None
    assert result["examples"] == [{"input": {"query": "x"}}]
    assert result["dataset_name"] is None


def test_resolve_template_and_optimizer_context_branches(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(
            content="create a prompt named greeter", context={"template": "Hi there"}
        ),
        session_factory=session_factory,
        user=USER,
    )
    assert {s.name: s.value for s in resolved.slots}.get("template") == "Hi there"

    metaprompt = svc.resolve_intent(
        sid,
        IntentResolveRequest(content="optimize prompt greeter with dataset ED-1 using metaprompt"),
        session_factory=session_factory,
        user=USER,
    )
    assert {s.name: s.value for s in metaprompt.slots}.get("optimizer_type") == "MetaPrompt"


def test_resolve_mcp_without_transport(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    resolved = svc.resolve_intent(
        sid,
        IntentResolveRequest(content="create an mcp server named plainserver"),
        session_factory=session_factory,
        user=USER,
    )
    assert resolved.intent.name == "create_mcp_server"
    assert "transport" not in {s.name for s in resolved.slots}


def test_resolve_context_slot_variants(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    save = svc.resolve_intent(
        sid,
        IntentResolveRequest(
            content="save this dataset",
            context={"dataset_name": "ctx_ds", "examples": [{"input": {"q": "x"}, "expected": {}}]},
        ),
        session_factory=session_factory,
        user=USER,
    )
    save_slots = {s.name: s.value for s in save.slots}
    assert save_slots.get("dataset_name") == "ctx_ds"
    assert save_slots.get("examples")

    review = svc.resolve_intent(
        sid,
        IntentResolveRequest(
            content="review the optimization result", context={"job_id": "RFN-ctx"}
        ),
        session_factory=session_factory,
        user=USER,
    )
    assert {s.name: s.value for s in review.slots}.get("job_id") == "RFN-ctx"

    review_wf = svc.resolve_intent(
        sid,
        IntentResolveRequest(
            content="review the workflow winner candidate", context={"job_id": "RFN-wfctx"}
        ),
        session_factory=session_factory,
        user=USER,
    )
    assert {s.name: s.value for s in review_wf.slots}.get("job_id") == "RFN-wfctx"

    promo = svc.resolve_intent(
        sid,
        IntentResolveRequest(
            content="promote this prompt named mover",
            context={"target_alias": "prod", "source_version": 7},
        ),
        session_factory=session_factory,
        user=USER,
    )
    promo_slots = {s.name: s.value for s in promo.slots}
    assert promo_slots.get("target_alias") == "prod"
    assert promo_slots.get("source_version") == 7


def test_generate_test_cases_with_fetcher() -> None:
    svc = AssistantService(
        engine=FakeAssistantEngine(), prompt_fetcher=lambda name: "You are a grounded assistant."
    )
    cases = svc._generate_test_cases("support-agent")
    assert isinstance(cases, list)
    assert len(cases) >= 1


def test_generate_test_cases_engine_raises() -> None:
    class _Boom:
        def run_turn(
            self, request: object, *, toolset: object | None = None
        ) -> AssistantTurnResult:
            raise RuntimeError("boom")

    svc = AssistantService(engine=_Boom())  # type: ignore[arg-type]
    cases = svc._generate_test_cases("support-agent")
    # falls back to the deterministic set
    assert len(cases) == 3


def test_parse_test_cases_more_variants() -> None:
    assert AssistantService._parse_test_cases('{"a": 1}') == []
    with_backslash = r'[{"input":{"query":"a \" b"},"expected":123}]'
    parsed = AssistantService._parse_test_cases(with_backslash)
    assert parsed[0]["expected"] == {}


def test_cap_attachment_text() -> None:
    short, truncated = AssistantService._cap_attachment_text("hi")
    assert (short, truncated) == ("hi", False)
    long_text = "x" * (ATTACHMENT_TEXT_MAX_CHARS + 5)
    capped, was_truncated = AssistantService._cap_attachment_text(long_text)
    assert was_truncated is True
    assert len(capped) == ATTACHMENT_TEXT_MAX_CHARS


def test_attachment_and_queue_limits(session_factory: sessionmaker[Session]) -> None:
    svc = AssistantService(
        engine=FakeAssistantEngine(),
        settings=AssistantRuntimeSettings(max_attachments_per_session=1, max_queued_per_session=1),
    )
    sid = _new_session(svc, session_factory)
    svc.add_text_attachment(sid, name="a", text="first", session_factory=session_factory, user=USER)
    with pytest.raises(ConflictError, match="limit reached"):
        svc.add_text_attachment(
            sid, name="b", text="second", session_factory=session_factory, user=USER
        )

    svc.enqueue_message(sid, content="q1", session_factory=session_factory, user=USER)
    with pytest.raises(ConflictError, match="limit reached"):
        svc.enqueue_message(sid, content="q2", session_factory=session_factory, user=USER)


def test_attachment_and_queue_cross_owner(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    att = svc.add_text_attachment(
        sid, name="a", text="body", session_factory=session_factory, user=USER
    )
    queued = svc.enqueue_message(sid, content="q", session_factory=session_factory, user=USER)
    assert (
        svc.delete_attachment(att.attachment_id, session_factory=session_factory, user="@other")
        is False
    )
    assert (
        svc.cancel_queued(queued.queue_id, session_factory=session_factory, user="@other") is False
    )


def test_draft_prompt_returns_template_via_fake() -> None:
    svc = AssistantService(engine=FakeAssistantEngine())
    result = svc.draft_prompt_from_description(
        "Answer billing questions strictly from policy docs."
    )
    assert result["template"] == "Hello, {{name}}!"
    assert result["name"] == "fake_prompt"


def test_update_draft_title_and_max_drafts(session_factory: sessionmaker[Session]) -> None:
    svc = AssistantService(engine=FakeAssistantEngine())
    sid = _new_session(svc, session_factory)
    with session_factory() as db:
        draft = CaliberAssistantDraft(
            draft_id=new_assistant_draft_id(),
            session_id=sid,
            artifact_type="tool",
            status="draft",
            title="old",
            spec={},
            artifact={},
            created_by=USER,
            updated_by=USER,
        )
        db.add(draft)
        db.commit()
        did = draft.draft_id
    updated = svc.update_draft(
        did,
        DraftUpdateRequest(version=1, title="renamed"),
        session_factory=session_factory,
        user=USER,
    )
    assert updated is not None
    assert updated.title == "renamed"


def test_send_message_max_drafts_skips_new_delta(session_factory: sessionmaker[Session]) -> None:
    class _DraftEngine:
        def run_turn(
            self, request: object, *, toolset: object | None = None
        ) -> AssistantTurnResult:
            return AssistantTurnResult(
                reply="drafting",
                draft_deltas=[
                    DraftDelta(artifact_type="tool", title="new", artifact={"name": "x"})
                ],
            )

    svc = AssistantService(
        engine=_DraftEngine(),  # type: ignore[arg-type]
        settings=AssistantRuntimeSettings(max_drafts_per_session=0),
    )
    sid = _new_session(svc, session_factory)
    turn = svc.send_message(
        sid,
        MessageSendRequest(content="build it", mode="build", approval_mode="manual"),
        session_factory=session_factory,
        user=USER,
    )
    # The new (draft_id-less) delta is dropped once the per-session cap is hit.
    assert turn.draft_updates == []


def test_prompt_alias_policy_non_dict_snapshot(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as db:
        db.add(
            CaliberApprovalRequest(
                approval_id="AP-nodict",
                job_id="J",
                agent_id="p",
                status="approved",
                candidate_snapshot=["not", "a", "dict"],
            )
        )
        db.commit()
        ok, rep = svc._prompt_alias_publish_policy(
            db,
            draft_id="D",
            artifact={
                "target_alias": "prod",
                "name": "p",
                "template": "t",
                "approval_id": "AP-nodict",
            },
        )
    # A non-dict candidate snapshot is coerced to ``{}`` and then fails the
    # prompt-name / alias equality checks (agent_id "p" == name "p", empty alias).
    assert ok is False
    assert "targets alias" in rep["reason"]


# ---------------------------------------------------------------------------
# Final branch fills: tool validation/test failures, calibration valid path,
# generate fetcher failure, tool sandbox via test_draft, get_draft no-user
# ---------------------------------------------------------------------------


def test_execute_create_tool_validation_failed(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    result = svc._execute_create_tool(
        _plan(
            "create_tool",
            {
                "tool_name": "broken_tool",
                "source": "def broken(:\n    pass",  # syntax error
                "callable_name": "broken",
                "tests": [{"name": "t", "input": {}, "expected": {}}],
            },
        ),
        session_id=sid,
        session_factory=session_factory,
        user=USER,
    )
    assert result["result_type"] == "tool_draft"
    assert result["status"] == "blocked"
    assert result["validation_report"]["valid"] is False
    assert result["warnings"]
    with session_factory() as db:
        row = db.get(CaliberAssistantDraft, result["ids"]["draft_id"])
        assert row is not None
        assert row.status == "validation_failed"


def test_execute_create_tool_test_failed(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    result = svc._execute_create_tool(
        _plan(
            "create_tool",
            {
                "tool_name": "adder",
                "source": "def adder(x: int) -> dict:\n    return {'value': x + 1}\n",
                "callable_name": "adder",
                "tests": [{"name": "wrong", "input": {"x": 1}, "expected": {"value": 99}}],
            },
        ),
        session_id=sid,
        session_factory=session_factory,
        user=USER,
    )
    assert result["result_type"] == "tool_draft"
    assert result["status"] == "blocked"
    assert result["validation_report"]["valid"] is True
    assert result["test_report"]["passed"] is False
    assert result["warnings"]


def test_execute_workflow_calibration_with_agent(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    _seed_wf_calibration(session_factory)
    plan = _plan(
        "run_workflow_calibration",
        {
            "workflow_id": "WF-CAL",
            "agent_id": "wf-agent",
            "objective": "quality",
            "epsilon": 0.1,
            "max_candidates": 2,
        },
    )
    result = svc._execute_workflow_calibration(plan, session_factory=session_factory, user=USER)
    assert result["result_type"] == "workflow_calibration_run"
    assert result["ids"]["agent_id"] == "wf-agent"
    assert result["warnings"] == []


def test_generate_test_cases_fetcher_raises() -> None:
    def _boom(name: str) -> str:
        raise RuntimeError("fetch failed")

    svc = AssistantService(engine=FakeAssistantEngine(), prompt_fetcher=_boom)
    cases = svc._generate_test_cases("support-agent")
    assert isinstance(cases, list)
    assert len(cases) >= 1


def test_test_draft_tool_with_tests(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    with session_factory() as db:
        db.add(
            CaliberAssistantDraft(
                draft_id="ADRF-tooltests",
                session_id=sid,
                artifact_type="tool",
                status="draft",
                title="dbl",
                spec={},
                artifact={
                    "name": "dbl",
                    "source": "def dbl(x: int) -> dict:\n    return {'value': x * 2}\n",
                    "callable_name": "dbl",
                    "tests": [{"name": "d", "input": {"x": 2}, "expected": {"value": 4}}],
                },
                created_by=USER,
                updated_by=USER,
            )
        )
        db.commit()
    report = svc.test_draft("ADRF-tooltests", session_factory=session_factory, user=USER)
    assert report.passed is True


def test_get_draft_without_user(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    with session_factory() as db:
        draft = CaliberAssistantDraft(
            draft_id=new_assistant_draft_id(),
            session_id=sid,
            artifact_type="tool",
            status="draft",
            title="t",
            spec={},
            artifact={},
            created_by=USER,
            updated_by=USER,
        )
        db.add(draft)
        db.commit()
        did = draft.draft_id
    fetched = svc.get_draft(did, session_factory=session_factory)
    assert fetched is not None
    assert fetched.draft_id == did


def test_execute_create_workflow_with_description(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    result = svc._execute_create_workflow(
        _plan(
            "create_workflow",
            {
                "workflow_name": "Desc WF",
                "manifest": {"foo": "bar"},
                "description": "a described workflow",
            },
        ),
        session_id=sid,
        session_factory=session_factory,
        user=USER,
    )
    assert result["result_type"] == "workflow_draft"
    assert result["draft"]["artifact"]["description"] == "a described workflow"


def test_resolve_name_from_generic_name_context(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    skill = svc.resolve_intent(
        sid,
        IntentResolveRequest(content="create a skill", context={"name": "named-skill"}),
        session_factory=session_factory,
        user=USER,
    )
    assert {s.name: s.value for s in skill.slots}.get("skill_name") == "named-skill"

    workflow = svc.resolve_intent(
        sid,
        IntentResolveRequest(content="create a workflow", context={"name": "named workflow"}),
        session_factory=session_factory,
        user=USER,
    )
    assert {s.name: s.value for s in workflow.slots}.get("workflow_name") == "named workflow"

    mcp = svc.resolve_intent(
        sid,
        IntentResolveRequest(
            content="create an mcp server", context={"name": "named-server", "transport": "stdio"}
        ),
        session_factory=session_factory,
        user=USER,
    )
    assert {s.name: s.value for s in mcp.slots}.get("server_name") == "named-server"


def test_get_and_validate_draft_not_found(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    assert svc.get_draft("ADRF-none", session_factory=session_factory) is None
    report = svc.validate_draft("ADRF-none", session_factory=session_factory)
    assert report.valid is False


def test_get_operation_status_returns_operation(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    plan = svc.create_intent_plan(
        sid,
        IntentPlanRequest(intent_name="generate_test_cases", slot_overrides={"prompt_name": "p"}),
        session_factory=session_factory,
        user=USER,
    )
    executed = svc.execute_intent_plan(
        sid,
        IntentExecuteRequest(plan_id=plan.plan_id, confirm=True),
        session_factory=session_factory,
        user=USER,
    )
    op = svc.get_operation_status(
        sid, executed.operation_id, session_factory=session_factory, user=USER
    )
    assert op is not None
    assert op.operation_id == executed.operation_id
    assert op.status == "completed"
    assert op.run is not None
    assert op.plan_id == plan.plan_id


def test_run_workflow_compile_error(svc: AssistantService) -> None:
    report = svc._run_workflow_compile_tests({"manifest": make_support_manifest("cov_compile_err")})
    assert report.passed is False
    assert report.error


def test_execute_create_tool_all_optionals(
    svc: AssistantService, session_factory: sessionmaker[Session]
) -> None:
    sid = _new_session(svc, session_factory)
    result = svc._execute_create_tool(
        _plan(
            "create_tool",
            {
                "tool_name": "opt_tool",
                "source": "def opt_tool(x: int) -> dict:\n    return {'value': x}\n",
                "callable_name": "opt_tool",
                "tests": [{"name": "t", "input": {"x": 1}, "expected": {"value": 1}}],
                "side_effect_level": "external_action",
                "requires_approval": True,
                "allow_in_preview": False,
                "secret_refs": ["SECRET_A"],
                "description": "an optional tool",
                "input_schema": {"type": "object"},
                "output_schema": {"type": "object"},
                "module_path": "custom.mod.path",
                "version": "2.0.0",
            },
        ),
        session_id=sid,
        session_factory=session_factory,
        user=USER,
    )
    assert result["result_type"] == "tool_draft"
    assert result["status"] == "completed"
    artifact = result["draft"]["artifact"]
    assert artifact["side_effect_level"] == "external_action"
    assert artifact["requires_approval"] is True
    assert artifact["module_path"] == "custom.mod.path"
    assert artifact["version"] == "2.0.0"
    assert artifact["secret_refs"] == ["SECRET_A"]
