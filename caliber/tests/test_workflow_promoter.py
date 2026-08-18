"""Focused branch tests for workflow promotion service helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from sqlalchemy.orm import Session

from caliber.config import CaliberConfig, WorkflowStorageConfig
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberProject,
    CaliberSkill,
    CaliberToolRegistry,
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowPromotion,
    CaliberWorkflowVersion,
)
from caliber.storage import LocalStorageBackend, WorkingDirectoryService
from caliber.workflows import promoter
from caliber.workflows.compiler import CompileError, build_ir
from caliber.workflows.manifest import DeployGate, parse_manifest
from caliber.workflows.promoter import (
    DeployError,
    GateResult,
    PublishError,
    RollbackError,
    approve_promotion,
    compile_version,
    evaluate_deploy_gates,
    promote,
    prune_workflow_runs,
    publish_version,
    reject_promotion,
    resolver_from_session,
    rollback,
    run_preview,
)
from caliber.workflows.runtime import (
    OpenAIAgentsWorkflowExecutor,
    OpenAIChatWorkflowExecutor,
    OpenAIResponsesWorkflowExecutor,
)
from tests.workflow_helpers import fake_resolver, make_manifest


def _seed_workflow(session: Session) -> None:
    session.add(CaliberWorkflow(workflow_id="wf", name="Workflow", owner="@test"))


def _version(
    *,
    version_id: str = "wfv-1",
    status: str = "published",
    manifest: dict[str, object] | None = None,
    number: int = 1,
) -> CaliberWorkflowVersion:
    return CaliberWorkflowVersion(
        version_id=version_id,
        workflow_id="wf",
        version_number=number,
        status=status,
        manifest=manifest or make_manifest("wf"),
        manifest_hash=f"hash-{version_id}",
    )


def test_run_preview_reads_hash_verified_managed_project_file(
    db_session: Session,
    tmp_path: Path,
) -> None:
    storage_config = WorkflowStorageConfig(base_uri=f"file://{tmp_path}/preview-files")
    service = WorkingDirectoryService(LocalStorageBackend(storage_config.base_uri), storage_config)
    project = CaliberProject(
        project_id="PRJ-preview",
        tenant_id="tenant-preview",
        name="Preview files",
        owner="@test",
    )
    workflow = CaliberWorkflow(
        workflow_id="wf-preview",
        project_id=project.project_id,
        name="Managed preview",
        owner="@test",
    )
    db_session.add_all([project, workflow])
    record = service.register_project_file(
        db_session,
        project_id=project.project_id,
        tenant_id=project.tenant_id,
        kind="input",
        filename="source.md",
        data=b"verified preview content",
        media_type="text/markdown",
        actor="@test",
    )
    manifest = make_manifest("wf-preview")
    manifest["nodes"]["managed_source"] = {
        "id": "managed_source",
        "type": "file_input",
        "file_ref": record.to_api()["immutable_ref"],
    }
    manifest["edges"] = [
        {
            "id": "start_source",
            "from": "start",
            "to": "managed_source",
            "map": {"msg": "path"},
        },
        {
            "id": "source_agent",
            "from": "managed_source",
            "to": "agent",
            "map": {"text": "input"},
        },
        {
            "id": "agent_final",
            "from": "agent",
            "to": "final",
            "map": {"final_output": "response"},
        },
    ]
    version = CaliberWorkflowVersion(
        version_id="wfv-preview",
        workflow_id=workflow.workflow_id,
        version_number=1,
        status="draft",
        manifest=manifest,
        manifest_hash="preview-hash",
        created_by="@test",
    )
    db_session.add(version)
    db_session.commit()

    result = run_preview(
        db_session,
        version,
        "ignored",
        config=CaliberConfig(workflow_storage=storage_config),
    )

    assert result["status"] == "completed"
    source_step = next(step for step in result["steps"] if step["node_id"] == "managed_source")
    assert source_step["output"] == "verified preview content"


def test_resolver_from_session_records_successor_refs(db_session: Session) -> None:
    successor = CaliberToolRegistry(
        tool_id="tool-new",
        name="lookup_policy",
        version="2.0",
        module_path="m",
        callable_name="lookup_v2",
        status="active",
    )
    old = CaliberToolRegistry(
        tool_id="tool-old",
        name="lookup_policy",
        version="1.0",
        module_path="m",
        callable_name="lookup_v1",
        status="deprecated",
        successor_tool_id="tool-new",
    )
    archived = CaliberToolRegistry(
        tool_id="tool-archived",
        name="archived",
        version="1.0",
        module_path="m",
        callable_name="a",
        status="archived",
    )
    db_session.add_all([successor, old, archived])
    db_session.commit()

    resolver = resolver_from_session(db_session)
    old_resolution = resolver.resolve("tool.lookup_policy.v1", "<2.0")

    assert old_resolution.entry.successor_ref == "tool.lookup_policy.v2"
    assert "tool.lookup_policy.v2" in old_resolution.warnings[0]
    with pytest.raises(Exception, match="not registered"):
        resolver.resolve("tool.archived.v1")


def test_compile_version_persist_false_leaves_version_metadata_unset(
    db_session: Session,
) -> None:
    _seed_workflow(db_session)
    version = _version(status="draft")
    db_session.add(version)
    db_session.commit()

    result = compile_version(db_session, version, resolver=fake_resolver(), persist=False)

    assert result.manifest_hash
    assert version.compiled_artifact_uri is None
    assert version.compiled_bundle is None


def test_prune_workflow_runs_disabled_returns_zero(db_session: Session) -> None:
    assert prune_workflow_runs(db_session, retention_days=0) == 0


def test_publish_version_rejects_deprecated_and_wraps_compile_error(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_workflow(db_session)
    deprecated = _version(status="deprecated")
    with pytest.raises(PublishError, match="deprecated"):
        publish_version(db_session, deprecated, actor="@test")

    draft = _version(status="draft", version_id="wfv-draft")
    monkeypatch.setattr(
        promoter,
        "compile_version",
        lambda *args, **kwargs: (_ for _ in ()).throw(CompileError("bad graph")),
    )
    with pytest.raises(PublishError, match="does not compile"):
        publish_version(db_session, draft, actor="@test")


def test_example_input_and_gate_name_fallbacks() -> None:
    assert (
        promoter._example_input(SimpleNamespace(input={"other": "first string"})) == "first string"
    )
    assert (
        promoter._example_input(SimpleNamespace(input={"nested": {"x": 1}}))
        == '{"nested": {"x": 1}}'
    )

    manifest = parse_manifest(make_manifest("wf"))
    gate = DeployGate(dataset_ref="external-dataset", required_for_aliases=["prod"])
    assert promoter._gate_name(manifest, gate) == "external-dataset"


@pytest.mark.parametrize(
    ("age_enabled", "sync_status", "expected_mode"),
    [
        (True, "synced", "age_graph"),
        (True, "processing", "graph_hybrid"),
        (False, "synced", "graph_hybrid"),
    ],
)
def test_build_plan_resolves_empty_knowledge_query_modes_from_kb_default(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
    age_enabled: bool,
    sync_status: str,
    expected_mode: str,
) -> None:
    _seed_workflow(db_session)
    manifest = make_manifest("wf")
    manifest["nodes"]["knowledge"] = {
        "id": "knowledge",
        "type": "knowledge_query",
        "knowledge_base_id": "KB-1",
        "version_ids": [],
        "retrieval_modes": [],
        "top_k": 4,
    }
    manifest["edges"] = [
        {"id": "e_start_knowledge", "from": "start", "to": "knowledge", "map": {"msg": "question"}},
        {
            "id": "e_knowledge_final",
            "from": "knowledge",
            "to": "final",
            "map": {"answer": "response"},
        },
    ]
    version = _version(manifest=manifest)
    db_session.add(version)
    db_session.commit()

    captured: dict[str, object] = {}

    class _QueryResult:
        def __init__(self, question: str) -> None:
            self._question = question

        def model_dump(self, mode: str = "json") -> dict[str, object]:
            return {"question": self._question, "versions": []}

    class _FakeKnowledgeService:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def get_knowledge_base(self, knowledge_base_id: str, *, identity) -> SimpleNamespace:
            captured["knowledge_base_id"] = knowledge_base_id
            return SimpleNamespace(active_version_id="KBV-7")

        def get_version(self, version_id: str, *, identity) -> SimpleNamespace:
            captured["resolved_version_id"] = version_id
            return SimpleNamespace(
                graph_config=SimpleNamespace(
                    default_retrieval_mode="age_graph",
                    output_target="object_store_and_age",
                ),
                summary={"age_sync_status": sync_status},
            )

        def options(self) -> SimpleNamespace:
            return SimpleNamespace(age_enabled=age_enabled)

        def query(self, request, *, identity) -> _QueryResult:
            captured["request"] = request
            return _QueryResult(request.question)

    monkeypatch.setattr(promoter, "KnowledgeBaseService", _FakeKnowledgeService)

    plan = promoter.build_plan(db_session, version, resolver=fake_resolver())
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
    assert request.version_ids == ["KBV-7"]
    assert request.retrieval_modes == [expected_mode]
    assert payload["question"] == "What is the refund policy?"
    # The primary (build_plan) runner pins the resolved active version into the
    # node output so the run stays reproducible after the KB active pointer moves.
    assert payload["resolved_version_ids"] == ["KBV-7"]
    assert payload["resolved_knowledge_base_id"] == "KB-1"


def test_build_plan_knowledge_build_runner_can_wait_and_activate(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_workflow(db_session)
    manifest = make_manifest("wf")
    manifest["nodes"]["knowledge_build"] = {
        "id": "knowledge_build",
        "type": "knowledge_build",
        "knowledge_base_id": "KB-1",
        "chunking_strategy": "recursive",
        "embedding_model": "BAAI/bge-m3",
        "wait_for_completion": True,
        "activate_when_complete": True,
    }
    manifest["edges"] = [
        {"id": "e_start_build", "from": "start", "to": "knowledge_build", "map": {"msg": "input"}},
        {
            "id": "e_build_final",
            "from": "knowledge_build",
            "to": "final",
            "map": {"text": "response"},
        },
    ]
    version = _version(manifest=manifest)
    db_session.add(version)
    db_session.commit()

    captured: dict[str, object] = {}

    class _Dumpable(SimpleNamespace):
        def model_dump(self, mode: str = "json") -> dict[str, object]:
            del mode
            return dict(self.__dict__)

    class _FakeKnowledgeService:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def create_version(self, knowledge_base_id: str, request, *, identity, actor):
            captured["knowledge_base_id"] = knowledge_base_id
            captured["request"] = request
            captured["actor"] = actor
            return SimpleNamespace(
                knowledge_base=_Dumpable(
                    knowledge_base_id=knowledge_base_id, active_version_id="KBV-1"
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
            return _Dumpable(
                knowledge_base_version_id=version_id,
                version_number=9,
                status="completed",
                chunking_strategy="recursive",
                embedding_model="BAAI/bge-m3",
                error_summary=None,
            )

        def get_knowledge_base(self, knowledge_base_id: str, *, identity):
            return _Dumpable(knowledge_base_id=knowledge_base_id, active_version_id="KBV-1")

        def list_runs(self, knowledge_base_id: str, *, identity):
            return [_Dumpable(knowledge_base_run_id="KBR-9", status="completed")]

        def activate_version(self, knowledge_base_id: str, version_id: str, *, identity, actor):
            captured["activated_version_id"] = version_id
            return _Dumpable(knowledge_base_id=knowledge_base_id, active_version_id=version_id)

    monkeypatch.setattr(promoter, "KnowledgeBaseService", _FakeKnowledgeService)

    plan = promoter.build_plan(db_session, version, resolver=fake_resolver())
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
    assert captured["activated_version_id"] == "KBV-9"
    assert payload["status"] == "completed"
    assert payload["activation"]["status"] == "activated"
    assert payload["knowledge_base"]["active_version_id"] == "KBV-9"


def test_build_plan_resolves_agent_skills_from_stored_skill_rows(
    db_session: Session,
) -> None:
    _seed_workflow(db_session)
    manifest = make_manifest("wf")
    manifest["nodes"]["agent"]["skills"] = ["tone", "safety", "missing"]
    version = _version(manifest=manifest)
    db_session.add_all(
        [
            version,
            CaliberSkill(
                skill_id="SK-tone",
                name="tone",
                description="",
                content="Be concise.",
                owner="@test",
                tags=[],
                status="active",
                version=1,
            ),
            CaliberSkill(
                skill_id="SK-safety",
                name="safety",
                description="",
                content="Refuse harmful asks.",
                owner="@test",
                tags=[],
                status="active",
                version=1,
            ),
        ]
    )
    db_session.commit()

    plan = promoter.build_plan(db_session, version, resolver=fake_resolver())

    agent = plan.ir.nodes["agent"]
    assert agent.skill_instructions == ["Be concise.", "Refuse harmful asks."]


def test_evaluate_deploy_gate_with_missing_dataset_fails_closed(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        promoter, "compile_workflow", lambda *args, **kwargs: SimpleNamespace(ir=None)
    )
    manifest = parse_manifest(
        make_manifest(
            "wf",
            deploy_gates={
                "support_eval": {
                    "type": "deploy_gate",
                    "dataset_ref": "missing",
                    "required_for_aliases": ["prod"],
                    "thresholds": {"min_pass_rate": 1.0},
                }
            },
        )
    )

    result = evaluate_deploy_gates(
        db_session,
        manifest,
        "prod",
        resolver=fake_resolver(),
        executor=promoter.build_executor(None),
    )

    assert result.has_gate is True
    assert result.passed is False
    assert result.runs[0].passed is False
    assert result.runs[0].pass_rate == 0.0
    assert result.runs[0].n_examples == 0
    assert result.runs[0].detail == "dataset not found; gate failed closed"


def test_evaluate_deploy_gate_with_empty_dataset_fails_closed(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        promoter, "compile_workflow", lambda *args, **kwargs: SimpleNamespace(ir=None)
    )
    db_session.add(
        CaliberEvalDataset(
            dataset_id="eval-empty",
            name="empty",
            owner="@test",
            status="active",
            version=1,
        )
    )
    db_session.commit()
    manifest = parse_manifest(
        make_manifest(
            "wf",
            deploy_gates={
                "empty_eval": {
                    "type": "deploy_gate",
                    "dataset_ref": "empty",
                    "required_for_aliases": ["prod"],
                }
            },
        )
    )

    result = evaluate_deploy_gates(
        db_session,
        manifest,
        "prod",
        resolver=fake_resolver(),
        executor=promoter.build_executor(None),
    )

    assert result.passed is False
    assert result.runs[0].passed is False
    assert result.runs[0].pass_rate == 0.0
    assert result.runs[0].n_examples == 0
    assert result.runs[0].detail == "dataset has no active examples; gate failed closed"


def test_evaluate_deploy_gate_with_archived_dataset_fails_closed(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        promoter, "compile_workflow", lambda *args, **kwargs: SimpleNamespace(ir=None)
    )
    dataset = CaliberEvalDataset(
        dataset_id="eval-archived",
        name="archived",
        owner="@test",
        status="archived",
        version=1,
    )
    db_session.add(dataset)
    db_session.add(
        CaliberEvalDatasetExample(
            example_id="archived-example",
            dataset_id=dataset.dataset_id,
            dataset_version=1,
            input={"input": "must not run"},
            expected={},
        )
    )
    db_session.commit()
    manifest = parse_manifest(
        make_manifest(
            "wf",
            deploy_gates={
                "archived_eval": {
                    "type": "deploy_gate",
                    "dataset_ref": "archived",
                    "required_for_aliases": ["prod"],
                }
            },
        )
    )

    result = evaluate_deploy_gates(
        db_session,
        manifest,
        "prod",
        resolver=fake_resolver(),
        executor=promoter.build_executor(None),
    )

    assert result.passed is False
    assert result.runs[0].passed is False
    assert result.runs[0].pass_rate == 0.0
    assert result.runs[0].n_examples == 0
    assert result.runs[0].detail == "dataset is archived; gate failed closed"


def test_evaluate_deploy_gate_orders_bounded_sample_and_uses_preview(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        promoter, "compile_workflow", lambda *args, **kwargs: SimpleNamespace(ir=None)
    )
    dataset = CaliberEvalDataset(
        dataset_id="eval-ordered",
        name="ordered",
        owner="@test",
        status="active",
        version=1,
    )
    db_session.add(dataset)
    db_session.add_all(
        [
            CaliberEvalDatasetExample(
                example_id="example-c",
                dataset_id=dataset.dataset_id,
                dataset_version=1,
                input={"input": "late"},
                expected={},
                created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            ),
            CaliberEvalDatasetExample(
                example_id="example-b",
                dataset_id=dataset.dataset_id,
                dataset_version=1,
                input={"input": "second"},
                expected={},
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
            CaliberEvalDatasetExample(
                example_id="example-a",
                dataset_id=dataset.dataset_id,
                dataset_version=1,
                input={"input": "first"},
                expected={},
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            ),
        ]
    )
    db_session.commit()
    calls: list[tuple[str, bool]] = []

    def _execute(plan, input_text, *, executor, preview=False):
        del plan, executor
        calls.append((input_text, preview))
        return SimpleNamespace(status="completed", output=input_text, tokens=3, error=None)

    monkeypatch.setattr(promoter, "execute", _execute)
    manifest = parse_manifest(
        make_manifest(
            "wf",
            deploy_gates={
                "ordered_eval": {
                    "type": "deploy_gate",
                    "dataset_ref": "ordered",
                    "required_for_aliases": ["prod"],
                    # These examples carry no expected output, so the assertion
                    # available here is completion, not quality. ``min_pass_rate``
                    # now means "completed AND met the scorer threshold", so the
                    # completion-only claim has its own explicit key.
                    "thresholds": {"min_completion_rate": 1.0},
                }
            },
        )
    )

    result = evaluate_deploy_gates(
        db_session,
        manifest,
        "prod",
        resolver=fake_resolver(),
        executor=promoter.build_executor(None),
        sample_size=2,
    )

    assert calls == [("first", True), ("second", True)]
    # A bounded sample must disclose that it was bounded, and identify exactly
    # which rows it graded.
    run = result.runs[0]
    assert run.n_examples == 2
    assert run.available_examples == 3
    assert run.sample_digest is not None and run.sample_digest.startswith("sha256:")
    assert run.dataset_id == "eval-ordered"
    assert run.dataset_version == 1
    assert run.metrics["completion_rate"] == 1.0
    assert run.metrics["total_tokens"] == 6.0
    assert result.passed is True
    assert result.runs[0].n_examples == 2


def test_promote_prod_supersedes_pending_requests(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_workflow(db_session)
    old_version = _version(version_id="wfv-old", number=0)
    version = _version()
    existing = CaliberWorkflowDeployment(
        deployment_id="dep-prod",
        workflow_id="wf",
        alias="prod",
        version_id="wfv-old",
    )
    prior = CaliberWorkflowPromotion(
        promotion_id="prom-old",
        workflow_id="wf",
        alias="prod",
        version_id="wfv-old",
        status="pending",
        gate_result={},
        requested_by="@old",
    )
    db_session.add_all([old_version, version, existing, prior])
    db_session.commit()
    # v1 single-environment leaves GATED_ALIASES empty; restore the gated path so
    # this test still covers the dormant promotion-approval machinery.
    monkeypatch.setattr(promoter, "GATED_ALIASES", frozenset({"prod"}))
    monkeypatch.setattr(
        promoter,
        "evaluate_deploy_gates",
        lambda *args, **kwargs: GateResult(has_gate=True, passed=True),
    )

    result = promote(
        db_session,
        "wf",
        "prod",
        version,
        actor="@test",
        resolver=fake_resolver(),
        executor=promoter.build_executor(None),
    )

    assert result.rotated is False
    assert result.deployment is existing
    assert prior.status == "superseded"
    assert result.promotion is not None
    assert result.promotion.status == "pending"


def test_approve_and_reject_promotion_error_paths(db_session: Session) -> None:
    _seed_workflow(db_session)
    published = _version(version_id="wfv-published")
    draft = _version(version_id="wfv-draft", status="draft", number=2)
    pending = CaliberWorkflowPromotion(
        promotion_id="prom-pending",
        workflow_id="wf",
        alias="prod",
        version_id=published.version_id,
        status="pending",
        gate_result={},
        requested_by="@test",
    )
    approved = CaliberWorkflowPromotion(
        promotion_id="prom-approved",
        workflow_id="wf",
        alias="prod",
        version_id=published.version_id,
        status="approved",
        gate_result={},
        requested_by="@test",
    )
    bad_target = CaliberWorkflowPromotion(
        promotion_id="prom-draft",
        workflow_id="wf",
        alias="prod",
        version_id=draft.version_id,
        status="pending",
        gate_result={},
        requested_by="@test",
    )
    db_session.add_all([published, draft, pending, approved, bad_target])
    db_session.commit()

    with pytest.raises(DeployError, match="not pending"):
        approve_promotion(db_session, approved, actor="@test")
    with pytest.raises(DeployError, match="missing or not published"):
        approve_promotion(db_session, bad_target, actor="@test")
    with pytest.raises(DeployError, match="not pending"):
        reject_promotion(db_session, approved, actor="@test")


def test_rollback_errors_without_deployment_or_checkpoint(db_session: Session) -> None:
    _seed_workflow(db_session)
    with pytest.raises(RollbackError, match="no deployment"):
        rollback(db_session, "wf", "dev", actor="@test")

    db_session.add(
        CaliberWorkflowDeployment(
            deployment_id="dep-dev",
            workflow_id="wf",
            alias="dev",
            version_id="wfv-1",
            rollback_checkpoint=[],
        )
    )
    db_session.commit()

    with pytest.raises(RollbackError, match="no rollback checkpoint"):
        rollback(db_session, "wf", "dev", actor="@test")


def test_promote_registers_agent_nodes_in_fleet(
    db_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deploying (rotating) a version upserts its agent nodes into the fleet."""
    _seed_workflow(db_session)
    version = _version()
    db_session.add(version)
    db_session.commit()
    monkeypatch.setattr(
        promoter,
        "evaluate_deploy_gates",
        lambda *args, **kwargs: GateResult(has_gate=False, passed=True),
    )

    result = promote(
        db_session,
        "wf",
        "dev",
        version,
        actor="@deployer",
        resolver=fake_resolver(),
        executor=promoter.build_executor(None),
    )
    assert result.rotated is True

    agent_id = promoter._fleet_agent_id("wf", "agent")
    agent = db_session.get(CaliberAgentConfig, agent_id)
    assert agent is not None
    assert agent.name == "test-agent"
    assert agent.owner == "@deployer"
    assert "prompt" in agent.artifact_types
    assert agent.optimizer_config["source_workflow_id"] == "wf"

    # Re-deploying the same version is idempotent — no duplicate fleet rows.
    promote(
        db_session,
        "wf",
        "staging",
        version,
        actor="@deployer",
        resolver=fake_resolver(),
        executor=promoter.build_executor(None),
    )
    rows = db_session.query(CaliberAgentConfig).all()
    assert len(rows) == 1


def test_build_executor_threads_gateway_base_url(
    app_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When config.llm_base_url is set, the OpenAI executor client points at it
    (so workflow agent calls route through the MLflow AI Gateway)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-xyz")
    cfg = app_config.model_copy(
        update={"llm_provider": "openai", "llm_base_url": "http://gw:5000/gateway/mlflow/v1"}
    )
    ex = promoter.build_executor(cfg)
    assert str(ex._client.base_url).rstrip("/") == "http://gw:5000/gateway/mlflow/v1"
    assert ex._parallel_tool_calls is False
    assert ex._prompt_cache_enabled is False
    assert ex._prompt_cache_retention is None


def test_build_executor_default_keeps_direct_openai(
    app_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty llm_base_url (default) preserves direct api.openai.com routing."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-xyz")
    cfg = app_config.model_copy(update={"llm_provider": "openai"})
    ex = promoter.build_executor(cfg)
    assert urlparse(str(ex._client.base_url)).hostname == "api.openai.com"
    assert ex._parallel_tool_calls is True
    assert ex._prompt_cache_enabled is True
    assert ex._prompt_cache_retention is None


def test_build_executor_can_select_openai_responses_executor(
    app_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-xyz")
    cfg = app_config.model_copy(
        update={"llm_provider": "openai", "openai_workflow_api": "responses"}
    )
    ex = promoter.build_executor(cfg)
    assert isinstance(ex, OpenAIResponsesWorkflowExecutor)
    assert ex._parallel_tool_calls is True
    assert ex._prompt_cache_enabled is True


def test_build_executor_can_select_openai_agents_executor(
    app_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-xyz")
    cfg = app_config.model_copy(
        update={"llm_provider": "openai", "openai_workflow_api": "agents_sdk"}
    )
    ex = promoter.build_executor(cfg)
    assert isinstance(ex, OpenAIAgentsWorkflowExecutor)
    assert ex._prompt_cache_enabled is True
    assert ex._prompt_cache_retention is None


def test_build_executor_can_force_prompt_cache_hints_for_agents_gateways(
    app_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-xyz")
    cfg = app_config.model_copy(
        update={
            "llm_provider": "openai",
            "llm_base_url": "http://gw:5000/gateway/mlflow/v1",
            "openai_workflow_api": "agents_sdk",
            "openai_prompt_cache_mode": "enabled",
            "openai_prompt_cache_retention": "24h",
        }
    )
    ex = promoter.build_executor(cfg)
    assert isinstance(ex, OpenAIAgentsWorkflowExecutor)
    assert ex._prompt_cache_enabled is True
    assert ex._prompt_cache_retention == "24h"


def test_build_executor_can_force_parallel_tool_calls_for_agents_gateways(
    app_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-xyz")
    cfg = app_config.model_copy(
        update={
            "llm_provider": "openai",
            "llm_base_url": "http://gw:5000/gateway/mlflow/v1",
            "openai_workflow_api": "agents_sdk",
            "openai_workflow_parallel_tool_calls": "enabled",
        }
    )
    ex = promoter.build_executor(cfg)
    assert isinstance(ex, OpenAIAgentsWorkflowExecutor)
    assert ex._parallel_tool_calls is True


def test_build_executor_can_force_parallel_tool_calls_for_gateways(
    app_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-xyz")
    cfg = app_config.model_copy(
        update={
            "llm_provider": "openai",
            "llm_base_url": "http://gw:5000/gateway/mlflow/v1",
            "openai_workflow_parallel_tool_calls": "enabled",
        }
    )
    ex = promoter.build_executor(cfg)
    assert isinstance(ex, OpenAIChatWorkflowExecutor)
    assert ex._parallel_tool_calls is True


def test_build_executor_can_force_prompt_cache_hints_for_gateways(
    app_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-xyz")
    cfg = app_config.model_copy(
        update={
            "llm_provider": "openai",
            "llm_base_url": "http://gw:5000/gateway/mlflow/v1",
            "openai_prompt_cache_mode": "enabled",
            "openai_prompt_cache_retention": "24h",
        }
    )
    ex = promoter.build_executor(cfg)
    assert isinstance(ex, OpenAIChatWorkflowExecutor)
    assert ex._prompt_cache_enabled is True
    assert ex._prompt_cache_retention == "24h"


def test_build_executor_respects_workflow_openai_api_override(
    app_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-xyz")
    cfg = app_config.model_copy(update={"llm_provider": "openai"})
    manifest = parse_manifest(
        make_manifest(
            runtime={
                "sdk": "openai-agents-python",
                "sdk_version_policy": "runtime-pinned",
                "compiler_version": "caliber-workflow-compiler-v1",
                "default_model_ref": "CALIBER_WORKFLOW_DEFAULT_MODEL",
                "openai": {"workflow_api": "responses"},
            }
        )
    )
    ir = build_ir(manifest, fake_resolver(), version="7")
    ex = promoter.build_executor(cfg, ir=ir)
    assert isinstance(ex, OpenAIResponsesWorkflowExecutor)


def test_build_executor_respects_workflow_openai_runtime_overrides(
    app_config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-xyz")
    cfg = app_config.model_copy(
        update={
            "llm_provider": "openai",
            "llm_base_url": "http://gw:5000/gateway/mlflow/v1",
            "openai_workflow_parallel_tool_calls": "disabled",
            "openai_prompt_cache_mode": "disabled",
        }
    )
    manifest = parse_manifest(
        make_manifest(
            runtime={
                "sdk": "openai-agents-python",
                "sdk_version_policy": "runtime-pinned",
                "compiler_version": "caliber-workflow-compiler-v1",
                "default_model_ref": "CALIBER_WORKFLOW_DEFAULT_MODEL",
                "openai": {
                    "parallel_tool_calls": "enabled",
                    "prompt_cache_mode": "enabled",
                    "prompt_cache_retention": "24h",
                },
            }
        )
    )
    ir = build_ir(manifest, fake_resolver(), version="7")
    ex = promoter.build_executor(cfg, ir=ir)
    assert isinstance(ex, OpenAIChatWorkflowExecutor)
    assert ex._parallel_tool_calls is True
    assert ex._prompt_cache_enabled is True
    assert ex._prompt_cache_retention == "24h"
