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
import json
import logging
import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select
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
from caliber.deployment_environments import environment_class
from caliber.egress import EgressPolicy
from caliber.eval.scorecard import expected_text, run_scorecard
from caliber.ids import (
    new_workflow_deployment_id,
    new_workflow_promotion_id,
    new_workflow_run_id,
    new_workflow_version_id,
)
from caliber.knowledge.schemas import KnowledgeBaseVersionCreateRequest, KnowledgeQueryRequest
from caliber.knowledge.service import KnowledgeBaseService
from caliber.secrets import resolve_secret
from caliber.storage.base import StorageError
from caliber.workflows.compiler import CompileError, CompileResult, compile_workflow
from caliber.workflows.deploy_gate import GateMetrics, evaluate_thresholds
from caliber.workflows.file_tools import (
    bind_project_managed_file_runtime,
    managed_file_tool_aliases,
)
from caliber.workflows.ir import IRWorkflow
from caliber.workflows.manifest import (
    AgentNode,
    DeployGate,
    WorkflowManifest,
    compute_manifest_hash,
    parse_manifest,
)
from caliber.workflows.runtime import (
    DEFAULT_PARALLEL_BRANCH_MAX_WORKERS,
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

logger = logging.getLogger("caliber.workflows.promoter")

# Legacy explicit alias list requiring human approval (a pending promotion
# request) before the alias rotates. Empty by default and retained as an
# ADDITIONAL opt-in: the primary rule is now the alias's *environment class*, via
# ``requires_human_approval`` below. Keying release governance to a literal alias
# string was the same defect as keying MCP isolation to one — 'production' and
# 'prod-eu' silently escaped it.
GATED_ALIASES: frozenset[str] = frozenset()


def requires_human_approval(alias: str, config: CaliberConfig | None = None) -> bool:
    """Whether promoting to ``alias`` must pause for a human approval.

    Resolved from the environment class, so every spelling of production is
    covered. Off by default (``release_require_human_approval_for_environment_classes``
    is empty) because the shipped single-environment product deploys immediately;
    the machinery is fully wired and tested, so enabling it is one setting rather
    than a code change.
    """
    if alias in GATED_ALIASES:
        return True
    raw = getattr(config, "release_require_human_approval_for_environment_classes", "") or ""
    classes = {item.strip().casefold() for item in str(raw).split(",") if item.strip()}
    return environment_class(alias, config) in classes


def requires_graded_executor(alias: str, config: CaliberConfig | None = None) -> bool:
    """Whether ``alias``'s deploy gate must be graded by a real configured model.

    Defaults to the ``production`` class. The deterministic fake executor is the
    right default for tests and local demos, but a gate verdict it produced proves
    the graph and the dataset — not the model that will answer production traffic.
    Publishing that as release evidence is the false-confidence failure this policy
    exists to prevent, so a production promotion graded by the fake is refused and
    says why.
    """
    raw = getattr(config, "release_require_graded_executor_for_environment_classes", None)
    if raw is None:
        raw = "production"
    classes = {item.strip().casefold() for item in str(raw).split(",") if item.strip()}
    return environment_class(alias, config) in classes


def requires_quality_gate(alias: str, config: CaliberConfig | None = None) -> bool:
    """Whether promoting to ``alias`` must have a passing deploy gate.

    Defaults to the ``production`` class: rotating a production alias onto a
    version with no graded evidence is exactly the "false release evidence"
    problem, and refusing it is the fail-closed answer. Development and staging
    deploy freely.
    """
    raw = getattr(config, "release_require_quality_gate_for_environment_classes", None)
    if raw is None:
        raw = "production"
    classes = {item.strip().casefold() for item in str(raw).split(",") if item.strip()}
    return environment_class(alias, config) in classes


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


class AliasPreflightError(Exception):
    """Raised when an alias transition's target fails MCP deployment preflight.

    Distinct from :class:`DeployError` so a caller can report the blockers, and
    so rollback — which previously ran no preflight at all — can surface a
    precise reason instead of a generic 400.
    """

    def __init__(self, message: str, *, blockers: list[str] | None = None) -> None:
        super().__init__(message)
        self.blockers = list(blockers or [])


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


#: Executor classes that answer from a deterministic script rather than a model.
#: Named by class name so a test double registered elsewhere is classified the same
#: way, and so this does not import the fake into a production import path.
_DETERMINISTIC_EXECUTOR_CLASSES = frozenset({"FakeWorkflowExecutor", "ScriptedWorkflowExecutor"})


def describe_executor(
    executor: WorkflowExecutor, config: CaliberConfig | None = None
) -> dict[str, Any]:
    """Identify the executor that graded a gate, for the evidence record.

    A gate verdict is only interpretable if you know what produced it. Before this,
    a stored ``gate_result`` was silent about whether a real model answered or a
    deterministic script did, so a passing production gate and a passing local demo
    gate were indistinguishable in the audit trail.

    ``deterministic`` is derived from the executor *class*, not from configuration:
    the question is what actually ran, and a config-derived answer would be wrong
    exactly when a caller passed an explicit executor that disagrees with config.
    """
    class_name = type(executor).__name__
    deterministic = class_name in _DETERMINISTIC_EXECUTOR_CLASSES
    provider = "fake" if deterministic else str(getattr(config, "llm_provider", "") or "unknown")
    return {
        "provider": provider.strip().lower(),
        "executor": class_name,
        "model": (
            None if deterministic else (getattr(config, "llm_diagnosis_model", None) or None)
        ),
        "deterministic": deterministic,
    }


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
        result = knowledge_service.query(
            request,
            identity=identity,
        ).model_dump(mode="json")
        # Pin the resolved corpus version(s) so a run that followed the KB's
        # active pointer is reproducible after that pointer moves.
        result["resolved_version_ids"] = version_ids
        if knowledge_base_id:
            result["resolved_knowledge_base_id"] = knowledge_base_id
        return result

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
        result = knowledge_service.query(
            request,
            identity=workflow_identity,
        ).model_dump(mode="json")
        # Pin the resolved corpus version(s) into the node output so a run that
        # followed the KB's *active* pointer stays reproducible after that
        # pointer moves. This is the PRIMARY live path (orchestrator, run/preview,
        # evals, subworkflows, assistant), so the pin must be here too — not only
        # on the deploy-gate runner.
        result["resolved_version_ids"] = version_ids
        if knowledge_base_id:
            result["resolved_knowledge_base_id"] = knowledge_base_id
        return result

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
        parallel_branch_max_workers=(
            config.workflow_parallel_branch_max_workers
            if config is not None
            else DEFAULT_PARALLEL_BRANCH_MAX_WORKERS
        ),
        # Empty when no config is threaded (preview/eval/calibration builders), which
        # fails closed: an external_app node refuses rather than importing.
        external_app_entrypoint_allowlist=(
            config.external_app_entrypoint_allowlist if config is not None else ""
        ),
        # The operator's configured sandbox limits, not the class defaults.
        sandbox_max_memory_bytes=(
            config.tool_sandbox_max_memory_bytes if config is not None else None
        ),
        sandbox_max_file_bytes=(config.tool_sandbox_max_file_bytes if config is not None else None),
        sandbox_max_open_files=(config.tool_sandbox_max_open_files if config is not None else None),
        sandbox_config=config,
        # Built here, from config, so CALIBER_EGRESS_* actually reaches the nodes it
        # governs. ``from_config(None)`` returns the safe default rather than no
        # policy, so a builder that threads no config still constrains egress.
        egress_policy=EgressPolicy.from_config(config),
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
    resolved_config = config or CaliberConfig()
    plan = build_plan(
        session,
        version,
        manifest_override=manifest_override,
        config=resolved_config,
    )
    executor = build_executor(resolved_config, ir=plan.ir)
    workflow = session.get(CaliberWorkflow, version.workflow_id)
    managed_snapshots = [
        file_ref
        for node in plan.ir.nodes.values()
        if (file_ref := getattr(node, "file_ref", None)) is not None
    ]
    managed_resolver, managed_tools = bind_project_managed_file_runtime(
        session,
        storage_config=resolved_config.workflow_storage,
        project_id=(workflow.project_id if workflow is not None else None),
        workflow_id=version.workflow_id,
        runtime_id=f"preview-{version.version_id}",
        snapshots=managed_snapshots,
        extract_document_aliases=managed_file_tool_aliases(plan.ir),
    )
    budget = TokenBudget(token_budget) if token_budget else TokenBudget()
    result = execute(
        plan,
        input_text,
        executor=executor,
        session_id=session_id,
        preview=True,
        token_budget=budget,
        extra_tools=managed_tools,
        managed_file_resolver=(managed_resolver.read_text if managed_resolver else None),
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
    # Age by the first available timestamp: started runs use ``started_at``,
    # but queued/preview runs leave it NULL — they previously evaded pruning
    # entirely (unbounded index growth), so fall back to ``queued_at`` then
    # ``completed_at``.
    age_ts = func.coalesce(
        CaliberWorkflowRun.started_at,
        CaliberWorkflowRun.queued_at,
        CaliberWorkflowRun.completed_at,
    )
    rows = session.execute(select(CaliberWorkflowRun).where(age_ts < cutoff)).scalars().all()
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
    #: Everything measured during the replay: completion, weighted scorer means,
    #: latency percentiles, token spend, and the baseline it was compared with.
    #: Persisted with the promotion so a release decision can be re-read later
    #: instead of being reduced to a single boolean.
    metrics: dict[str, Any] = field(default_factory=dict)
    #: Per-threshold verdicts, including thresholds that failed *closed* because
    #: they were unsupported or unmeasurable.
    thresholds: list[dict[str, Any]] = field(default_factory=list)
    #: Dataset identity this verdict is about. Without it a stored gate result
    #: cannot be tied back to the data that produced it.
    dataset_id: str | None = None
    dataset_version: int | None = None
    #: SHA-256 over the ordered (example_id, input, expected, weight) tuples that
    #: were actually replayed. Two gate results with the same digest graded the
    #: same evidence; a different digest means the sample changed.
    sample_digest: str | None = None
    #: Total examples active in the dataset before the sample bound was applied,
    #: so a bounded gate cannot be mistaken for an exhaustive one.
    available_examples: int | None = None
    scorers: list[str] = field(default_factory=list)


@dataclass
class GateResult:
    has_gate: bool
    passed: bool
    runs: list[GateRun] = field(default_factory=list)
    #: Which executor graded the replays — see :func:`describe_executor`. ``None``
    #: only when no gate applied, so there was nothing to grade.
    executor: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "has_gate": self.has_gate,
            "passed": self.passed,
            "executor": self.executor,
            "runs": [
                {
                    "name": r.name,
                    "dataset_ref": r.dataset_ref,
                    "passed": r.passed,
                    "pass_rate": r.pass_rate,
                    "n_examples": r.n_examples,
                    "detail": r.detail,
                    "metrics": r.metrics,
                    "thresholds": r.thresholds,
                    "dataset_id": r.dataset_id,
                    "dataset_version": r.dataset_version,
                    "sample_digest": r.sample_digest,
                    "available_examples": r.available_examples,
                    "scorers": r.scorers,
                }
                for r in self.runs
            ],
        }


def _sample_digest(examples: list[CaliberEvalDatasetExample]) -> str:
    """Content digest of the replayed sample.

    Over the *ordered* graded inputs — example id, input, expected, and weight —
    so the digest changes if the data, the weighting, or the sample boundary
    changes. This is what makes a stored gate verdict checkable later rather than
    reproducible only by convention.
    """
    payload = json.dumps(
        [
            {
                "example_id": example.example_id,
                "input": example.input or {},
                "expected": example.expected or {},
                "weight": example.weight,
            }
            for example in examples
        ],
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()


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
    config: CaliberConfig | None = None,
) -> GateResult:
    """Run the deploy gates relevant to ``alias`` against the eval datasets.

    Each replay is **graded**, not merely counted. The sample is scored against
    the dataset's expected output with the deterministic scorers the evaluation
    product already ships, and wall-clock duration plus token spend are measured
    per replay, so a release can be gated on quality, latency, and cost rather
    than on "the graph did not crash". Every configured threshold is then either
    evaluated or reported as unsupported/unmeasurable and fails the gate closed —
    a threshold can never be silently ignored (see
    :mod:`caliber.workflows.deploy_gate`).

    Replays use preview mode so a promotion check never becomes an unannounced
    production-side-effect path. A missing dataset, an archived dataset, or a
    dataset with no active examples fails closed: none of those is evidence that
    a version is safe to promote.

    The result carries the dataset identity, the pre-truncation example count, and
    a content digest of the exact sample replayed, so a stored verdict can be tied
    back to the evidence that produced it.
    """
    gates = [g for g in manifest.deploy_gates.values() if alias in g.required_for_aliases]
    if not gates:
        return GateResult(has_gate=False, passed=True)

    compiled = compile_workflow(manifest, resolver=resolver, version="gate", use_cache=True)
    resolved_config = config or CaliberConfig()
    workflow_identity = build_workflow_identity(
        session,
        manifest.workflow_id,
    )
    knowledge_query_runner, knowledge_build_runner = build_knowledge_runtime_runners(
        session,
        identity=workflow_identity,
    )
    workflow = session.get(CaliberWorkflow, manifest.workflow_id)
    managed_snapshots = [
        file_ref
        for node in (compiled.ir.nodes.values() if compiled.ir is not None else [])
        if (file_ref := getattr(node, "file_ref", None)) is not None
    ]
    try:
        managed_resolver, managed_tools = bind_project_managed_file_runtime(
            session,
            storage_config=resolved_config.workflow_storage,
            project_id=(workflow.project_id if workflow is not None else None),
            workflow_id=manifest.workflow_id,
            runtime_id=f"deploy-gate-{alias}",
            snapshots=managed_snapshots,
            extract_document_aliases=(
                managed_file_tool_aliases(compiled.ir) if compiled.ir is not None else set()
            ),
        )
    except (ValueError, RuntimeError) as exc:
        return GateResult(
            has_gate=True,
            passed=False,
            runs=[
                GateRun(
                    name=_gate_name(manifest, gate),
                    dataset_ref=gate.dataset_ref,
                    passed=False,
                    pass_rate=0.0,
                    n_examples=0,
                    detail=f"managed file preflight failed: {exc}",
                )
                for gate in gates
            ],
            executor=describe_executor(executor, resolved_config),
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
        available = 0
        if dataset is not None and dataset.status == "active":
            active_filter = (
                CaliberEvalDatasetExample.dataset_id == dataset.dataset_id,
                CaliberEvalDatasetExample.superseded_at.is_(None),
            )
            # Count before truncating: a bounded sample must never be reported as
            # if it were the whole dataset.
            available = int(
                session.execute(
                    select(func.count())
                    .select_from(CaliberEvalDatasetExample)
                    .where(*active_filter)
                ).scalar()
                or 0
            )
            examples_query = (
                select(CaliberEvalDatasetExample)
                .where(*active_filter)
                .order_by(
                    CaliberEvalDatasetExample.created_at.asc(),
                    CaliberEvalDatasetExample.example_id.asc(),
                )
            )
            if sample_size and sample_size > 0:
                examples_query = examples_query.limit(sample_size)
            examples = list(session.execute(examples_query).scalars().all())

        if not examples:
            all_passed = False
            if dataset is None:
                detail = "dataset not found; gate failed closed"
            elif dataset.status != "active":
                detail = "dataset is archived; gate failed closed"
            else:
                detail = "dataset has no active examples; gate failed closed"
            runs.append(
                GateRun(
                    name=_gate_name(manifest, gate),
                    dataset_ref=gate.dataset_ref,
                    passed=False,
                    pass_rate=0.0,
                    n_examples=0,
                    detail=detail,
                    dataset_id=dataset.dataset_id if dataset is not None else None,
                    dataset_version=dataset.version if dataset is not None else None,
                    available_examples=available,
                )
            )
            continue

        managed_execute_kwargs: dict[str, Any] = {}
        if managed_resolver is not None:
            managed_execute_kwargs = {
                "extra_tools": managed_tools,
                "managed_file_resolver": managed_resolver.read_text,
            }
        metrics = _replay_and_score(
            session,
            ir=compiled.ir,
            resolver=resolver,
            knowledge_query_runner=knowledge_query_runner,
            knowledge_build_runner=knowledge_build_runner,
            executor=executor,
            examples=examples,
            gate=gate,
            managed_execute_kwargs=managed_execute_kwargs,
        )
        if "min_overall_delta" in gate.thresholds:
            metrics.baseline_overall = _baseline_overall(
                session,
                workflow_id=manifest.workflow_id,
                alias=alias,
                resolver=resolver,
                executor=executor,
                examples=examples,
                gate=gate,
                config=resolved_config,
            )

        # No implicit default: a gate that configures no threshold asserts
        # nothing, and evaluate_thresholds says so and fails closed rather than
        # inventing a claim on the operator's behalf.
        outcomes = evaluate_thresholds(dict(gate.thresholds), metrics)
        failures = [outcome for outcome in outcomes if not outcome.passed]
        passed = not failures
        all_passed = all_passed and passed
        detail = "; ".join(outcome.detail for outcome in (failures if failures else outcomes))
        runs.append(
            GateRun(
                name=_gate_name(manifest, gate),
                dataset_ref=gate.dataset_ref,
                passed=passed,
                pass_rate=metrics.pass_rate,
                n_examples=metrics.n_examples,
                detail=detail,
                metrics=metrics.to_dict(),
                thresholds=[outcome.to_dict() for outcome in outcomes],
                dataset_id=dataset.dataset_id if dataset is not None else None,
                dataset_version=dataset.version if dataset is not None else None,
                sample_digest=_sample_digest(examples),
                available_examples=available,
                scorers=list(gate.scorers),
            )
        )

    return GateResult(
        has_gate=True,
        passed=all_passed,
        runs=runs,
        executor=describe_executor(executor, resolved_config),
    )


def _replay_and_score(
    session: Session,
    *,
    ir: Any,
    resolver: InMemoryToolResolver,
    knowledge_query_runner: Any,
    knowledge_build_runner: Any,
    executor: WorkflowExecutor,
    examples: list[CaliberEvalDatasetExample],
    gate: DeployGate,
    managed_execute_kwargs: dict[str, Any],
) -> GateMetrics:
    """Replay ``examples`` in preview mode and grade the outputs.

    Scoring reuses :func:`caliber.eval.scorecard.run_scorecard` rather than
    reimplementing it, so a deploy gate and an evaluation run agree by
    construction on weights, per-scorer aggregates, incomplete-row policy, and
    what "passed" means. A replay that does not reach ``completed`` raises inside
    ``predict``, which the scorecard records as an errored row: it cannot pass and
    is excluded from per-scorer aggregates, exactly as an evaluation error is.
    """
    del session  # replay needs no further session access; kept for signature parity
    plan = RuntimePlan(
        ir=ir,
        resolver=resolver,
        knowledge_query_runner=knowledge_query_runner,
        knowledge_build_runner=knowledge_build_runner,
    )
    metrics = GateMetrics(
        n_examples=len(examples),
        # Counted from the data, not from the scorer list: a scorer configured
        # against a dataset with no expected outputs measures nothing.
        scored_examples=sum(1 for example in examples if expected_text(example.expected)),
    )
    by_id = {example.example_id: example for example in examples}

    def predict(inputs: Mapping[str, Any]) -> str:
        example = by_id[str(inputs.get("__gate_example_id", ""))]
        started = time.monotonic()
        try:
            run = execute(
                plan,
                _example_input(example),
                executor=executor,
                preview=True,
                **managed_execute_kwargs,
            )
        finally:
            metrics.latencies_ms.append((time.monotonic() - started) * 1000.0)
        metrics.tokens.append(int(run.tokens or 0))
        if run.status != "completed":
            raise RuntimeError(f"replay status {run.status!r}: {run.error or 'no error detail'}")
        metrics.completed += 1
        return run.output

    scorecard_examples = [
        {
            "example_id": example.example_id,
            # The gate replays a workflow, whose input is derived from the whole
            # example rather than a single template field, so the example id is
            # threaded through and ``predict`` resolves the row from it.
            "input": {**(example.input or {}), "__gate_example_id": example.example_id},
            "expected": example.expected or {},
            "weight": example.weight,
            "tags": list(example.tags or []),
        }
        for example in examples
    ]
    result = run_scorecard(
        scorecard_examples,
        predict,
        gate.scorers or None,
        pass_threshold=gate.pass_threshold,
    )
    metrics.pass_rate = result.pass_rate
    metrics.overall = result.overall
    metrics.scorer_means = dict(result.aggregate)
    metrics.errored = sum(1 for row in result.rows if row.error)
    return metrics


def _baseline_overall(
    session: Session,
    *,
    workflow_id: str,
    alias: str,
    resolver: InMemoryToolResolver,
    executor: WorkflowExecutor,
    examples: list[CaliberEvalDatasetExample],
    gate: DeployGate,
    config: CaliberConfig,
) -> float | None:
    """Overall score of the alias's currently deployed version on the same sample.

    This is what makes ``min_overall_delta`` a real regression check instead of a
    decorative field: the candidate is compared against the version it would
    replace, graded on identical data. Returns ``None`` when nothing is deployed
    on the alias, when its version or manifest is unusable, or when replaying it
    raises — the threshold then fails closed rather than passing on absent
    evidence.

    The baseline binds **its own** managed files, resolved from its own manifest.
    Replaying it with no bindings at all (the previous behaviour) made a baseline
    that reads a managed document score near zero, so the candidate's delta looked
    like a large improvement when nothing had improved — a regression check that
    systematically favours the candidate is worse than none. Binding each side to its
    own documents is the comparison an operator means by "did this get worse?".
    """
    deployment = (
        session.execute(
            select(CaliberWorkflowDeployment).where(
                CaliberWorkflowDeployment.workflow_id == workflow_id,
                CaliberWorkflowDeployment.alias == alias,
                CaliberWorkflowDeployment.status == "active",
            )
        )
        .scalars()
        .first()
    )
    if deployment is None:
        return None
    version = session.get(CaliberWorkflowVersion, deployment.version_id)
    if version is None or not isinstance(version.manifest, dict):
        return None
    try:
        baseline_manifest = parse_manifest(version.manifest)
        compiled = compile_workflow(
            baseline_manifest, resolver=resolver, version="gate-baseline", use_cache=True
        )
        workflow_identity = build_workflow_identity(session, workflow_id)
        knowledge_query_runner, knowledge_build_runner = build_knowledge_runtime_runners(
            session, identity=workflow_identity
        )
        workflow = session.get(CaliberWorkflow, workflow_id)
        baseline_snapshots = [
            file_ref
            for node in (compiled.ir.nodes.values() if compiled.ir is not None else [])
            if (file_ref := getattr(node, "file_ref", None)) is not None
        ]
        managed_execute_kwargs: dict[str, Any] = {}
        if baseline_snapshots or compiled.ir is not None:
            baseline_resolver, baseline_tools = bind_project_managed_file_runtime(
                session,
                storage_config=config.workflow_storage,
                project_id=(workflow.project_id if workflow is not None else None),
                workflow_id=workflow_id,
                runtime_id=f"deploy-gate-baseline-{alias}",
                snapshots=baseline_snapshots,
                extract_document_aliases=(
                    managed_file_tool_aliases(compiled.ir) if compiled.ir is not None else set()
                ),
            )
            if baseline_resolver is not None:
                managed_execute_kwargs = {
                    "extra_tools": baseline_tools,
                    "managed_file_resolver": baseline_resolver.read_text,
                }
        return _replay_and_score(
            session,
            ir=compiled.ir,
            resolver=resolver,
            knowledge_query_runner=knowledge_query_runner,
            knowledge_build_runner=knowledge_build_runner,
            executor=executor,
            examples=examples,
            gate=gate,
            managed_execute_kwargs=managed_execute_kwargs,
        ).overall
    except Exception:
        logger.warning(
            "deploy gate baseline replay failed for %s@%s; min_overall_delta will fail closed",
            workflow_id,
            alias,
            exc_info=True,
        )
        del config  # accepted for signature parity with the candidate replay
        return None


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


def require_alias_target_ready(
    session: Session,
    alias: str,
    version_id: str,
    *,
    config: CaliberConfig | None = None,
) -> None:
    """MCP deployment preflight for one alias transition.

    Called from :func:`_rotate_alias`, i.e. from **every** path that moves an
    alias — forward promotion, promotion approval, refinement-candidate
    rotation, and rollback. Route-level preflight covered only the first two, so
    a rollback or an approved refinement candidate could rotate the live alias
    onto a version whose MCP dependencies were disabled, deleted, or blocked by
    policy. Enforcing it here makes the check a property of the transition rather
    than of the caller that happens to remember it.
    """
    from caliber.mcp_policy import deployment_blockers  # noqa: PLC0415

    version = session.get(CaliberWorkflowVersion, version_id)
    if version is None:
        raise AliasPreflightError(
            f"alias {alias!r} cannot rotate to missing version {version_id!r}",
            blockers=[f"version {version_id!r} does not exist"],
        )
    blockers = deployment_blockers(
        session,
        version.manifest or {},
        alias=alias,
        config=config,
        # An alias rotation must be able to prove the whole graph, children
        # included; a run submission keeps its runtime-error contract.
        require_resolvable_subworkflows=True,
    )
    blockers.extend(_managed_file_blockers(session, version, config=config))
    if blockers:
        raise AliasPreflightError(
            f"deployment preflight failed for alias {alias!r}: " + "; ".join(blockers),
            blockers=blockers,
        )


def _managed_file_blockers(
    session: Session,
    version: CaliberWorkflowVersion,
    *,
    config: CaliberConfig | None,
) -> list[str]:
    """Re-verify the version's pinned managed files at the moment of rotation.

    Closes a time-of-check/time-of-use hole: a deploy gate verified every pinned
    object when it evaluated, but approval (potentially much later) did not
    re-read them, so deleting or replacing an object between evaluation and
    approval could rotate the alias onto a version that cannot run. Rollback had
    the same hole with no check at all.

    Verification is by row id, object version, size, and byte digest — the same
    contract the runtime enforces — so a *replaced* object is caught, not just a
    deleted one.
    """
    manifest_dict = version.manifest or {}
    try:
        manifest = parse_manifest(manifest_dict)
    except Exception as exc:
        return [f"version {version.version_id!r} manifest is not parseable: {exc}"]
    # Walrus rather than ``node.file_ref``: the node union is 29 variants wide and
    # only a few carry the attribute, so the bound name is what makes this typed.
    snapshots = [
        file_ref
        for node in manifest.nodes.values()
        if (file_ref := getattr(node, "file_ref", None)) is not None
    ]
    if not snapshots:
        return []
    workflow = session.get(CaliberWorkflow, version.workflow_id)
    resolved_config = config or CaliberConfig()
    try:
        bind_project_managed_file_runtime(
            session,
            storage_config=resolved_config.workflow_storage,
            project_id=(workflow.project_id if workflow is not None else None),
            workflow_id=version.workflow_id,
            runtime_id=f"alias-preflight-{version.version_id}",
            snapshots=snapshots,
        )
    except (ValueError, RuntimeError, OSError, StorageError) as exc:
        # StorageError covers the case the review named specifically: an object
        # that physically disappeared. It is not a ValueError, so catching only
        # the validation errors would let a missing object escape as a 500.
        return [f"managed file preflight failed: {exc}"]
    return []


def _rotate_alias(
    session: Session,
    workflow_id: str,
    alias: str,
    version_id: str,
    *,
    actor: str,
    config: CaliberConfig | None = None,
    preflight: bool = True,
) -> CaliberWorkflowDeployment:
    """Point ``alias`` at ``version_id``, pushing the prior target onto the
    rollback stack. Creates the deployment row on first use.

    Runs MCP deployment preflight first (``preflight=False`` only for callers
    that have already run an equivalent check on the same version and alias, to
    avoid a redundant manifest walk).
    """
    if preflight:
        require_alias_target_ready(session, alias, version_id, config=config)
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
    # ``environment`` was a dormant column: promote requests could not set it and
    # alias rotation never populated it, so nothing could report what class of
    # environment a deployment actually served. Derive it from the same resolver
    # that keys the isolation requirement, so the stored value and the enforced
    # policy can never disagree.
    resolved_environment = environment_class(alias, config)
    if deployment is None:
        deployment = CaliberWorkflowDeployment(
            deployment_id=new_workflow_deployment_id(),
            workflow_id=workflow_id,
            alias=alias,
            version_id=version_id,
            environment=resolved_environment,
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
        deployment.environment = resolved_environment
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
    config: CaliberConfig | None = None,
) -> PromotionResult:
    """Run deploy gates, then rotate or request approval per release policy.

    Two independent policies, both resolved from the alias's *environment class*
    rather than from a literal alias string:

    * :func:`requires_quality_gate` — defaults to the ``production`` class. A
      production promotion with no attached deploy gate is refused, because
      rotating a live alias onto a version with no graded evidence is precisely
      the false-release-evidence problem.
    * :func:`requires_human_approval` — off by default. When on, a passing gate
      creates a pending :class:`CaliberWorkflowPromotion` and the alias stays put
      until a reviewer approves it (plan §15.3).

    Raises :class:`DeployError` if the version is not published or a gated alias
    has no deploy gate, and :class:`DeployGateFailedError` if gates fail.
    """
    if version.status != "published":
        raise DeployError("only published versions can be promoted")

    resolver = resolver or resolver_from_session(session)
    manifest = parse_manifest(version.manifest)
    if executor is None:
        # ``build_executor(None)`` — the previous default — always returned the
        # deterministic fake, so a real provider in application configuration never
        # reached the normal deployment route and every route-driven gate graded a
        # scripted answer. Passing ``config`` and the manifest is what makes the
        # verdict about the model that will actually serve the alias, including its
        # workflow-scoped OpenAI runtime overrides.
        try:
            executor = build_executor(config, manifest=manifest)
        except RuntimeError as exc:
            # A misconfigured provider must not silently downgrade to the fake: that
            # would resurrect exactly the false-evidence defect. Surfaced as a
            # DeployError so the route answers 400 with the reason rather than 500.
            raise DeployError(
                f"cannot grade the deploy gate for {alias!r}: {exc}. Fix the provider "
                "configuration, or set CALIBER_LLM_PROVIDER=fake to grade "
                "deterministically (which production classes refuse by default)"
            ) from exc

    gate_result = evaluate_deploy_gates(
        session,
        manifest,
        alias,
        resolver=resolver,
        executor=executor,
        config=config,
    )
    if requires_quality_gate(alias, config) and not gate_result.has_gate:
        raise DeployError(
            f"promoting to {alias!r} ({environment_class(alias, config)}) requires a "
            "deploy gate; attach one whose required_for_aliases includes this alias"
        )
    # Checked only when a gate actually ran: with no gate there is no verdict to
    # misrepresent, and the requires_quality_gate branch above already owns that case.
    if (
        gate_result.has_gate
        and requires_graded_executor(alias, config)
        and bool((gate_result.executor or {}).get("deterministic"))
    ):
        raise DeployError(
            f"promoting to {alias!r} ({environment_class(alias, config)}) requires a "
            "deploy gate graded by a real configured model, but it was graded by the "
            f"deterministic {(gate_result.executor or {}).get('executor')}. Configure "
            "CALIBER_LLM_PROVIDER, or set "
            "CALIBER_RELEASE_REQUIRE_GRADED_EXECUTOR_FOR_ENVIRONMENT_CLASSES='' to "
            "accept deterministic release evidence"
        )
    if not gate_result.passed:
        raise DeployGateFailedError(
            f"deploy gate failed for alias {alias!r}", detail=gate_result.to_dict()
        )

    if requires_human_approval(alias, config):
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

    deployment = _rotate_alias(
        session, workflow_id, alias, version.version_id, actor=actor, config=config
    )
    return PromotionResult(gate_result=gate_result, rotated=True, deployment=deployment)


def approve_promotion(
    session: Session,
    promotion: CaliberWorkflowPromotion,
    *,
    actor: str,
    config: CaliberConfig | None = None,
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
        session,
        promotion.workflow_id,
        promotion.alias,
        promotion.version_id,
        actor=actor,
        config=config,
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
    config: CaliberConfig | None = None,
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
    # Preflight runs inside _rotate_alias: an approved refinement candidate used
    # to rotate the alias with no MCP dependency check at all.
    deployment = _rotate_alias(
        session, workflow_id, alias, version.version_id, actor=actor, config=config
    )
    return version, deployment


def rollback(
    session: Session,
    workflow_id: str,
    alias: str,
    *,
    actor: str,
    config: CaliberConfig | None = None,
) -> CaliberWorkflowDeployment:
    """Restore the alias to its previous target (plan §17.5).

    Runs the same MCP deployment preflight a forward promotion does. Rollback is
    still an alias rotation onto a live endpoint: if the older version's MCP
    dependencies have since been disabled, deleted, or reclassified, restoring it
    would put a broken or policy-violating graph into service. Preflight failure
    raises :class:`AliasPreflightError`; the alias and its checkpoint stack are
    left untouched, so the operator can fix the dependency and retry.
    """
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
    prior_version_id = str(prior["version_id"])
    require_alias_target_ready(session, alias, prior_version_id, config=config)
    deployment.version_id = prior_version_id
    deployment.rollback_checkpoint = checkpoint
    deployment.environment = environment_class(alias, config)
    deployment.deployed_by = actor
    deployment.deployed_at = datetime.now(timezone.utc)
    session.flush()
    return deployment


__all__ = [
    "GATED_ALIASES",
    "LIVE_ALIASES",
    "AliasPreflightError",
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
    "describe_executor",
    "evaluate_deploy_gates",
    "promote",
    "promote_workflow_candidate",
    "prune_workflow_runs",
    "publish_version",
    "record_workflow_run",
    "reject_promotion",
    "require_alias_target_ready",
    "requires_graded_executor",
    "requires_human_approval",
    "requires_quality_gate",
    "resolver_from_session",
    "rollback",
    "run_preview",
]
