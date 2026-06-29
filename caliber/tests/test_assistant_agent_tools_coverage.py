"""Additional coverage for Aria's agentic tool surface (``agent_tools``).

Complements ``test_assistant_agent_tools.py`` by exercising the handler
branches that file does not: the dispatch fallbacks (unknown tool / no handler /
handler exception), the read-handler success + missing-id + not-found paths, the
KB read/query validation branches, ``run_tool_sandbox`` rejection paths, the
``create_eval_dataset`` validation branches, the ``evaluate_*`` not-found /
wrong-type / empty-dataset edges, ``run_quick_eval`` with an injected fake LLM,
and the ``run_workflow`` / ``approve_draft`` / ``publish_draft`` mutate edges.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.orm import Session, sessionmaker

from caliber.assistant.fake import FakeAssistantEngine
from caliber.assistant.models import SessionCreateRequest
from caliber.assistant.service import AssistantService
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberAssistantDraft,
    CaliberKnowledgeBase,
    CaliberSkill,
    CaliberToolRegistry,
    CaliberWorkflow,
    CaliberWorkflowRun,
    CaliberWorkflowVersion,
)
from caliber.ids import new_assistant_draft_id
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
):
    return svc._build_agent_toolset(
        session_factory=factory, user=USER, session_id=sid, mode=mode, approval_mode=approval
    )


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


# ---------------------------------------------------------------------------
# dispatch() fallback branches
# ---------------------------------------------------------------------------


class TestDispatchFallbacks:
    def test_unknown_tool_name(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        out = json.loads(ts.dispatch("does_not_exist", {}))
        assert "error" in out and "unknown tool" in out["error"]

    def test_handler_exception_is_caught(self, svc, session_factory) -> None:
        """A handler that raises is wrapped in an ``_err`` (loop never breaks)."""
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        # get_workflow_run_trace will call db.get with a non-string run_id that
        # makes the underlying handler raise — but more reliably we force an
        # exception by passing arguments the handler chokes on. Here we make the
        # manifest tool blow up by passing a non-int version_number.
        out = json.loads(
            ts.dispatch("get_workflow_manifest", {"workflow_id": "WF-x", "version_number": "abc"})
        )
        assert "error" in out
        assert "ValueError" in out["error"]


# ---------------------------------------------------------------------------
# read handlers — success + missing-id + not-found
# ---------------------------------------------------------------------------


class TestReadHandlerBranches:
    def test_get_skill_requires_name(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        out = json.loads(ts.dispatch("get_skill", {"name": ""}))
        assert "error" in out and "name is required" in out["error"]

    def test_get_skill_not_found(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        out = json.loads(ts.dispatch("get_skill", {"name": "ghost"}))
        assert "error" in out and "not found" in out["error"]

    def test_get_skill_success(self, svc, session_factory) -> None:
        with session_factory() as db:
            db.add(
                CaliberSkill(
                    skill_id="SK-cov",
                    name="coverage",
                    summary="sum",
                    content="body",
                    owner=USER,
                    category="custom",
                    allowed_tools="lookup_policy",
                    depends_on=[],
                )
            )
            db.commit()
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        out = json.loads(ts.dispatch("get_skill", {"name": "coverage"}))
        assert out["ok"]
        assert out["data"]["name"] == "coverage"
        assert out["data"]["content"] == "body"
        assert out["data"]["allowed_tools"] == "lookup_policy"

    def test_list_tools(self, svc, session_factory) -> None:
        with session_factory() as db:
            db.add(
                CaliberToolRegistry(
                    tool_id="TL-cov",
                    name="my_tool",
                    version="1.0",
                    description="does a thing",
                    module_path="m",
                    callable_name="f",
                    owner=USER,
                )
            )
            db.commit()
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        out = json.loads(ts.dispatch("list_tools", {}))
        assert out["ok"]
        assert any(t["name"] == "my_tool" for t in out["data"])

    def test_list_workflows(self, svc, session_factory) -> None:
        with session_factory() as db:
            db.add(
                CaliberWorkflow(
                    workflow_id="WF-cov",
                    name="Coverage WF",
                    owner=USER,
                    status="active",
                    description="d",
                )
            )
            db.commit()
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        out = json.loads(ts.dispatch("list_workflows", {}))
        assert out["ok"]
        assert any(w["workflow_id"] == "WF-cov" for w in out["data"])

    def test_get_workflow_manifest_requires_id(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        out = json.loads(ts.dispatch("get_workflow_manifest", {"workflow_id": ""}))
        assert "error" in out and "workflow_id is required" in out["error"]

    def test_get_workflow_manifest_not_found(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        out = json.loads(ts.dispatch("get_workflow_manifest", {"workflow_id": "WF-none"}))
        assert "error" in out and "no version found" in out["error"]

    def test_get_workflow_manifest_success_specific_version(self, svc, session_factory) -> None:
        with session_factory() as db:
            db.add(CaliberWorkflow(workflow_id="WF-m", name="M", owner=USER, status="active"))
            for n in (1, 2):
                db.add(
                    CaliberWorkflowVersion(
                        version_id=f"WFV-m{n}",
                        workflow_id="WF-m",
                        version_number=n,
                        status="draft",
                        manifest=make_manifest("WF-m"),
                        manifest_hash=f"h{n}",
                        created_by=USER,
                    )
                )
            db.commit()
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        # version_number omitted -> latest (2)
        latest = json.loads(ts.dispatch("get_workflow_manifest", {"workflow_id": "WF-m"}))
        assert latest["ok"] and latest["data"]["version_number"] == 2
        # explicit version_number -> that version
        v1 = json.loads(
            ts.dispatch("get_workflow_manifest", {"workflow_id": "WF-m", "version_number": 1})
        )
        assert v1["ok"] and v1["data"]["version_number"] == 1

    def test_list_workflow_runs_filtered(self, svc, session_factory) -> None:
        with session_factory() as db:
            db.add(
                CaliberWorkflowRun(
                    workflow_run_id="WR-a",
                    workflow_id="WF-keep",
                    status="completed",
                    source="ui",
                    trace_id="tr-a",
                )
            )
            db.add(
                CaliberWorkflowRun(
                    workflow_run_id="WR-b",
                    workflow_id="WF-other",
                    status="failed",
                    source="ui",
                    error_summary="boom",
                )
            )
            db.commit()
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        out = json.loads(ts.dispatch("list_workflow_runs", {"workflow_id": "WF-keep", "limit": 5}))
        assert out["ok"]
        ids = {r["run_id"] for r in out["data"]}
        assert ids == {"WR-a"}

    def test_get_workflow_run_success(self, svc, session_factory) -> None:
        with session_factory() as db:
            db.add(
                CaliberWorkflowRun(
                    workflow_run_id="WR-ok",
                    workflow_id="WF-ok",
                    workflow_version_id="WFV-ok",
                    status="completed",
                    trace_id="tr-ok",
                    summary={"result": "fine"},
                )
            )
            db.commit()
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        out = json.loads(ts.dispatch("get_workflow_run", {"run_id": "WR-ok"}))
        assert out["ok"]
        assert out["data"]["status"] == "completed"
        assert out["data"]["version_id"] == "WFV-ok"

    def test_get_workflow_run_trace_run_not_found(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        out = json.loads(ts.dispatch("get_workflow_run_trace", {"run_id": "WR-none"}))
        assert "error" in out and "not found" in out["error"]

    def test_list_session_drafts(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        _make_draft(session_factory, sid, "tool", _FAKE_TOOL)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        out = json.loads(ts.dispatch("list_session_drafts", {}))
        assert out["ok"]
        assert len(out["data"]) == 1
        assert out["data"][0]["artifact_type"] == "tool"

    def test_get_draft_not_found(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        out = json.loads(ts.dispatch("get_draft", {"draft_id": "AD-none"}))
        assert "error" in out and "draft not found" in out["error"]

    def test_get_draft_success(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        did = _make_draft(session_factory, sid, "tool", _FAKE_TOOL)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        out = json.loads(ts.dispatch("get_draft", {"draft_id": did}))
        assert out["ok"]
        assert out["data"]["draft_id"] == did


# ---------------------------------------------------------------------------
# knowledge-base read + query validation
# ---------------------------------------------------------------------------


def _seed_kb(factory: sessionmaker[Session], *, active_version_id: str | None = None) -> str:
    kb_id = "KB-cov"
    with factory() as db:
        db.add(
            CaliberKnowledgeBase(
                knowledge_base_id=kb_id,
                name="CovKB",
                owner=USER,
                status="active",
                visibility="public",
                source_bucket="kb-bucket",
                active_version_id=active_version_id,
            )
        )
        db.commit()
    return kb_id


class TestKnowledgeBaseBranches:
    def test_get_knowledge_base_success(self, svc, session_factory) -> None:
        _seed_kb(session_factory)
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="chat", approval="manual")
        out = json.loads(ts.dispatch("get_knowledge_base", {"knowledge_base_id": "KB-cov"}))
        assert out["ok"]
        assert out["data"]["knowledge_base_id"] == "KB-cov"
        assert isinstance(out["data"]["versions"], list)

    def test_query_kb_requires_question(self, svc, session_factory) -> None:
        _seed_kb(session_factory)
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(
            ts.dispatch("query_knowledge_base", {"knowledge_base_id": "KB-cov", "question": ""})
        )
        assert "error" in out and "question is required" in out["error"]

    def test_query_kb_unknown_retrieval_mode(self, svc, session_factory) -> None:
        _seed_kb(session_factory)
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(
            ts.dispatch(
                "query_knowledge_base",
                {"knowledge_base_id": "KB-cov", "question": "hi", "retrieval_mode": "telepathy"},
            )
        )
        assert "error" in out and "unknown retrieval_mode" in out["error"]


# ---------------------------------------------------------------------------
# run_tool_sandbox — rejection paths
# ---------------------------------------------------------------------------


class TestRunToolSandboxBranches:
    def test_draft_not_found(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(ts.dispatch("run_tool_sandbox", {"draft_id": "AD-none", "input": {}}))
        assert "error" in out and "draft not found" in out["error"]

    def test_missing_source(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        # A tool draft with no source / callable name.
        did = _make_draft(session_factory, sid, "tool", {"name": "", "source": ""})
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(ts.dispatch("run_tool_sandbox", {"draft_id": did, "input": {}}))
        assert "error" in out and "missing source" in out["error"]


# ---------------------------------------------------------------------------
# create_eval_dataset — validation branches
# ---------------------------------------------------------------------------


class TestCreateEvalDatasetBranches:
    def test_name_required(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(
            ts.dispatch("create_eval_dataset", {"name": "  ", "examples": [{"input": "a"}]})
        )
        assert "error" in out and "name is required" in out["error"]

    def test_examples_must_be_non_empty_list(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(ts.dispatch("create_eval_dataset", {"name": "ds", "examples": []}))
        assert "error" in out and "non-empty list" in out["error"]

    def test_no_valid_examples(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        # Examples present, but none are dicts -> count stays 0.
        out = json.loads(
            ts.dispatch("create_eval_dataset", {"name": "ds-novalid", "examples": ["a", "b"]})
        )
        assert "error" in out and "no valid examples" in out["error"]


# ---------------------------------------------------------------------------
# evaluate_* — not-found / wrong-type / empty-dataset edges
# ---------------------------------------------------------------------------


class TestEvaluateEdges:
    def test_evaluate_tool_draft_not_found(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(
            ts.dispatch("evaluate_tool_draft", {"draft_id": "AD-none", "dataset_id": "ED-x"})
        )
        assert "error" in out and "draft not found" in out["error"]

    def test_evaluate_tool_draft_wrong_type(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        did = _make_draft(session_factory, sid, "workflow", make_manifest("wf"))
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(
            ts.dispatch("evaluate_tool_draft", {"draft_id": did, "dataset_id": "ED-x"})
        )
        assert "error" in out and "not a tool" in out["error"]

    def test_evaluate_tool_draft_missing_source(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        did = _make_draft(session_factory, sid, "tool", {"name": "", "source": ""})
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(
            ts.dispatch("evaluate_tool_draft", {"draft_id": did, "dataset_id": "ED-x"})
        )
        assert "error" in out and "missing source" in out["error"]

    def test_evaluate_tool_draft_empty_dataset(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        did = _make_draft(session_factory, sid, "tool", _FAKE_TOOL)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        # Dataset id with no example rows -> "dataset has no examples".
        out = json.loads(
            ts.dispatch("evaluate_tool_draft", {"draft_id": did, "dataset_id": "ED-empty"})
        )
        assert "error" in out and "no examples" in out["error"]

    def test_evaluate_workflow_draft_not_found(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(
            ts.dispatch("evaluate_workflow_draft", {"draft_id": "AD-none", "dataset_id": "ED-x"})
        )
        assert "error" in out and "draft not found" in out["error"]

    def test_evaluate_workflow_draft_wrong_type(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        did = _make_draft(session_factory, sid, "tool", _FAKE_TOOL)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(
            ts.dispatch("evaluate_workflow_draft", {"draft_id": did, "dataset_id": "ED-x"})
        )
        assert "error" in out and "not a workflow" in out["error"]

    def test_evaluate_workflow_draft_empty_dataset(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        did = _make_draft(session_factory, sid, "workflow", make_manifest("wf_empty"))
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(
            ts.dispatch("evaluate_workflow_draft", {"draft_id": did, "dataset_id": "ED-empty"})
        )
        assert "error" in out and "no examples" in out["error"]

    def test_evaluate_skill_selection_empty_dataset(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(ts.dispatch("evaluate_skill_selection", {"dataset_id": "ED-empty"}))
        assert "error" in out and "no examples" in out["error"]


# ---------------------------------------------------------------------------
# run_quick_eval — instructions required + scored path with injected LLM
# ---------------------------------------------------------------------------


class TestRunQuickEval:
    def test_instructions_required(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(ts.dispatch("run_quick_eval", {"dataset_id": "ED-x", "instructions": ""}))
        assert "error" in out and "instructions are required" in out["error"]

    def test_dataset_not_found(self, svc, session_factory, monkeypatch: pytest.MonkeyPatch) -> None:
        # Inject a fake completion fn so we get past the provider gate and reach
        # the dataset lookup branch.
        monkeypatch.setattr(
            "caliber.eval.predict.build_completion_fn",
            lambda _cfg: lambda _system, _user: "X",
        )
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(
            ts.dispatch("run_quick_eval", {"dataset_id": "ED-missing", "instructions": "Be terse."})
        )
        assert "error" in out and "not found" in out["error"]

    def test_scored_path_with_injected_llm(
        self, svc, session_factory, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tests.workflow_helpers import seed_eval_dataset

        # Inject a fake completion that echoes the user message uppercased; the
        # dataset's expected values match so the scorer reports a perfect pass.
        monkeypatch.setattr(
            "caliber.eval.predict.build_completion_fn",
            lambda _cfg: lambda _system, user: user.upper(),
        )
        with session_factory() as db:
            dataset_id = seed_eval_dataset(db, name="quick-eval-cov", inputs=["hello", "world"])
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(
            ts.dispatch(
                "run_quick_eval",
                {"dataset_id": dataset_id, "instructions": "Echo it.", "max_examples": 2},
            )
        )
        assert out["ok"]
        assert out["data"]["n_examples"] == 2
        assert "pass_rate" in out["data"]
        assert "aggregate" in out["data"]


# ---------------------------------------------------------------------------
# propose_workflow_patch — draft not found / wrong type
# ---------------------------------------------------------------------------


class TestProposeWorkflowPatchEdges:
    def test_draft_not_found(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(
            ts.dispatch("propose_workflow_patch", {"draft_id": "AD-none", "evidence": {}})
        )
        assert "error" in out and "draft not found" in out["error"]

    def test_wrong_type(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        did = _make_draft(session_factory, sid, "tool", _FAKE_TOOL)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_safe")
        out = json.loads(ts.dispatch("propose_workflow_patch", {"draft_id": did, "evidence": {}}))
        assert "error" in out and "not a workflow" in out["error"]


# ---------------------------------------------------------------------------
# mutate handlers — run_workflow not-found, approve/publish edges
# ---------------------------------------------------------------------------


class TestMutateEdges:
    def test_run_workflow_version_not_found(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_all")
        out = json.loads(
            ts.dispatch("run_workflow", {"version_id": "WFV-none", "input_text": "go"})
        )
        assert "error" in out and "not found" in out["error"]

    def test_run_workflow_parent_missing(self, svc, session_factory) -> None:
        # A version row whose parent workflow does not exist.
        with session_factory() as db:
            db.add(
                CaliberWorkflowVersion(
                    version_id="WFV-orphan",
                    workflow_id="WF-ghost",
                    version_number=1,
                    status="published",
                    manifest=make_manifest("WF-ghost"),
                    manifest_hash="h",
                    created_by=USER,
                )
            )
            db.commit()
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_all")
        out = json.loads(
            ts.dispatch("run_workflow", {"version_id": "WFV-orphan", "input_text": "go"})
        )
        assert "error" in out and "parent workflow not found" in out["error"]

    def test_approve_draft_not_found(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_all")
        out = json.loads(ts.dispatch("approve_draft", {"draft_id": "AD-none"}))
        assert "error" in out and "draft not found" in out["error"]

    def test_approve_draft_success(self, svc, session_factory) -> None:
        sid = _session(svc, session_factory)
        did = _make_draft(session_factory, sid, "tool", _FAKE_TOOL)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_all")
        out = json.loads(ts.dispatch("approve_draft", {"draft_id": did}))
        assert out["ok"]
        assert out["data"]["draft_id"] == did
        assert out["data"]["status"] == "approved"

    def test_publish_draft_not_found_reports_failure(self, svc, session_factory) -> None:
        # publish_draft returns a structured report; an unknown draft yields a
        # successful-call wrapper around a failure report.
        sid = _session(svc, session_factory)
        ts = _toolset(svc, session_factory, sid, mode="build", approval="auto_all")
        out = json.loads(ts.dispatch("publish_draft", {"draft_id": "AD-none"}))
        assert out["ok"]
        assert out["data"]["success"] is False
        assert "not found" in out["data"]["error"].lower()
