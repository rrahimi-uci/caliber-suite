"""Helpers for executing generated exported workflow modules.

The compiler's direct Agents SDK export is readable for pure agent graphs, but
mixed workflows (template/tool/router/wait/subworkflow/etc.) need the full
runtime interpreter to behave faithfully. Generated modules import this helper
when they embed an IR snapshot and want ``run()`` to execute the whole graph.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from caliber.auth import (
    SCOPE_ADMIN,
    SCOPE_APPROVER,
    SCOPE_OPERATOR,
    SCOPE_VIEWER,
    CaliberIdentity,
)
from caliber.config import CaliberConfig
from caliber.db.models import CaliberWorkflowDeployment, CaliberWorkflowVersion
from caliber.db.session import create_engine_from_config, sessionmaker_from_engine
from caliber.knowledge.schemas import KnowledgeBaseVersionCreateRequest, KnowledgeQueryRequest
from caliber.knowledge.service import KnowledgeBaseService
from caliber.workflows.ir import IRKnowledgeBuild, IRKnowledgeQuery, IRSubworkflow, IRWorkflow
from caliber.workflows.runtime import (
    FakeWorkflowExecutor,
    RuntimePlan,
    WorkflowExecutor,
    WorkflowRunResult,
    current_run_context,
    execute,
)
from caliber.workflows.session_memory import (
    InMemoryWorkflowSessionMemoryStore,
    SqlWorkflowSessionMemoryStore,
    WorkflowSessionMemoryStore,
)
from caliber.workflows.tools import InMemoryToolResolver, ToolResolver


def _monotonic() -> float:
    return time.monotonic()


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _default_identity(*, active_project_id: str | None) -> CaliberIdentity:
    """Identity used when an exported module runs outside request auth.

    Exported modules are trusted automation artifacts rather than user-scoped UI
    requests, so the default is an internal service identity with broad
    visibility. Callers can override this with an explicit ``identity``.
    """

    return CaliberIdentity(
        user_id="@exported-workflow",
        scopes=frozenset(
            {
                SCOPE_ADMIN,
                SCOPE_APPROVER,
                SCOPE_OPERATOR,
                SCOPE_VIEWER,
            }
        ),
        active_project_id=active_project_id,
    )


def _workflow_requires_database(
    ir: IRWorkflow,
    *,
    subworkflow_runner: Callable[..., Any] | None,
    knowledge_query_runner: Callable[[dict[str, Any]], dict[str, Any]] | None,
    knowledge_build_runner: Callable[[dict[str, Any]], dict[str, Any]] | None,
    session_memory_store: WorkflowSessionMemoryStore | None,
) -> bool:
    if ir.session_mode == "persistent" and session_memory_store is None:
        return True
    for node in ir.nodes.values():
        if isinstance(node, IRSubworkflow) and subworkflow_runner is None:
            return True
        if isinstance(node, IRKnowledgeQuery) and knowledge_query_runner is None:
            return True
        if isinstance(node, IRKnowledgeBuild) and knowledge_build_runner is None:
            return True
    return False


def _build_session_factory(config: CaliberConfig) -> sessionmaker[Session]:
    try:
        return sessionmaker_from_engine(create_engine_from_config(config))
    except Exception as exc:  # pragma: no cover - exercised through runtime errors
        raise RuntimeError(
            "exported workflow requires a database-backed CALIBER runtime; "
            "set CALIBER_DATABASE_URL appropriately and install the required DB driver extras"
        ) from exc


def _default_session_memory_store(
    ir: IRWorkflow,
    *,
    session_factory: sessionmaker[Session] | None,
) -> WorkflowSessionMemoryStore | None:
    if ir.session_mode == "in_memory":
        return InMemoryWorkflowSessionMemoryStore()
    if ir.session_mode == "persistent":
        if session_factory is None:
            raise RuntimeError(
                "persistent workflow session memory requires a database-backed session factory"
            )
        return SqlWorkflowSessionMemoryStore(session_factory=session_factory)
    return None


def _default_subworkflow_runner(
    *,
    session_factory: sessionmaker[Session],
    config: CaliberConfig,
) -> Callable[[str, str, str, float, int, WorkflowExecutor, bool], dict[str, Any]]:
    from caliber.workflows.promoter import build_plan  # noqa: PLC0415

    def _resolve_version(
        session: Session, workflow_id: str, run_alias: str
    ) -> CaliberWorkflowVersion:
        if run_alias == "manual":
            latest = (
                session.execute(
                    select(CaliberWorkflowVersion)
                    .where(CaliberWorkflowVersion.workflow_id == workflow_id)
                    .order_by(CaliberWorkflowVersion.version_number.desc())
                )
                .scalars()
                .first()
            )
            if latest is None:
                raise RuntimeError(f"no versions found for subworkflow {workflow_id!r}")
            return latest
        deployment = (
            session.execute(
                select(CaliberWorkflowDeployment)
                .where(
                    CaliberWorkflowDeployment.workflow_id == workflow_id,
                    CaliberWorkflowDeployment.alias == run_alias,
                    CaliberWorkflowDeployment.status == "active",
                )
                .order_by(CaliberWorkflowDeployment.deployed_at.desc())
            )
            .scalars()
            .first()
        )
        if deployment is None:
            raise RuntimeError(
                f"no active deployment for subworkflow {workflow_id!r} alias {run_alias!r}"
            )
        target = session.get(CaliberWorkflowVersion, deployment.version_id)
        if target is None:
            raise RuntimeError(
                f"deployment {deployment.deployment_id!r} references missing version "
                f"{deployment.version_id!r}"
            )
        return target

    def _run_subworkflow(
        workflow_id: str,
        run_alias: str,
        input_text: str,
        timeout_seconds: float,  # noqa: ARG001 - part of the public runner signature
        depth: int,
        executor: WorkflowExecutor,
        preview: bool,
    ) -> dict[str, Any]:
        max_depth = 3
        if depth > max_depth:
            return {
                "status": "error",
                "output": "",
                "error": f"subworkflow recursion depth exceeded ({max_depth})",
                "tokens": 0,
            }
        with session_factory() as session:
            target_version = _resolve_version(session, workflow_id, run_alias)
            sub_plan = build_plan(
                session,
                target_version,
                alias=run_alias,
                subworkflow_depth=depth,
                config=config,
                session_factory=session_factory,
            )
        run_ctx = current_run_context()
        result = execute(
            sub_plan,
            input_text,
            executor=executor,
            session_id=run_ctx.session_id if run_ctx else None,
            preview=preview,
        )
        return {
            "status": result.status,
            "workflow_id": workflow_id,
            "alias": run_alias,
            "workflow_version_id": target_version.version_id,
            "workflow_version_number": target_version.version_number,
            "output": result.output,
            "error": result.error,
            "tokens": result.tokens,
            "steps": [step.node_id for step in result.steps],
        }

    return _run_subworkflow


def _default_knowledge_query_runner(
    *,
    session_factory: sessionmaker[Session],
    config: CaliberConfig,
    identity: CaliberIdentity,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    knowledge_service = KnowledgeBaseService(config=config, session_factory=session_factory)

    def _normalize_retrieval_modes(value: Any) -> list[str] | None:
        if value is None:
            return None
        items = [value] if isinstance(value, str) else list(value)
        return list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))

    def _resolve_default_retrieval_modes(*, resolved_version_ids: list[str]) -> list[str]:
        primary_version = knowledge_service.get_version(
            resolved_version_ids[0],
            identity=identity,
        )
        graph_config = primary_version.graph_config
        summary = primary_version.summary if isinstance(primary_version.summary, dict) else {}
        mode = str(graph_config.default_retrieval_mode or "graph_hybrid")
        age_ready = (
            knowledge_service.options().age_enabled
            and graph_config.output_target == "object_store_and_age"
            and str(summary.get("age_sync_status") or "").lower() == "synced"
        )
        if mode == "age_graph" and not age_ready:
            mode = "graph_hybrid"
        return [mode]

    def _run_knowledge_query(payload: dict[str, Any]) -> dict[str, Any]:
        raw = dict(payload)
        version_ids = [
            str(item).strip() for item in list(raw.get("version_ids") or []) if str(item).strip()
        ]
        knowledge_base_id = str(raw.get("knowledge_base_id") or "").strip()
        if not version_ids and knowledge_base_id:
            knowledge_base = knowledge_service.get_knowledge_base(
                knowledge_base_id,
                identity=identity,
            )
            if not knowledge_base.active_version_id:
                raise RuntimeError(f"knowledge base {knowledge_base_id!r} has no active version")
            version_ids = [knowledge_base.active_version_id]
        if not version_ids:
            raise RuntimeError(
                "knowledge_query node requires at least one version_id or knowledge_base_id"
            )
        raw_retrieval_modes = _normalize_retrieval_modes(raw.get("retrieval_modes"))
        retrieval_modes = (
            _resolve_default_retrieval_modes(resolved_version_ids=version_ids)
            if raw_retrieval_modes == []
            else (raw_retrieval_modes or ["dense"])
        )
        request = KnowledgeQueryRequest.model_validate(
            {
                "version_ids": version_ids,
                "question": raw.get("question") or "",
                "history": raw.get("history") or [],
                "top_k": raw.get("top_k", 6),
                "chat_model": raw.get("chat_model"),
                "retrieval_modes": retrieval_modes,
                "graph_overrides": raw.get("graph_overrides"),
            }
        )
        return knowledge_service.query(
            request,
            identity=identity,
        ).model_dump(mode="json")

    return _run_knowledge_query


def _default_knowledge_build_runner(
    *,
    session_factory: sessionmaker[Session],
    config: CaliberConfig,
    identity: CaliberIdentity,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    knowledge_service = KnowledgeBaseService(config=config, session_factory=session_factory)

    def _run_knowledge_build(payload: dict[str, Any]) -> dict[str, Any]:
        raw = dict(payload)
        knowledge_base_id = str(raw.get("knowledge_base_id") or "").strip()
        if not knowledge_base_id:
            raise RuntimeError("knowledge_build node requires knowledge_base_id")
        chunking_strategy = str(raw.get("chunking_strategy") or "").strip()
        embedding_model = str(raw.get("embedding_model") or "").strip()
        if not chunking_strategy:
            raise RuntimeError("knowledge_build node requires chunking_strategy")
        if not embedding_model:
            raise RuntimeError("knowledge_build node requires embedding_model")

        request = KnowledgeBaseVersionCreateRequest.model_validate(
            {
                "sources": raw.get("sources"),
                "chunking_strategy": chunking_strategy,
                "embedding_model": embedding_model,
                "chunking_config": raw.get("chunking_config") or {},
                "graph_config": raw.get("graph_config"),
            }
        )
        result = knowledge_service.create_version(
            knowledge_base_id,
            request,
            identity=identity,
            actor=identity.user_id,
        )
        knowledge_base = result.knowledge_base
        version = result.version
        run = result.run

        wait_requested = bool(raw.get("wait_for_completion"))
        wait_timeout_seconds = max(1.0, float(raw.get("wait_timeout_seconds") or 300.0))
        await_status = "not_requested"
        if wait_requested:
            deadline = _monotonic() + wait_timeout_seconds
            while version.status in {"queued", "processing"} and _monotonic() < deadline:
                _sleep(min(1.0, max(0.0, deadline - _monotonic())))
                version = knowledge_service.get_version(
                    version.knowledge_base_version_id,
                    identity=identity,
                )
            await_status = "timeout" if version.status in {"queued", "processing"} else "completed"
            knowledge_base = knowledge_service.get_knowledge_base(
                knowledge_base_id,
                identity=identity,
            )
            latest_runs = knowledge_service.list_runs(knowledge_base_id, identity=identity)
            run = next(
                (
                    item
                    for item in latest_runs
                    if item.knowledge_base_run_id == run.knowledge_base_run_id
                ),
                run,
            )
            if version.status == "failed":
                raise RuntimeError(
                    f"knowledge build {version.knowledge_base_version_id!r} failed: "
                    f"{version.error_summary or 'build failed'}"
                )

        activation: dict[str, Any] = {
            "requested": bool(raw.get("activate_when_complete")),
            "status": "skipped",
        }
        if raw.get("activate_when_complete"):
            if version.status == "completed":
                knowledge_base = knowledge_service.activate_version(
                    knowledge_base_id,
                    version.knowledge_base_version_id,
                    identity=identity,
                    actor=identity.user_id,
                )
                activation = {
                    "requested": True,
                    "status": "activated",
                    "active_version_id": knowledge_base.active_version_id,
                }
            else:
                activation = {
                    "requested": True,
                    "status": "pending",
                    "wait_status": await_status,
                }

        status = str(version.status)
        summary = (
            f"Knowledge build {status} for v{version.version_number}"
            f" using {version.chunking_strategy} / {version.embedding_model}."
        )
        if activation.get("status") == "activated":
            summary += " Activated as the knowledge base default."
        elif await_status == "timeout":
            summary += " The workflow stopped waiting before the build reached a terminal state."
        return {
            "summary": summary,
            "status": status,
            "knowledge_base": knowledge_base.model_dump(mode="json"),
            "version": version.model_dump(mode="json"),
            "run": run.model_dump(mode="json"),
            "await_completion": {
                "requested": wait_requested,
                "status": await_status,
                "timeout_seconds": wait_timeout_seconds,
            },
            "activation": activation,
        }

    return _run_knowledge_build


def execute_exported_workflow(
    ir: IRWorkflow,
    input_text: str,
    *,
    session_id: str | None = None,
    config: CaliberConfig | None = None,
    executor: WorkflowExecutor | None = None,
    resolver: ToolResolver | None = None,
    preview: bool = False,
    extra_tools: dict[str, Callable[..., Any]] | None = None,
    workflow_alias: str | None = None,
    workflow_version_id: str | None = None,
    session_factory: sessionmaker[Session] | None = None,
    identity: CaliberIdentity | None = None,
    active_project_id: str | None = None,
    knowledge_query_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    knowledge_build_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    subworkflow_runner: (
        Callable[[str, str, str, float, int, WorkflowExecutor, bool], dict[str, Any]] | None
    ) = None,
    session_memory_store: WorkflowSessionMemoryStore | None = None,
) -> WorkflowRunResult:
    """Execute an exported workflow IR through the full CALIBER interpreter."""

    resolved_config = config if config is not None else CaliberConfig.load()
    resolved_resolver = resolver if resolver is not None else InMemoryToolResolver()
    resolved_identity = identity or _default_identity(active_project_id=active_project_id)

    runtime_session_factory = session_factory
    if (
        _workflow_requires_database(
            ir,
            subworkflow_runner=subworkflow_runner,
            knowledge_query_runner=knowledge_query_runner,
            knowledge_build_runner=knowledge_build_runner,
            session_memory_store=session_memory_store,
        )
        and runtime_session_factory is None
    ):
        runtime_session_factory = _build_session_factory(resolved_config)

    resolved_session_memory_store = session_memory_store
    if resolved_session_memory_store is None:
        resolved_session_memory_store = _default_session_memory_store(
            ir,
            session_factory=runtime_session_factory,
        )

    resolved_subworkflow_runner = subworkflow_runner
    if resolved_subworkflow_runner is None and any(
        isinstance(node, IRSubworkflow) for node in ir.nodes.values()
    ):
        if runtime_session_factory is None:
            raise RuntimeError("subworkflow execution requires a database-backed session factory")
        resolved_subworkflow_runner = _default_subworkflow_runner(
            session_factory=runtime_session_factory,
            config=resolved_config,
        )

    resolved_knowledge_query_runner = knowledge_query_runner
    if resolved_knowledge_query_runner is None and any(
        isinstance(node, IRKnowledgeQuery) for node in ir.nodes.values()
    ):
        if runtime_session_factory is None:
            raise RuntimeError(
                "knowledge_query execution requires a database-backed session factory"
            )
        resolved_knowledge_query_runner = _default_knowledge_query_runner(
            session_factory=runtime_session_factory,
            config=resolved_config,
            identity=resolved_identity,
        )

    resolved_knowledge_build_runner = knowledge_build_runner
    if resolved_knowledge_build_runner is None and any(
        isinstance(node, IRKnowledgeBuild) for node in ir.nodes.values()
    ):
        if runtime_session_factory is None:
            raise RuntimeError(
                "knowledge_build execution requires a database-backed session factory"
            )
        resolved_knowledge_build_runner = _default_knowledge_build_runner(
            session_factory=runtime_session_factory,
            config=resolved_config,
            identity=resolved_identity,
        )

    resolved_executor = executor
    if resolved_executor is None:
        if ir.agents():
            from caliber.workflows.promoter import build_executor  # noqa: PLC0415

            resolved_executor = build_executor(resolved_config, ir=ir)
        else:
            resolved_executor = FakeWorkflowExecutor()

    plan = RuntimePlan(
        ir=ir,
        resolver=resolved_resolver,
        workflow_version_id=workflow_version_id,
        workflow_alias=workflow_alias,
        compiler_version="export-runtime",
        subworkflow_runner=resolved_subworkflow_runner,
        knowledge_query_runner=resolved_knowledge_query_runner,
        knowledge_build_runner=resolved_knowledge_build_runner,
        session_memory_store=resolved_session_memory_store,
        max_output_bytes=resolved_config.tool_sandbox_max_output_bytes,
        foreach_max_workers=resolved_config.workflow_foreach_max_workers,
    )
    return execute(
        plan,
        input_text,
        executor=resolved_executor,
        session_id=session_id,
        preview=preview,
        extra_tools=extra_tools,
    )


def run_exported_workflow(
    ir: IRWorkflow,
    input_text: str,
    **kwargs: Any,
) -> str:
    """Execute an exported workflow and return only its final output.

    Raises ``RuntimeError`` when the run blocks or fails so generated ``run()``
    helpers behave like ordinary callables rather than returning a partial
    ``WorkflowRunResult`` object on error.
    """

    result = execute_exported_workflow(ir, input_text, **kwargs)
    if result.status == "completed":
        return result.output
    detail = result.error or result.output or "workflow did not complete successfully"
    raise RuntimeError(f"exported workflow {ir.workflow_id!r} {result.status}: {detail}")


__all__ = [
    "execute_exported_workflow",
    "run_exported_workflow",
]
