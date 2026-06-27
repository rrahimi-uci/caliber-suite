"""Direct tests for exported workflow runtime helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.orm import Session, sessionmaker

from caliber.config import CaliberConfig
from caliber.db.models import CaliberWorkflow, CaliberWorkflowDeployment, CaliberWorkflowVersion
from caliber.workflows import export_runtime
from caliber.workflows.compiler import build_ir
from caliber.workflows.manifest import parse_manifest
from caliber.workflows.runtime import FakeWorkflowExecutor, RuntimePlan, WorkflowRunResult
from caliber.workflows.session_memory import InMemoryWorkflowSessionMemoryStore
from tests.workflow_helpers import fake_resolver, make_manifest


def _ir(manifest_dict: dict[str, object]):
    return build_ir(parse_manifest(manifest_dict), fake_resolver(), version="7")


def _workflow_version(
    *,
    workflow_id: str,
    version_id: str,
    version_number: int,
) -> CaliberWorkflowVersion:
    return CaliberWorkflowVersion(
        version_id=version_id,
        workflow_id=workflow_id,
        version_number=version_number,
        status="published",
        manifest=make_manifest(workflow_id),
        manifest_hash=f"hash-{version_id}",
        created_by="@test",
    )


def _knowledge_query_manifest() -> dict[str, object]:
    data = make_manifest("export-query")
    data["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "knowledge": {
            "id": "knowledge",
            "type": "knowledge_query",
            "knowledge_base_id": "KB-1",
            "version_ids": [],
            "retrieval_modes": [],
            "top_k": 4,
            "inputs": {"question": {"type": "string"}},
            "outputs": {
                "answer": {"type": "string"},
                "result": {"type": "structured"},
            },
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    data["edges"] = [
        {"id": "e_start_knowledge", "from": "start", "to": "knowledge", "map": {"msg": "question"}},
        {
            "id": "e_knowledge_final",
            "from": "knowledge",
            "to": "final",
            "map": {"answer": "response"},
        },
    ]
    return data


def _knowledge_build_manifest() -> dict[str, object]:
    data = make_manifest("export-build")
    data["nodes"] = {
        "start": {
            "id": "start",
            "type": "start",
            "outputs": {"msg": {"type": "string"}},
        },
        "knowledge_build": {
            "id": "knowledge_build",
            "type": "knowledge_build",
            "knowledge_base_id": "KB-1",
            "chunking_strategy": "recursive",
            "embedding_model": "BAAI/bge-m3",
            "inputs": {"input": {"type": "string"}},
            "outputs": {
                "text": {"type": "string"},
                "result": {"type": "structured"},
            },
        },
        "final": {
            "id": "final",
            "type": "output",
            "inputs": {"response": {"type": "string"}},
        },
    }
    data["edges"] = [
        {
            "id": "e_start_build",
            "from": "start",
            "to": "knowledge_build",
            "map": {"msg": "input"},
        },
        {
            "id": "e_build_final",
            "from": "knowledge_build",
            "to": "final",
            "map": {"text": "response"},
        },
    ]
    return data


class _Dumpable(SimpleNamespace):
    def model_dump(self, mode: str = "json") -> dict[str, object]:
        del mode
        return dict(self.__dict__)


def test_default_subworkflow_runner_uses_latest_manual_version_and_propagates_run_context(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with session_factory() as session:
        session.add(
            CaliberWorkflow(
                workflow_id="WF-child",
                name="Child Workflow",
                description="",
                owner="@test",
            )
        )
        session.add_all(
            [
                _workflow_version(
                    workflow_id="WF-child",
                    version_id="WFV-child-1",
                    version_number=1,
                ),
                _workflow_version(
                    workflow_id="WF-child",
                    version_id="WFV-child-7",
                    version_number=7,
                ),
            ]
        )
        session.commit()

    captured: dict[str, object] = {}

    def _fake_build_plan(
        session: Session,
        target_version: CaliberWorkflowVersion,
        *,
        alias: str,
        subworkflow_depth: int,
        config: CaliberConfig,
        session_factory,
    ) -> SimpleNamespace:
        captured["session"] = session
        captured["target_version_id"] = target_version.version_id
        captured["target_version_number"] = target_version.version_number
        captured["alias"] = alias
        captured["subworkflow_depth"] = subworkflow_depth
        captured["config"] = config
        captured["session_factory"] = session_factory
        return SimpleNamespace(plan_id="child-plan", version_id=target_version.version_id)

    def _fake_execute(
        plan,
        input_text: str,
        *,
        executor,
        session_id: str | None = None,
        preview: bool = False,
        extra_tools=None,
    ) -> WorkflowRunResult:
        del extra_tools
        captured["plan"] = plan
        captured["input_text"] = input_text
        captured["executor"] = executor
        captured["session_id"] = session_id
        captured["preview"] = preview
        return WorkflowRunResult(
            status="completed",
            output="child response",
            tokens=23,
            steps=[
                SimpleNamespace(node_id="child_start"),
                SimpleNamespace(node_id="child_final"),
            ],
        )

    monkeypatch.setattr("caliber.workflows.promoter.build_plan", _fake_build_plan)
    monkeypatch.setattr(export_runtime, "execute", _fake_execute)
    monkeypatch.setattr(
        export_runtime,
        "current_run_context",
        lambda: SimpleNamespace(session_id="SESSION-parent"),
    )

    runner = export_runtime._default_subworkflow_runner(
        session_factory=session_factory,
        config=app_config,
    )
    executor = FakeWorkflowExecutor()

    payload = runner(
        "WF-child",
        "manual",
        "Escalate to child",
        90.0,
        2,
        executor,
        True,
    )

    assert captured["target_version_id"] == "WFV-child-7"
    assert captured["target_version_number"] == 7
    assert captured["alias"] == "manual"
    assert captured["subworkflow_depth"] == 2
    assert captured["config"] is app_config
    assert captured["session_factory"] is session_factory
    assert captured["plan"].version_id == "WFV-child-7"
    assert captured["input_text"] == "Escalate to child"
    assert captured["executor"] is executor
    assert captured["session_id"] == "SESSION-parent"
    assert captured["preview"] is True
    assert payload == {
        "status": "completed",
        "workflow_id": "WF-child",
        "alias": "manual",
        "workflow_version_id": "WFV-child-7",
        "workflow_version_number": 7,
        "output": "child response",
        "error": None,
        "tokens": 23,
        "steps": ["child_start", "child_final"],
    }


def test_default_subworkflow_runner_uses_active_alias_deployment(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployed_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    with session_factory() as session:
        session.add(
            CaliberWorkflow(
                workflow_id="WF-aliased-child",
                name="Aliased Child",
                description="",
                owner="@test",
            )
        )
        session.add(
            _workflow_version(
                workflow_id="WF-aliased-child",
                version_id="WFV-aliased-4",
                version_number=4,
            )
        )
        session.add(
            CaliberWorkflowDeployment(
                deployment_id="WFD-aliased-prod",
                workflow_id="WF-aliased-child",
                alias="prod",
                version_id="WFV-aliased-4",
                environment="prod",
                status="active",
                deployed_by="@test",
                deployed_at=deployed_at,
            )
        )
        session.commit()

    captured: dict[str, object] = {}

    def _fake_build_plan(
        session: Session,
        target_version: CaliberWorkflowVersion,
        *,
        alias: str,
        subworkflow_depth: int,
        config: CaliberConfig,
        session_factory,
    ) -> SimpleNamespace:
        del session, subworkflow_depth, config, session_factory
        captured["target_version_id"] = target_version.version_id
        captured["target_version_number"] = target_version.version_number
        captured["alias"] = alias
        return SimpleNamespace(plan_id="aliased-child-plan")

    monkeypatch.setattr("caliber.workflows.promoter.build_plan", _fake_build_plan)
    monkeypatch.setattr(
        export_runtime,
        "execute",
        lambda *args, **kwargs: WorkflowRunResult(
            status="completed",
            output="prod child",
            tokens=5,
            steps=[SimpleNamespace(node_id="child_final")],
        ),
    )
    monkeypatch.setattr(export_runtime, "current_run_context", lambda: None)

    runner = export_runtime._default_subworkflow_runner(
        session_factory=session_factory,
        config=app_config,
    )

    payload = runner(
        "WF-aliased-child",
        "prod",
        "ping",
        60.0,
        1,
        FakeWorkflowExecutor(),
        False,
    )

    assert captured["target_version_id"] == "WFV-aliased-4"
    assert captured["target_version_number"] == 4
    assert captured["alias"] == "prod"
    assert payload["workflow_version_id"] == "WFV-aliased-4"
    assert payload["workflow_version_number"] == 4
    assert payload["output"] == "prod child"


def test_default_subworkflow_runner_raises_when_manual_version_missing(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        session.add(
            CaliberWorkflow(
                workflow_id="WF-empty-child",
                name="Empty Child",
                description="",
                owner="@test",
            )
        )
        session.commit()

    runner = export_runtime._default_subworkflow_runner(
        session_factory=session_factory,
        config=app_config,
    )

    with pytest.raises(RuntimeError, match="no versions found for subworkflow 'WF-empty-child'"):
        runner(
            "WF-empty-child",
            "manual",
            "hello",
            30.0,
            1,
            FakeWorkflowExecutor(),
            False,
        )


def test_default_subworkflow_runner_returns_recursion_limit_error(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
) -> None:
    runner = export_runtime._default_subworkflow_runner(
        session_factory=session_factory,
        config=app_config,
    )

    payload = runner(
        "WF-any-child",
        "manual",
        "hello",
        30.0,
        4,
        FakeWorkflowExecutor(),
        False,
    )

    assert payload == {
        "status": "error",
        "output": "",
        "error": "subworkflow recursion depth exceeded (3)",
        "tokens": 0,
    }


def test_default_subworkflow_runner_raises_when_alias_has_no_active_deployment(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        session.add(
            CaliberWorkflow(
                workflow_id="WF-missing-alias",
                name="Missing Alias Child",
                description="",
                owner="@test",
            )
        )
        session.add(
            _workflow_version(
                workflow_id="WF-missing-alias",
                version_id="WFV-missing-alias-1",
                version_number=1,
            )
        )
        session.commit()

    runner = export_runtime._default_subworkflow_runner(
        session_factory=session_factory,
        config=app_config,
    )

    with pytest.raises(
        RuntimeError,
        match="no active deployment for subworkflow 'WF-missing-alias' alias 'prod'",
    ):
        runner(
            "WF-missing-alias",
            "prod",
            "hello",
            30.0,
            1,
            FakeWorkflowExecutor(),
            False,
        )


def test_execute_exported_workflow_builds_runtime_executor_for_agent_graph(
    app_config: CaliberConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _ExecutorSentinel:
        pass

    def _fake_build_executor(config: CaliberConfig, **_kwargs) -> _ExecutorSentinel:
        captured["executor_config"] = config
        captured["executor_kwargs"] = _kwargs
        return _ExecutorSentinel()

    def _fake_execute(
        plan: RuntimePlan,
        input_text: str,
        *,
        executor,
        session_id: str | None = None,
        preview: bool = False,
        extra_tools=None,
    ) -> WorkflowRunResult:
        del session_id, preview, extra_tools
        captured["plan"] = plan
        captured["input_text"] = input_text
        captured["executor"] = executor
        return WorkflowRunResult(status="completed", output="agent export ok")

    monkeypatch.setattr("caliber.workflows.promoter.build_executor", _fake_build_executor)
    monkeypatch.setattr(export_runtime, "execute", _fake_execute)

    result = export_runtime.execute_exported_workflow(
        _ir(make_manifest("agent-export")),
        "hello team",
        config=app_config,
    )

    assert result.output == "agent export ok"
    assert captured["executor_config"] is app_config
    assert captured["executor_kwargs"]["ir"].workflow_id == "agent-export"
    assert captured["input_text"] == "hello team"
    assert captured["plan"].ir.workflow_id == "agent-export"
    assert captured["executor"].__class__.__name__ == "_ExecutorSentinel"


def test_execute_exported_workflow_autowires_default_knowledge_query_runner(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _QueryResult:
        def __init__(self, question: str) -> None:
            self._question = question

        def model_dump(self, mode: str = "json") -> dict[str, object]:
            del mode
            return {"question": self._question, "versions": []}

    class _FakeKnowledgeService:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def get_knowledge_base(self, knowledge_base_id: str, *, identity) -> SimpleNamespace:
            captured["knowledge_base_id"] = knowledge_base_id
            captured["knowledge_identity"] = identity
            return SimpleNamespace(active_version_id="KBV-7")

        def get_version(self, version_id: str, *, identity) -> SimpleNamespace:
            captured["resolved_version_id"] = version_id
            captured["version_identity"] = identity
            return SimpleNamespace(
                graph_config=SimpleNamespace(
                    default_retrieval_mode="age_graph",
                    output_target="object_store_and_age",
                ),
                summary={"age_sync_status": "processing"},
            )

        def options(self) -> SimpleNamespace:
            return SimpleNamespace(age_enabled=True)

        def query(self, request, *, identity) -> _QueryResult:
            captured["request"] = request
            captured["query_identity"] = identity
            return _QueryResult(request.question)

    def _fake_build_session_factory(config: CaliberConfig):
        captured["built_config"] = config
        return session_factory

    def _fake_execute(
        plan: RuntimePlan,
        input_text: str,
        *,
        executor,
        session_id: str | None = None,
        preview: bool = False,
        extra_tools=None,
    ) -> WorkflowRunResult:
        del session_id, preview, extra_tools
        captured["plan"] = plan
        captured["executor"] = executor
        captured["input_text"] = input_text
        return WorkflowRunResult(status="completed", output="top-level output")

    monkeypatch.setattr(export_runtime, "KnowledgeBaseService", _FakeKnowledgeService)
    monkeypatch.setattr(export_runtime, "_build_session_factory", _fake_build_session_factory)
    monkeypatch.setattr(export_runtime, "execute", _fake_execute)

    result = export_runtime.execute_exported_workflow(
        _ir(_knowledge_query_manifest()),
        "refund policy",
        config=app_config,
        active_project_id="PRJ-9",
    )

    assert result.output == "top-level output"
    assert captured["built_config"] is app_config
    assert captured["input_text"] == "refund policy"
    assert isinstance(captured["executor"], FakeWorkflowExecutor)

    plan = captured["plan"]
    assert isinstance(plan, RuntimePlan)
    assert plan.knowledge_query_runner is not None

    payload = plan.knowledge_query_runner(
        {
            "knowledge_base_id": "KB-1",
            "version_ids": [],
            "question": "What is the refund policy?",
            "retrieval_modes": [],
        }
    )

    request = captured["request"]
    identity = captured["query_identity"]
    assert request.version_ids == ["KBV-7"]
    assert request.retrieval_modes == ["graph_hybrid"]
    assert identity.user_id == "@exported-workflow"
    assert identity.active_project_id == "PRJ-9"
    assert payload["question"] == "What is the refund policy?"


def test_default_knowledge_query_runner_requires_active_version_or_version_ids(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeKnowledgeService:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def get_knowledge_base(self, knowledge_base_id: str, *, identity):
            del knowledge_base_id, identity
            return SimpleNamespace(active_version_id=None)

        def get_version(self, version_id: str, *, identity):
            del version_id, identity
            raise AssertionError("version lookup should not happen without an active version")

        def options(self) -> SimpleNamespace:
            return SimpleNamespace(age_enabled=False)

        def query(self, request, *, identity):
            del identity
            return _Dumpable(
                question=request.question,
                versions=request.version_ids,
                retrieval_modes=request.retrieval_modes,
            )

    monkeypatch.setattr(export_runtime, "KnowledgeBaseService", _FakeKnowledgeService)
    runner = export_runtime._default_knowledge_query_runner(
        session_factory=session_factory,
        config=app_config,
        identity=export_runtime._default_identity(active_project_id=None),
    )

    with pytest.raises(RuntimeError, match="knowledge base 'KB-2' has no active version"):
        runner(
            {
                "knowledge_base_id": "KB-2",
                "version_ids": [],
                "retrieval_modes": [],
                "question": "hello",
            }
        )

    with pytest.raises(
        RuntimeError,
        match="knowledge_query node requires at least one version_id or knowledge_base_id",
    ):
        runner({"version_ids": [], "question": "hello"})


def test_default_knowledge_query_runner_normalizes_string_retrieval_mode(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeKnowledgeService:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def get_version(self, version_id: str, *, identity):
            del version_id, identity
            raise AssertionError("default retrieval resolution should not run for explicit modes")

        def options(self) -> SimpleNamespace:
            return SimpleNamespace(age_enabled=False)

        def query(self, request, *, identity):
            captured["request"] = request
            captured["identity"] = identity
            return _Dumpable(
                question=request.question,
                versions=request.version_ids,
                retrieval_modes=request.retrieval_modes,
            )

    monkeypatch.setattr(export_runtime, "KnowledgeBaseService", _FakeKnowledgeService)
    runner = export_runtime._default_knowledge_query_runner(
        session_factory=session_factory,
        config=app_config,
        identity=export_runtime._default_identity(active_project_id="PRJ-77"),
    )

    payload = runner(
        {
            "version_ids": ["KBV-9"],
            "retrieval_modes": "graph_hybrid",
            "question": "hello",
        }
    )

    request = captured["request"]
    identity = captured["identity"]
    assert request.version_ids == ["KBV-9"]
    assert request.retrieval_modes == ["graph_hybrid"]
    assert identity.active_project_id == "PRJ-77"
    assert payload["retrieval_modes"] == ["graph_hybrid"]


def test_execute_exported_workflow_autowires_default_knowledge_build_runner(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeKnowledgeService:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def create_version(self, knowledge_base_id: str, request, *, identity, actor):
            captured["knowledge_base_id"] = knowledge_base_id
            captured["request"] = request
            captured["actor"] = actor
            captured["create_identity"] = identity
            return SimpleNamespace(
                knowledge_base=_Dumpable(
                    knowledge_base_id=knowledge_base_id,
                    active_version_id="KBV-1",
                ),
                version=_Dumpable(
                    knowledge_base_version_id="KBV-9",
                    version_number=9,
                    status="processing",
                    chunking_strategy=request.chunking_strategy,
                    embedding_model=request.embedding_model,
                    error_summary=None,
                ),
                run=_Dumpable(knowledge_base_run_id="KBR-9", status="running"),
            )

        def get_version(self, version_id: str, *, identity):
            captured["waited_version_id"] = version_id
            captured["wait_identity"] = identity
            return _Dumpable(
                knowledge_base_version_id=version_id,
                version_number=9,
                status="completed",
                chunking_strategy="recursive",
                embedding_model="BAAI/bge-m3",
                error_summary=None,
            )

        def get_knowledge_base(self, knowledge_base_id: str, *, identity):
            captured["knowledge_identity"] = identity
            return _Dumpable(knowledge_base_id=knowledge_base_id, active_version_id="KBV-1")

        def list_runs(self, knowledge_base_id: str, *, identity):
            del knowledge_base_id, identity
            return [_Dumpable(knowledge_base_run_id="KBR-9", status="completed")]

        def activate_version(self, knowledge_base_id: str, version_id: str, *, identity, actor):
            captured["activated_version_id"] = version_id
            captured["activate_identity"] = identity
            captured["activate_actor"] = actor
            return _Dumpable(
                knowledge_base_id=knowledge_base_id,
                active_version_id=version_id,
            )

    def _fake_build_session_factory(config: CaliberConfig):
        captured["built_config"] = config
        return session_factory

    def _fake_execute(
        plan: RuntimePlan,
        input_text: str,
        *,
        executor,
        session_id: str | None = None,
        preview: bool = False,
        extra_tools=None,
    ) -> WorkflowRunResult:
        del session_id, preview, extra_tools
        captured["plan"] = plan
        captured["executor"] = executor
        captured["input_text"] = input_text
        return WorkflowRunResult(status="completed", output="top-level output")

    monkeypatch.setattr(export_runtime, "KnowledgeBaseService", _FakeKnowledgeService)
    monkeypatch.setattr(export_runtime, "_build_session_factory", _fake_build_session_factory)
    monkeypatch.setattr(export_runtime, "execute", _fake_execute)
    monkeypatch.setattr(export_runtime, "_sleep", lambda seconds: None)

    result = export_runtime.execute_exported_workflow(
        _ir(_knowledge_build_manifest()),
        "refresh KB",
        config=app_config,
    )

    assert result.output == "top-level output"
    assert captured["built_config"] is app_config
    assert captured["input_text"] == "refresh KB"
    assert isinstance(captured["executor"], FakeWorkflowExecutor)

    plan = captured["plan"]
    assert isinstance(plan, RuntimePlan)
    assert plan.knowledge_build_runner is not None

    payload = plan.knowledge_build_runner(
        {
            "knowledge_base_id": "KB-1",
            "chunking_strategy": "recursive",
            "embedding_model": "BAAI/bge-m3",
            "wait_for_completion": True,
            "wait_timeout_seconds": 1,
            "activate_when_complete": True,
        }
    )

    request = captured["request"]
    assert request.chunking_strategy == "recursive"
    assert request.embedding_model == "BAAI/bge-m3"
    assert captured["actor"] == "@exported-workflow"
    assert captured["activated_version_id"] == "KBV-9"
    assert payload["status"] == "completed"
    assert payload["await_completion"]["status"] == "completed"
    assert payload["activation"]["status"] == "activated"
    assert payload["knowledge_base"]["active_version_id"] == "KBV-9"


def test_default_knowledge_build_runner_raises_when_waited_build_fails(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeKnowledgeService:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def create_version(self, knowledge_base_id: str, request, *, identity, actor):
            del knowledge_base_id, request, identity, actor
            return SimpleNamespace(
                knowledge_base=_Dumpable(knowledge_base_id="KB-1", active_version_id="KBV-1"),
                version=_Dumpable(
                    knowledge_base_version_id="KBV-9",
                    version_number=9,
                    status="processing",
                    chunking_strategy="recursive",
                    embedding_model="BAAI/bge-m3",
                    error_summary=None,
                ),
                run=_Dumpable(knowledge_base_run_id="KBR-9", status="running"),
            )

        def get_version(self, version_id: str, *, identity):
            del identity
            return _Dumpable(
                knowledge_base_version_id=version_id,
                version_number=9,
                status="failed",
                chunking_strategy="recursive",
                embedding_model="BAAI/bge-m3",
                error_summary="OCR crashed",
            )

        def get_knowledge_base(self, knowledge_base_id: str, *, identity):
            del identity
            return _Dumpable(knowledge_base_id=knowledge_base_id, active_version_id="KBV-1")

        def list_runs(self, knowledge_base_id: str, *, identity):
            del knowledge_base_id, identity
            return [_Dumpable(knowledge_base_run_id="KBR-9", status="failed")]

    monkeypatch.setattr(export_runtime, "KnowledgeBaseService", _FakeKnowledgeService)
    monkeypatch.setattr(export_runtime, "_sleep", lambda seconds: None)

    runner = export_runtime._default_knowledge_build_runner(
        session_factory=session_factory,
        config=app_config,
        identity=export_runtime._default_identity(active_project_id=None),
    )

    with pytest.raises(RuntimeError, match="failed: OCR crashed"):
        runner(
            {
                "knowledge_base_id": "KB-1",
                "chunking_strategy": "recursive",
                "embedding_model": "BAAI/bge-m3",
                "wait_for_completion": True,
                "wait_timeout_seconds": 1,
            }
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"chunking_strategy": "recursive", "embedding_model": "BAAI/bge-m3"},
            "knowledge_build node requires knowledge_base_id",
        ),
        (
            {"knowledge_base_id": "KB-1", "embedding_model": "BAAI/bge-m3"},
            "knowledge_build node requires chunking_strategy",
        ),
        (
            {"knowledge_base_id": "KB-1", "chunking_strategy": "recursive"},
            "knowledge_build node requires embedding_model",
        ),
    ],
)
def test_default_knowledge_build_runner_requires_mandatory_fields(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, str],
    message: str,
) -> None:
    class _FakeKnowledgeService:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def create_version(self, *args, **kwargs):
            raise AssertionError("invalid payloads should fail before build creation")

    monkeypatch.setattr(export_runtime, "KnowledgeBaseService", _FakeKnowledgeService)
    runner = export_runtime._default_knowledge_build_runner(
        session_factory=session_factory,
        config=app_config,
        identity=export_runtime._default_identity(active_project_id=None),
    )

    with pytest.raises(RuntimeError, match=message):
        runner(payload)


def test_default_knowledge_build_runner_reports_timeout_and_pending_activation(
    app_config: CaliberConfig,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeKnowledgeService:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def create_version(self, knowledge_base_id: str, request, *, identity, actor):
            del request, identity, actor
            return SimpleNamespace(
                knowledge_base=_Dumpable(
                    knowledge_base_id=knowledge_base_id, active_version_id="KBV-1"
                ),
                version=_Dumpable(
                    knowledge_base_version_id="KBV-12",
                    version_number=12,
                    status="processing",
                    chunking_strategy="recursive",
                    embedding_model="BAAI/bge-m3",
                    error_summary=None,
                ),
                run=_Dumpable(knowledge_base_run_id="KBR-12", status="running"),
            )

        def get_version(self, version_id: str, *, identity):
            del identity
            return _Dumpable(
                knowledge_base_version_id=version_id,
                version_number=12,
                status="processing",
                chunking_strategy="recursive",
                embedding_model="BAAI/bge-m3",
                error_summary=None,
            )

        def get_knowledge_base(self, knowledge_base_id: str, *, identity):
            del identity
            return _Dumpable(knowledge_base_id=knowledge_base_id, active_version_id="KBV-1")

        def list_runs(self, knowledge_base_id: str, *, identity):
            del knowledge_base_id, identity
            return [_Dumpable(knowledge_base_run_id="KBR-12", status="running")]

    monotonic_values = iter([0.0, 0.0, 0.5, 2.0])
    monkeypatch.setattr(export_runtime, "KnowledgeBaseService", _FakeKnowledgeService)
    monkeypatch.setattr(export_runtime, "_sleep", lambda seconds: None)
    monkeypatch.setattr(export_runtime, "_monotonic", lambda: next(monotonic_values))

    runner = export_runtime._default_knowledge_build_runner(
        session_factory=session_factory,
        config=app_config,
        identity=export_runtime._default_identity(active_project_id=None),
    )

    payload = runner(
        {
            "knowledge_base_id": "KB-1",
            "chunking_strategy": "recursive",
            "embedding_model": "BAAI/bge-m3",
            "wait_for_completion": True,
            "wait_timeout_seconds": 1,
            "activate_when_complete": True,
        }
    )

    assert payload["status"] == "processing"
    assert payload["await_completion"]["status"] == "timeout"
    assert payload["activation"] == {
        "requested": True,
        "status": "pending",
        "wait_status": "timeout",
    }
    assert "stopped waiting before the build reached a terminal state" in payload["summary"]


def test_execute_exported_workflow_uses_explicit_session_memory_store_without_db_bootstrap(
    app_config: CaliberConfig,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = make_manifest(
        "persistent-export",
        runtime={
            "sdk": "openai-agents-python",
            "sdk_version_policy": "runtime-pinned",
            "compiler_version": "caliber-workflow-compiler-v1",
            "default_model_ref": "CALIBER_WORKFLOW_DEFAULT_MODEL",
            "session": {"type": "persistent"},
        },
    )
    captured: dict[str, object] = {}
    store = InMemoryWorkflowSessionMemoryStore()

    def _fail_build_session_factory(config: CaliberConfig):
        del config
        raise AssertionError("session factory should not be built when a store is supplied")

    def _fake_execute(
        plan: RuntimePlan,
        input_text: str,
        *,
        executor,
        session_id: str | None = None,
        preview: bool = False,
        extra_tools=None,
    ) -> WorkflowRunResult:
        del executor, session_id, preview, extra_tools
        captured["plan"] = plan
        captured["input_text"] = input_text
        return WorkflowRunResult(status="completed", output="memory-ok")

    monkeypatch.setattr(export_runtime, "_build_session_factory", _fail_build_session_factory)
    monkeypatch.setattr(export_runtime, "execute", _fake_execute)

    result = export_runtime.execute_exported_workflow(
        _ir(manifest),
        "hello",
        config=app_config,
        executor=FakeWorkflowExecutor(),
        session_memory_store=store,
    )

    assert result.output == "memory-ok"
    assert captured["input_text"] == "hello"
    plan = captured["plan"]
    assert isinstance(plan, RuntimePlan)
    assert plan.session_memory_store is store


def test_run_exported_workflow_returns_output_when_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ir = _ir(make_manifest("exported-run-success"))
    monkeypatch.setattr(
        export_runtime,
        "execute_exported_workflow",
        lambda *args, **kwargs: WorkflowRunResult(status="completed", output="done"),
    )

    assert export_runtime.run_exported_workflow(ir, "hello") == "done"


def test_run_exported_workflow_raises_with_detail_when_not_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ir = _ir(make_manifest("exported-run-blocked"))
    monkeypatch.setattr(
        export_runtime,
        "execute_exported_workflow",
        lambda *args, **kwargs: WorkflowRunResult(
            status="blocked",
            output="",
            error="waiting on runtime approval",
        ),
    )

    with pytest.raises(
        RuntimeError,
        match="exported workflow 'exported-run-blocked' blocked: waiting on runtime approval",
    ):
        export_runtime.run_exported_workflow(ir, "hello")
