"""Tests for Aria's agentic tool surface (agent_tools) + the engine loop.

Covers the permission gate (mode x approval_mode), the read/safe/mutate tool
handlers against real service-layer functions, and the OpenAI engine surfacing
executed tool calls on the turn result.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session, sessionmaker

from caliber.assistant.fake import FakeAssistantEngine
from caliber.assistant.models import AssistantTurnRequest, SessionCreateRequest
from caliber.assistant.openai_engine import OpenAIAssistantEngine
from caliber.assistant.service import AssistantService
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberAssistantDraft,
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberKnowledgeBase,
    CaliberKnowledgeBaseTestRun,
    CaliberSkill,
    CaliberToolRegistry,
    CaliberWorkflow,
    CaliberWorkflowRun,
    CaliberWorkflowVersion,
)
from caliber.ids import new_assistant_draft_id, new_eval_example_id
from tests.workflow_helpers import make_manifest

USER = "@test"

_FAKE_TOOL = {
    "name": "upper",
    "description": "uppercases",
    "source": "def upper(x: str) -> str:\n    return x.upper()\n",
    "input_schema": {"type": "object", "properties": {"x": {"type": "string"}}},
    "output_schema": {"type": "object", "properties": {"result": {"type": "string"}}},
}


@pytest.fixture
def svc() -> AssistantService:
    # A generous sandbox timeout, because these tests are about *what the sandbox
    # does*, not how fast it does it. The 5s product default is a sensible ceiling for
    # a production request but is decided by machine load in a 5,500-test run: a cold
    # `python -I` start plus imports, on a box already running the rest of the suite,
    # can exceed it and turn "completed" into "timed_out". That made this file pass in
    # isolation and fail under load — a test asserting the wrong thing, not a product
    # defect. Latency limits are covered in tests/test_tool_sandbox_service.py.
    return AssistantService(
        engine=FakeAssistantEngine(),
        runtime_config=CaliberConfig(
            tool_sandbox_timeout_seconds=60.0,
            egress_allowed_hosts="tickets.example.com",
            egress_allow_unresolvable_hosts=True,
        ),
    )


def _session(svc: AssistantService, factory: sessionmaker[Session]) -> str:
    return svc.create_session(
        SessionCreateRequest(title="S"), session_factory=factory, user=USER
    ).session_id


def _toolset(
    svc: AssistantService, factory: sessionmaker[Session], sid: str, *, mode: str, approval: str
):
    return svc._build_agent_toolset(
        session_factory=factory, user=USER, session_id=sid, mode=mode, approval_mode=approval
    )


def _names(toolset) -> set[str]:
    return {s["function"]["name"] for s in toolset.specs()}


def _make_draft(
    factory: sessionmaker[Session], sid: str, artifact_type: str, artifact: dict
) -> str:
    did = new_assistant_draft_id()
    with factory() as db:
        db.add(
            CaliberAssistantDraft(
                draft_id=did,
                session_id=sid,
                artifact_type=artifact_type,
                title="t",
                summary="s",
                spec={},
                artifact=artifact,
                created_by=USER,
                updated_by=USER,
            )
        )
        db.commit()
    return did


def _openapi_tool(
    factory: sessionmaker[Session],
    *,
    tool_id: str,
    name: str,
    side_effect_level: str = "read",
    requires_approval: bool = False,
) -> str:
    with factory() as db:
        db.add(
            CaliberToolRegistry(
                tool_id=tool_id,
                name=name,
                version="1.0",
                description=f"{name} via OpenAPI",
                module_path="<openapi_http>",
                callable_name="invoke",
                execution_backend="openapi_http",
                backend_config={
                    "kind": "openapi_http",
                    "method": "GET" if side_effect_level == "read" else "POST",
                    "path": "/tickets/{ticket_id}" if side_effect_level == "read" else "/tickets",
                    "server_url": "https://tickets.example.com",
                    "auth_binding": None,
                    "request_content_types": ["application/json"] if side_effect_level != "read" else [],
                },
                input_schema={
                    "type": "object",
                    "properties": (
                        {"path_params": {"type": "object", "properties": {"ticket_id": {"type": "string"}}}}
                        if side_effect_level == "read"
                        else {"body": {"type": "object", "properties": {"title": {"type": "string"}}}}
                    ),
                },
                output_schema={"type": "object", "properties": {"status_code": {"type": "integer"}}},
                side_effect_level=side_effect_level,
                requires_approval=requires_approval,
                allow_in_preview=True,
                secret_refs=[],
                owner=USER,
                visibility="user",
                status="active",
            )
        )
        db.commit()
    return tool_id


def _mock_openapi(monkeypatch, handler) -> None:
    def _build_client(*, policy, timeout):  # noqa: ANN001
        transport = httpx.MockTransport(handler)
        return httpx.Client(transport=transport, timeout=timeout)

    monkeypatch.setattr("caliber.integrations.openapi.executor.build_client", _build_client)


# ---------------------------------------------------------------------------
# Permission gate
# ---------------------------------------------------------------------------


class TestGating:
    def test_manual_build_is_read_only(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        names = _names(_toolset(svc, session_factory, sid, mode="build", approval="manual"))
        assert "list_workflow_runs" in names and "get_workflow_run_trace" in names
        assert "validate_draft" not in names
        assert "run_workflow" not in names

    def test_auto_safe_adds_safe_tools(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        names = _names(_toolset(svc, session_factory, sid, mode="build", approval="auto_safe"))
        assert {
            "validate_draft",
            "test_draft",
            "preview_workflow_draft",
            "run_quick_eval",
            "propose_workflow_patch",
        } <= names
        assert "run_workflow" not in names and "publish_draft" not in names

    def test_auto_all_adds_mutate_tools(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        names = _names(_toolset(svc, session_factory, sid, mode="build", approval="auto_all"))
        assert "run_workflow" in names
        assert "approve_draft" not in names
        assert "publish_draft" not in names

    def test_gated_draft_tools_cannot_be_dispatched(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        toolset = _toolset(svc, session_factory, sid, mode="build", approval="auto_all")
        for tool_name in ("approve_draft", "publish_draft"):
            out = json.loads(toolset.dispatch(tool_name, {"draft_id": "AD-none"}))
            assert "error" in out and "not permitted" in out["error"]

    def test_chat_mode_is_read_only_even_auto_all(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        names = _names(_toolset(svc, session_factory, sid, mode="chat", approval="auto_all"))
        assert "list_skills" in names
        assert "validate_draft" not in names and "run_workflow" not in names

    def test_dispatch_denied_when_out_of_scope(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="manual")
        out = json.loads(ts.dispatch("validate_draft", {"draft_id": "x"}))
        assert "error" in out and "not permitted" in out["error"]

    def test_dynamic_openapi_write_tool_requires_mutating_build_policy(
        self, svc, session_factory
    ) -> None:
        _openapi_tool(
            session_factory,
            tool_id="TL-openapi-write",
            name="create_ticket",
            side_effect_level="write",
        )
        sid = _session(svc, session_factory)
        assert not any(
            name.startswith("openapi_tool_create_ticket")
            for name in _names(_toolset(svc, session_factory, sid, mode="chat", approval="manual"))
        )
        assert not any(
            name.startswith("openapi_tool_create_ticket")
            for name in _names(_toolset(svc, session_factory, sid, mode="build", approval="auto_safe"))
        )
        assert any(
            name.startswith("openapi_tool_create_ticket")
            for name in _names(_toolset(svc, session_factory, sid, mode="build", approval="auto_all"))
        )

    def test_dynamic_openapi_approval_gated_tool_is_not_exposed(
        self, svc, session_factory
    ) -> None:
        _openapi_tool(
            session_factory,
            tool_id="TL-openapi-gated",
            name="delete_ticket",
            side_effect_level="external_action",
            requires_approval=True,
        )
        sid = _session(svc, session_factory)
        assert not any(
            name.startswith("openapi_tool_delete_ticket")
            for name in _names(_toolset(svc, session_factory, sid, mode="build", approval="auto_all"))
        )


# ---------------------------------------------------------------------------
# Read handlers
# ---------------------------------------------------------------------------


class TestReadHandlers:
    def test_list_skills(self, svc, session_factory) -> None:
        with session_factory() as db:
            db.add(
                CaliberSkill(
                    skill_id="SK-a",
                    name="billing",
                    summary="b",
                    content="c",
                    owner=USER,
                    category="custom",
                )
            )
            db.commit()
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        out = json.loads(ts.dispatch("list_skills", {}))
        assert out["ok"] and any(s["name"] == "billing" for s in out["data"])

    def test_get_workflow_run_missing(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        out = json.loads(ts.dispatch("get_workflow_run", {"run_id": "WR-none"}))
        assert "error" in out

    def test_get_workflow_run_trace_empty(self, svc, session_factory) -> None:
        with session_factory() as db:
            db.add(
                CaliberWorkflowRun(
                    workflow_run_id="WR-1", workflow_id="WF-1", status="completed", trace_id=None
                )
            )
            db.commit()
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        out = json.loads(ts.dispatch("get_workflow_run_trace", {"run_id": "WR-1"}))
        assert out["ok"] and out["data"]["span_count"] == 0

    def test_dynamic_openapi_read_tool_executes(self, svc, session_factory, monkeypatch) -> None:
        _openapi_tool(
            session_factory,
            tool_id="TL-openapi-read",
            name="get_ticket",
            side_effect_level="read",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url == "https://tickets.example.com/tickets/T-1"
            return httpx.Response(200, json={"ticket_id": "T-1", "status": "open"})

        _mock_openapi(monkeypatch, handler)

        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        tool_name = next(
            name for name in _names(ts) if name.startswith("openapi_tool_get_ticket")
        )
        out = json.loads(ts.dispatch(tool_name, {"path_params": {"ticket_id": "T-1"}}))
        assert out["ok"] is True
        assert out["data"]["status_code"] == 200
        assert out["data"]["json"]["status"] == "open"


# ---------------------------------------------------------------------------
# Safe + mutate handlers
# ---------------------------------------------------------------------------


class TestExecuteHandlers:
    def test_validate_and_test_draft(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        did = _make_draft(session_factory, sid, "tool", _FAKE_TOOL)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        v = json.loads(ts.dispatch("validate_draft", {"draft_id": did}))
        assert v["ok"] and v["data"]["valid"] is True
        t = json.loads(ts.dispatch("test_draft", {"draft_id": did}))
        assert t["ok"] and t["data"]["passed"] is True

    def test_preview_workflow_draft(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        did = _make_draft(session_factory, sid, "workflow", make_manifest("wf_preview"))
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(
            ts.dispatch("preview_workflow_draft", {"draft_id": did, "input_text": "hi"})
        )
        assert out["ok"]
        assert out["data"]["status"] is not None
        assert isinstance(out["data"]["steps"], list)

    def test_preview_rejects_non_workflow_draft(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        did = _make_draft(session_factory, sid, "tool", _FAKE_TOOL)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(
            ts.dispatch("preview_workflow_draft", {"draft_id": did, "input_text": "hi"})
        )
        assert "error" in out and "not a workflow" in out["error"]

    def test_run_workflow_enqueues(self, svc, session_factory) -> None:
        with session_factory() as db:
            db.add(CaliberWorkflow(workflow_id="WF-r", name="R", owner=USER, status="active"))
            db.add(
                CaliberWorkflowVersion(
                    version_id="WFV-r",
                    workflow_id="WF-r",
                    version_number=1,
                    status="published",
                    manifest=make_manifest("WF-r"),
                    manifest_hash="h",
                    created_by=USER,
                )
            )
            db.commit()
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_all")
        out = json.loads(ts.dispatch("run_workflow", {"version_id": "WFV-r", "input_text": "go"}))
        assert out["ok"]
        assert out["data"]["workflow_run_id"].startswith("WR-")
        assert out["data"]["status"] == "queued"

    def test_propose_workflow_patch(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        did = _make_draft(session_factory, sid, "workflow", make_manifest("wf_patch"))
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(
            ts.dispatch(
                "propose_workflow_patch",
                {
                    "draft_id": did,
                    "evidence": {
                        "category": "tool_use",
                        "tool_called": False,
                        "required_tools": ["lookup_policy"],
                        "node_id": "agent",
                    },
                },
            )
        )
        assert out["ok"]
        assert "ops" in out["data"] and "patched_manifest" in out["data"]

    def test_quick_eval_requires_real_provider(self, svc, session_factory) -> None:
        # Default config provider is fake → build_completion_fn returns None.
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(
            ts.dispatch("run_quick_eval", {"dataset_id": "ED-x", "instructions": "Be terse."})
        )
        assert "error" in out and "real LLM provider" in out["error"]


# ---------------------------------------------------------------------------
# Knowledge bases, tool sandbox, skill selection (increment 2)
# ---------------------------------------------------------------------------


def _seed_kb(factory: sessionmaker[Session], *, active_version_id: str | None = None) -> str:
    kb_id = "KB-a1"
    with factory() as db:
        db.add(
            CaliberKnowledgeBase(
                knowledge_base_id=kb_id,
                name="Policies",
                owner=USER,
                status="active",
                visibility="public",
                source_bucket="kb-bucket",
                active_version_id=active_version_id,
            )
        )
        db.commit()
    return kb_id


class TestKnowledgeAndSandboxGating:
    def test_kb_reads_available_in_all_modes(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        names = _names(_toolset(svc, session_factory, sid, mode="chat", approval="manual"))
        assert {
            "list_knowledge_bases",
            "get_knowledge_base",
            "get_knowledge_base_calibration",
            "preview_skill_selection",
        } <= names

    def test_kb_query_and_sandbox_are_safe(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        manual = _names(_toolset(svc, session_factory, sid, mode="build", approval="manual"))
        safe = _names(_toolset(svc, session_factory, sid, mode="build", approval="auto_safe"))
        assert "query_knowledge_base" not in manual and "run_tool_sandbox" not in manual
        assert {"query_knowledge_base", "run_tool_sandbox"} <= safe


class TestKnowledgeHandlers:
    def test_list_knowledge_bases(self, svc, session_factory) -> None:
        _seed_kb(session_factory)
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        out = json.loads(ts.dispatch("list_knowledge_bases", {}))
        assert out["ok"] and any(kb["knowledge_base_id"] == "KB-a1" for kb in out["data"])

    def test_query_kb_without_active_version_errors(self, svc, session_factory) -> None:
        _seed_kb(session_factory, active_version_id=None)
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(
            ts.dispatch(
                "query_knowledge_base", {"knowledge_base_id": "KB-a1", "question": "refunds?"}
            )
        )
        assert "error" in out and "active version" in out["error"]

    def test_get_calibration_returns_metrics(self, svc, session_factory) -> None:
        _seed_kb(session_factory)
        with session_factory() as db:
            db.add(
                CaliberKnowledgeBaseTestRun(
                    test_run_id="KBTR-1",
                    knowledge_base_id="KB-a1",
                    knowledge_base_version_id="KBV-1",
                    retrieval_mode="dense",
                    metrics={"recall_at_k": 0.8, "ndcg_at_k": 0.7},
                )
            )
            db.commit()
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        out = json.loads(
            ts.dispatch("get_knowledge_base_calibration", {"knowledge_base_id": "KB-a1"})
        )
        assert out["ok"] and out["data"][0]["metrics"]["recall_at_k"] == 0.8


class TestToolSandboxAndSkillTools:
    def test_run_tool_sandbox_executes_draft(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        did = _make_draft(session_factory, sid, "tool", _FAKE_TOOL)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(ts.dispatch("run_tool_sandbox", {"draft_id": did, "input": {"x": "hi"}}))
        assert out["ok"]
        assert out["data"]["status"] == "completed"
        assert out["data"]["output"] == "HI"

    def test_run_tool_sandbox_rejects_non_tool(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        did = _make_draft(session_factory, sid, "workflow", make_manifest("wf"))
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(ts.dispatch("run_tool_sandbox", {"draft_id": did, "input": {}}))
        assert "error" in out and "not a tool" in out["error"]

    def test_preview_skill_selection(self, svc, session_factory) -> None:
        with session_factory() as db:
            db.add(
                CaliberSkill(
                    skill_id="SK-sel",
                    name="refund-policy",
                    summary="Handles refund questions",
                    content="Cite the refund policy.",
                    owner=USER,
                    category="custom",
                )
            )
            db.commit()
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        out = json.loads(ts.dispatch("preview_skill_selection", {"query": "how do refunds work?"}))
        assert out["ok"]
        assert isinstance(out["data"]["selected"], list)


def _seed_dataset(factory: sessionmaker[Session], dataset_id: str, examples) -> None:
    with factory() as db:
        db.add(
            CaliberEvalDataset(
                dataset_id=dataset_id, name=dataset_id, owner=USER, status="active", version=1
            )
        )
        for inp, exp in examples:
            db.add(
                CaliberEvalDatasetExample(
                    example_id=new_eval_example_id(),
                    dataset_id=dataset_id,
                    dataset_version=1,
                    input=inp,
                    expected=exp,
                )
            )
        db.commit()


class TestDatasetAndEvalGating:
    def test_dataset_and_eval_tools_are_safe(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        manual = _names(_toolset(svc, session_factory, sid, mode="build", approval="manual"))
        safe = _names(_toolset(svc, session_factory, sid, mode="build", approval="auto_safe"))
        tools = {
            "create_eval_dataset",
            "evaluate_tool_draft",
            "evaluate_workflow_draft",
            "evaluate_skill_selection",
        }
        assert not (tools & manual)
        assert tools <= safe


class TestDatasetAndEvalTools:
    def test_create_eval_dataset_persists(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(
            ts.dispatch(
                "create_eval_dataset",
                {
                    "name": "refund-cases",
                    "examples": [
                        {"input": {"x": "hi"}, "expected": {"expected": "HI"}},
                        {"input": "yo", "expected": "YO"},
                    ],
                },
            )
        )
        assert out["ok"] and out["data"]["examples"] == 2
        with session_factory() as db:
            ds = (
                db.query(CaliberEvalDataset)
                .filter(CaliberEvalDataset.name == "refund-cases")
                .first()
            )
            assert ds is not None
        # Duplicate name is rejected.
        dup = json.loads(
            ts.dispatch(
                "create_eval_dataset",
                {"name": "refund-cases", "examples": [{"input": "a", "expected": "b"}]},
            )
        )
        assert "error" in dup and "already exists" in dup["error"]

    def test_evaluate_tool_draft_scores(self, svc, session_factory) -> None:
        _seed_dataset(
            session_factory,
            "ED-tool",
            [({"x": "hi"}, {"expected": "HI"}), ({"x": "yo"}, {"expected": "YO"})],
        )
        sid = _session(svc, session_factory)
        did = _make_draft(session_factory, sid, "tool", _FAKE_TOOL)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(
            ts.dispatch("evaluate_tool_draft", {"draft_id": did, "dataset_id": "ED-tool"})
        )
        assert out["ok"]
        assert out["data"]["n_examples"] == 2
        # `upper` returns the uppercased input → exact match with expected.
        assert out["data"]["pass_rate"] == 1.0

    def test_evaluate_tool_draft_rejects_all_zero_dataset_weights(
        self, svc, session_factory
    ) -> None:
        _seed_dataset(
            session_factory,
            "ED-zero-tool",
            [({"x": "hi"}, {"expected": "HI"})],
        )
        with session_factory() as db:
            row = (
                db.query(CaliberEvalDatasetExample)
                .filter(CaliberEvalDatasetExample.dataset_id == "ED-zero-tool")
                .one()
            )
            row.weight = 0.0
            row.tags = ["excluded"]
            db.commit()

        sid = _session(svc, session_factory)
        did = _make_draft(session_factory, sid, "tool", _FAKE_TOOL)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(
            ts.dispatch(
                "evaluate_tool_draft",
                {"draft_id": did, "dataset_id": "ED-zero-tool"},
            )
        )

        assert "error" in out
        assert "at least one value greater than zero" in out["error"]

    def test_evaluate_workflow_draft_scores(self, svc, session_factory) -> None:
        _seed_dataset(session_factory, "ED-wf", [({"input": "hello"}, {})])
        sid = _session(svc, session_factory)
        did = _make_draft(session_factory, sid, "workflow", make_manifest("wf_eval"))
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(
            ts.dispatch("evaluate_workflow_draft", {"draft_id": did, "dataset_id": "ED-wf"})
        )
        assert out["ok"]
        assert out["data"]["n_examples"] == 1
        assert "completion_rate" in out["data"]["scores"]

    def test_evaluate_skill_selection(self, svc, session_factory) -> None:
        with session_factory() as db:
            db.add(
                CaliberSkill(
                    skill_id="SK-bill",
                    name="billing",
                    summary="Handles refunds",
                    content="Cite the refund policy.",
                    owner=USER,
                    category="custom",
                )
            )
            db.commit()
        _seed_dataset(
            session_factory, "ED-skill", [({"input": "how do refunds work?"}, {"skill": "billing"})]
        )
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(ts.dispatch("evaluate_skill_selection", {"dataset_id": "ED-skill"}))
        assert out["ok"]
        assert out["data"]["scored"] == 1
        assert out["data"]["selection_accuracy"] in (0.0, 1.0)


# ---------------------------------------------------------------------------
# Engine loop surfacing
# ---------------------------------------------------------------------------


class _StubToolset:
    def specs(self):
        return [{"type": "function", "function": {"name": "list_skills", "parameters": {}}}]

    def dispatch(self, name, arguments):
        return json.dumps({"ok": True, "data": [{"name": "billing"}]})


def test_openai_engine_surfaces_tool_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """First model turn requests a tool; second returns a final reply."""
    import openai

    calls = {"n": 0}

    def _tool_call() -> SimpleNamespace:
        return SimpleNamespace(
            id="call_1",
            function=SimpleNamespace(name="list_skills", arguments="{}"),
        )

    class _FakeOpenAI:
        def __init__(self, **_: Any) -> None:
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kwargs: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 1:
                msg = SimpleNamespace(content=None, tool_calls=[_tool_call()])
            else:
                msg = SimpleNamespace(content="Here are your skills.", tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    engine = OpenAIAssistantEngine(api_key="sk-x")
    result = engine.run_turn(
        AssistantTurnRequest(session_id="s1", user_message="show skills"),
        toolset=_StubToolset(),
    )
    assert result.reply == "Here are your skills."
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "list_skills"
    assert result.tool_calls[0].ok is True
