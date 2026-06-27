"""Workflow service layer: compile, publish, deploy gates, promote, rollback.

This is the DB-aware orchestration layer that the route handlers call. It bridges
the pure engine (manifest/compiler/runtime) and the ORM (versions, deployments,
tool registry), and owns the publish/promote/rollback state machine (plan §17.5).

Key decisions:

* **Deploy gates block promotion** (plan §9.4, §17.4). Gates relevant to the
  target alias replay the referenced eval dataset through the compiled workflow;
  a gate passes when the example pass-rate meets ``min_pass_rate`` (default 1.0).
  Promoting to ``prod`` *requires* at least one gate naming ``prod``.
* **Rotation + rollback checkpoint** are atomic with the caller's transaction.
  The prior alias target is pushed onto the deployment's rollback stack so
  rollback restores it.

Note on prod approval: the plan's end-state routes a passing prod gate through a
human approval before rotation (§15.3). The MVP here rotates on gate pass and
records the actor in the audit log; wiring the human-approval-before-rotation
step onto the existing Approvals surface is a tracked follow-up.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from caliber.audit import get_redactor
from caliber.audit import record as audit_record
from caliber.auth import SCOPE_VIEWER, CaliberIdentity
from caliber.config import CaliberConfig
from caliber.db.models import (
    CaliberAgentConfig,
    CaliberEvalDataset,
    CaliberEvalDatasetExample,
    CaliberSkill,
    CaliberToolRegistry,
    CaliberWorkflow,
    CaliberWorkflowDeployment,
    CaliberWorkflowPromotion,
    CaliberWorkflowRun,
    CaliberWorkflowVersion,
)
from caliber.ids import (
    new_workflow_deployment_id,
    new_workflow_promotion_id,
    new_workflow_run_id,
    new_workflow_version_id,
)
from caliber.knowledge.schemas import KnowledgeBaseVersionCreateRequest, KnowledgeQueryRequest
from caliber.knowledge.service import KnowledgeBaseService
from caliber.secrets import resolve_secret
from caliber.workflows.compiler import CompileError, CompileResult, compile_workflow
from caliber.workflows.ir import IRWorkflow
from caliber.workflows.manifest import (
    AgentNode,
    DeployGate,
    WorkflowManifest,
    compute_manifest_hash,
    parse_manifest,
)
from caliber.workflows.runtime import (
    AnthropicChatWorkflowExecutor,
    FakeWorkflowExecutor,
    NodeStep,
    OpenAIAgentsWorkflowExecutor,
    OpenAIChatWorkflowExecutor,
    OpenAIResponsesWorkflowExecutor,
    RuntimePlan,
    WorkflowExecutor,
    WorkflowRunResult,
    current_run_context,
    execute,
)
from caliber.workflows.sandbox import TokenBudget
from caliber.workflows.session_memory import (
    InMemoryWorkflowSessionMemoryStore,
    SqlWorkflowSessionMemoryStore,
    WorkflowSessionMemoryStore,
)
from caliber.workflows.tools import InMemoryToolResolver, ToolRegistryEntry

# Aliases that require human approval (a pending promotion request) before the
# alias rotates — even after deploy gates pass (plan §15.3).
#
# v1 single-environment mode: empty. Builds deploy to the single live alias
# immediately; deploy gates still run when a workflow attaches one (optional
# safety net), but nothing forces a human approval step and there is no
# dev→staging→prod ladder. The full multi-stage governance loop is restored by
# adding the gated alias(es) back here, e.g. ``frozenset({"prod"})`` — the
# promotion/approval machinery below is intact and dormant, not removed.
GATED_ALIASES: frozenset[str] = frozenset()

# Aliases that represent a *live/production* deployment for protection purposes:
# a workflow with a deployment on one of these cannot be archived or deleted
# (plan §19.9). This is deliberately separate from ``GATED_ALIASES`` (which
# controls approval gating): the live alias stays protected even in v1
# single-environment mode, where nothing is gated. ``prod`` is the live alias in
# both single- and multi-environment modes.
LIVE_ALIASES: frozenset[str] = frozenset({"prod"})

# Max eval-dataset examples replayed per deploy gate (ext D3 / plan §26.4) — keeps
# the synchronous promote request bounded on large datasets.
DEPLOY_GATE_SAMPLE_SIZE = 50


class PublishError(Exception):
    """Raised when a version cannot be published (e.g. deprecated, invalid)."""


class DeployError(Exception):
    """Raised for invalid deployment requests (missing gate, unpublished version)."""


class DeployGateFailedError(Exception):
    """Raised when a deploy gate's thresholds are not met."""

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


class RollbackError(Exception):
    """Raised when there is nothing to roll back to."""


# ---------------------------------------------------------------------------
# Resolver / executor / plan construction
# ---------------------------------------------------------------------------


def resolver_from_session(session: Session) -> InMemoryToolResolver:
    """Build a tool resolver from the live tool-registry rows (non-archived)."""
    rows = (
        session.execute(select(CaliberToolRegistry).where(CaliberToolRegistry.status != "archived"))
        .scalars()
        .all()
    )
    by_id = {r.tool_id: r for r in rows}
    entries: list[ToolRegistryEntry] = []
    for row in rows:
        successor_ref = None
        if row.successor_tool_id and row.successor_tool_id in by_id:
            succ = by_id[row.successor_tool_id]
            successor_ref = ToolRegistryEntry(
                name=succ.name, version=succ.version, module_path="", callable_name=""
            ).registry_ref
        entries.append(
            ToolRegistryEntry(
                name=row.name,
                version=row.version,
                module_path=row.module_path,
                callable_name=row.callable_name,
                side_effect_level=row.side_effect_level,
                requires_approval=row.requires_approval,
                allow_in_preview=row.allow_in_preview,
                input_schema=row.input_schema,
                output_schema=row.output_schema,
                secret_refs=tuple(row.secret_refs or ()),
                status=row.status,
                successor_ref=successor_ref,
                description=row.description,
            )
        )
    return InMemoryToolResolver(entries)


def build_executor(
    config: CaliberConfig | None,
    *,
    manifest: WorkflowManifest | None = None,
    ir: IRWorkflow | None = None,
) -> WorkflowExecutor:
    """Pick a workflow executor for the configured provider.

    ``fake`` is the safe default for unit tests and local demos. ``openai``
    performs a real LLM call per agent node while still using CALIBER's
    deterministic graph interpreter, tool registry, guardrails, and run index.
    """
    provider = (config.llm_provider if config is not None else "fake").strip().lower()
    if provider in ("", "fake", "deterministic"):
        return FakeWorkflowExecutor()
    if provider == "openai":
        assert config is not None
        api_key = resolve_secret(config.llm_api_key_env)
        if not api_key:
            raise RuntimeError(
                f"CALIBER_LLM_PROVIDER=openai requires a secret at {config.llm_api_key_env!r}"
            )
        common_kwargs: dict[str, Any] = {
            "api_key": api_key,
            "default_model": config.llm_diagnosis_model,
        }
        if config.llm_base_url:
            common_kwargs["base_url"] = config.llm_base_url
        workflow_api = _openai_workflow_api(config, manifest=manifest, ir=ir)
        if workflow_api == "responses":
            return OpenAIResponsesWorkflowExecutor(
                **common_kwargs,
                parallel_tool_calls=_openai_parallel_tool_calls_enabled(
                    config, manifest=manifest, ir=ir
                ),
                prompt_cache_enabled=_openai_prompt_cache_enabled(config, manifest=manifest, ir=ir),
                prompt_cache_retention=_openai_prompt_cache_retention(
                    config, manifest=manifest, ir=ir
                ),
            )
        if workflow_api == "agents_sdk":
            return OpenAIAgentsWorkflowExecutor(
                **common_kwargs,
                parallel_tool_calls=_openai_parallel_tool_calls_enabled(
                    config, manifest=manifest, ir=ir
                ),
                prompt_cache_enabled=_openai_prompt_cache_enabled(config, manifest=manifest, ir=ir),
                prompt_cache_retention=_openai_prompt_cache_retention(
                    config, manifest=manifest, ir=ir
                ),
            )
        return OpenAIChatWorkflowExecutor(
            **common_kwargs,
            parallel_tool_calls=_openai_parallel_tool_calls_enabled(
                config, manifest=manifest, ir=ir
            ),
            prompt_cache_enabled=_openai_prompt_cache_enabled(config, manifest=manifest, ir=ir),
            prompt_cache_retention=_openai_prompt_cache_retention(config, manifest=manifest, ir=ir),
        )
    if provider == "anthropic":
        assert config is not None
        api_key = resolve_secret(config.llm_api_key_env)
        if not api_key:
            raise RuntimeError(
                f"CALIBER_LLM_PROVIDER=anthropic requires a secret at {config.llm_api_key_env!r} "
                "(set CALIBER_LLM_API_KEY_ENV=ANTHROPIC_API_KEY)"
            )
        return AnthropicChatWorkflowExecutor(
            api_key=api_key,
            default_model=config.llm_diagnosis_model,
        )
    raise RuntimeError(f"unknown workflow LLM provider {provider!r}")


def _workflow_openai_override(
    *,
    manifest: WorkflowManifest | None = None,
    ir: IRWorkflow | None = None,
    field_name: str,
) -> str | None:
    """Return the workflow-scoped OpenAI runtime override, if any."""

    if ir is not None:
        value = getattr(ir, f"openai_{field_name}", None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    if manifest is None:
        return None
    openai = getattr(getattr(manifest, "runtime", None), "openai", None)
    value = getattr(openai, field_name, None)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _openai_workflow_api(
    config: CaliberConfig, *, manifest: WorkflowManifest | None = None, ir: IRWorkflow | None = None
) -> str:
    value = _workflow_openai_override(manifest=manifest, ir=ir, field_name="workflow_api")
    if value:
        return value
    return str(config.openai_workflow_api or "chat_completions").strip().lower()


def _openai_parallel_tool_calls_enabled(
    config: CaliberConfig,
    *,
    manifest: WorkflowManifest | None = None,
    ir: IRWorkflow | None = None,
) -> bool:
    """Resolve the parallel tool-calls mode for OpenAI workflow executors.

    ``auto`` keeps the safer compatibility posture for OpenAI-compatible
    gateways, which may not implement the parameter yet, while enabling the
    capability for direct api.openai.com traffic.
    """
    mode = (
        _workflow_openai_override(manifest=manifest, ir=ir, field_name="parallel_tool_calls")
        or str(config.openai_workflow_parallel_tool_calls or "auto").strip().lower()
    )
    if mode == "enabled":
        return True
    if mode == "disabled":
        return False
    return not bool((config.llm_base_url or "").strip())


def _openai_prompt_cache_enabled(
    config: CaliberConfig,
    *,
    manifest: WorkflowManifest | None = None,
    ir: IRWorkflow | None = None,
) -> bool:
    """Resolve whether CALIBER should send explicit OpenAI prompt-cache hints."""

    mode = (
        _workflow_openai_override(manifest=manifest, ir=ir, field_name="prompt_cache_mode")
        or str(config.openai_prompt_cache_mode or "auto").strip().lower()
    )
    if mode == "enabled":
        return True
    if mode == "disabled":
        return False
    return not bool((config.llm_base_url or "").strip())


def _openai_prompt_cache_retention(
    config: CaliberConfig,
    *,
    manifest: WorkflowManifest | None = None,
    ir: IRWorkflow | None = None,
) -> str | None:
    """Normalize the requested OpenAI prompt-cache retention policy."""

    value = (
        _workflow_openai_override(manifest=manifest, ir=ir, field_name="prompt_cache_retention")
        or str(config.openai_prompt_cache_retention or "default").strip().lower()
    )
    if value in {"", "default"}:
        return None
    return value


def build_workflow_identity(
    session: Session,
    workflow_id: str,
    *,
    fallback_user: str = "",
) -> CaliberIdentity:
    workflow = session.get(CaliberWorkflow, workflow_id)
    workflow_owner = workflow.owner if workflow is not None and workflow.owner else fallback_user
    workflow_project_id = workflow.project_id if workflow is not None else None
    return CaliberIdentity(
        user_id=workflow_owner or "",
        scopes=frozenset({SCOPE_VIEWER}),
        active_project_id=workflow_project_id,
    )


def build_knowledge_runtime_runners(  # noqa: PLR0915 - wires several runtime helper closures
    session: Session,
    *,
    identity: CaliberIdentity,
    config: CaliberConfig | None = None,
    session_factory: sessionmaker[Session] | None = None,
    actor: str | None = None,
) -> tuple[
    Callable[[dict[str, Any]], dict[str, Any]],
    Callable[[dict[str, Any]], dict[str, Any]],
]:
    resolved_config = config or CaliberConfig()
    runtime_session_factory = session_factory or sessionmaker(
        bind=session.get_bind(),
        class_=Session,
        expire_on_commit=False,
    )
    knowledge_service = KnowledgeBaseService(
        config=resolved_config,
        session_factory=runtime_session_factory,
    )
    resolved_actor = actor or identity.user_id

    def _normalize_retrieval_modes(value: Any) -> list[str] | None:
        if value is None:
            return None
        items = [value] if isinstance(value, str) else list(value)
        return list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))

    def _resolve_default_retrieval_modes(
        *,
        resolved_version_ids: list[str],
    ) -> list[str]:
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
                raise PublishError(f"knowledge base {knowledge_base_id!r} has no active version")
            version_ids = [knowledge_base.active_version_id]
        if not version_ids:
            raise PublishError(
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

    def _run_knowledge_build(payload: dict[str, Any]) -> dict[str, Any]:
        raw = dict(payload)
        knowledge_base_id = str(raw.get("knowledge_base_id") or "").strip()
        if not knowledge_base_id:
            raise PublishError("knowledge_build node requires knowledge_base_id")
        chunking_strategy = str(raw.get("chunking_strategy") or "").strip()
        embedding_model = str(raw.get("embedding_model") or "").strip()
        if not chunking_strategy:
            raise PublishError("knowledge_build node requires chunking_strategy")
        if not embedding_model:
            raise PublishError("knowledge_build node requires embedding_model")

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
            actor=resolved_actor,
        )
        knowledge_base = result.knowledge_base
        version = result.version
        run = result.run

        wait_requested = bool(raw.get("wait_for_completion"))
        wait_timeout_seconds = max(1.0, float(raw.get("wait_timeout_seconds") or 300.0))
        await_status = "not_requested"
        if wait_requested:
            deadline = time.monotonic() + wait_timeout_seconds
            while version.status in {"queued", "processing"} and time.monotonic() < deadline:
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
                version = knowledge_service.get_version(
                    version.knowledge_base_version_id,
                    identity=identity,
                )
            await_status = "timeout" if version.status in {"queued", "processing"} else "completed"
            knowledge_base = knowledge_service.get_knowledge_base(
                knowledge_base_id,
                identity=identity,
            )
            latest_runs = knowledge_service.list_runs(
                knowledge_base_id,
                identity=identity,
            )
            run = next(
                (
                    item
                    for item in latest_runs
                    if item.knowledge_base_run_id == run.knowledge_base_run_id
                ),
                run,
            )
            if version.status == "failed":
                raise PublishError(
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
                    actor=resolved_actor,
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

    return _run_knowledge_query, _run_knowledge_build


def compile_version(
    session: Session,
    version: CaliberWorkflowVersion,
    *,
    resolver: InMemoryToolResolver | None = None,
    persist: bool = True,
) -> CompileResult:
    """Compile a version's manifest, optionally persisting compile metadata."""
    manifest = parse_manifest(version.manifest)
    resolver = resolver or resolver_from_session(session)
    result = compile_workflow(manifest, resolver=resolver, version=str(version.version_number))
    if persist:
        version.compiler_version = result.report["compiler_version"]
        version.validation_report = result.report["validation"]
        version.manifest_hash = result.manifest_hash
        # Store the immutable compiled bundle with the version (plan §13.3) so
        # the generated code/report are durable and exportable without recompiling.
        version.compiled_bundle = {
            "generated_python": result.generated_python,
            "compiler_report": result.report,
            "requirements": result.requirements,
        }
        version.compiled_artifact_uri = f"caliber-workflow://{version.version_id}/bundle"
    return result


def _skill_contents_for(session: Session, manifest: WorkflowManifest) -> dict[str, str]:
    """Resolve ``name -> content`` for every skill any agent references.

    Empty when no agent declares skills, so the compile path is unchanged for
    skill-free workflows. Missing skills are simply absent (validation surfaces
    unknown refs); the compiler composes only what resolves.
    """
    names = {
        name
        for node in manifest.nodes.values()
        if isinstance(node, AgentNode)
        for name in node.skills
    }
    if not names:
        return {}
    rows = session.execute(
        select(CaliberSkill.name, CaliberSkill.content).where(CaliberSkill.name.in_(names))
    ).all()
    return {name: content for name, content in rows if content}


def build_plan(  # noqa: PLR0915 - central workflow plan assembler
    session: Session,
    version: CaliberWorkflowVersion,
    *,
    resolver: InMemoryToolResolver | None = None,
    alias: str | None = None,
    subworkflow_depth: int = 0,
    manifest_override: dict[str, Any] | None = None,
    config: CaliberConfig | None = None,
    session_factory: sessionmaker[Session] | None = None,
) -> RuntimePlan:
    """Compile and wrap a version into an executable :class:`RuntimePlan`.

    ``manifest_override`` compiles an in-memory manifest *instead of* the stored
    ``version.manifest`` — used by preview-run to execute an unsaved canvas edit
    (the copilot iterate loop) without first persisting it. Compile-only: the
    stored version is never mutated.
    """
    resolver = resolver or resolver_from_session(session)
    manifest = parse_manifest(
        manifest_override if manifest_override is not None else version.manifest
    )
    result = compile_workflow(
        manifest,
        resolver=resolver,
        version=str(version.version_number),
        use_cache=True,
        skill_contents=_skill_contents_for(session, manifest),
    )
    resolved_config = config or CaliberConfig()
    runtime_session_factory = session_factory or sessionmaker(
        bind=session.get_bind(),
        class_=Session,
        expire_on_commit=False,
    )
    workflow = session.get(CaliberWorkflow, version.workflow_id)
    workflow_owner = (
        workflow.owner if workflow is not None and workflow.owner else (version.created_by or "")
    )
    workflow_project_id = workflow.project_id if workflow is not None else None
    workflow_identity = CaliberIdentity(
        user_id=workflow_owner,
        scopes=frozenset({SCOPE_VIEWER}),
        active_project_id=workflow_project_id,
    )
    knowledge_service = KnowledgeBaseService(
        config=resolved_config,
        session_factory=runtime_session_factory,
    )
    session_memory_store: WorkflowSessionMemoryStore | None = None
    if result.ir.session_mode == "in_memory":
        session_memory_store = InMemoryWorkflowSessionMemoryStore()
    elif result.ir.session_mode == "persistent":
        session_memory_store = SqlWorkflowSessionMemoryStore(
            session_factory=runtime_session_factory,
        )

    def _normalize_retrieval_modes(value: Any) -> list[str] | None:
        if value is None:
            return None
        items = [value] if isinstance(value, str) else list(value)
        return list(dict.fromkeys(str(item).strip() for item in items if str(item).strip()))

    def _resolve_default_retrieval_modes(
        *,
        resolved_version_ids: list[str],
    ) -> list[str]:
        primary_version = knowledge_service.get_version(
            resolved_version_ids[0],
            identity=workflow_identity,
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

    def _resolve_version(workflow_id: str, run_alias: str) -> CaliberWorkflowVersion:
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
                raise PublishError(f"no versions found for subworkflow {workflow_id!r}")
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
            raise PublishError(
                f"no active deployment for subworkflow {workflow_id!r} alias {run_alias!r}"
            )
        target = session.get(CaliberWorkflowVersion, deployment.version_id)
        if target is None:
            raise PublishError(
                f"deployment {deployment.deployment_id!r} references missing version "
                f"{deployment.version_id!r}"
            )
        return target

    def _run_subworkflow(
        workflow_id: str,
        run_alias: str,
        input_text: str,
        timeout_seconds: float,  # noqa: ARG001 (part of the subworkflow_runner signature; per-node timeouts apply)
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
        target_version = _resolve_version(workflow_id, run_alias)
        sub_plan = build_plan(
            session,
            target_version,
            resolver=resolver,
            alias=run_alias,
            subworkflow_depth=depth,
            config=config,
            session_factory=runtime_session_factory,
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

    def _run_knowledge_query(payload: dict[str, Any]) -> dict[str, Any]:
        raw = dict(payload)
        version_ids = [
            str(item).strip() for item in list(raw.get("version_ids") or []) if str(item).strip()
        ]
        knowledge_base_id = str(raw.get("knowledge_base_id") or "").strip()
        if not version_ids and knowledge_base_id:
            knowledge_base = knowledge_service.get_knowledge_base(
                knowledge_base_id,
                identity=workflow_identity,
            )
            if not knowledge_base.active_version_id:
                raise PublishError(f"knowledge base {knowledge_base_id!r} has no active version")
            version_ids = [knowledge_base.active_version_id]
        if not version_ids:
            raise PublishError(
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
            identity=workflow_identity,
        ).model_dump(mode="json")

    def _run_knowledge_build(payload: dict[str, Any]) -> dict[str, Any]:
        raw = dict(payload)
        knowledge_base_id = str(raw.get("knowledge_base_id") or "").strip()
        if not knowledge_base_id:
            raise PublishError("knowledge_build node requires knowledge_base_id")
        chunking_strategy = str(raw.get("chunking_strategy") or "").strip()
        embedding_model = str(raw.get("embedding_model") or "").strip()
        if not chunking_strategy:
            raise PublishError("knowledge_build node requires chunking_strategy")
        if not embedding_model:
            raise PublishError("knowledge_build node requires embedding_model")

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
            identity=workflow_identity,
            actor=workflow_identity.user_id,
        )
        knowledge_base = result.knowledge_base
        version = result.version
        run = result.run

        wait_requested = bool(raw.get("wait_for_completion"))
        wait_timeout_seconds = max(1.0, float(raw.get("wait_timeout_seconds") or 300.0))
        await_status = "not_requested"
        if wait_requested:
            deadline = time.monotonic() + wait_timeout_seconds
            while version.status in {"queued", "processing"} and time.monotonic() < deadline:
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
                version = knowledge_service.get_version(
                    version.knowledge_base_version_id,
                    identity=workflow_identity,
                )
            await_status = "timeout" if version.status in {"queued", "processing"} else "completed"
            knowledge_base = knowledge_service.get_knowledge_base(
                knowledge_base_id,
                identity=workflow_identity,
            )
            latest_runs = knowledge_service.list_runs(
                knowledge_base_id,
                identity=workflow_identity,
            )
            run = next(
                (
                    item
                    for item in latest_runs
                    if item.knowledge_base_run_id == run.knowledge_base_run_id
                ),
                run,
            )
            if version.status == "failed":
                raise PublishError(
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
                    identity=workflow_identity,
                    actor=workflow_identity.user_id,
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

    return RuntimePlan(
        ir=result.ir,
        resolver=resolver,
        workflow_version_id=version.version_id,
        workflow_alias=alias,
        compiler_version=result.report["compiler_version"],
        subworkflow_runner=_run_subworkflow,
        knowledge_query_runner=_run_knowledge_query,
        knowledge_build_runner=_run_knowledge_build,
        session_memory_store=session_memory_store,
        subworkflow_depth=subworkflow_depth,
        max_output_bytes=(config.tool_sandbox_max_output_bytes if config is not None else None),
        foreach_max_workers=(config.workflow_foreach_max_workers if config is not None else 1),
    )


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def run_preview(
    session: Session,
    version: CaliberWorkflowVersion,
    input_text: str,
    *,
    session_id: str | None = None,
    config: CaliberConfig | None = None,
    token_budget: int | None = None,
    manifest_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compile + execute a version in preview mode (tools sandboxed) (plan §26.3).

    ``manifest_override`` previews an unsaved in-memory manifest (the copilot
    iterate loop) instead of the stored version; the version is not mutated.
    """
    plan = build_plan(session, version, manifest_override=manifest_override, config=config)
    executor = build_executor(config, ir=plan.ir)
    budget = TokenBudget(token_budget) if token_budget else TokenBudget()
    result = execute(
        plan,
        input_text,
        executor=executor,
        session_id=session_id,
        preview=True,
        token_budget=budget,
    )
    preview_manifest = (
        deepcopy(manifest_override) if manifest_override is not None else deepcopy(version.manifest)
    )
    preview_manifest_metadata = (
        {
            "manifest_mode": "snapshot",
            "manifest_hash": compute_manifest_hash(preview_manifest),
        }
        if manifest_override is not None
        else {
            "manifest_mode": "saved_version",
            "manifest_hash": version.manifest_hash,
            "workflow_version_number": version.version_number,
        }
    )
    # Synchronously index the run for trace→workflow localization (plan §14.5).
    run = record_workflow_run(
        session,
        workflow_id=version.workflow_id,
        version_id=version.version_id,
        alias=None,
        result=result,
        session_id=session_id,
        preview=True,
        manifest_snapshot=preview_manifest,
        manifest_summary=preview_manifest_metadata,
    )
    session.commit()
    return {
        "workflow_run_id": run.workflow_run_id,
        "status": result.status,
        "output": result.output,
        "error": result.error,
        "tokens": result.tokens,
        "tags": result.tags,
        "steps": [
            {
                "node_id": s.node_id,
                "node_type": s.node_type,
                "status": s.status,
                "output": s.output,
                "tool_calls": s.tool_calls,
                "handoff_target": s.handoff_target,
                "detail": s.detail,
                "duration_ms": s.duration_ms,
                "input_by_port": dict(s.input_by_port or {}),
                "output_by_port": dict(s.output_by_port or {}),
            }
            for s in result.steps
        ],
        "guardrail_results": result.guardrail_results,
        "preview": True,
    }


def prune_workflow_runs(
    session: Session,
    *,
    retention_days: float,
    now: datetime | None = None,
) -> int:
    """Delete workflow-run index rows older than the retention window (ext D2).

    Returns the number of rows deleted. The janitor calls this each sweep so the
    ``caliber_workflow_runs`` index doesn't grow without bound (plan §26.3 sets
    a 7-day default for preview traces). ``retention_days <= 0`` disables pruning.
    """
    if retention_days <= 0:
        return 0
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
    rows = (
        session.execute(select(CaliberWorkflowRun).where(CaliberWorkflowRun.started_at < cutoff))
        .scalars()
        .all()
    )
    for row in rows:
        session.delete(row)
    if rows:
        session.flush()
    return len(rows)


def record_workflow_run(
    session: Session,
    *,
    workflow_id: str,
    version_id: str | None,
    alias: str | None,
    result: WorkflowRunResult,
    session_id: str | None = None,
    trace_id: str | None = None,
    mlflow_run_id: str | None = None,
    preview: bool = False,
    manifest_snapshot: dict[str, Any] | None = None,
    manifest_summary: dict[str, Any] | None = None,
) -> CaliberWorkflowRun:
    """Write the authoritative workflow→trace index row for one run (plan §14.5).

    Called synchronously by the runtime/preview path *before* the response is
    returned (not a lazy cache). MLflow remains the full span/content store;
    this row is the index CALIBER queries to localize feedback to a workflow
    version + node path. Production executors pass the resolved ``trace_id`` /
    ``mlflow_run_id``; preview runs leave them ``None`` and set ``preview``.
    """
    summary = workflow_run_summary(result, preview=preview)
    if manifest_summary:
        summary.update(manifest_summary)
    run = CaliberWorkflowRun(
        workflow_run_id=new_workflow_run_id(),
        workflow_id=workflow_id,
        workflow_version_id=version_id,
        deployment_alias=alias,
        mlflow_run_id=mlflow_run_id,
        trace_id=trace_id,
        session_id=session_id,
        status=result.status,
        completed_at=datetime.now(timezone.utc),
        summary=summary,
        manifest_snapshot=deepcopy(manifest_snapshot)
        if isinstance(manifest_snapshot, dict)
        else None,
    )
    session.add(run)
    session.flush()
    return run


def workflow_run_summary(result: WorkflowRunResult, *, preview: bool) -> dict[str, Any]:
    """Build the redacted JSON summary stored on ``caliber_workflow_runs``."""

    def _step_payload(s: NodeStep) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "node_id": s.node_id,
            "node_type": s.node_type,
            "status": s.status,
            "output": s.output[:2000],
            "tool_calls": s.tool_calls,
            "handoff_target": s.handoff_target,
            "detail": s.detail,
            "duration_ms": s.duration_ms,
            "input_by_port": dict(s.input_by_port or {}),
            "output_by_port": dict(s.output_by_port or {}),
        }
        if s.tokens > 0:
            payload["tokens"] = s.tokens
        if s.prompt_tokens > 0:
            payload["prompt_tokens"] = s.prompt_tokens
        if s.completion_tokens > 0:
            payload["completion_tokens"] = s.completion_tokens
        if s.cached_prompt_tokens > 0:
            payload["cached_prompt_tokens"] = s.cached_prompt_tokens
        if s.cost_usd > 0:
            payload["cost_usd"] = s.cost_usd
        if isinstance(s.model, str) and s.model:
            payload["model"] = s.model
        if isinstance(s.prompt_version, str) and s.prompt_version:
            payload["prompt_version"] = s.prompt_version
        return payload

    summary: dict[str, Any] = {
        "output": result.output[:1000],
        "tokens": result.tokens,
        "error": result.error,
        "preview": preview,
        "node_path": [s.node_id for s in result.steps],
        "steps": [_step_payload(s) for s in result.steps],
        "tags": result.tags,
        "guardrail_results": result.guardrail_results,
    }
    # Redact PII/secret-looking strings before persisting (ext A4) — tool
    # outputs and agent text land in this index, so apply the same redactor the
    # audit log uses.
    redacted = get_redactor().redact_value(summary)
    return redacted if isinstance(redacted, dict) else summary


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


def publish_version(
    session: Session,
    version: CaliberWorkflowVersion,
    *,
    actor: str,
) -> CaliberWorkflowVersion:
    """Publish a draft version (idempotent on already-published).

    Raises :class:`PublishError` for deprecated versions or versions whose
    manifest fails validation/compilation.
    """
    if version.status == "published":
        return version  # idempotent (plan §15.2 note)
    if version.status == "deprecated":
        raise PublishError("cannot publish a deprecated version")

    try:
        compile_version(session, version, persist=True)
    except CompileError as exc:
        raise PublishError(f"version does not compile: {exc}") from exc

    version.status = "published"
    version.published_by = actor
    version.published_at = datetime.now(timezone.utc)
    return version


# ---------------------------------------------------------------------------
# Deploy gates
# ---------------------------------------------------------------------------


@dataclass
class GateRun:
    name: str
    dataset_ref: str
    passed: bool
    pass_rate: float
    n_examples: int
    detail: str = ""


@dataclass
class GateResult:
    has_gate: bool
    passed: bool
    runs: list[GateRun] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "has_gate": self.has_gate,
            "passed": self.passed,
            "runs": [
                {
                    "name": r.name,
                    "dataset_ref": r.dataset_ref,
                    "passed": r.passed,
                    "pass_rate": r.pass_rate,
                    "n_examples": r.n_examples,
                    "detail": r.detail,
                }
                for r in self.runs
            ],
        }


def _example_input(example: CaliberEvalDatasetExample) -> str:
    data = example.input or {}
    for key in ("input", "user_message", "message", "query", "text"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    # Fall back to the first string value, else a JSON dump.
    for value in data.values():
        if isinstance(value, str):
            return value
    import json  # noqa: PLC0415

    return json.dumps(data)


def evaluate_deploy_gates(
    session: Session,
    manifest: WorkflowManifest,
    alias: str,
    *,
    resolver: InMemoryToolResolver,
    executor: WorkflowExecutor,
    sample_size: int = DEPLOY_GATE_SAMPLE_SIZE,
) -> GateResult:
    """Run the deploy gates relevant to ``alias`` against the eval datasets."""
    gates = [g for g in manifest.deploy_gates.values() if alias in g.required_for_aliases]
    if not gates:
        return GateResult(has_gate=False, passed=True)

    compiled = compile_workflow(manifest, resolver=resolver, version="gate", use_cache=True)
    workflow_identity = build_workflow_identity(
        session,
        manifest.workflow_id,
    )
    knowledge_query_runner, knowledge_build_runner = build_knowledge_runtime_runners(
        session,
        identity=workflow_identity,
    )
    runs: list[GateRun] = []
    all_passed = True
    for gate in gates:
        artifact = manifest.artifacts.eval_datasets.get(gate.dataset_ref)
        dataset_name = artifact.dataset_name if artifact else gate.dataset_ref
        dataset = (
            session.execute(
                select(CaliberEvalDataset).where(CaliberEvalDataset.name == dataset_name)
            )
            .scalars()
            .first()
        )
        examples: list[CaliberEvalDatasetExample] = []
        if dataset is not None:
            examples = list(
                session.execute(
                    select(CaliberEvalDatasetExample).where(
                        CaliberEvalDatasetExample.dataset_id == dataset.dataset_id,
                        CaliberEvalDatasetExample.superseded_at.is_(None),
                    )
                )
                .scalars()
                .all()
            )

        if not examples:
            runs.append(
                GateRun(
                    name=_gate_name(manifest, gate),
                    dataset_ref=gate.dataset_ref,
                    passed=True,
                    pass_rate=1.0,
                    n_examples=0,
                    detail="no examples; gate treated as pass",
                )
            )
            continue

        # Sample a bounded slice so a large dataset doesn't time out the
        # synchronous promote request (ext D3 / plan §26.4).
        sampled = examples[:sample_size] if sample_size and sample_size > 0 else examples
        examples = sampled

        plan = RuntimePlan(
            ir=compiled.ir,
            resolver=resolver,
            knowledge_query_runner=knowledge_query_runner,
            knowledge_build_runner=knowledge_build_runner,
        )
        completed = 0
        for example in examples:
            run = execute(plan, _example_input(example), executor=executor)
            if run.status == "completed":
                completed += 1
        pass_rate = completed / len(examples)
        threshold = float(gate.thresholds.get("min_pass_rate", 1.0))
        passed = pass_rate >= threshold
        all_passed = all_passed and passed
        runs.append(
            GateRun(
                name=_gate_name(manifest, gate),
                dataset_ref=gate.dataset_ref,
                passed=passed,
                pass_rate=pass_rate,
                n_examples=len(examples),
                detail=f"pass_rate {pass_rate:.2f} vs min {threshold:.2f}",
            )
        )

    return GateResult(has_gate=True, passed=all_passed, runs=runs)


def _gate_name(manifest: WorkflowManifest, gate: DeployGate) -> str:
    for name, candidate in manifest.deploy_gates.items():
        if candidate is gate:
            return name
    return gate.dataset_ref


# ---------------------------------------------------------------------------
# Agent Fleet sync
# ---------------------------------------------------------------------------


def _fleet_agent_id(workflow_id: str, node_id: str) -> str:
    """Deterministic fleet ``agent_id`` for an agent node, keyed on
    ``(workflow_id, node_id)`` so re-deploys upsert the same row instead of
    creating duplicates. Bounded well under the 64-char column."""
    digest = hashlib.sha256(f"{workflow_id}:{node_id}".encode()).hexdigest()
    return f"wfa-{digest[:24]}"


def _fleet_experiment_id(workflow_id: str, node_id: str) -> str:
    """Deterministic, globally-unique synthetic ``experiment_id`` for an
    auto-registered agent. The fleet's ``experiment_id`` column carries a unique
    constraint, so agents from the same workflow cannot share the workflow's
    default experiment — derive a stable per-node id instead."""
    digest = hashlib.sha256(f"exp:{workflow_id}:{node_id}".encode()).hexdigest()
    return f"wfx-{digest[:24]}"


def sync_fleet_from_version(
    session: Session,
    version: CaliberWorkflowVersion,
    *,
    actor: str,
) -> list[CaliberAgentConfig]:
    """Upsert one Agent Fleet entry per agent node in a deployed version.

    Called whenever a deployment alias rotates to a published version (see
    :func:`_rotate_alias`) so agents authored in Workflow Studio show up in the
    Agent Fleet automatically — no separate ``POST /agents`` call needed.

    Idempotent: the synthetic ``agent_id`` is keyed on ``(workflow_id, node_id)``
    so a re-deploy updates the existing row (refreshing the name / artifact types
    / skill list) rather than creating a duplicate. A malformed manifest never
    blocks the deploy — it just yields an empty sync.
    """
    try:
        manifest = parse_manifest(version.manifest)
    except Exception:  # fleet sync must never break a deploy
        return []

    workflow = session.get(CaliberWorkflow, version.workflow_id)
    project_id = workflow.project_id if workflow is not None else None

    synced: list[CaliberAgentConfig] = []
    for node_id, node in manifest.nodes.items():
        if not isinstance(node, AgentNode):
            continue

        agent_id = _fleet_agent_id(version.workflow_id, node_id)
        artifact_types = ["prompt"]
        if node.skills:
            artifact_types.append("skill")
        if node.tools:
            artifact_types.append("tool")
        optimizer_config: dict[str, Any] = {
            "source_workflow_id": version.workflow_id,
            "source_node_id": node_id,
        }
        if node.skills:
            optimizer_config["skills"] = list(node.skills)

        existing = session.get(CaliberAgentConfig, agent_id)
        if existing is None:
            agent = CaliberAgentConfig(
                agent_id=agent_id,
                experiment_id=_fleet_experiment_id(version.workflow_id, node_id),
                name=node.name,
                owner=actor,
                project_id=project_id,
                visibility="project" if project_id else "user",
                artifact_types=artifact_types,
                optimizer_config=optimizer_config,
                enabled=True,
            )
            session.add(agent)
            audit_record(
                session,
                actor=actor,
                action="register_agent",
                entity_type="agent",
                entity_id=agent_id,
                details={
                    "source": "workflow_deploy",
                    "workflow_id": version.workflow_id,
                    "node_id": node_id,
                    "version_id": version.version_id,
                },
            )
            synced.append(agent)
        else:
            # Refresh the mutable, workflow-derived fields; leave operator-managed
            # fields (enabled, thresholds, approvals) untouched.
            existing.name = node.name
            existing.artifact_types = artifact_types
            existing.optimizer_config = {**existing.optimizer_config, **optimizer_config}
            synced.append(existing)

    session.flush()
    return synced


# ---------------------------------------------------------------------------
# Promote / rollback
# ---------------------------------------------------------------------------


@dataclass
class PromotionResult:
    """Outcome of a promote call.

    Ungated aliases rotate immediately (``rotated=True``, ``deployment`` set).
    Gated aliases (``prod``) create a pending approval request instead
    (``rotated=False``, ``promotion`` set, ``deployment`` is the pre-existing
    target if any).
    """

    gate_result: GateResult
    rotated: bool
    deployment: CaliberWorkflowDeployment | None = None
    promotion: CaliberWorkflowPromotion | None = None


def _rotate_alias(
    session: Session,
    workflow_id: str,
    alias: str,
    version_id: str,
    *,
    actor: str,
) -> CaliberWorkflowDeployment:
    """Point ``alias`` at ``version_id``, pushing the prior target onto the
    rollback stack. Creates the deployment row on first use."""
    deployment = (
        session.execute(
            select(CaliberWorkflowDeployment).where(
                CaliberWorkflowDeployment.workflow_id == workflow_id,
                CaliberWorkflowDeployment.alias == alias,
            )
        )
        .scalars()
        .first()
    )
    now = datetime.now(timezone.utc)
    if deployment is None:
        deployment = CaliberWorkflowDeployment(
            deployment_id=new_workflow_deployment_id(),
            workflow_id=workflow_id,
            alias=alias,
            version_id=version_id,
            status="active",
            deployed_by=actor,
            deployed_at=now,
            rollback_checkpoint=[],
        )
        session.add(deployment)
    else:
        checkpoint = list(deployment.rollback_checkpoint or [])
        checkpoint.append(
            {
                "version_id": deployment.version_id,
                "deployed_at": deployment.deployed_at.isoformat()
                if deployment.deployed_at
                else None,
                "deployed_by": deployment.deployed_by,
            }
        )
        deployment.rollback_checkpoint = checkpoint
        deployment.version_id = version_id
        deployment.deployed_by = actor
        deployment.deployed_at = now
        deployment.status = "active"
    session.flush()

    # Register/refresh the deployed version's agent nodes in the Agent Fleet so
    # agents authored in Workflow Studio appear there as soon as they're deployed.
    version = session.get(CaliberWorkflowVersion, version_id)
    if version is not None:
        sync_fleet_from_version(session, version, actor=actor)

    return deployment


def promote(
    session: Session,
    workflow_id: str,
    alias: str,
    version: CaliberWorkflowVersion,
    *,
    actor: str,
    resolver: InMemoryToolResolver | None = None,
    executor: WorkflowExecutor | None = None,
) -> PromotionResult:
    """Run deploy gates, then rotate (ungated) or request approval (gated).

    For a **gated** alias (``prod``) the deploy gate must exist and pass; on
    success a pending :class:`CaliberWorkflowPromotion` is created and the alias
    is left unchanged until a reviewer approves it (plan §15.3). For an
    **ungated** alias the alias rotates immediately after gates pass.

    Raises :class:`DeployError` if the version is not published or a gated alias
    has no deploy gate, and :class:`DeployGateFailedError` if gates fail.
    """
    if version.status != "published":
        raise DeployError("only published versions can be promoted")

    resolver = resolver or resolver_from_session(session)
    executor = executor or build_executor(None)
    manifest = parse_manifest(version.manifest)

    gate_result = evaluate_deploy_gates(
        session, manifest, alias, resolver=resolver, executor=executor
    )
    if alias in GATED_ALIASES and not gate_result.has_gate:
        raise DeployError(f"promoting to {alias!r} requires a deploy gate")
    if not gate_result.passed:
        raise DeployGateFailedError(
            f"deploy gate failed for alias {alias!r}", detail=gate_result.to_dict()
        )

    if alias in GATED_ALIASES:
        # Supersede any prior pending request for this alias, then record a new one.
        prior_pending = (
            session.execute(
                select(CaliberWorkflowPromotion).where(
                    CaliberWorkflowPromotion.workflow_id == workflow_id,
                    CaliberWorkflowPromotion.alias == alias,
                    CaliberWorkflowPromotion.status == "pending",
                )
            )
            .scalars()
            .all()
        )
        for pending in prior_pending:
            pending.status = "superseded"
        promotion = CaliberWorkflowPromotion(
            promotion_id=new_workflow_promotion_id(),
            workflow_id=workflow_id,
            alias=alias,
            version_id=version.version_id,
            status="pending",
            gate_result=gate_result.to_dict(),
            requested_by=actor,
        )
        session.add(promotion)
        session.flush()
        existing = (
            session.execute(
                select(CaliberWorkflowDeployment).where(
                    CaliberWorkflowDeployment.workflow_id == workflow_id,
                    CaliberWorkflowDeployment.alias == alias,
                )
            )
            .scalars()
            .first()
        )
        return PromotionResult(
            gate_result=gate_result, rotated=False, deployment=existing, promotion=promotion
        )

    deployment = _rotate_alias(session, workflow_id, alias, version.version_id, actor=actor)
    return PromotionResult(gate_result=gate_result, rotated=True, deployment=deployment)


def approve_promotion(
    session: Session,
    promotion: CaliberWorkflowPromotion,
    *,
    actor: str,
) -> CaliberWorkflowDeployment:
    """Approve a pending promotion: rotate the alias and mark it approved."""
    if promotion.status != "pending":
        raise DeployError(
            f"promotion {promotion.promotion_id!r} is not pending (status={promotion.status!r})"
        )
    version = session.get(CaliberWorkflowVersion, promotion.version_id)
    if version is None or version.status != "published":
        raise DeployError("promotion target version is missing or not published")
    deployment = _rotate_alias(
        session, promotion.workflow_id, promotion.alias, promotion.version_id, actor=actor
    )
    promotion.status = "approved"
    promotion.decided_by = actor
    promotion.decided_at = datetime.now(timezone.utc)
    session.flush()
    return deployment


def reject_promotion(
    session: Session,
    promotion: CaliberWorkflowPromotion,
    *,
    actor: str,
    reason: str | None = None,
) -> CaliberWorkflowPromotion:
    """Reject a pending promotion; the alias stays on its current target."""
    if promotion.status != "pending":
        raise DeployError(
            f"promotion {promotion.promotion_id!r} is not pending (status={promotion.status!r})"
        )
    promotion.status = "rejected"
    promotion.decided_by = actor
    promotion.decided_at = datetime.now(timezone.utc)
    promotion.decision_reason = reason
    session.flush()
    return promotion


def promote_workflow_candidate(
    session: Session,
    *,
    workflow_id: str,
    candidate_manifest: dict[str, Any],
    alias: str,
    actor: str,
) -> tuple[CaliberWorkflowVersion, CaliberWorkflowDeployment]:
    """Publish a new version from a CALIBER candidate manifest and rotate ``alias``.

    Used by the approvals route when a workflow-manifest refinement job is
    approved (plan §17.5): a new immutable version is created from the candidate
    manifest, compiled+published, and the deployment alias is rotated to it with
    a rollback checkpoint. ``alias`` is expected to be ungated (``dev``/
    ``staging``); gated aliases still go through the human promotion request.
    """
    max_number = (
        session.execute(
            select(CaliberWorkflowVersion.version_number)
            .where(CaliberWorkflowVersion.workflow_id == workflow_id)
            .order_by(CaliberWorkflowVersion.version_number.desc())
        )
        .scalars()
        .first()
    )
    manifest = parse_manifest(candidate_manifest)
    version = CaliberWorkflowVersion(
        version_id=new_workflow_version_id(),
        workflow_id=workflow_id,
        version_number=(max_number or 0) + 1,
        status="draft",
        manifest=manifest.to_dict(),
        manifest_hash=manifest.manifest_hash(),
        created_by=actor,
    )
    session.add(version)
    session.flush()
    publish_version(session, version, actor=actor)
    deployment = _rotate_alias(session, workflow_id, alias, version.version_id, actor=actor)
    return version, deployment


def rollback(
    session: Session,
    workflow_id: str,
    alias: str,
    *,
    actor: str,
) -> CaliberWorkflowDeployment:
    """Restore the alias to its previous target (plan §17.5)."""
    deployment = (
        session.execute(
            select(CaliberWorkflowDeployment).where(
                CaliberWorkflowDeployment.workflow_id == workflow_id,
                CaliberWorkflowDeployment.alias == alias,
            )
        )
        .scalars()
        .first()
    )
    if deployment is None:
        raise RollbackError(f"no deployment for alias {alias!r}")
    checkpoint = list(deployment.rollback_checkpoint or [])
    if not checkpoint:
        raise RollbackError("no rollback checkpoint")
    prior = checkpoint.pop()
    deployment.version_id = prior["version_id"]
    deployment.rollback_checkpoint = checkpoint
    deployment.deployed_by = actor
    deployment.deployed_at = datetime.now(timezone.utc)
    session.flush()
    return deployment


__all__ = [
    "GATED_ALIASES",
    "LIVE_ALIASES",
    "DeployError",
    "DeployGateFailedError",
    "GateResult",
    "GateRun",
    "PromotionResult",
    "PublishError",
    "RollbackError",
    "approve_promotion",
    "build_executor",
    "build_plan",
    "compile_version",
    "evaluate_deploy_gates",
    "promote",
    "promote_workflow_candidate",
    "prune_workflow_runs",
    "publish_version",
    "record_workflow_run",
    "reject_promotion",
    "resolver_from_session",
    "rollback",
    "run_preview",
]
