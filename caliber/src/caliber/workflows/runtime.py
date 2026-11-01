"""Workflow runtime: execute the IR with node-level trace metadata (plan §11).

MVP execution runs *inside* the CALIBER process (plan §11.0). Rather than
``exec``-ing the generated module, the runtime walks the typed IR through a
graph interpreter and delegates each agent turn to a pluggable
:class:`WorkflowExecutor`:

* :class:`FakeWorkflowExecutor` — deterministic, no network; used by tests and
  by previews when the configured provider is ``fake``. It still invokes bound
  tool callables so tool-grounding behavior is observable.
* real provider executors (constructed lazily) — OpenAI Chat Completions,
  OpenAI Responses API, the OpenAI Agents SDK, or Anthropic Messages,
  depending on configuration.

Every run is wrapped in :func:`run_with_caliber_context`, which sets the
required ``caliber.*`` run tags (plan §11.1) on the active MLflow run when one
exists and exposes the resolved model via :func:`workflow_model` to generated
code. The interpreter supports the MVP shapes: Start → Agent → [Guardrail] →
Output, bounded multi-hop handoffs, and deterministic routers.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import contextvars
import hashlib
import importlib
import inspect
import json
import logging
import re
import textwrap
import time
from collections.abc import Callable, Collection, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from functools import partial
from http import HTTPStatus
from pathlib import Path
from threading import Lock
from typing import Any, NoReturn, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from caliber.auth import CaliberIdentity
from caliber.budget import step_reservation as _step_reservation
from caliber.config import provider_request_timeout
from caliber.context.authorize import ContextDenied
from caliber.effects import step_proposal as _step_proposal
from caliber.effects.connectors import mcp_tool as _mcp_tool
from caliber.effects.connectors import openapi_http as _openapi_http
from caliber.effects.dispatcher import (
    HTTP_CONNECTOR,
    HTTP_VERSION,
    EffectRefused,
    EffectRequest,
    UnsupportedEffectPath,
    UnsupportedInReferenceProfile,
    admit_backend,
    dispatch,
    submit_proposal,
)
from caliber.effects.mcp_tools import McpToolRoute
from caliber.effects.model_tool_calls import (
    GOVERNED_CONTEXT_REQUIRED,
    MODEL_TOOL_ROUTE_REFUSED,
    PROTECTED_TOOL_REFUSED,
    REGISTRY_ROW_MISMATCH,
    REGISTRY_ROW_UNREADABLE,
    TOOL_INPUT_INVALID,
    TOOL_NOT_PROTECTED,
    TOOL_REPLACED,
    UNSUPPORTED_EFFECT_PATH,
    ModelToolCallRefused,
    ToolProposal,
    ToolProposalBinder,
    refusal_marker,
)
from caliber.effects.reconcile import UnknownEffectError
from caliber.effects.records import RecordedProposal
from caliber.effects.refusals import (
    refuse_in_reference_profile,
    refuse_unshipped_tool_in_reference_profile,
)
from caliber.effects.registered_tools import (
    INLINE,
    OPENAPI_BACKEND,
    REFUSE,
    REGISTERED_TOOL_CONNECTOR,
    REGISTERED_TOOL_VERSION,
    RegisteredToolIdentity,
    ToolRoute,
    check_tool_arguments,
    route_openapi_tool,
    route_registered_tool,
    tool_intent_args,
    tool_resource,
)
from caliber.integrations.openapi.executor import (
    OpenApiPreflightError,
    bind_legacy_openapi_http_tool,
    bind_openapi_http_tool,
    openapi_operation_method,
)
from caliber.llm.models import (
    DEFAULT_OPENAI_REASONING_EFFORT,
    is_reasoning_model,
    reasoning_effort_for_model,
)
from caliber.mcp_gateway import McpGatewayError, invoke_tool_by_server_id_sync, load_server_config
from caliber.observability.mlflow_tracing import Tracer, get_tracer, model_cost_usd
from caliber.observability.span_attributes import causal_attributes, without_reserved_keys
from caliber.ownership import Lease
from caliber.tool_sandbox.models import ToolSandboxRunRequest
from caliber.tool_sandbox.service import (
    sandbox_from_optional_config,
)
from caliber.workflows import agent_step as _agent_step
from caliber.workflows.approval_policy import approval_valid_at_commit
from caliber.workflows.code_policy import (
    REFUSAL_CODE,
    CodePolicy,
    ir_refusals,
    require_sandboxed_tool_binding,
    summarize,
)
from caliber.workflows.guardrails import (
    GuardrailBlockedError,
    GuardrailContext,
)
from caliber.workflows.ir import (
    IRAgent,
    IRApiRequest,
    IRDataTransform,
    IREdge,
    IRErrorBoundary,
    IRExternalApp,
    IRFileInput,
    IRFolderInput,
    IRForEach,
    IRGuardrail,
    IRHumanApproval,
    IRInputBucket,
    IRJoin,
    IRKnowledgeBuild,
    IRKnowledgeQuery,
    IRLoop,
    IRManagedFileReference,
    IRMcpResource,
    IRNode,
    IROutputBucket,
    IROutputFolder,
    IRParallel,
    IRPythonCode,
    IRReviewQueueEnqueue,
    IRRouter,
    IRSubworkflow,
    IRTemplate,
    IRTool,
    IRToolBinding,
    IRWaitForEvent,
    IRWaitUntil,
    IRWebhook,
    IRWorkflow,
    NodeType,
    PromptRef,
)
from caliber.workflows.sandbox import TokenBudget, make_preview_callable
from caliber.workflows.session_memory import HistoryMessage, WorkflowSessionMemoryStore
from caliber.workflows.tools import ToolResolver, bind_registered_tool

logger = logging.getLogger("caliber.workflows.runtime")
_PYTHON_NODE_CALLABLE = "run_python_node"
_TEMPLATE_VARIABLE_PATTERN = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")

# MLflow span_type for each non-agent node kind (agents/tools already emit their
# own AGENT/TOOL spans — see ``_run_agent_traced`` / ``_invoke_agent_tools`` — so
# they are intentionally absent here and never double-wrapped). Anything not in
# the table falls back to ``CHAIN``. These names broaden run-trace coverage so a
# trace shows the full graph (router / loop / parallel / guardrail / knowledge /
# output / subworkflow …), not just the agent + tool spans.
_NODE_SPAN_TYPES: dict[NodeType, str] = {
    NodeType.ROUTER: "ROUTER",
    NodeType.LOOP: "CHAIN",
    NodeType.FOR_EACH: "CHAIN",
    NodeType.PARALLEL: "CHAIN",
    NodeType.JOIN: "CHAIN",
    NodeType.GUARDRAIL: "GUARDRAIL",
    NodeType.KNOWLEDGE_QUERY: "RETRIEVER",
    NodeType.KNOWLEDGE_BUILD: "CHAIN",
    NodeType.SUBWORKFLOW: "CHAIN",
    NodeType.OUTPUT: "CHAIN",
    NodeType.TEMPLATE: "CHAIN",
    NodeType.DATA_TRANSFORM: "CHAIN",
    NodeType.PYTHON_CODE: "TOOL",
    NodeType.MCP_RESOURCE: "TOOL",
    NodeType.TOOL: "TOOL",
    NodeType.HUMAN_APPROVAL: "CHAIN",
    NodeType.WAIT_FOR_EVENT: "CHAIN",
    NodeType.WAIT_UNTIL: "CHAIN",
    NodeType.ERROR_BOUNDARY: "CHAIN",
    NodeType.EXTERNAL_APP: "TOOL",
    NodeType.FILE_INPUT: "PARSER",
    NodeType.FOLDER_INPUT: "PARSER",
    NodeType.INPUT_BUCKET: "PARSER",
    NodeType.OUTPUT_BUCKET: "CHAIN",
    NodeType.OUTPUT_FOLDER: "CHAIN",
}

# Ordinary Preview cannot isolate these dedicated capability nodes from the
# host filesystem, local subprocesses, configured storage, or outbound
# integrations.  Fail closed for the whole IR before the interpreter starts;
# checking every node (not only currently reachable nodes) avoids a graph edit
# silently turning a previously dormant capability live during the same run.
# A content-pinned managed file is the one exception: it is read through the
# run-scoped resolver supplied by the application, which verifies project
# ownership, row metadata, object version, byte length, and digest. Legacy host
# paths remain blocked.
# Knowledge queries and registered tools keep their existing preview policy:
# their runners/callables may still execute when explicitly configured as
# preview-safe.  Knowledge builds retain their existing preview-skip behavior.
_PREVIEW_UNISOLATED_NODE_TYPES: frozenset[NodeType] = frozenset(
    {
        NodeType.FILE_INPUT,
        NodeType.FOLDER_INPUT,
        NodeType.INPUT_BUCKET,
        NodeType.OUTPUT_BUCKET,
        NodeType.OUTPUT_FOLDER,
        NodeType.MCP_RESOURCE,
        NodeType.PYTHON_CODE,
        NodeType.EXTERNAL_APP,
        NodeType.WEBHOOK,
        NodeType.API_REQUEST,
    }
)


def _node_span_type(node: IRNode) -> str:
    """MLflow span_type for a non-agent node (defaults to ``CHAIN``)."""
    return _NODE_SPAN_TYPES.get(node.node_type, "CHAIN")


def _preview_unisolated_nodes(ir: IRWorkflow) -> list[IRNode]:
    """Return deterministic Preview blockers from the complete workflow IR."""
    return sorted(
        (
            node
            for node in ir.nodes.values()
            if node.node_type in _PREVIEW_UNISOLATED_NODE_TYPES
            and not (isinstance(node, IRFileInput) and node.file_ref is not None)
        ),
        key=lambda node: (node.node_id, node.node_type.value),
    )


# ---------------------------------------------------------------------------
# Run context + tags
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaliberRunContext:
    workflow_id: str
    workflow_version: str
    entry_node_id: str
    workflow_alias: str | None = None
    workflow_version_id: str | None = None
    compiler_version: str | None = None
    manifest_hash: str | None = None
    default_model_ref: str = "CALIBER_WORKFLOW_DEFAULT_MODEL"
    session_id: str | None = None
    preview: bool = False
    trace_group_tags: dict[str, str] = field(default_factory=dict)


_RUN_CONTEXT: contextvars.ContextVar[CaliberRunContext | None] = contextvars.ContextVar(
    "caliber_workflow_run_context", default=None
)
_STEP_ATTEMPT: contextvars.ContextVar[int] = contextvars.ContextVar(
    "caliber_workflow_step_attempt", default=1
)
_STEP_OCCURRENCE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "caliber_workflow_step_occurrence", default=None
)
_STEP_PROVENANCE: contextvars.ContextVar[tuple[dict[str, Any], ...]] = contextvars.ContextVar(
    "caliber_workflow_step_provenance", default=()
)
ManagedFileResolver = Callable[
    [IRManagedFileReference, str, int],
    tuple[str, dict[str, Any]],
]
_MANAGED_FILE_RESOLVER: contextvars.ContextVar[ManagedFileResolver | None] = contextvars.ContextVar(
    "caliber_managed_file_resolver", default=None
)


def current_run_context() -> CaliberRunContext | None:
    return _RUN_CONTEXT.get()


@contextlib.contextmanager
def _bind_managed_file_resolver(
    resolver: ManagedFileResolver | None,
) -> Iterator[None]:
    token = _MANAGED_FILE_RESOLVER.set(resolver)
    try:
        yield
    finally:
        _MANAGED_FILE_RESOLVER.reset(token)


def run_tags(
    ctx: CaliberRunContext,
    *,
    node_id: str | None = None,
    proposal_id: str | None = None,
    decision_id: str | None = None,
    approval_id: str | None = None,
    effect_id: str | None = None,
) -> dict[str, str]:
    """Build the ``caliber.*`` run/span tag dict for a run (plan §11.1).

    The four causal ids (B11.3a) are persisted-row identifiers the *caller* has read from the
    database; each is emitted as ``caliber.<name>`` only when given and only if it is an opaque
    identifier (:func:`caliber.observability.span_attributes.causal_attributes`).  The run's
    manifest-supplied ``trace_group_tags`` can never set those keys, so a workflow author cannot
    forge a causal link.
    """
    tags: dict[str, str] = {
        "caliber.workflow_id": ctx.workflow_id,
        "caliber.workflow_version": ctx.workflow_version,
        "caliber.entry_node_id": ctx.entry_node_id,
    }
    if ctx.workflow_version_id:
        tags["caliber.workflow_version_id"] = ctx.workflow_version_id
    if ctx.workflow_alias:
        tags["caliber.workflow_alias"] = ctx.workflow_alias
    if ctx.compiler_version:
        tags["caliber.compiler_version"] = ctx.compiler_version
    if ctx.manifest_hash:
        tags["caliber.manifest_hash"] = ctx.manifest_hash
    if ctx.session_id:
        tags["caliber.session_id"] = ctx.session_id
    if ctx.preview:
        tags["caliber.preview"] = "true"
    if node_id:
        tags["caliber.node_id"] = node_id
    tags.update(
        causal_attributes(
            proposal_id=proposal_id,
            decision_id=decision_id,
            approval_id=approval_id,
            effect_id=effect_id,
        )
    )
    if ctx.trace_group_tags:
        return {**without_reserved_keys(ctx.trace_group_tags), **tags}
    return tags


@contextlib.contextmanager
def run_with_caliber_context(
    *,
    workflow_id: str,
    workflow_version: str,
    entry_node_id: str,
    session_id: str | None = None,
    workflow_alias: str | None = None,
    workflow_version_id: str | None = None,
    compiler_version: str | None = None,
    manifest_hash: str | None = None,
    default_model_ref: str = "CALIBER_WORKFLOW_DEFAULT_MODEL",
    preview: bool = False,
    extra_tags: dict[str, str] | None = None,
) -> Iterator[CaliberRunContext]:
    """Bind a :class:`CaliberRunContext` and set MLflow run tags best-effort.

    Tagging is best-effort: if MLflow isn't installed or no run is active, the
    contextvar is still set so :func:`workflow_model` and the interpreter work.
    """
    ctx = CaliberRunContext(
        workflow_id=workflow_id,
        workflow_version=workflow_version,
        entry_node_id=entry_node_id,
        workflow_alias=workflow_alias,
        workflow_version_id=workflow_version_id,
        compiler_version=compiler_version,
        manifest_hash=manifest_hash,
        default_model_ref=default_model_ref,
        session_id=session_id,
        preview=preview,
        trace_group_tags=dict(extra_tags or {}),
    )
    token = _RUN_CONTEXT.set(ctx)
    _set_mlflow_tags(run_tags(ctx))
    try:
        yield ctx
    finally:
        _RUN_CONTEXT.reset(token)


def _set_mlflow_tags(tags: dict[str, str]) -> None:
    try:
        import mlflow  # noqa: PLC0415

        if mlflow.active_run() is not None:
            mlflow.set_tags(tags)
    except Exception as exc:
        # Tagging is best-effort: never fail a run because MLflow is absent.
        logger.debug("skipping caliber run tags: %s", exc)


def _manual_resume_override_requested(inputs: dict[str, Any]) -> bool:
    for key in ("resume_event", "event_payload", "event"):
        value = inputs.get(key)
        if isinstance(value, dict) and value.get("manual_resume") is True:
            return True
    return False


def workflow_model(node_id: str, *, overrides: dict[str, str] | None = None) -> str:
    """Resolve the model for a node (plan §1.1 runtime-pinned model resolution).

    Order: per-node override → the active run context's ``default_model_ref`` →
    a process-wide fallback. ``"inherit"`` always resolves to the default.
    """
    if overrides and node_id in overrides:
        return overrides[node_id]
    ctx = current_run_context()
    if ctx is not None:
        return ctx.default_model_ref
    return "CALIBER_WORKFLOW_DEFAULT_MODEL"


# ---------------------------------------------------------------------------
# Executor protocol + results
# ---------------------------------------------------------------------------


@dataclass
class AgentTurnResult:
    final_output: str
    structured_output: Any | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    handoff_target: str | None = None
    # True when the executor already evaluated and applied its own handoff
    # graph (for example the OpenAI Agents SDK path). The runtime must not
    # auto-follow its runtime-managed fallback handoff chain in that case.
    handoffs_resolved_in_executor: bool = False
    prompt_version: str | None = None
    tokens: int = 0
    # Prompt/completion split + model, when the executor can supply them, so the
    # agent span can attribute real cost (golden-path roadmap, Wave 1). 0/None
    # when unknown (e.g. the fake executor) — cost is then left to autolog.
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0
    cost_usd: float = 0.0
    model: str | None = None


ResolvedHandoffAgents = dict[str, tuple[IRAgent, dict[str, Callable[..., Any]]]]


#: The governed-step protocol (B08.1b); the Protocol itself lives in ``caliber.workflows.agent_step``.
AgentStepAdapter = _agent_step.AgentStepAdapter


class WorkflowExecutor(Protocol):
    """Runs a single agent turn given resolved tool callables."""

    def run_agent(
        self,
        agent: IRAgent,
        input_text: str,
        *,
        history: list[HistoryMessage] | None = None,
        handoff_agents: ResolvedHandoffAgents | None = None,
        tool_callables: dict[str, Callable[..., Any]],
        preview: bool,
    ) -> AgentTurnResult: ...


class FakeWorkflowExecutor:
    """Deterministic executor for tests/previews (no network).

    It invokes each of the agent's bound tools once (so tool-grounding is
    observable and the registry binding is exercised), composes a deterministic
    response, and follows the agent's first handoff when present.

    It is also the scripted :class:`~caliber.workflows.agent_step.AgentStepAdapter` (``run_step``, B08.1b): ``step_script``
    lists the outcomes it answers governed steps with (see :class:`~caliber.workflows.agent_step.StepScript`); without one
    every step completes with a deterministic estimated usage. It proposes and answers; it never runs a protected tool (a
    scripted step may name read tools, which run only through ``read_tools``).
    """

    def __init__(
        self,
        *,
        follow_handoffs: bool = True,
        skip_tools: bool = False,
        step_script: _agent_step.ScriptEntries | None = None,
    ) -> None:
        self.step_script = _agent_step.StepScript(step_script)
        self.step_requests: list[_agent_step.AgentStepRequest] = []
        self.follow_handoffs = follow_handoffs
        # When True the executor does NOT invoke the agent's tools — simulating
        # an un-grounded agent so guardrails like tool_required_before_claim
        # block (used by deploy-gate-fail and refinement-loop tests).
        self.skip_tools = skip_tools
        self.calls: list[str] = []
        self.history_calls: list[list[HistoryMessage]] = []

    def run_agent(
        self,
        agent: IRAgent,
        input_text: str,
        *,
        history: list[HistoryMessage] | None = None,
        handoff_agents: ResolvedHandoffAgents | None = None,
        tool_callables: dict[str, Callable[..., Any]],
        preview: bool,  # noqa: ARG002 - part of the WorkflowExecutor protocol
    ) -> AgentTurnResult:
        del handoff_agents
        self.calls.append(agent.node_id)
        self.history_calls.append(list(history or []))
        # Share the instrumented helper so the default (fake) run path also emits
        # per-tool TOOL spans; ``skip_tools`` simulates an un-grounded agent.
        tool_calls = (
            [] if self.skip_tools else _invoke_agent_tools(agent, input_text, tool_callables)
        )
        handoff_target = (
            agent.handoffs[0].target_node_id if self.follow_handoffs and agent.handoffs else None
        )
        prompt_version = None
        if agent.instructions and agent.instructions.kind == "mlflow_prompt":
            prompt_version = "resolved"
        structured_output = _fake_agent_structured_output(
            agent,
            input_text=input_text,
            tool_call_count=len(tool_calls),
        )
        if structured_output is not None:
            text = _structured_output_text(structured_output)
        else:
            text = f"[{agent.name}] processed: {input_text}"
            if tool_calls:
                text += f" (used {len(tool_calls)} tool(s))"
        return AgentTurnResult(
            final_output=text,
            structured_output=structured_output,
            tool_calls=tool_calls,
            handoff_target=handoff_target,
            prompt_version=prompt_version,
            tokens=len(input_text.split()) + 8,
        )

    def run_step(
        self, request: _agent_step.AgentStepRequest, step_input: _agent_step.AgentStepInput
    ) -> _agent_step.AgentStepResult:
        """One governed step, answered from :attr:`step_script`; no protected tool is called and no effect happens."""
        self.step_requests.append(request)
        return self.step_script.answer(request, step_input)


def _rejects_tools_with_reasoning(exc: Exception) -> bool:
    """True for the 400 a model returns when a chat-completions request carries both function
    tools and a reasoning effort ("Function tools with reasoning_effort are not supported for
    <model> in /v1/chat/completions")."""
    return (
        getattr(exc, "status_code", None) == HTTPStatus.BAD_REQUEST
        and "function tools with reasoning_effort" in str(exc).lower()
    )


def _is_reasoning_model(model: str) -> bool:
    """True for OpenAI reasoning models (o-series, gpt-5*) — shared detection.

    These reject a custom ``temperature`` (only the default is allowed) and return
    a 400 otherwise, so the executor must omit it for them.
    """
    return is_reasoning_model(model)


def _structured_output_definition(agent: IRAgent) -> dict[str, Any] | None:
    raw = agent.output_type
    if not isinstance(raw, dict) or not raw:
        return None
    if raw.get("type") == "json_object":
        return {"mode": "json_object"}
    if raw.get("type") == "json_schema" and isinstance(raw.get("json_schema"), dict):
        inner = dict(raw["json_schema"])
        schema = inner.get("schema")
        if not isinstance(schema, dict):
            schema = {
                key: value
                for key, value in inner.items()
                if key not in {"name", "strict", "description"}
            }
        if not isinstance(schema, dict) or not schema:
            return None
        return {
            "mode": "json_schema",
            "name": str(inner.get("name") or _structured_output_name(agent)),
            "strict": bool(inner.get("strict", True)),
            "schema": schema,
        }
    if isinstance(raw.get("schema"), dict):
        return {
            "mode": "json_schema",
            "name": str(raw.get("name") or _structured_output_name(agent)),
            "strict": bool(raw.get("strict", True)),
            "schema": dict(raw["schema"]),
        }
    return {
        "mode": "json_schema",
        "name": _structured_output_name(agent),
        "strict": True,
        "schema": dict(raw),
    }


def _structured_output_name(agent: IRAgent) -> str:
    seed = f"{agent.name or agent.node_id}_output"
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", seed).strip("_")
    return cleaned[:64] or "structured_output"


def _openai_response_format(agent: IRAgent) -> dict[str, Any] | None:
    definition = _structured_output_definition(agent)
    if definition is None:
        return None
    if definition.get("mode") == "json_object":
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {
            "name": str(definition["name"]),
            "strict": bool(definition.get("strict", True)),
            "schema": dict(definition["schema"]),
        },
    }


def _openai_text_format(agent: IRAgent) -> dict[str, Any] | None:
    definition = _structured_output_definition(agent)
    if definition is None:
        return None
    if definition.get("mode") == "json_object":
        return {"format": {"type": "json_object"}}
    return {
        "format": {
            "type": "json_schema",
            "strict": bool(definition.get("strict", True)),
            "schema": dict(definition["schema"]),
        }
    }


def _structured_output_prompt_suffix(agent: IRAgent) -> str:
    definition = _structured_output_definition(agent)
    if definition is None:
        return ""
    if definition.get("mode") == "json_object":
        return "Return only valid JSON."
    schema_text = json.dumps(definition["schema"], indent=2, ensure_ascii=False, default=str)
    return f"Return only valid JSON that matches this schema exactly.\n```json\n{schema_text}\n```"


def _parse_structured_output_text(text: str) -> Any | None:
    raw = (text or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _structured_output_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


def _sample_structured_output(  # noqa: PLR0911, PLR0912 - schema-shape sampler
    schema: dict[str, Any] | None,
    *,
    path: str,
    input_text: str,
    agent_name: str,
    tool_call_count: int,
) -> Any:
    if not isinstance(schema, dict):
        return {"input": input_text, "agent": agent_name, "tool_calls": tool_call_count}
    enum = schema.get("enum")
    if isinstance(enum, list) and enum:
        return enum[0]
    if "const" in schema:
        return schema["const"]
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if isinstance(branches, list) and branches:
            first = branches[0]
            if isinstance(first, dict):
                return _sample_structured_output(
                    first,
                    path=path,
                    input_text=input_text,
                    agent_name=agent_name,
                    tool_call_count=tool_call_count,
                )
    schema_type = schema.get("type")
    if isinstance(schema_type, list):
        non_null = next((item for item in schema_type if item != "null"), None)
        schema_type = non_null or schema_type[0]
    if schema_type == "object" or isinstance(schema.get("properties"), dict):
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties:
            return {"input": input_text, "agent": agent_name, "tool_calls": tool_call_count}
        return {
            str(name): _sample_structured_output(
                prop if isinstance(prop, dict) else None,
                path=f"{path}.{name}" if path else str(name),
                input_text=input_text,
                agent_name=agent_name,
                tool_call_count=tool_call_count,
            )
            for name, prop in properties.items()
        }
    if schema_type == "array":
        items = schema.get("items")
        return [
            _sample_structured_output(
                items if isinstance(items, dict) else None,
                path=f"{path}[]",
                input_text=input_text,
                agent_name=agent_name,
                tool_call_count=tool_call_count,
            )
        ]
    if schema_type == "integer":
        return tool_call_count
    if schema_type == "number":
        return float(tool_call_count)
    if schema_type == "boolean":
        return bool(tool_call_count)
    if schema_type == "null":
        return None
    lower_path = path.lower()
    if any(token in lower_path for token in ("message", "summary", "answer", "content", "text")):
        return f"[{agent_name}] processed: {input_text}"
    if "input" in lower_path or "query" in lower_path:
        return input_text
    if "agent" in lower_path:
        return agent_name
    return (path.rsplit(".", maxsplit=1)[-1] if path else "value").replace("_", " ")


def _fake_agent_structured_output(
    agent: IRAgent,
    *,
    input_text: str,
    tool_call_count: int,
) -> Any | None:
    definition = _structured_output_definition(agent)
    if definition is None or definition.get("mode") == "json_object":
        return None
    schema = definition.get("schema")
    if not isinstance(schema, dict):
        return None
    return _sample_structured_output(
        schema,
        path="root",
        input_text=input_text,
        agent_name=agent.name or agent.node_id,
        tool_call_count=tool_call_count,
    )


class OpenAIChatWorkflowExecutor:
    """Executor that runs agent turns through OpenAI chat completions.

    This is deliberately lazy-imported and config-gated so local development,
    tests, and demos keep using :class:`FakeWorkflowExecutor` unless the
    operator explicitly sets ``CALIBER_LLM_PROVIDER=openai`` and installs the
    ``[llm]`` extra. Tools are resolved by CALIBER and exposed to the model as
    function tools; the model chooses which to call (with arguments) and the loop
    executes them through the same registry bindings — a real agentic
    tool-calling loop (golden-path roadmap, Wave 4), bounded by
    ``MAX_AGENT_TOOL_ITERATIONS``.
    """

    def __init__(
        self,
        *,
        api_key: str,
        default_model: str,
        reasoning_effort: str = DEFAULT_OPENAI_REASONING_EFFORT,
        client: Any = None,
        base_url: str | None = None,
        parallel_tool_calls: bool = False,
        prompt_cache_enabled: bool = False,
        prompt_cache_retention: str | None = None,
        step_clock: Callable[[], Any] | None = None,
    ) -> None:
        if client is not None:
            self._client = client
        else:
            try:
                from openai import OpenAI  # noqa: PLC0415
            except ImportError as exc:
                raise RuntimeError(
                    "openai is not installed. Install with "
                    "`pip install caliber-suite[llm]` to enable real workflow LLM runs."
                ) from exc
            # ``base_url`` routes calls through an OpenAI-compatible endpoint (e.g.
            # the MLflow AI Gateway at .../gateway/mlflow/v1) instead of api.openai.com.
            # ``None`` (the default) preserves the direct-OpenAI behaviour exactly.
            self._client = OpenAI(
                api_key=api_key,
                base_url=base_url or None,
                # Without this the SDK default governs (openai ships a 600s read
                # timeout with 2 automatic retries), so one call could hold a
                # worker far past any CALIBER-side deadline.
                timeout=provider_request_timeout(),
            )
        self._default_model = default_model
        self._reasoning_effort = reasoning_effort
        self._parallel_tool_calls = parallel_tool_calls
        self._prompt_cache_enabled = prompt_cache_enabled
        self._prompt_cache_retention = prompt_cache_retention or None
        # The clock a step's deadline is read from (an aware datetime); ``None`` is the real UTC clock.
        self._step_clock = step_clock

    def run_step(
        self, request: _agent_step.AgentStepRequest, step_input: _agent_step.AgentStepInput
    ) -> _agent_step.AgentStepResult:
        """One governed step through chat completions (B08, #729); see :mod:`caliber.workflows.step_openai_chat`.

        The model's read-tool calls run only through ``step_input.read_tools``; a call to any other tool is returned as a
        proposed action (the model's unbound claim) and never run. The request's limits, the provider's usage and a
        provider failure (``adapter_timeout``, ``provider_error``, ``malformed_result``) are typed results; nothing the
        provider said is placed in a result or a log line. :meth:`run_agent` is unchanged.
        """
        from caliber.workflows import step_openai_chat  # noqa: PLC0415

        config = step_openai_chat.ChatStepConfig(
            client=self._client,
            default_model=self._default_model,
            reasoning_effort=self._reasoning_effort,
            parallel_tool_calls=self._parallel_tool_calls,
            prompt_cache_enabled=self._prompt_cache_enabled,
            prompt_cache_retention=self._prompt_cache_retention,
            clock=self._step_clock,
        )
        return step_openai_chat.run_openai_chat_step(config, request, step_input)

    def run_agent(  # noqa: PLR0915 - provider request/response loop with tool replay
        self,
        agent: IRAgent,
        input_text: str,
        *,
        history: list[HistoryMessage] | None = None,
        handoff_agents: ResolvedHandoffAgents | None = None,
        tool_callables: dict[str, Callable[..., Any]],
        preview: bool,  # noqa: ARG002 - part of the WorkflowExecutor protocol
    ) -> AgentTurnResult:
        del handoff_agents
        system_prompt = _agent_instruction_text(agent) or "You are a helpful workflow agent."
        model = agent.model if agent.model and agent.model != "inherit" else self._default_model
        specs = _openai_tool_specs(agent)
        bindings = _binding_by_name(agent)
        response_format = _openai_response_format(agent)
        prompt_cache_key = (
            _openai_prompt_cache_key(
                api_surface="chat",
                model=model,
                agent=agent,
                instructions=system_prompt,
                tools=specs,
                output_format=response_format,
            )
            if self._prompt_cache_enabled and _supports_openai_prompt_cache(model)
            else None
        )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            *(history or []),
            {"role": "user", "content": input_text},
        ]
        collected: list[dict[str, Any]] = []
        prompt_toks = completion_toks = total_toks = cached_prompt_toks = 0
        output = ""
        structured_output: Any | None = None

        # Some reasoning models (the default gpt-5.6-luna among them) take function tools OR a
        # reasoning effort on /v1/chat/completions, not both. Once the API says so, every later
        # request of this turn keeps the tools and sends effort "none".
        tools_need_no_reasoning = False

        def _create(use_tools: bool) -> Any:
            nonlocal tools_need_no_reasoning
            kwargs: dict[str, Any] = {"model": model, "messages": messages}
            # Reasoning models (o-series, gpt-5*) only accept the default temperature
            # — sending a custom value is a 400. Plain chat models keep the low temp
            # for determinism.
            if not _is_reasoning_model(model):
                kwargs["temperature"] = 0.2
            elif effort := reasoning_effort_for_model(model, self._reasoning_effort):
                kwargs["reasoning_effort"] = (
                    "none" if tools_need_no_reasoning and use_tools and specs else effort
                )
            if response_format is not None:
                kwargs["response_format"] = response_format
            if use_tools and specs:
                kwargs["tools"] = specs
                kwargs["tool_choice"] = "auto"
                if self._parallel_tool_calls:
                    kwargs["parallel_tool_calls"] = True
            if prompt_cache_key:
                kwargs["prompt_cache_key"] = prompt_cache_key
                if self._prompt_cache_retention:
                    kwargs["prompt_cache_retention"] = self._prompt_cache_retention
            try:
                return self._client.chat.completions.create(**kwargs)
            except Exception as exc:
                if not (
                    use_tools
                    and specs
                    and kwargs.get("reasoning_effort") not in (None, "none")
                    and _rejects_tools_with_reasoning(exc)
                ):
                    raise
                tools_need_no_reasoning = True
                kwargs["reasoning_effort"] = "none"
                return self._client.chat.completions.create(**kwargs)

        for _ in range(MAX_AGENT_TOOL_ITERATIONS):
            response = _create(use_tools=True)
            usage = getattr(response, "usage", None)
            prompt_toks += int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_toks += int(getattr(usage, "completion_tokens", 0) or 0)
            total_toks += int(getattr(usage, "total_tokens", 0) or 0)
            cached_prompt_toks += _usage_cached_prompt_tokens(usage)
            message = response.choices[0].message
            tool_calls = list(getattr(message, "tool_calls", None) or [])
            if not tool_calls:
                refusal = getattr(message, "refusal", None)
                parsed = getattr(message, "parsed", None)
                if parsed is not None:
                    structured_output = parsed
                    output = _structured_output_text(parsed)
                elif isinstance(refusal, str) and refusal.strip():
                    output = refusal.strip()
                else:
                    content = getattr(message, "content", None)
                    output = (
                        content
                        if isinstance(content, str)
                        else _structured_output_text(content or "")
                    )
                    if response_format is not None:
                        structured_output = _parse_structured_output_text(output)
                        if structured_output is not None:
                            output = _structured_output_text(structured_output)
                        elif output:
                            logger.warning(
                                "workflow agent %s returned non-JSON structured output",
                                agent.node_id,
                            )
                break
            messages.append(_openai_assistant_message(message, tool_calls))
            for tc in tool_calls:
                name = getattr(tc.function, "name", "")
                args = _parse_tool_arguments(getattr(tc.function, "arguments", ""))
                result, result_text = _execute_model_tool_call(
                    name=name,
                    arguments=args,
                    tool_callables=tool_callables,
                    bindings=bindings,
                    fallback_input=input_text,
                )
                collected.append({"tool": name, "result": result})
                messages.append(
                    {"role": "tool", "tool_call_id": getattr(tc, "id", ""), "content": result_text}
                )
        else:
            # Tool-iteration cap reached with calls still pending: force a final
            # text answer with tools disabled so the run always terminates.
            response = _create(use_tools=False)
            usage = getattr(response, "usage", None)
            prompt_toks += int(getattr(usage, "prompt_tokens", 0) or 0)
            completion_toks += int(getattr(usage, "completion_tokens", 0) or 0)
            total_toks += int(getattr(usage, "total_tokens", 0) or 0)
            cached_prompt_toks += _usage_cached_prompt_tokens(usage)
            output = response.choices[0].message.content or ""

        if total_toks <= 0:
            total_toks = len(input_text.split()) + max(1, len(output.split()))
        return AgentTurnResult(
            final_output=output,
            structured_output=structured_output,
            tool_calls=collected,
            prompt_version="openai",
            tokens=total_toks,
            prompt_tokens=prompt_toks,
            completion_tokens=completion_toks,
            cached_prompt_tokens=cached_prompt_toks,
            cost_usd=model_cost_usd(
                model,
                prompt_tokens=prompt_toks,
                completion_tokens=completion_toks,
                cached_prompt_tokens=cached_prompt_toks,
            ),
            model=model,
        )


class OpenAIResponsesWorkflowExecutor:
    """Executor that runs agent turns through OpenAI's Responses API.

    This keeps the same bounded CALIBER-owned tool loop as
    :class:`OpenAIChatWorkflowExecutor`, but uses ``client.responses.create`` with
    ``text.format`` structured outputs and ``function_call_output`` follow-ups.
    It is opt-in because many OpenAI-compatible gateways still only implement
    Chat Completions.
    """

    def __init__(
        self,
        *,
        api_key: str,
        default_model: str,
        reasoning_effort: str = DEFAULT_OPENAI_REASONING_EFFORT,
        client: Any = None,
        base_url: str | None = None,
        parallel_tool_calls: bool = False,
        prompt_cache_enabled: bool = False,
        prompt_cache_retention: str | None = None,
        step_clock: Callable[[], datetime] | None = None,
    ) -> None:
        if client is not None:
            self._client = client
        else:
            try:
                from openai import OpenAI  # noqa: PLC0415
            except ImportError as exc:
                raise RuntimeError(
                    "openai is not installed. Install with "
                    "`pip install caliber-suite[llm]` to enable real workflow LLM runs."
                ) from exc
            self._client = OpenAI(
                api_key=api_key,
                base_url=base_url or None,
                # Without this the SDK default governs (openai ships a 600s read
                # timeout with 2 automatic retries), so one call could hold a
                # worker far past any CALIBER-side deadline.
                timeout=provider_request_timeout(),
            )
        self._default_model = default_model
        self._reasoning_effort = reasoning_effort
        self._parallel_tool_calls = parallel_tool_calls
        self._prompt_cache_enabled = prompt_cache_enabled
        self._prompt_cache_retention = prompt_cache_retention or None
        self._step_clock = step_clock

    def run_step(
        self, request: _agent_step.AgentStepRequest, step_input: _agent_step.AgentStepInput
    ) -> _agent_step.AgentStepResult:
        """Run one bounded governed Responses step; :meth:`run_agent` remains unchanged."""
        from caliber.workflows.step_openai_responses import (  # noqa: PLC0415
            ResponsesStepConfig,
            run_openai_responses_step,
        )

        return run_openai_responses_step(
            ResponsesStepConfig(
                client=self._client,
                default_model=self._default_model,
                reasoning_effort=self._reasoning_effort,
                parallel_tool_calls=self._parallel_tool_calls,
                prompt_cache_enabled=self._prompt_cache_enabled,
                prompt_cache_retention=self._prompt_cache_retention,
                clock=self._step_clock,
            ),
            request,
            step_input,
        )

    def run_agent(  # noqa: PLR0915 - provider request/response loop with tool replay
        self,
        agent: IRAgent,
        input_text: str,
        *,
        history: list[HistoryMessage] | None = None,
        handoff_agents: ResolvedHandoffAgents | None = None,
        tool_callables: dict[str, Callable[..., Any]],
        preview: bool,  # noqa: ARG002 - part of the WorkflowExecutor protocol
    ) -> AgentTurnResult:
        del handoff_agents
        system_prompt = _agent_instruction_text(agent) or "You are a helpful workflow agent."
        model = agent.model if agent.model and agent.model != "inherit" else self._default_model
        specs = _openai_responses_tool_specs(agent)
        bindings = _binding_by_name(agent)
        text_format = _openai_text_format(agent)
        prompt_cache_key = (
            _openai_prompt_cache_key(
                api_surface="responses",
                model=model,
                agent=agent,
                instructions=system_prompt,
                tools=specs,
                output_format=text_format,
            )
            if self._prompt_cache_enabled and _supports_openai_prompt_cache(model)
            else None
        )
        pending_input: list[dict[str, Any]] = [
            *[
                {"role": str(item.get("role") or "user"), "content": item.get("content") or ""}
                for item in (history or [])
            ],
            {"role": "user", "content": input_text},
        ]
        previous_response_id: str | None = None
        collected: list[dict[str, Any]] = []
        prompt_toks = completion_toks = total_toks = cached_prompt_toks = 0
        output = ""
        structured_output: Any | None = None

        def _create(use_tools: bool) -> Any:
            kwargs: dict[str, Any] = {
                "model": model,
                "instructions": system_prompt,
                "input": pending_input,
            }
            if previous_response_id:
                kwargs["previous_response_id"] = previous_response_id
            if not _is_reasoning_model(model):
                kwargs["temperature"] = 0.2
            elif effort := reasoning_effort_for_model(model, self._reasoning_effort):
                kwargs["reasoning"] = {"effort": effort}
            if text_format is not None:
                kwargs["text"] = text_format
            if specs:
                kwargs["tools"] = specs
                kwargs["tool_choice"] = "auto" if use_tools else "none"
                if use_tools and self._parallel_tool_calls:
                    kwargs["parallel_tool_calls"] = True
            if prompt_cache_key:
                kwargs["prompt_cache_key"] = prompt_cache_key
                if self._prompt_cache_retention:
                    kwargs["prompt_cache_retention"] = self._prompt_cache_retention
            return self._client.responses.create(**kwargs)

        for _ in range(MAX_AGENT_TOOL_ITERATIONS):
            response = _create(use_tools=True)
            usage = getattr(response, "usage", None)
            prompt_toks += int(getattr(usage, "input_tokens", 0) or 0)
            completion_toks += int(getattr(usage, "output_tokens", 0) or 0)
            total_toks += int(getattr(usage, "total_tokens", 0) or 0)
            cached_prompt_toks += _usage_cached_prompt_tokens(usage)
            function_calls = _openai_response_function_calls(response)
            if not function_calls:
                output = _openai_response_output_text(response)
                if text_format is not None:
                    structured_output = _parse_structured_output_text(output)
                    if structured_output is not None:
                        output = _structured_output_text(structured_output)
                    elif output:
                        logger.warning(
                            "workflow agent %s returned non-JSON structured output via Responses API",
                            agent.node_id,
                        )
                break
            previous_response_id = str(getattr(response, "id", "") or "")
            pending_input = []
            for item in function_calls:
                name = str(_response_item_value(item, "name") or "")
                args = _parse_tool_arguments(_response_item_value(item, "arguments"))
                result, result_text = _execute_model_tool_call(
                    name=name,
                    arguments=args,
                    tool_callables=tool_callables,
                    bindings=bindings,
                    fallback_input=input_text,
                )
                collected.append({"tool": name, "result": result})
                pending_input.append(
                    {
                        "type": "function_call_output",
                        "call_id": str(_response_item_value(item, "call_id") or ""),
                        "output": result_text,
                    }
                )
        else:
            response = _create(use_tools=False)
            usage = getattr(response, "usage", None)
            prompt_toks += int(getattr(usage, "input_tokens", 0) or 0)
            completion_toks += int(getattr(usage, "output_tokens", 0) or 0)
            total_toks += int(getattr(usage, "total_tokens", 0) or 0)
            cached_prompt_toks += _usage_cached_prompt_tokens(usage)
            output = _openai_response_output_text(response)
            if text_format is not None:
                structured_output = _parse_structured_output_text(output)
                if structured_output is not None:
                    output = _structured_output_text(structured_output)
                elif output:
                    logger.warning(
                        "workflow agent %s returned non-JSON structured output via Responses API",
                        agent.node_id,
                    )

        if total_toks <= 0:
            total_toks = len(input_text.split()) + max(1, len(output.split()))
        return AgentTurnResult(
            final_output=output,
            structured_output=structured_output,
            tool_calls=collected,
            prompt_version="openai_responses",
            tokens=total_toks,
            prompt_tokens=prompt_toks,
            completion_tokens=completion_toks,
            cached_prompt_tokens=cached_prompt_toks,
            cost_usd=model_cost_usd(
                model,
                prompt_tokens=prompt_toks,
                completion_tokens=completion_toks,
                cached_prompt_tokens=cached_prompt_toks,
            ),
            model=model,
        )


class OpenAIAgentsWorkflowExecutor:
    """Executor that runs workflow agent turns through the OpenAI Agents SDK.

    This path uses ``agents.Agent`` + ``Runner.run_sync`` with first-class tool
    schemas and an OpenAI model provider that honors CALIBER's ``base_url``. It
    is opt-in because many OpenAI-compatible gateways still only support older
    chat-completions semantics. Structured outputs use the same JSON-schema
    prompt contract as the Anthropic executor so manifest-authored schemas stay
    backend-agnostic.
    """

    def __init__(
        self,
        *,
        api_key: str,
        default_model: str,
        reasoning_effort: str = DEFAULT_OPENAI_REASONING_EFFORT,
        base_url: str | None = None,
        parallel_tool_calls: bool = False,
        prompt_cache_enabled: bool = False,
        prompt_cache_retention: str | None = None,
        step_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._api_key = api_key
        self._default_model = default_model
        self._reasoning_effort = reasoning_effort
        self._base_url = base_url or None
        self._parallel_tool_calls = parallel_tool_calls
        self._prompt_cache_enabled = prompt_cache_enabled
        self._prompt_cache_retention = prompt_cache_retention or None
        self._step_clock = step_clock

    @staticmethod
    def _sdk() -> tuple[Any, Any, Any, Any, Any, Any, Any, Any, Any]:
        try:
            from agents import (  # noqa: PLC0415
                Agent,
                FunctionTool,
                ItemHelpers,
                MaxTurnsExceeded,
                ModelSettings,
                OpenAIProvider,
                RunConfig,
                Runner,
                handoff,
            )
        except ImportError as exc:
            raise RuntimeError(
                "openai-agents is not installed. Install with "
                "`pip install caliber-suite[llm]` to enable Agents SDK workflow runs."
            ) from exc
        return (
            Agent,
            FunctionTool,
            ItemHelpers,
            MaxTurnsExceeded,
            ModelSettings,
            OpenAIProvider,
            RunConfig,
            Runner,
            handoff,
        )

    def run_step(
        self, request: _agent_step.AgentStepRequest, step_input: _agent_step.AgentStepInput
    ) -> _agent_step.AgentStepResult:
        """Run one bounded governed Agents SDK step; governed execution is integrated.

        Read tools are the only callable tools and protected tools become unbound proposals.
        """
        from caliber.workflows.step_openai_agents import (  # noqa: PLC0415
            AgentsStepConfig,
            run_openai_agents_step,
        )

        return run_openai_agents_step(
            AgentsStepConfig(
                default_model=self._default_model,
                reasoning_effort=self._reasoning_effort,
                api_key=self._api_key,
                base_url=self._base_url,
                parallel_tool_calls=self._parallel_tool_calls,
                prompt_cache_enabled=self._prompt_cache_enabled,
                prompt_cache_retention=self._prompt_cache_retention,
                clock=self._step_clock,
                sdk_loader=self._sdk,
            ),
            request,
            step_input,
        )

    def run_agent(  # noqa: PLR0915 - SDK orchestration plus tool/result bridging
        self,
        agent: IRAgent,
        input_text: str,
        *,
        history: list[HistoryMessage] | None = None,
        handoff_agents: ResolvedHandoffAgents | None = None,
        tool_callables: dict[str, Callable[..., Any]],
        preview: bool,  # noqa: ARG002 - part of the WorkflowExecutor protocol
    ) -> AgentTurnResult:
        (
            sdk_agent_cls,
            function_tool_cls,
            item_helpers_cls,
            max_turns_exc_cls,
            model_settings_cls,
            openai_provider_cls,
            run_config_cls,
            runner_cls,
            handoff_helper,
        ) = self._sdk()
        collected: list[dict[str, Any]] = []
        runtime_agents: ResolvedHandoffAgents = {
            agent.node_id: (agent, dict(tool_callables)),
        }
        if handoff_agents:
            runtime_agents.update(
                {
                    node_id: (target_agent, dict(target_callables))
                    for node_id, (target_agent, target_callables) in handoff_agents.items()
                }
            )

        def _agent_prompt(agent_def: IRAgent) -> tuple[str, str]:
            base_system_prompt = (
                _agent_instruction_text(agent_def) or "You are a helpful workflow agent."
            )
            structured_suffix = _structured_output_prompt_suffix(agent_def)
            system_prompt = (
                f"{base_system_prompt}\n\n{structured_suffix}"
                if structured_suffix
                else base_system_prompt
            )
            model = (
                agent_def.model
                if agent_def.model and agent_def.model != "inherit"
                else self._default_model
            )
            return system_prompt, model

        async def _invoke_tool(
            *,
            name: str,
            raw_input: str,
            bindings: dict[str, IRToolBinding],
            agent_tool_callables: dict[str, Callable[..., Any]],
        ) -> str:
            arguments = _parse_tool_arguments(raw_input)

            def _call() -> tuple[Any, str]:
                return _execute_model_tool_call(
                    name=name,
                    arguments=arguments,
                    tool_callables=agent_tool_callables,
                    bindings=bindings,
                    fallback_input=input_text,
                )

            result, result_text = await asyncio.to_thread(_call)
            collected.append({"tool": name, "result": result})
            return result_text

        sdk_agents: dict[str, Any] = {}

        def _build_sdk_agent(agent_id: str) -> Any:
            cached = sdk_agents.get(agent_id)
            if cached is not None:
                return cached
            agent_def, agent_tool_callables = runtime_agents[agent_id]
            system_prompt, agent_model = _agent_prompt(agent_def)
            bindings = _binding_by_name(agent_def)
            tool_specs = _openai_responses_tool_specs(agent_def)
            prompt_cache_key = (
                _openai_prompt_cache_key(
                    api_surface="agents",
                    model=agent_model,
                    agent=agent_def,
                    instructions=system_prompt,
                    tools=tool_specs,
                    output_format=None,
                )
                if self._prompt_cache_enabled and _supports_openai_prompt_cache(agent_model)
                else None
            )
            tools = [
                function_tool_cls(
                    name=binding.local_name,
                    description=binding.registry_ref or binding.local_name,
                    params_json_schema=_tool_parameters(binding),
                    on_invoke_tool=(
                        lambda _ctx, raw_input, *, name=binding.local_name, bindings=bindings, agent_tool_callables=agent_tool_callables: (
                            _invoke_tool(
                                name=name,
                                raw_input=raw_input,
                                bindings=bindings,
                                agent_tool_callables=agent_tool_callables,
                            )
                        )
                    ),
                    strict_json_schema=True,
                    needs_approval=False,
                    timeout_seconds=binding.timeout_seconds,
                    timeout_behavior="error_as_result",
                )
                for binding in agent_def.tools
            ]
            model_settings_kwargs: dict[str, Any] = {"include_usage": True}
            if not _is_reasoning_model(agent_model):
                model_settings_kwargs["temperature"] = 0.2
            elif effort := reasoning_effort_for_model(agent_model, self._reasoning_effort):
                from openai.types.shared.reasoning import Reasoning  # noqa: PLC0415

                model_settings_kwargs["reasoning"] = Reasoning(effort=effort)
            extra_args: dict[str, Any] = {}
            if self._parallel_tool_calls and tools:
                extra_args["parallel_tool_calls"] = True
            if prompt_cache_key:
                extra_args["prompt_cache_key"] = prompt_cache_key
                if self._prompt_cache_retention:
                    model_settings_kwargs["prompt_cache_retention"] = self._prompt_cache_retention
            if extra_args:
                model_settings_kwargs["extra_args"] = extra_args
            sdk_agent = sdk_agent_cls(
                name=agent_def.node_id or agent_def.name or "caliber.workflow.agent",
                instructions=system_prompt,
                model=agent_model,
                tools=tools,
                handoffs=[],
                model_settings=model_settings_cls(**model_settings_kwargs),
                tool_use_behavior="run_llm_again",
            )
            sdk_agents[agent_id] = sdk_agent
            sdk_agent.handoffs = [
                handoff_helper(
                    _build_sdk_agent(handoff.target_node_id),
                    **(
                        {"tool_description_override": handoff.description}
                        if str(handoff.description or "").strip()
                        else {}
                    ),
                    **(
                        {"input_filter": workflow_handoff_input_filter(handoff.input_filter)}
                        if isinstance(handoff.input_filter, str) and handoff.input_filter.strip()
                        else {}
                    ),
                    **(
                        {"is_enabled": workflow_handoff_is_enabled(handoff.condition)}
                        if isinstance(handoff.condition, str) and handoff.condition.strip()
                        else {}
                    ),
                )
                for handoff in agent_def.handoffs
                if handoff.target_node_id in runtime_agents
            ]
            return sdk_agent

        _system_prompt, _model = _agent_prompt(agent)
        sdk_agent = _build_sdk_agent(agent.node_id)
        has_sdk_handoffs = bool(getattr(sdk_agent, "handoffs", None))
        provider = openai_provider_cls(
            api_key=self._api_key,
            base_url=self._base_url,
            use_responses=True,
        )
        run_config = run_config_cls(
            model_provider=provider,
            tracing_disabled=True,
            workflow_name="CALIBER workflow agent",
        )
        runner_input: str | list[dict[str, Any]]
        if history:
            runner_input = [
                *[
                    {
                        "role": str(item.get("role") or "user"),
                        "content": item.get("content") or "",
                    }
                    for item in history
                ],
                {"role": "user", "content": input_text},
            ]
        else:
            runner_input = input_text
        structured_output: Any | None = None
        output_agent = agent

        try:
            result = runner_cls.run_sync(
                sdk_agent,
                runner_input,
                max_turns=MAX_AGENT_TOOL_ITERATIONS + 1,
                run_config=run_config,
            )
            raw_responses = list(getattr(result, "raw_responses", None) or [])
            last_agent = getattr(result, "last_agent", None)
            output_agent = next(
                (
                    runtime_agents[agent_id][0]
                    for agent_id, built_agent in sdk_agents.items()
                    if built_agent is last_agent
                ),
                agent,
            )
            output = _openai_agents_output_text(
                result=getattr(result, "final_output", None),
                raw_responses=raw_responses,
                item_helpers=item_helpers_cls,
            )
        except max_turns_exc_cls as exc:
            raw_responses = list(
                getattr(getattr(exc, "run_data", None), "raw_responses", None) or []
            )
            output = (
                _openai_agents_output_text(
                    result=None,
                    raw_responses=raw_responses,
                    item_helpers=item_helpers_cls,
                )
                or "Tool loop exceeded the maximum agent turns."
            )
            logger.warning(
                "workflow agent %s exceeded the Agents SDK turn limit; returning latest text output",
                agent.node_id,
            )

        prompt_toks, completion_toks, total_toks, cached_prompt_toks = _openai_agents_usage(
            raw_responses
        )
        if _structured_output_definition(output_agent) is not None:
            structured_output = _parse_structured_output_text(output)
            if structured_output is not None:
                output = _structured_output_text(structured_output)
            elif output:
                logger.warning(
                    "workflow agent %s returned non-JSON structured output via OpenAI Agents SDK",
                    agent.node_id,
                )

        if total_toks <= 0:
            total_toks = len(input_text.split()) + max(1, len(output.split()))
        resolved_model = (
            output_agent.model
            if output_agent.model and output_agent.model != "inherit"
            else self._default_model
        )
        return AgentTurnResult(
            final_output=output,
            structured_output=structured_output,
            tool_calls=collected,
            handoffs_resolved_in_executor=has_sdk_handoffs,
            prompt_version="openai_agents",
            tokens=total_toks,
            prompt_tokens=prompt_toks,
            completion_tokens=completion_toks,
            cached_prompt_tokens=cached_prompt_toks,
            cost_usd=model_cost_usd(
                resolved_model,
                prompt_tokens=prompt_toks,
                completion_tokens=completion_toks,
                cached_prompt_tokens=cached_prompt_toks,
            ),
            model=resolved_model,
        )


class AnthropicChatWorkflowExecutor:
    """Executor that runs agent turns through the Anthropic Messages API.

    Mirrors :class:`OpenAIChatWorkflowExecutor` — a real, bounded agentic
    tool-calling loop using Anthropic ``tool_use`` blocks (golden-path roadmap,
    Wave 4). Lazy-imports ``anthropic`` and is config-gated via
    ``CALIBER_LLM_PROVIDER=anthropic``.
    """

    def __init__(
        self,
        *,
        api_key: str,
        default_model: str,
        max_tokens: int = 4096,
        client: Any = None,
        step_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._api_key = api_key
        self._default_model = default_model
        self._max_tokens = max_tokens
        self._client = client
        # Reads the time for a governed step's deadline (aware); ``None`` is UTC now. Only :meth:`run_step` uses it.
        self._step_clock = step_clock

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from anthropic import Anthropic  # type: ignore[import-not-found]  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "anthropic is not installed. Install the Anthropic SDK to enable "
                "`CALIBER_LLM_PROVIDER=anthropic` workflow runs."
            ) from exc
        self._client = Anthropic(api_key=self._api_key, timeout=provider_request_timeout())
        return self._client

    def run_step(
        self, request: _agent_step.AgentStepRequest, step_input: _agent_step.AgentStepInput
    ) -> _agent_step.AgentStepResult:
        """Run one governed agent step through the Anthropic Messages API (B08.anthropic, #727).

        The model's read-tool calls run from ``step_input.read_tools`` only; a call of any other tool is never run and
        becomes the step's one proposed action. The limits, usage, failure and refusal semantics are those of
        :func:`caliber.workflows.step_adapters.anthropic_chat.run_anthropic_step`; :meth:`run_agent` is unchanged.
        """
        from caliber.workflows.step_adapters.anthropic_chat import (  # noqa: PLC0415
            run_anthropic_step,
        )

        return run_anthropic_step(
            request,
            step_input,
            client_factory=self._get_client,
            default_model=self._default_model,
            max_tokens=self._max_tokens,
            clock=self._step_clock,
        )

    def run_agent(  # noqa: PLR0912, PLR0915 - bounded Anthropic tool loop
        self,
        agent: IRAgent,
        input_text: str,
        *,
        history: list[HistoryMessage] | None = None,
        handoff_agents: ResolvedHandoffAgents | None = None,
        tool_callables: dict[str, Callable[..., Any]],
        preview: bool,  # noqa: ARG002 - part of the WorkflowExecutor protocol
    ) -> AgentTurnResult:
        del handoff_agents
        client = self._get_client()
        base_system_prompt = _agent_instruction_text(agent) or "You are a helpful workflow agent."
        structured_suffix = _structured_output_prompt_suffix(agent)
        if structured_suffix:
            system_prompt = f"{base_system_prompt}\n\n{structured_suffix}"
        else:
            system_prompt = base_system_prompt
        model = agent.model if agent.model and agent.model != "inherit" else self._default_model
        specs = _anthropic_tool_specs(agent)
        bindings = _binding_by_name(agent)
        messages: list[dict[str, Any]] = [*(history or []), {"role": "user", "content": input_text}]
        collected: list[dict[str, Any]] = []
        in_toks = out_toks = 0
        output = ""
        structured_output: Any | None = None

        def _create(use_tools: bool) -> Any:
            kwargs: dict[str, Any] = {
                "model": model,
                "max_tokens": self._max_tokens,
                "system": system_prompt,
                "messages": messages,
            }
            if specs:
                # Keep tool definitions present even on the forced final answer:
                # Anthropic returns 400 if the message history contains
                # tool_use/tool_result blocks while ``tools`` is omitted.
                # ``tool_choice`` none forces a text reply with no new tool calls.
                kwargs["tools"] = specs
                kwargs["tool_choice"] = {"type": "auto"} if use_tools else {"type": "none"}
            return client.messages.create(**kwargs)

        for _ in range(MAX_AGENT_TOOL_ITERATIONS):
            response = _create(use_tools=True)
            usage = getattr(response, "usage", None)
            in_toks += int(getattr(usage, "input_tokens", 0) or 0)
            out_toks += int(getattr(usage, "output_tokens", 0) or 0)
            blocks = list(getattr(response, "content", None) or [])
            tool_uses = [b for b in blocks if getattr(b, "type", None) == "tool_use"]
            text = "".join(
                getattr(b, "text", "") for b in blocks if getattr(b, "type", None) == "text"
            )
            if not tool_uses:
                output = text or output
                if structured_suffix:
                    structured_output = _parse_structured_output_text(output)
                    if structured_output is not None:
                        output = _structured_output_text(structured_output)
                    elif output:
                        logger.warning(
                            "workflow agent %s returned non-JSON Anthropic structured output",
                            agent.node_id,
                        )
                break
            messages.append({"role": "assistant", "content": _anthropic_assistant_content(blocks)})
            results: list[dict[str, Any]] = []
            for tu in tool_uses:
                name = getattr(tu, "name", "")
                args = dict(getattr(tu, "input", None) or {})
                result, result_text = _execute_model_tool_call(
                    name=name,
                    arguments=args,
                    tool_callables=tool_callables,
                    bindings=bindings,
                    fallback_input=input_text,
                )
                collected.append({"tool": name, "result": result})
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": getattr(tu, "id", ""),
                        "content": result_text,
                    }
                )
            messages.append({"role": "user", "content": results})
        else:
            response = _create(use_tools=False)
            usage = getattr(response, "usage", None)
            in_toks += int(getattr(usage, "input_tokens", 0) or 0)
            out_toks += int(getattr(usage, "output_tokens", 0) or 0)
            blocks = list(getattr(response, "content", None) or [])
            output = (
                "".join(
                    getattr(b, "text", "") for b in blocks if getattr(b, "type", None) == "text"
                )
                or output
            )
            if structured_suffix:
                structured_output = _parse_structured_output_text(output)
                if structured_output is not None:
                    output = _structured_output_text(structured_output)
                elif output:
                    logger.warning(
                        "workflow agent %s returned non-JSON Anthropic structured output",
                        agent.node_id,
                    )

        total = in_toks + out_toks
        if total <= 0:
            total = len(input_text.split()) + max(1, len(output.split()))
        return AgentTurnResult(
            final_output=output,
            structured_output=structured_output,
            tool_calls=collected,
            prompt_version="anthropic",
            tokens=total,
            prompt_tokens=in_toks,
            completion_tokens=out_toks,
            cost_usd=model_cost_usd(
                model,
                prompt_tokens=in_toks,
                completion_tokens=out_toks,
            ),
            model=model,
        )


def _invoke_agent_tools(
    agent: IRAgent,
    input_text: str,
    tool_callables: dict[str, Callable[..., Any]],
) -> list[dict[str, Any]]:
    """Invoke each resolved tool once with the run input.

    The workflow interpreter owns routing and guardrails; executor-level tool
    invocation keeps parity between fake and OpenAI-backed runs and makes tool
    usage visible in node logs.
    """
    tracer = get_tracer()
    tool_calls: list[dict[str, Any]] = []
    for binding in agent.tools:
        # Gate approval-required tools in the fake/default path too (not only the
        # LLM loop): the fake executor is the default for manual runs, so an
        # unapproved write tool must not auto-execute. Record a gated marker
        # (preserving the call count) and skip execution.
        if getattr(binding, "requires_approval", False):
            tool_calls.append(
                {
                    "tool": binding.local_name,
                    "result": {
                        "_gated": True,
                        "tool": binding.local_name,
                        "reason": "tool requires approval; not executed in an autonomous run",
                    },
                }
            )
            continue
        fn = tool_callables.get(binding.local_name)
        if fn is None:
            continue
        # Per-tool TOOL span: name, (redacted) input/output, latency, errors —
        # covers registry tools and agent-bound MCP tools, which resolve into the
        # same ``tool_callables`` map (golden-path roadmap, Wave 1). No-op when
        # tracing is inert; tool errors propagate unchanged.
        try:
            with tracer.span(
                f"tool.{binding.local_name}",
                span_type="TOOL",
                attributes={
                    "caliber.tool": binding.local_name,
                    "caliber.tool.input": input_text,
                },
            ) as span:
                started = time.perf_counter()
                try:
                    result = fn(input_text)
                except TypeError:
                    result = fn()
                finally:
                    span.set_attribute(
                        "caliber.tool.latency_ms",
                        round((time.perf_counter() - started) * 1000, 3),
                    )
                span.set_attribute("caliber.tool.output", result)
        except ModelToolCallRefused as refusal:
            # A protected tool in a governed plan's loop is refused before it is entered (B04.6): the call is recorded as
            # refused, with a fixed code and nothing else, exactly as the provider loops record it.
            tool_calls.append(
                {
                    "tool": binding.local_name,
                    "result": refusal_marker(binding.local_name, refusal.code),
                }
            )
            continue
        tool_calls.append({"tool": binding.local_name, "result": result})
    return tool_calls


def _agent_skill_names(agent: IRAgent) -> list[str]:
    """Short, redaction-safe labels for the skill blocks composed into the agent.

    The IR stores only the *resolved skill content* (``skill_instructions``), not
    the original skill ids, so derive a stable per-skill label from each block's
    leading non-empty line (capped) — enough to show *which* skills were pulled
    in the trace without dumping full instruction bodies.
    """
    names: list[str] = []
    blocks = getattr(agent, "skill_instructions", None) or []
    for index, block in enumerate(blocks):
        if not block or not block.strip():
            continue
        first_line = next((line.strip() for line in block.splitlines() if line.strip()), "")
        label = first_line.lstrip("#").strip() or f"skill_{index + 1}"
        names.append(label[:80])
    return names


def _trace_agent_resolution(tracer: Tracer, agent: IRAgent) -> None:
    """Emit a short span recording which prompt/skills the agent resolved.

    Nested under the agent span, this surfaces the resolved prompt source
    (inline vs MLflow registry ref/alias) and the composed skill labels so the
    trace shows what instructions the agent actually ran with. No-op-safe and
    additive — resolution itself happens in the executor; this only records the
    refs, never mutating anything or changing control flow.
    """
    ref = getattr(agent, "instructions", None)
    attributes: dict[str, Any] = {"caliber.node_id": agent.node_id}
    if ref is not None:
        attributes["caliber.prompt.kind"] = ref.kind
        if ref.kind == "mlflow_prompt":
            if ref.registry_name:
                attributes["caliber.prompt.registry_name"] = ref.registry_name
            if ref.alias:
                attributes["caliber.prompt.alias"] = ref.alias
            if ref.mlflow_uri:
                attributes["caliber.prompt.ref"] = ref.mlflow_uri
    skill_names = _agent_skill_names(agent)
    if skill_names:
        attributes["caliber.skills"] = skill_names
        attributes["caliber.skill_count"] = len(skill_names)
    with tracer.span(
        f"resolve.{agent.node_id}",
        span_type="CHAIN",
        attributes=attributes,
    ):
        # The resolution is recorded entirely via the span attributes above; the
        # block is intentionally empty so the span captures only the refs (cheap,
        # no I/O) and the executor performs the actual prompt/skill composition.
        return


def _run_agent_traced(
    executor: WorkflowExecutor,
    agent: IRAgent,
    input_text: str,
    *,
    history: list[HistoryMessage] | None = None,
    handoff_agents: ResolvedHandoffAgents | None = None,
    tool_callables: dict[str, Callable[..., Any]],
    preview: bool,
) -> AgentTurnResult:
    """Run one agent turn inside an ``AGENT`` span (golden-path roadmap, Wave 1).

    Wraps every ``executor.run_agent`` call site so per-agent latency, tokens,
    and (when the executor supplies the split + model) cost are attributed in the
    trace; tool spans from ``_invoke_agent_tools`` nest beneath this span. The
    span is a no-op when tracing is inert and the executor's exceptions propagate
    unchanged.
    """
    tracer = get_tracer()
    with tracer.span(
        f"agent.{agent.node_id}",
        span_type="AGENT",
        attributes={
            "caliber.agent": agent.name,
            "caliber.node_id": agent.node_id,
            "caliber.model": agent.model or "inherit",
        },
    ) as span:
        _trace_agent_resolution(tracer, agent)
        run_agent = executor.run_agent
        try:
            signature = inspect.signature(run_agent)
        except (TypeError, ValueError):
            signature = None
        supports_history = False
        supports_handoff_agents = False
        if signature is not None:
            supports_var_kwargs = any(
                param.kind == inspect.Parameter.VAR_KEYWORD
                for param in signature.parameters.values()
            )
            supports_history = "history" in signature.parameters or supports_var_kwargs
            supports_handoff_agents = (
                "handoff_agents" in signature.parameters or supports_var_kwargs
            )
        kwargs: dict[str, Any] = {
            "tool_callables": tool_callables,
            "preview": preview,
        }
        if supports_history:
            kwargs["history"] = history
        if supports_handoff_agents and handoff_agents is not None:
            kwargs["handoff_agents"] = handoff_agents
        result = run_agent(agent, input_text, **kwargs)
        span.record_usage(
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            cached_prompt_tokens=result.cached_prompt_tokens,
            total_tokens=result.tokens,
            model=result.model,
        )
        if result.prompt_version:
            span.set_attribute("caliber.prompt_version", result.prompt_version)
        return result


# ---------------------------------------------------------------------------
# Real agentic tool-calling loop (Wave 4) — shared by the OpenAI / Anthropic
# executors. The model chooses tools + arguments; the loop executes them through
# CALIBER's resolved (preview-gated) callables and feeds results back, bounded.
# ---------------------------------------------------------------------------

MAX_AGENT_TOOL_ITERATIONS = 8
MAX_AGENT_HANDOFF_HOPS = 8

_OPENAI_PROMPT_CACHE_MODEL_PREFIXES = (
    "gpt-4o",
    "gpt-4.1",
    "gpt-4.5",
    "gpt-5",
    "o1",
    "o3",
    "o4",
)


def _usage_field(raw: Any, key: str) -> Any:
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw.get(key)
    return getattr(raw, key, None)


def _usage_cached_prompt_tokens(usage: Any) -> int:
    """Return cached prompt/input tokens from OpenAI usage metadata when present."""

    for detail_key in ("prompt_tokens_details", "input_tokens_details"):
        details = _usage_field(usage, detail_key)
        cached = _usage_field(details, "cached_tokens")
        if cached is not None:
            return int(cached or 0)
    direct = _usage_field(usage, "cached_input_tokens")
    if direct is not None:
        return int(direct or 0)
    return 0


def _supports_openai_prompt_cache(model: str) -> bool:
    needle = str(model or "").strip().lower()
    return any(needle.startswith(prefix) for prefix in _OPENAI_PROMPT_CACHE_MODEL_PREFIXES)


def _openai_prompt_cache_key(
    *,
    api_surface: str,
    model: str,
    agent: IRAgent,
    instructions: str,
    tools: list[dict[str, Any]],
    output_format: dict[str, Any] | None,
) -> str:
    """Derive a deterministic prompt-cache bucket from the request's static prefix."""

    digest = hashlib.sha256(
        _stable_json(
            {
                "api_surface": api_surface,
                "model": model,
                "node_id": agent.node_id,
                "instructions": instructions,
                "tools": tools,
                "output_format": output_format,
            }
        ).encode("utf-8")
    ).hexdigest()[:24]
    return f"caliber:{api_surface}:{digest}"


def _binding_by_name(agent: IRAgent) -> dict[str, IRToolBinding]:
    return {binding.local_name: binding for binding in agent.tools}


def _tool_parameters(binding: IRToolBinding) -> dict[str, Any]:
    """JSON-schema parameters for a tool, or a generic single-input schema."""
    schema = binding.input_schema
    if isinstance(schema, dict) and schema.get("type") == "object":
        return schema
    return {
        "type": "object",
        "properties": {"input": {"type": "string", "description": "Tool input."}},
        "required": [],
    }


def _openai_tool_specs(agent: IRAgent) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": binding.local_name,
                "description": binding.registry_ref or binding.local_name,
                "parameters": _tool_parameters(binding),
            },
        }
        for binding in agent.tools
    ]


def _openai_responses_tool_specs(agent: IRAgent) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": binding.local_name,
            "description": binding.registry_ref or binding.local_name,
            "parameters": _tool_parameters(binding),
        }
        for binding in agent.tools
    ]


def _anthropic_tool_specs(agent: IRAgent) -> list[dict[str, Any]]:
    return [
        {
            "name": binding.local_name,
            "description": binding.registry_ref or binding.local_name,
            "input_schema": _tool_parameters(binding),
        }
        for binding in agent.tools
    ]


def _openai_assistant_message(message: Any, tool_calls: list[Any]) -> dict[str, Any]:
    """Rebuild the assistant message (with tool_calls) to precede tool results."""
    return {
        "role": "assistant",
        "content": getattr(message, "content", None) or "",
        "tool_calls": [
            {
                "id": getattr(tc, "id", ""),
                "type": "function",
                "function": {
                    "name": getattr(tc.function, "name", ""),
                    "arguments": getattr(tc.function, "arguments", "") or "{}",
                },
            }
            for tc in tool_calls
        ],
    }


def _anthropic_assistant_content(blocks: list[Any]) -> list[dict[str, Any]]:
    """Rebuild assistant content blocks (text + tool_use) for the next turn."""
    content: list[dict[str, Any]] = []
    for block in blocks:
        btype = getattr(block, "type", None)
        if btype == "text":
            # The Anthropic API rejects assistant content containing a text block
            # whose ``text`` is empty/whitespace (400 invalid_request_error). Claude
            # can emit such a block alongside ``tool_use`` (omitted reasoning, leading
            # whitespace), so skip it — a tool_use-only assistant turn is valid.
            text = getattr(block, "text", "")
            if text and text.strip():
                content.append({"type": "text", "text": text})
        elif btype == "tool_use":
            content.append(
                {
                    "type": "tool_use",
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": dict(getattr(block, "input", None) or {}),
                }
            )
    return content


def _response_item_value(item: Any, key: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(key, default)
    return getattr(item, key, default)


def _openai_response_function_calls(response: Any) -> list[Any]:
    return [
        item
        for item in list(getattr(response, "output", None) or [])
        if _response_item_value(item, "type") == "function_call"
    ]


def _openai_response_output_text(response: Any) -> str:
    direct = getattr(response, "output_text", None)
    if isinstance(direct, str) and direct.strip():
        return direct
    parts: list[str] = []
    for item in list(getattr(response, "output", None) or []):
        if _response_item_value(item, "type") != "message":
            continue
        for content_item in list(_response_item_value(item, "content", []) or []):
            content_type = _response_item_value(content_item, "type")
            if content_type == "output_text":
                text = _response_item_value(content_item, "text", "")
                if isinstance(text, str):
                    parts.append(text)
            elif content_type == "refusal":
                refusal = _response_item_value(content_item, "refusal", "")
                if isinstance(refusal, str):
                    parts.append(refusal)
    return "".join(parts).strip()


def _openai_agents_output_text(
    *,
    result: Any,
    raw_responses: list[Any],
    item_helpers: Any,
) -> str:
    text = ""
    if isinstance(result, str) and result.strip():
        text = result.strip()
    elif result is not None:
        if hasattr(result, "model_dump"):
            text = _structured_output_text(result.model_dump())
        else:
            text = _structured_output_text(result)
    if text:
        return text
    for response in reversed(raw_responses):
        for item in reversed(list(getattr(response, "output", None) or [])):
            for extractor_name in ("extract_text", "extract_refusal", "extract_last_content"):
                with contextlib.suppress(Exception):
                    extracted = getattr(item_helpers, extractor_name)(item)
                    if isinstance(extracted, str) and extracted.strip():
                        return extracted.strip()
    return text


def _openai_agents_usage(raw_responses: list[Any]) -> tuple[int, int, int, int]:
    prompt_toks = completion_toks = total_toks = cached_prompt_toks = 0
    for response in raw_responses:
        usage = getattr(response, "usage", None)
        prompt_toks += int(getattr(usage, "input_tokens", getattr(usage, "prompt_tokens", 0)) or 0)
        completion_toks += int(
            getattr(usage, "output_tokens", getattr(usage, "completion_tokens", 0)) or 0
        )
        total_toks += int(getattr(usage, "total_tokens", 0) or 0)
        cached_prompt_toks += _usage_cached_prompt_tokens(usage)
    return prompt_toks, completion_toks, total_toks, cached_prompt_toks


def _parse_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_arguments_from_node_inputs(inputs: dict[str, Any]) -> dict[str, Any]:
    if "arguments" in inputs:
        raw = inputs.get("arguments")
        if isinstance(raw, dict):
            return dict(raw)
        parsed = _parse_tool_arguments(raw)
        if parsed:
            return parsed
        if raw is not None:
            return {"input": raw}
        return {}
    raw_input = inputs.get("input")
    if isinstance(raw_input, dict):
        return dict(raw_input)
    return _parse_tool_arguments(raw_input)


def _call_tool(fn: Callable[..., Any], arguments: dict[str, Any], *, fallback_input: str) -> Any:
    """Invoke a resolved tool with model arguments, choosing the call shape from
    the callable's signature and invoking it **exactly once**.

    Shapes, in order: keyword args (``input_schema`` properties → tool params), a
    single dict positional (MCP-style), the legacy single-string input, then
    no-arg. The shape is selected by ``inspect.signature(...).bind`` *without
    executing the tool*, so a ``TypeError`` raised inside the tool body is never
    mistaken for a binding failure and the tool is never re-invoked (which would
    repeat side effects). Exceptions from the tool body propagate to the caller
    (``_execute_model_tool_call`` converts them into an ``_error`` marker).
    """
    shapes: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    if arguments:
        shapes.append(((), dict(arguments)))  # fn(**arguments)
        shapes.append(((arguments,), {}))  # fn(arguments) — MCP-style dict positional
    shapes.append(((fallback_input,), {}))  # fn(fallback_input) — legacy single input
    shapes.append(((), {}))  # fn()

    # An out-of-process tool selects its own convention: the signature needed to choose
    # lives in the child, which is the point of the sandbox. Checked before introspection
    # rather than after it fails, so the out-of-process case is explicit here instead of
    # being a side effect of ``inspect.signature`` raising.
    selector = getattr(fn, "caliber_shape_selector", None)
    if callable(selector):
        return selector(shapes)

    try:
        sig: inspect.Signature | None = inspect.signature(fn)
    except (TypeError, ValueError):
        sig = None

    if sig is not None:
        for args, kwargs in shapes:
            try:
                sig.bind(*args, **kwargs)
            except TypeError:
                continue
            return fn(*args, **kwargs)
        # No shape bound (unusual): surface the natural error from the first shape.
        first_args, first_kwargs = shapes[0]
        return fn(*first_args, **first_kwargs)

    # Non-introspectable callable (builtin/partial). Best-effort across shapes,
    # but only swallow a *binding* TypeError to advance — the last shape's error
    # always propagates so a genuine body failure is never hidden.
    last = len(shapes) - 1
    for index, (args, kwargs) in enumerate(shapes):
        try:
            return fn(*args, **kwargs)
        except TypeError:
            if index == last:
                raise
    # Unreachable: ``shapes`` is non-empty and the final shape returns or raises.
    raise RuntimeError("tool callable accepted no supported argument shape")


def _tool_result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str)
    except TypeError:
        return str(result)


def _tool_node_result_text(binding: IRToolBinding, result: Any) -> str:
    if binding.binding_type == "mcp_tool" and isinstance(result, dict):
        return _mcp_result_text(result.get("result", result))
    if isinstance(result, dict):
        for key in ("text", "output", "message", "content", "answer"):
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return _structured_output_text(result)


def _execute_model_tool_call(
    *,
    name: str,
    arguments: dict[str, Any],
    tool_callables: dict[str, Callable[..., Any]],
    bindings: dict[str, IRToolBinding],
    fallback_input: str,
    error_detail: bool = True,
) -> tuple[Any, str]:
    """Execute one model-chosen tool call inside a TOOL span.

    A tool that raises is reported to the model as an ``_error`` marker: the exception's class and message by default (the
    ungoverned loops), the class alone with ``error_detail=False`` (a governed step: an exception's text is the tool's, not the
    model's, and is not handed to the provider).

    Side-effect gating for write/external tools in preview is already applied by
    ``_resolve_tool_callables`` (callables are preview-wrapped). ``requires_approval``
    tools are NOT executed in the autonomous loop — a gated marker is returned so
    the model can adapt rather than triggering an unapproved side effect.

    A call the platform refuses before the tool is entered (B04.6, #768: a protected tool in a governed plan's loop, see
    :func:`_model_call_check`) is reported as a ``_refused`` marker holding the tool's name and a fixed code, and **nothing
    else**: no exception text and no argument. This is the one choke point of every provider's loop, the Agents SDK's tool
    callback and the fake executor's helper (:func:`_invoke_agent_tools`), so none of them can answer differently.
    """
    binding = bindings.get(name)
    fn = tool_callables.get(name)
    if binding is None or fn is None:
        marker: dict[str, Any] = {"_error": f"unknown tool {name!r}"}
        return marker, _tool_result_text(marker)
    if binding.requires_approval:
        marker = {
            "_gated": True,
            "tool": name,
            "reason": "tool requires approval; not executed in an autonomous run",
        }
        return marker, _tool_result_text(marker)
    tracer = get_tracer()
    started = time.perf_counter()
    try:
        with tracer.span(
            f"tool.{name}",
            span_type="TOOL",
            attributes={"caliber.tool": name, "caliber.tool.input": arguments or fallback_input},
        ) as span:
            try:
                result = _call_tool(fn, arguments, fallback_input=fallback_input)
            finally:
                span.set_attribute(
                    "caliber.tool.latency_ms", round((time.perf_counter() - started) * 1000, 3)
                )
            span.set_attribute("caliber.tool.output", result)
        return result, _tool_result_text(result)
    except ModelToolCallRefused as refusal:
        marker = refusal_marker(name, refusal.code)
        return marker, _tool_result_text(marker)
    except Exception as exc:  # fail soft: a failing tool must not crash the whole run
        # The span already recorded failed status (the exception propagated through
        # it). Feed an error marker back so the model can adapt instead of aborting.
        detail = f"{type(exc).__name__}: {exc}"[:500] if error_detail else type(exc).__name__
        marker = {"_error": detail, "tool": name}
        return marker, _tool_result_text(marker)


def _base_instruction_text(agent: IRAgent) -> str:
    ref = agent.instructions
    if ref is None:
        return ""
    if ref.kind == "inline":
        return ref.inline_text or ""
    if ref.mlflow_uri:
        try:
            import mlflow  # noqa: PLC0415

            loaded = mlflow.genai.load_prompt(ref.mlflow_uri)
            template = getattr(loaded, "template", None)
            if isinstance(template, str) and template.strip():
                return template
        except Exception:
            logger.debug("could not load MLflow prompt %s for workflow run", ref.mlflow_uri)
        return f"Follow the MLflow prompt registered at {ref.mlflow_uri}."
    return ""


def _agent_instruction_text(agent: IRAgent) -> str:
    """Base instructions with any resolved skill content composed in.

    Skills append after the base prompt as labelled blocks — the standard
    "skill = reusable instruction module" semantic — so an agent's effective
    system prompt is ``<instructions>\\n\\n## Skill\\n<content>…``.
    """
    base = _base_instruction_text(agent)
    blocks = [b for b in agent.skill_instructions if b and b.strip()]
    if not blocks:
        return base
    composed = "\n\n".join(f"## Skill\n{b.strip()}" for b in blocks)
    return f"{base}\n\n{composed}" if base else composed


# ---------------------------------------------------------------------------
# Run plan + interpreter
# ---------------------------------------------------------------------------


#: Default ceiling on concurrent parallel-branch threads. Chosen to match the
#: bound the rest of the runtime already assumes: enough width for the branch
#: counts real graphs use, low enough that a hostile or careless manifest cannot
#: exhaust the process's threads. Overridable via
#: ``CALIBER_WORKFLOW_PARALLEL_BRANCH_MAX_WORKERS``.
DEFAULT_PARALLEL_BRANCH_MAX_WORKERS = 8

#: Head-room the node-level deadline allows before it fires, so a node kind that
#: enforces the same timeout internally reports its own, more specific error
#: first. Small enough that a hung node is still bounded promptly.
_NODE_DEADLINE_GRACE_SECONDS = 0.25


@dataclass(frozen=True)
class AgentContextInput:
    """One input an agent step is given, with where it came from (B07.3b, #710).

    ``origin_node_type`` is the type of the node whose output edge delivered the text (``start`` when it is the run's own
    input); ``None`` means the origin is unknown (an item a ``for_each`` / ``loop`` / ``error_boundary`` handed to an
    inline agent, a resumed value), which is recorded as data of unknown origin, never as the run's own input. For a
    knowledge query ``origin_result`` is the query's result, which names the versions it read.
    """

    port: str
    text: str
    origin_node_id: str | None = None
    origin_node_type: str | None = None
    origin_result: dict[str, Any] | None = None
    transform: dict[str, Any] | None = None
    authorization_decision_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgentContextAssembly:
    """Everything an agent step is about to be given, in order, before any of it reaches the provider."""

    node_id: str
    instructions: PromptRef | None
    #: The instruction text as resolved when this was assembled (a registry prompt is loaded, not just named).
    instruction_text: str
    skill_instructions: tuple[str, ...]
    history: tuple[HistoryMessage, ...]
    inputs: tuple[AgentContextInput, ...]
    occurrence_id: str | None = None
    attempt: int = 1
    step_request_id: str | None = None
    memory_history: tuple[HistoryMessage, ...] = ()
    memory_reference: str | None = None
    memory_authorization_decision_id: str | None = None
    tool_specs: tuple[dict[str, Any], ...] = ()
    structured_output_suffix: str = ""
    tool_results: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class EffectAuthorization:
    """The exact approval and owner lease a governed HTTP effect may consume."""

    approval_id: str
    approver_scopes: frozenset[str] | set[str]
    lease: Lease
    current_resource_version: str | object | None = None
    evidence_hash: str | None = None


@dataclass(frozen=True)
class GovernedEffectContext:
    """Runtime-owned inputs needed to cross the B04 dispatcher boundary.

    ``authorize`` is deliberately a producer-owned gate: it must return an
    exact approval binding for the recorded proposal, never a boolean.  The
    receipt verifier is also explicit because generic HTTP has no universal
    read-by-key contract.  Callers that cannot verify a response fail closed
    before transport.
    """

    session_factory: Callable[[], Any]
    workflow_run_id: str
    authorize: Callable[[RecordedProposal], EffectAuthorization | None]
    receipt_verifier: Callable[[EffectRequest, Mapping[str, Any]], Any]
    backend_config: Mapping[str, Any] = field(default_factory=dict)
    #: The backend kinds this context may dispatch through (``None``: every admitted backend, the original behavior). The
    #: producer names only the kinds it has an authoritative receipt verifier for; a node whose backend is not named is refused
    #: **before** its proposal is recorded (``no_authoritative_verifier``), so no approval is asked for, or spent on, an effect the
    #: platform could never confirm and whose ambiguous outcome would park the run with no way out (#1055).
    dispatchable_backends: frozenset[str] | None = None


#: ``ir -> the agent nodes that can reach an operator-mapped tool`` (:func:`caliber.effects.governed_context.mapped_agent_nodes`, with the
#: caller's configuration bound). A replay that builds its own plan passes one so its agents are refused like any other plan's (#1051).
AgentsRequiringGovernance = Callable[[IRWorkflow], frozenset[str]]


@dataclass
class RuntimePlan:
    """Everything the interpreter needs to execute a workflow version."""

    ir: IRWorkflow
    resolver: ToolResolver
    mcp_identity: CaliberIdentity | None = None
    workflow_version_id: str | None = None
    workflow_alias: str | None = None
    compiler_version: str | None = None
    # Optional nested-workflow executor hook used by Subworkflow nodes.
    subworkflow_runner: (
        Callable[[str, str, str, float, int, WorkflowExecutor, bool], dict[str, Any]] | None
    ) = None
    knowledge_query_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    knowledge_build_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    #: Called with each agent step's context before the provider sees any of it (B07.3b); raising refuses the step.
    context_recorder: Callable[[AgentContextAssembly], str | None] | None = None
    #: Authorizes a run-scoped session-memory reference before the memory store loads it.
    memory_read_authorizer: Callable[[str], str] | None = None
    #: Provenance is carried out-of-band so transformed values retain authority without changing their business shape.
    context_provenance: dict[tuple[str, str], tuple[dict[str, Any], ...]] = field(
        default_factory=dict
    )
    context_provenance_lock: Lock = field(default_factory=Lock, repr=False)
    #: Resolves an authorized knowledge version to the decision id recorded before retrieval.
    context_provenance_resolver: Callable[[str], str | None] | None = None
    #: Whether the run has a source guard capable of re-validating replayed knowledge reads.
    context_source_guard_present: bool = False
    review_queue_enqueuer: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    # Outbound HTTP sender for Webhook nodes. ``None`` -> the httpx-backed default
    # (``_default_webhook_sender``); tests inject a fake to avoid real network I/O.
    webhook_sender: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    session_memory_store: WorkflowSessionMemoryStore | None = None
    subworkflow_max_depth: int = 3
    subworkflow_depth: int = 0
    # Output cap for python_code-node sandboxes, sourced from
    # ``config.tool_sandbox_max_output_bytes`` by ``build_plan``. ``None`` means
    # "use the sandbox class default" (preview/eval replay paths that don't
    # thread a config). Set on real runs so large node outputs aren't silently
    # truncated at the 64 KB class default.
    max_output_bytes: int | None = None
    # The rest of the configured sandbox limits. Previously only ``max_output_bytes``
    # reached the runtime, so ``tool_sandbox_max_memory_bytes`` /
    # ``max_file_bytes`` / ``max_open_files`` were read by ``from_config`` alone —
    # whose only caller is the standalone sandbox app — and every in-process
    # construction silently used the class defaults instead of the operator's.
    sandbox_max_memory_bytes: int | None = None
    sandbox_max_file_bytes: int | None = None
    sandbox_max_open_files: int | None = None
    # The configuration object that selected the sandbox backend. Runtime plans used to
    # carry only scalar limits, which made exported ``python_code`` nodes fall back to the
    # local backend even when their caller supplied a hardened custom implementation.
    sandbox_config: Any | None = None
    # Bounded parallelism for a ForEach node whose target is an agent. 1 (default)
    # = sequential, byte-identical to pre-concurrency behavior. Set from
    # ``config.workflow_foreach_max_workers`` by ``build_plan``; other plan
    # builders (preview/eval/calibration) leave it at 1.
    foreach_max_workers: int = 1
    # Cap on concurrent *parallel-branch* threads. Manifests permit large graphs
    # and the pool used to be sized to the branch count, so a wide fan-out spawned
    # one thread per branch with no ceiling — a graph-authoring choice could
    # exhaust the process's threads. Sourced from
    # ``config.workflow_parallel_branch_max_workers`` by ``build_plan``; branches
    # beyond the cap queue and run as threads free up, so behaviour is unchanged
    # apart from the bound.
    parallel_branch_max_workers: int = DEFAULT_PARALLEL_BRANCH_MAX_WORKERS
    # At-most-once external effects across a run restart. ``None`` on preview,
    # evaluation, and deploy-gate paths — those refuse unisolated effect nodes
    # outright, so there is nothing to deduplicate. Set by the queued run worker,
    # which is the only path that can be restarted after a lease expiry.
    effect_ledger: Any | None = None
    #: B04.2 context for manifest-backed webhook/API nodes. A plan with a
    #: workflow version but no context refuses these nodes rather than falling
    #: back to the legacy raw sender, unless ``legacy_guarded_effects`` is set.
    governed_effects: GovernedEffectContext | None = None
    #: The non-reference (development) profile's compatibility mode (#1068). When ``True`` and no ``governed_effects`` is
    #: bound, a manifest-backed plan runs a protected webhook/API node through ``_perform_guarded_effect`` and a protected
    #: registered or OpenAPI tool inline, exactly as before the dispatcher routing, instead of refusing. It is **not** the
    #: governed path: no exact-intent approval, no execution-owner lease, no proposal and no receipt. ``build_plan`` sets it
    #: from the deployment profile (``config.profile != "reference"``); the default ``False`` keeps every other plan, and
    #: every reference-profile plan, fail-closed. It never overrides a bound context (a bound context is always used) and
    #: never relaxes a refusal that is not about a missing context (EP-055, a registry mismatch, an unsupported path).
    legacy_guarded_effects: bool = False
    #: The reference profile's refusal of executable code (B12.4a, #754), applied by :func:`execute` to the whole IR before
    #: the first node runs. ``build_plan`` sets it from the configuration; the stricter of this and the process-wide policy
    #: (``code_policy.bind_code_policy``, bound by ``create_app``) applies, so a plan built by hand in a reference process is
    #: refused too. ``None`` defers to the process-wide policy, which is inactive outside the reference profile.
    code_policy: CodePolicy | None = None
    #: B08.2 context for governed agent steps (``caliber.effects.step_proposal``). ``None`` (every plan the runtime builds
    #: today) runs agent nodes exactly as before. With one, an agent node runs through ``run_agent_step``: the adapter is
    #: handed only the tools the platform classified ``read``, a protected action it proposes becomes a recorded ``proposed``
    #: effect plus an approval request, and the node parks (``waiting_approval``) instead of executing it. A governed step has
    #: no output text channel in the v1 result contract, so one that completes without proposing is refused.
    governed_agent_steps: _step_proposal.GovernedStepContext | None = None
    #: The agent nodes of this plan that are bound to a tool the operator mapped to a governed backend (``build_plan`` and the replay
    #: builders fill it in the reference profile). A plan whose ``governed_agent_steps`` does not govern one of them (a sub-workflow,
    #: a synchronous run, a replay) refuses to run it, **and any agent whose handoffs can reach it, directly or through others**,
    #: outside a preview, rather than as a legacy agent (#1051).
    agents_requiring_governance: frozenset[str] = frozenset()
    #: Reads a registry tool's own row by ``(name, version)`` (B04.3, #741). The registry row, not the manifest binding, says
    #: whether a tool is CALIBER's own and how risky it is; ``build_plan`` binds this to the database. A plan with neither this
    #: reader nor a governed context is not governed: registered tools keep running as before.
    registered_tool_identity: Callable[[str, str], RegisteredToolIdentity | None] | None = None
    #: Called with the ids of a node and of everything upstream of it before the node runs, outside its retries (B05.5b,
    #: #742). Raises ``caliber.effects.reconcile.UnknownEffectError`` while an effect those nodes asked for has no final
    #: outcome, so no step that depends on an effect nobody can vouch for runs. ``None`` where nothing records effects.
    effect_gate: Callable[[Collection[str]], None] | None = None
    # ``package.module:callable`` entrypoints an ``external_app`` node may invoke,
    # from ``config.external_app_entrypoint_allowlist``. Empty means none: this node
    # imports and calls installed Python in the control-plane process, and every
    # other execution surface already shipped an allowlist.
    external_app_entrypoint_allowlist: str = ""
    # Outbound egress policy for webhook/api_request nodes. ``None`` means "apply the
    # safe default" rather than "no policy": a preview/eval path that reaches an HTTP
    # node should still be constrained, and defaulting to unconstrained would make the
    # SSRF defence depend on which builder happened to construct the plan.
    egress_policy: Any | None = None


@dataclass
class NodeStep:
    node_id: str
    node_type: str
    status: str  # ok | blocked | skipped | error
    output: str = ""
    tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_prompt_tokens: int = 0
    cost_usd: float = 0.0
    model: str | None = None
    prompt_version: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    handoff_target: str | None = None
    detail: str = ""
    duration_ms: int = 0
    # Optional checkpoint metadata used when a node blocks and the runtime must
    # later resume it with the original gathered inputs or external event data.
    checkpoint_state: dict[str, Any] | None = None
    # Per-port snapshots persisted onto run events so the debugger can inspect
    # what the node actually received and emitted during this step.
    output_by_port: dict[str, Any] | None = None
    input_by_port: dict[str, Any] | None = None


@dataclass
class WorkflowRunResult:
    status: str  # completed | blocked | error
    output: str
    steps: list[NodeStep] = field(default_factory=list)
    tags: dict[str, str] = field(default_factory=dict)
    tokens: int = 0
    error: str | None = None
    guardrail_results: list[dict[str, Any]] = field(default_factory=list)
    # Named file artifacts produced by the run, ``{relative_path: text_content}``.
    # Collected from any node whose structured output carries an ``artifacts`` map
    # (e.g. a ``python_code`` node returning ``{"result": {"artifacts": {...}}}``).
    # The run route persists these to the run workspace; empty for runs that
    # don't emit any. Lets a single run produce multiple files (e.g. kg.json +
    # report.html) despite the output node flattening to a single string.
    artifacts: dict[str, str] = field(default_factory=dict)
    # MLflow linkage for the run (golden-path roadmap, Wave 1). Populated by
    # ``execute`` from the root trace; ``None`` when tracing is inert. Callers
    # persist these onto ``CaliberWorkflowRun.mlflow_run_id`` / for the UI link.
    mlflow_run_id: str | None = None
    mlflow_trace_id: str | None = None
    #: The structured refusal code when the run was refused before any node ran (B12.4a, #754: the reference profile's
    #: ``unsupported_in_reference_profile`` for executable code). ``None`` for every other outcome; ``error`` holds the text.
    error_code: str | None = None


@dataclass(frozen=True)
class RuntimeResumeCheckpoint:
    """Boundary checkpoint used to resume execution from a prior node output."""

    node_id: str
    checkpoint_kind: str | None = None
    output: str = ""
    output_by_port: dict[str, Any] | None = None
    input_by_port: dict[str, Any] | None = None
    injected_inputs: dict[str, Any] | None = None
    replay_output: bool = True


@dataclass
class _Connection:
    edge_id: str
    from_node: str
    to_node: str
    mappings: list[tuple[str, str]]  # (from_output, to_input)


def _connections(ir: IRWorkflow) -> list[_Connection]:
    grouped: dict[str, _Connection] = {}
    for edge in ir.edges:
        conn = grouped.get(edge.edge_id)
        if conn is None:
            conn = _Connection(edge.edge_id, edge.from_node, edge.to_node, [])
            grouped[edge.edge_id] = conn
        conn.mappings.append((edge.from_output, edge.to_input))
    # Stable order for determinism.
    return [grouped[k] for k in sorted(grouped)]


def _resume_checkpoint_kind_matches_node(
    checkpoint_kind: str | None,
    node: IRNode,
    *,
    governed_agent_steps: bool = False,
    governed_effects: bool = False,
) -> bool:
    if checkpoint_kind == "wait_for_event":
        return isinstance(node, IRWaitForEvent)
    if checkpoint_kind == "wait_until":
        return isinstance(node, IRWaitUntil)
    if checkpoint_kind == "human_approval":
        return isinstance(node, IRHumanApproval)
    if checkpoint_kind == "runtime_approval":
        # A gated tool node, a governed agent step parked behind the approval of its proposed action (B08.2) or a governed
        # workflow effect parked behind its exact approval (#1051): an agent node resumes from this kind only when the plan
        # governs it, and a webhook, API or tool node only in a plan that has a governed effect context.
        return (
            (isinstance(node, IRAgent) and governed_agent_steps)
            or (governed_effects and isinstance(node, IRWebhook | IRApiRequest | IRTool))
            or (
                isinstance(node, IRTool)
                and node.binding is not None
                and node.binding.requires_approval
            )
        )
    return True


def _validate_resume_checkpoint_for_node(
    resume_checkpoint: RuntimeResumeCheckpoint,
    node: IRNode,
) -> str | None:
    if isinstance(node, IRWaitForEvent) and not resume_checkpoint.replay_output:
        injected = dict(resume_checkpoint.injected_inputs or {})
        has_event_payload = any(
            key in injected and injected[key] not in (None, "", {})
            for key in ("event_payload", "resume_event", "event", node.event_name)
        )
        if not has_event_payload:
            return (
                "ToolExecutionError: "
                f"resume checkpoint for wait_for_event node {resume_checkpoint.node_id!r} "
                "requires a resumed event payload"
            )
        event_name = injected.get("event_name")
        if (
            isinstance(event_name, str)
            and event_name.strip()
            and event_name.strip() != node.event_name
        ):
            return (
                "ToolExecutionError: "
                f"resume checkpoint for wait_for_event node {resume_checkpoint.node_id!r} "
                f"expected event {node.event_name!r} but received {event_name.strip()!r}"
            )
    return None


class ToolExecutionError(Exception):
    """Raised when a registered tool fails after its retry budget (plan §18.1)."""


class ChildRefusedError(ToolExecutionError):
    """A subworkflow was refused by the reference profile's code policy (B12.4a, #754); the parent run keeps the code."""

    code = REFUSAL_CODE


def _refusal_code_of(exc: BaseException | None) -> str | None:
    """The reference profile's refusal code carried by ``exc`` or anything it was raised from, else ``None``."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if getattr(exc, "code", None) == REFUSAL_CODE:
            return REFUSAL_CODE
        exc = exc.__cause__ or exc.__context__
    return None


class ToolRouteRefused(ToolExecutionError):  # noqa: N818 - a refusal, named for what it says
    """A tool route refused a call before anything was proposed or run (B04.6, #768).

    ``code`` is a fixed token (one of :data:`caliber.effects.model_tool_calls.REFUSAL_CODES`, an OpenAPI preflight code or an
    MCP gate code); the message keeps the wording a workflow run's error has always had. A model-chosen call is refused with
    the code alone.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _resilient_callable(
    binding: IRToolBinding,
    fn: Callable[..., Any],
) -> Callable[..., Any]:
    """Wrap a tool callable with the binding's timeout + retry policy (ext B2).

    Retries up to ``max_retries`` additional attempts on exception/timeout.
    ``timeout_seconds`` is enforced by running the call in a worker thread and
    abandoning it if it overruns (the orphaned thread can't be force-killed in
    Python, but the workflow no longer blocks on it). Both are no-ops when unset.

    That last claim used to be false. The pool was entered with ``with``, whose
    ``__exit__`` calls ``shutdown(wait=True)`` and therefore *joins* the very
    thread the timeout just abandoned — so a tool declaring a 0.2s timeout and
    sleeping 3s returned control at 3s, not 0.2s. The deadline was observable in
    the raised error and nowhere in the wall clock, which is the failure mode
    that makes cancellation and SLO claims untrue. The pool is now shut down
    explicitly without waiting when the call overran, matching
    ``_run_external_app_entrypoint`` below.
    """
    timeout = binding.timeout_seconds
    attempts = max(1, binding.max_retries + 1)

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                if timeout is None:
                    return fn(*args, **kwargs)
                pool = ThreadPoolExecutor(max_workers=1)
                timed_out = False
                try:
                    return pool.submit(fn, *args, **kwargs).result(timeout=timeout)
                except FuturesTimeout:
                    timed_out = True
                    raise
                finally:
                    # ``wait=False`` on the timeout path is the whole point: the
                    # abandoned thread cannot be killed, but the workflow must not
                    # block on it. On the success path we still join normally.
                    pool.shutdown(wait=not timed_out, cancel_futures=timed_out)
            except FuturesTimeout:
                last_exc = ToolExecutionError(
                    f"tool {binding.local_name!r} timed out after {timeout}s"
                )
                logger.warning(
                    "tool %s timed out (attempt %d/%d)", binding.local_name, attempt + 1, attempts
                )
            except Exception as exc:  # retry any tool failure
                last_exc = exc
                logger.warning(
                    "tool %s failed (attempt %d/%d): %s",
                    binding.local_name,
                    attempt + 1,
                    attempts,
                    exc,
                )
        assert last_exc is not None
        raise ToolExecutionError(
            f"tool {binding.local_name!r} failed after {attempts} attempt(s): {last_exc}"
        ) from last_exc

    # ``_call_tool`` picks the call shape with ``inspect.signature(...).bind``. Without this,
    # the signature it sees is ``(*args, **kwargs)``, which binds every shape, so the first
    # (keyword arguments) is always chosen and a tool that takes one dict (an MCP-bound tool:
    # ``_invoke(arg)``) failed with "unexpected keyword argument" as soon as a tool node was
    # given structured arguments. ``__wrapped__`` makes ``inspect.signature`` see the real tool.
    # (``functools.wraps`` would also copy ``caliber_shape_selector`` and let a sandboxed tool
    # skip this wrapper's timeout and retries, so only the one attribute is set.)
    _wrapped.__wrapped__ = fn  # type: ignore[attr-defined]
    return _wrapped


def _model_call_arguments(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any]:
    """The arguments a model's call carries, in whichever shape :func:`_call_tool` chose; ``{}`` for a bare string or nothing."""
    if kwargs and not args:
        return dict(kwargs)
    if len(args) == 1 and not kwargs and isinstance(args[0], dict):
        return dict(args[0])
    return {}


def _model_call_check(
    plan: RuntimePlan, node_id: str, binding: IRToolBinding
) -> Callable[[dict[str, Any]], None] | None:
    """The per-call gate of one tool bound to an agent in an ungoverned loop, or ``None`` when the plan is not governed (B04.6, #768).

    A model chooses the tool and its arguments, so a call is classified **when it is made**, from the same routes a workflow
    tool node uses (:func:`_governed_tool_route`, :func:`_governed_openapi_route`, :func:`_governed_mcp_route`; the trusted
    registry row or MCP server record and the administrator's tool policy, never the binding, the model or the server's own
    claims). A ``read``-class call passes and runs inline. Any other outcome is refused with a typed code
    (:class:`~caliber.effects.model_tool_calls.ModelToolCallRefused`) **before the tool is touched**: a loop cannot propose,
    and it must not run what only an exact-intent approval may. A route that raises for any reason is a refusal too.

    ``None`` (no gate) for a plan that is not governed (no registry reader and no governed context: a standalone export or a
    caller-built plan) and for the development profile with no governed context (:func:`_legacy_effects_allowed`), whose
    model-chosen calls keep the behaviour they had before the dispatcher. Both are named, not hidden, in
    ``tests/test_effect_path_matrix.py::MODEL_TOOL_CALL_SITES``.
    """
    if plan.registered_tool_identity is None and plan.governed_effects is None:
        return None

    def check(arguments: dict[str, Any]) -> None:
        if _legacy_effects_allowed(plan):
            return
        try:
            routed: object
            if binding.binding_type == "mcp_tool":
                routed = _governed_mcp_route(
                    plan,
                    node_id=node_id,
                    server_id=binding.mcp_server_id or "",
                    tool_name=binding.mcp_tool_name or "",
                    timeout_seconds=binding.timeout_seconds or _mcp_tool.DEFAULT_TIMEOUT_SECONDS,
                    arguments=_mcp_arguments_from_input(arguments),
                    preview=False,
                )
            elif (
                binding.binding_type == "registered_function"
                and binding.execution_backend == OPENAPI_BACKEND
            ):
                routed = _governed_openapi_route(
                    plan,
                    binding,
                    node_id=node_id,
                    preview=False,
                    arguments=arguments,
                    overridden=False,
                )
            else:
                routed = _governed_tool_route(plan, binding, node_id=node_id, preview=False)
        except ToolRouteRefused as exc:
            code = PROTECTED_TOOL_REFUSED if exc.code == GOVERNED_CONTEXT_REQUIRED else exc.code
            raise ModelToolCallRefused(code) from None
        except Exception:
            raise ModelToolCallRefused(MODEL_TOOL_ROUTE_REFUSED) from None
        if routed is not None:
            # Protected, and a governed context is bound: only a proposal may carry it, and a loop cannot propose.
            raise ModelToolCallRefused(PROTECTED_TOOL_REFUSED)

    return check


def _gated_model_tool(
    fn: Callable[..., Any], check: Callable[[dict[str, Any]], None]
) -> Callable[..., Any]:
    """``fn`` behind ``check``: the call is classified, and refused or let through, before ``fn`` is entered."""

    def gated(*args: Any, **kwargs: Any) -> Any:
        check(_model_call_arguments(args, kwargs))
        return fn(*args, **kwargs)

    # ``inspect.signature`` (which :func:`_call_tool` uses to pick the call shape) must see the real tool.
    gated.__wrapped__ = fn  # type: ignore[attr-defined]
    selector = getattr(fn, "caliber_shape_selector", None)
    if callable(selector):

        def gated_selector(shapes: list[tuple[tuple[Any, ...], dict[str, Any]]]) -> Any:
            for shape_args, shape_kwargs in shapes:
                arguments = _model_call_arguments(shape_args, shape_kwargs)
                if arguments:
                    break
            else:
                arguments = {}
            check(arguments)
            return selector(shapes)

        gated.caliber_shape_selector = gated_selector  # type: ignore[attr-defined]
    return gated


def _gate_model_callables(
    plan: RuntimePlan,
    agent: IRAgent,
    callables: dict[str, Callable[..., Any]],
    *,
    preview: bool,
) -> dict[str, Callable[..., Any]]:
    """Put :func:`_model_call_check` in front of every callable an agent's loop can reach, injected tools included (B04.6, #768).

    Applied to the final map (after ``extra_tools`` replaced a bound tool), because the model's call reaches whichever
    callable the map holds under the tool's name, and the classification is the **binding's**, not the callable's. A preview
    never reaches a real effect (its callables are preview-wrapped), so it is left alone.
    """
    if preview:
        return callables
    bindings = {binding.local_name: binding for binding in agent.tools}
    gated: dict[str, Callable[..., Any]] = {}
    for name, fn in callables.items():
        binding = bindings.get(name)
        check = None if binding is None else _model_call_check(plan, agent.node_id, binding)
        gated[name] = fn if check is None else _gated_model_tool(fn, check)
    return gated


def _resolve_tool_callables(
    agent: IRAgent,
    resolver: ToolResolver,
    *,
    preview: bool,
    egress_policy: Any | None = None,
    mcp_identity: CaliberIdentity | None = None,
) -> dict[str, Callable[..., Any]]:
    callables: dict[str, Callable[..., Any]] = {}
    for binding in agent.tools:
        try:
            chosen = _resolve_bound_tool_callable(
                binding,
                resolver,
                preview=preview,
                required=False,
                egress_policy=egress_policy,
                mcp_identity=mcp_identity,
            )
        except UnsupportedEffectPath as exc:
            # A tool the reference profile does not support (B04.7, #774) must not fail the whole agent before the model has
            # asked for anything: the tool stays listed and every call of it is the refusal, as for any other refused tool.
            chosen = _refusing_callable(exc)
        if chosen is not None:
            callables[binding.local_name] = chosen
    return callables


def _refusing_callable(refusal: UnsupportedEffectPath) -> Callable[..., Any]:
    """A callable that raises ``refusal`` when it is called, and does nothing else (never retried)."""

    def _refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise refusal

    return _refuse


def _resolve_bound_tool_callable(
    binding: IRToolBinding,
    resolver: ToolResolver,
    *,
    preview: bool,
    required: bool,
    egress_policy: Any | None = None,
    mcp_identity: CaliberIdentity | None = None,
    legacy_openapi: bool = False,
) -> Callable[..., Any] | None:
    try:
        real = _bind(
            binding,
            resolver,
            egress_policy=egress_policy,
            mcp_identity=mcp_identity,
            legacy_openapi=legacy_openapi,
        )
    except UnsupportedEffectPath:
        if not preview:
            raise
        # A preview never reaches a real effect: a tool the reference profile does not support (B04.7, #774) is mocked there.
        real = None
    chosen = make_preview_callable(binding, real) if preview else real
    if chosen is None:
        if required:
            raise ToolExecutionError(
                f"tool {binding.local_name!r} could not be bound for execution"
            )
        return None
    return _resilient_callable(binding, chosen)


#: Operator sandbox limits for out-of-process registered tools. Bound at startup by
#: ``create_app``; ``None`` falls back to the class defaults, which are still bounded.
#: Process-global for the same reason the tool-module allowlist is: the binder is
#: reached from the runtime, generated compiler code, and route paths, and a limit only
#: some of them applied would not be a limit.
_ACTIVE_SANDBOX_CONFIG: Any | None = None


def bind_sandbox_config(config: Any | None) -> None:
    """Install the process-wide sandbox configuration for registered tools."""
    global _ACTIVE_SANDBOX_CONFIG  # noqa: PLW0603 - one process-wide policy by design
    _ACTIVE_SANDBOX_CONFIG = config


def _operator_config(plan: RuntimePlan) -> Any | None:
    """The operator configuration a node of ``plan`` runs under: the plan's own, else the process-wide one.

    It carries the deployment profile, which is what the reference profile's refusals (B04.7, #774) read.
    """
    return plan.sandbox_config if plan.sandbox_config is not None else _ACTIVE_SANDBOX_CONFIG


def _bind(
    binding: IRToolBinding,
    resolver: ToolResolver,
    *,
    egress_policy: Any | None = None,
    mcp_identity: CaliberIdentity | None = None,
    legacy_openapi: bool = False,
) -> Callable[..., Any] | None:
    if binding.binding_type == "mcp_tool":
        return _bind_mcp_tool(binding, identity=mcp_identity)
    if binding.execution_backend == "openapi_http":
        # ``egress_policy=None`` is not "unrestricted": the executor falls back to the
        # process-wide policy bound at startup, and to a safe default before that.
        # Development profile, no governed context (#1068): the pre-dispatcher call, NOT the governed path. Otherwise the
        # public entry, which sends a read and refuses a protected method (``unsupported_effect_path``).
        binder = bind_legacy_openapi_http_tool if legacy_openapi else bind_openapi_http_tool
        return binder(binding, egress_policy=egress_policy)
    override: Callable[..., Any] | None = None
    getter = getattr(resolver, "get_callable", None)
    if callable(getter):
        override = getter(binding.registry_ref)
    if override is not None:
        return override
    if binding.module_path == "<in-memory>":
        return None
    # C8: a registered module is imported and executed in a subprocess, never in the
    # control plane. This used to call ``bind_registered_tool`` unconditionally, which does
    # ``importlib.import_module`` *here* — so an admin-registered module shared the API
    # server's memory, descriptors, environment, and credentials, and its import ran in
    # this process too.
    if _registered_tool_sandbox_enabled():
        return _bind_sandboxed(binding)
    return _bind_in_process(binding)


def _bind_sandboxed(binding: IRToolBinding) -> Callable[..., Any] | None:
    """Bind a registered tool to a subprocess, subject to the operator allowlist.

    The allowlist still applies here. Routing execution into a subprocess narrows *where*
    a module runs; it does not change *which* modules an operator sanctioned, and an
    independent probe showed this path had dropped the check — with an allowlist of
    ``caliber.workflows.*``, ``os.getcwd`` still executed. Enforcing it keeps the two
    binder paths agreeing on admissibility, so containment and authorization do not trade
    off against each other.
    """
    from caliber.workflows.tools import registered_tool_module_allowed  # noqa: PLC0415

    if not registered_tool_module_allowed(binding.module_path):
        return None
    return _sandboxed_registered_tool(binding)


def _bind_in_process(binding: IRToolBinding) -> Callable[..., Any] | None:
    """The legacy in-process binder, reached only when the sandbox is disabled.

    ``bind_registered_tool`` enforces the same allowlist before importing. The reference profile never reaches the import:
    a tool imported into the control plane is unsandboxed code (B12.4a, #754).
    """
    require_sandboxed_tool_binding(binding.local_name)
    try:
        from caliber.workflows.tools import ToolRegistryEntry  # noqa: PLC0415

        entry = ToolRegistryEntry(
            name=binding.local_name,
            version="1.0",
            module_path=binding.module_path,
            callable_name=binding.callable_name,
        )
        return bind_registered_tool(entry, config=_ACTIVE_SANDBOX_CONFIG)
    except EffectRefused:
        raise
    except Exception:
        return None


def _standalone_config() -> Any:
    """Operator configuration for a generated export, **without touching globals**.

    A generated export runs standalone: nothing has called ``bind_sandbox_config`` or
    ``bind_module_allowlist``, so both process globals are ``None`` and the export would
    silently ignore every operator setting — allowlist, backend, timeout, and the disable
    switch alike. The generated module builds its agent graph on each ``run`` so explicit
    configuration can arrive first; when none is supplied, the environment must be read
    here or those standalone settings would still be lost.

    Returned rather than installed. An earlier version assigned ``_ACTIVE_SANDBOX_CONFIG``
    and called ``bind_module_allowlist``, which meant merely *binding a tool* mutated
    process-wide policy — and in a shared process (the test suite, an embedding host) that
    leaks into everything that runs afterwards. Process-global state set as a side effect
    of an unrelated call is a defect this codebase has already been bitten by more than
    once.

    When no process-wide configuration is bound, a default configuration object is loaded
    from the environment. Invalid operator configuration is a startup error: it must not
    silently erase an allowlist, backend, timeout, or disable decision.
    """
    if _ACTIVE_SANDBOX_CONFIG is not None:
        return _ACTIVE_SANDBOX_CONFIG
    try:
        import os  # noqa: PLC0415

        from caliber.config import CaliberConfig  # noqa: PLC0415

        return CaliberConfig.load(environ=dict(os.environ))
    except Exception as exc:
        from caliber.workflows.tools import ToolBindingError  # noqa: PLC0415

        raise ToolBindingError(
            "standalone export sandbox configuration is invalid; refusing to bind tools"
        ) from exc


def bind_exported_tool(entry: Any, *, config: Any | None = None) -> Callable[..., Any]:
    """Bind a registered tool for a **generated standalone export**.

    Compiled exports used to emit ``bind_registered_tool(...)`` directly, which imports and
    calls the module in whatever process runs the script. That is not the control-plane
    exposure C8 was about — an export runs in the developer's own process — but it made the
    export *behave differently from the platform*: sandboxed and allowlist-checked here,
    in-process and unchecked there. A workflow validated on CALIBER could then execute
    differently once exported, which is a fidelity bug even where it is not a privilege one.

    So an export takes the same decision the runtime takes, from the same configuration:
    subprocess by default, honouring the module allowlist, and in-process only where an
    operator has explicitly disabled the sandbox.

    ``config`` is accepted explicitly so generated ``run(config=...)`` calls select policy
    before the graph binds. With no explicit object, the standalone environment is loaded.
    Raises ``ToolBindingError`` rather than returning ``None`` so a refused module fails
    clearly while the run graph is constructed instead of yielding a null tool.
    """
    from caliber.workflows.tools import (  # noqa: PLC0415
        ToolBindingError,
        registered_tool_module_allowed,
    )

    config = config if config is not None else _standalone_config()
    if str(getattr(entry, "execution_backend", "") or "python_callable") == "openapi_http":
        binding = IRToolBinding(
            local_name=getattr(entry, "name", "") or "tool",
            registry_ref=getattr(entry, "registry_ref", "") or "",
            version_constraint="",
            requires_approval=False,
            side_effect_level=getattr(entry, "side_effect_level", "read"),
            allow_in_preview=bool(getattr(entry, "allow_in_preview", True)),
            module_path=getattr(entry, "module_path", "") or "<openapi_http>",
            callable_name=getattr(entry, "callable_name", "") or "invoke",
            execution_backend="openapi_http",
            backend_config=getattr(entry, "backend_config", None),
            input_schema=getattr(entry, "input_schema", None),
        )
        return bind_openapi_http_tool(binding)
    module_path = getattr(entry, "module_path", "") or ""
    if module_path == "<in-memory>":
        # No module to load in either mode. Delegated so the existing, specific
        # ToolBindingError is raised rather than a confusing import failure for a literal
        # ``<in-memory>`` module name inside the sandbox child.
        return bind_registered_tool(entry)
    # A generated export imports an administrator's module in whatever process runs it: not supported in the reference
    # profile (EP-016, B04.7, #774), whatever the sandbox and allowlist settings say; CALIBER's own shipped tools are.
    refuse_unshipped_tool_in_reference_profile(
        "EP-016",
        config,
        module_path=module_path,
        callable_name=getattr(entry, "callable_name", "") or "",
    )
    # This is an explicit config snapshot. Preserve an explicit empty allowlist as
    # unrestricted instead of converting it to ``None``, which tells the helper to
    # consult mutable process-global policy and can make a standalone export depend on
    # whichever CALIBER app happened to initialize first in the same interpreter.
    allowlist = str(getattr(config, "registered_tool_module_allowlist", "") or "")
    if not registered_tool_module_allowed(module_path, allowlist):
        raise ToolBindingError(
            f"tool module {module_path!r} is not in CALIBER_REGISTERED_TOOL_MODULE_ALLOWLIST"
        )
    if bool(getattr(config, "registered_tool_sandbox_enabled", True)):
        binding = IRToolBinding(
            local_name=getattr(entry, "name", "") or "tool",
            registry_ref=getattr(entry, "registry_ref", "") or "",
            version_constraint="",
            requires_approval=False,
            side_effect_level=getattr(entry, "side_effect_level", "read"),
            allow_in_preview=bool(getattr(entry, "allow_in_preview", True)),
            module_path=module_path,
            callable_name=getattr(entry, "callable_name", "") or "",
        )
        return _sandboxed_registered_tool(binding, config=config)
    require_sandboxed_tool_binding(
        getattr(entry, "name", "") or "tool", CodePolicy.from_config(config)
    )
    return bind_registered_tool(entry, module_allowlist=allowlist, config=config)


def _registered_tool_sandbox_enabled() -> bool:
    """Whether registered tools run out of process. Defaults to **on**.

    ``None`` config means on, not off: an unconfigured process must get the containment
    boundary rather than silently fall back to importing user modules into the control
    plane. A control that needs discovering before it applies is not a control.
    """
    return bool(getattr(_ACTIVE_SANDBOX_CONFIG, "registered_tool_sandbox_enabled", True))


def _sandboxed_registered_tool(
    binding: IRToolBinding, *, config: Any | None = None
) -> Callable[..., Any]:
    """Invoke a registered tool in a subprocess. **Wired — this closes C8.**

    Convention selection and error typing now live in the sandbox protocol, which is what
    previously blocked this. ``ToolSandboxRunRequest.shapes`` carries the candidate calling
    conventions and the child picks the first that binds, because the child is the only
    process holding the real function object — and the whole point of the sandbox is that
    the control plane never imports it. The child reports ``selected_shape`` so the choice
    is observable rather than inferred, and ``error_type`` so a tool's own ``TypeError`` is
    distinguishable from a binding failure without sniffing message prefixes.

    That last distinction is the load-bearing one. Selecting by trial-invocation in the
    parent would re-run a tool whose *body* raised ``TypeError``, repeating its side
    effects — a tool that charges a card must not be called twice because it raised.

    The control plane used to ``importlib.import_module`` an admin-registered module
    and call it inline, so a registered tool shared the API server's memory, file
    descriptors, environment, and credentials — and its *import* ran there too, which
    an allowlist narrows but does not contain.

    The module is now imported inside :class:`LocalSubprocessToolSandbox`: ``python -I``,
    an empty environment, a private working directory, POSIX CPU/address-space/file-size/
    descriptor limits, bounded output, a hard timeout, and process-group termination.

    Two honest limits, both unchanged by this:

    * the subprocess is a *process* boundary, not a container/VM/seccomp boundary, and
      the sandbox module says so at its own definition; and
    * arguments and the return value must survive JSON, so a registered tool taking or
      returning a live Python object no longer works. That is a real behaviour change
      and the reason this is worth stating rather than burying — a tool exchanging
      non-serializable values must move to an MCP server or an external app.
    """
    from caliber.tool_sandbox.models import ToolCallShape, ToolSandboxRunRequest  # noqa: PLC0415
    from caliber.tool_sandbox.service import sandbox_from_optional_config  # noqa: PLC0415

    # An administrator's module is not supported in the reference profile (EP-015); CALIBER's own shipped read-class tools are.
    # Refused here, when the tool is bound, so a protected call is refused before a proposal is recorded or an approval spent.
    refuse_unshipped_tool_in_reference_profile(
        "EP-015",
        config if config is not None else _ACTIVE_SANDBOX_CONFIG,
        module_path=binding.module_path,
        callable_name=binding.callable_name,
    )

    def _run(*, args: list[Any], kwargs: dict[str, Any], shapes: list[Any]) -> Any:
        # ``config`` lets a standalone export supply its own without mutating the
        # process-wide binding; inside the app it is simply the active one.
        effective = config if config is not None else _ACTIVE_SANDBOX_CONFIG
        sandbox = sandbox_from_optional_config(effective)
        result = sandbox.run_tool(
            ToolSandboxRunRequest(
                module_path=binding.module_path,
                callable_name=binding.callable_name,
                args=args,
                input=kwargs,
                shapes=shapes,
                # The registered-module budget, not the source-snippet one: this run pays
                # for a cold interpreter plus the module's imports before the tool body
                # starts. See the config field for the measured startup cost.
                timeout_seconds=float(
                    getattr(effective, "registered_tool_sandbox_timeout_seconds", 30.0)
                ),
            )
        )
        if result.status != "completed":
            error = result.error or result.status
            raise ToolExecutionError(
                f"registered tool {binding.local_name!r} failed in the sandbox: {error}"
            )
        return result.output

    def _invoke(*args: Any, **kwargs: Any) -> Any:
        # Direct call from the graph interpreter: the shape is whatever the caller used —
        # a single positional payload for some node types, keyword ports for others — so
        # there is nothing to select.
        return _run(args=list(args), kwargs=kwargs, shapes=[])

    def _invoke_with_shapes(shapes: list[tuple[tuple[Any, ...], dict[str, Any]]]) -> Any:
        """Hand the candidate conventions to the child and let it choose.

        Used by :func:`_call_tool`, which cannot introspect this callable — the signature
        it would need lives in another process by design.
        """
        return _run(
            args=[],
            kwargs={},
            shapes=[ToolCallShape(args=list(a), kwargs=dict(k)) for a, k in shapes],
        )

    # Advertised on the callable so ``_call_tool`` can detect the out-of-process case
    # explicitly, rather than inferring it from introspection failing.
    _invoke.caliber_shape_selector = _invoke_with_shapes  # type: ignore[attr-defined]

    _invoke.__name__ = binding.local_name or "registered_tool"
    # An earlier revision set ``__signature__`` to a non-Signature value here, to force
    # ``inspect.signature`` to raise and push ``_call_tool`` down its trial-invocation
    # branch. That worked by accident and was unsafe: the trial branch advances on
    # ``TypeError``, so a tool whose *body* raised ``TypeError`` was called again. The
    # explicit ``caliber_shape_selector`` above replaces the trick, and the signature is
    # left alone.
    return _invoke


def _bind_mcp_tool(
    binding: IRToolBinding,
    *,
    identity: CaliberIdentity | None = None,
) -> Callable[..., Any]:
    """Bind an MCP tool as a runtime-managed callable.

    The workflow runtime resolves the target MCP server by ``server_id`` and
    invokes the tool through the shared MCP gateway.
    """
    tool_name = binding.mcp_tool_name or ""
    server_id = binding.mcp_server_id or ""

    def _invoke(arg: Any = "") -> dict[str, Any]:
        arguments = _mcp_arguments_from_input(arg)
        try:
            invoke_kwargs: dict[str, Any] = {
                "server_id": server_id,
                "tool_name": tool_name,
                "arguments": arguments,
            }
            if identity is not None:
                invoke_kwargs["identity"] = identity
            result = invoke_tool_by_server_id_sync(**invoke_kwargs)
        except McpGatewayError as exc:
            raise ToolExecutionError(
                f"MCP tool {tool_name!r} invocation failed on server {server_id!r}: {exc}"
            ) from exc
        except EffectRefused as exc:
            # A protected MCP call reaches a server only through the dispatcher (B04.5); this inline callable never does.
            raise ToolExecutionError(
                f"MCP tool {tool_name!r} refused on server {server_id!r}: {exc.code}"
            ) from exc
        return {
            "server_id": server_id,
            "tool_name": tool_name,
            "arguments": arguments,
            "result": result,
        }

    return _invoke


def _mcp_arguments_from_input(value: Any) -> dict[str, Any]:
    """Normalize runtime input into MCP-style call arguments."""
    if isinstance(value, dict):
        return dict(value)
    if value is None:
        return {}
    if not isinstance(value, str):
        return {"query": str(value)}

    text = value.strip()
    if not text:
        return {}

    parsed: Any = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    return {"query": value}


def _mcp_result_text(result: Any) -> str:
    """Extract a readable text form from an MCP tool result payload."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("text", "output", "message"):
            value = result.get(key)
            if isinstance(value, str):
                return value
    return json.dumps(result, ensure_ascii=False, default=str)


def _python_node_source(code: str) -> str:
    """Normalize user-authored code into a runnable ``run_python_node`` callable."""
    raw = (code or "").rstrip()
    if not raw:
        raise ToolExecutionError("python_code node requires non-empty code")
    if re.search(r"^\s*def\s+run_python_node\s*\(", raw, flags=re.MULTILINE):
        return raw
    body = textwrap.indent(raw, "    ")
    return f"def run_python_node(input=None, context=None, inputs=None, run_input=''):\n{body}\n"


def _python_node_text(result: Any) -> str:
    """Human-readable text for python node output ports + step output."""
    if isinstance(result, str):
        return result
    if isinstance(result, dict):
        for key in ("text", "output", "message"):
            value = result.get(key)
            if isinstance(value, str):
                return value
    if result is None:
        return ""
    return json.dumps(result, ensure_ascii=False, default=str)


def _json_compatible(value: Any) -> Any:
    """Best-effort conversion to JSON-safe values for sandbox payloads."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_compatible(item) for item in value]
    return str(value)


def _node_output_snapshot(
    node: IRNode,
    port_values: dict[tuple[str, str], Any],
) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for port in node.outputs:
        value = port_values.get((node.node_id, port))
        if value is None and (node.node_id, port) not in port_values:
            continue
        snapshot[str(port)] = _json_compatible(value)
    return snapshot


def _attach_step_port_snapshots(
    step: NodeStep,
    *,
    node: IRNode,
    input_by_port: dict[str, Any],
    port_values: dict[tuple[str, str], Any],
) -> NodeStep:
    step.input_by_port = {
        str(port): _json_compatible(value) for port, value in input_by_port.items()
    }
    step.output_by_port = _node_output_snapshot(node, port_values)
    return step


def _default_webhook_sender(
    request: dict[str, Any], *, policy: Any | None = None
) -> dict[str, Any]:
    """Perform a real outbound HTTP request for a Webhook node.

    ``request`` carries ``url``/``method``/``headers``/``timeout_seconds``/``body``.
    A structured (dict/list) body is sent as JSON; anything else as a text body.
    Returns ``status_code``/``text``/``json``/``headers`` for the node to publish.
    Injected via ``RuntimePlan.webhook_sender`` in tests so no network is hit.

    ``policy`` is bound by the call site, so the client this opens enforces egress at
    connect time rather than trusting an earlier check — see ``caliber.egress`` on N4.
    """
    from caliber.egress import EgressBlockedError, build_client  # noqa: PLC0415

    method = str(request.get("method") or "POST").upper()
    url = str(request.get("url") or "")
    headers = {str(k): str(v) for k, v in (request.get("headers") or {}).items()}
    timeout = float(request.get("timeout_seconds") or 30.0)
    body = request.get("body")

    kwargs: dict[str, Any] = {"headers": headers}
    if method == "GET":
        if isinstance(body, dict):
            kwargs["params"] = body
    elif isinstance(body, (dict, list)):
        kwargs["json"] = body
    elif body is not None:
        kwargs["content"] = body if isinstance(body, (bytes, str)) else str(body)

    # The client enforces policy inside its transport and connects to the address it
    # vetted, so a name that re-resolves to the metadata endpoint cannot be reached
    # (N4). ``follow_redirects`` stays False by default — no longer the only defence,
    # since each hop would be re-checked, but still one fewer thing to reason about.
    # The pre-check in ``_check_egress`` already refused the obvious cases, so a block
    # raised *here* is the rebinding case specifically: the name passed the check and then
    # resolved somewhere else. Translated so the node reports a normal runtime failure
    # with the reason instead of an opaque RuntimeError.
    try:
        with build_client(policy=policy, timeout=timeout) as client:
            response = client.request(method, url, **kwargs)
    except EgressBlockedError as exc:
        raise ToolExecutionError(str(exc)) from exc

    parsed: Any = None
    try:
        parsed = response.json()
    except Exception:  # response simply wasn't JSON
        parsed = None
    return {
        "status_code": response.status_code,
        "text": response.text,
        "json": parsed,
        "headers": dict(response.headers),
    }


def _parse_curl(command: str) -> dict[str, Any]:
    """Parse a cURL command string into method/url/headers/body.

    Tokenizes with ``shlex`` (NO shell execution) and recognizes the common
    flags: ``-X/--request``, ``-H/--header``, ``-d/--data/--data-raw/
    --data-binary/--data-urlencode``, and ``--url``. The method defaults to GET,
    or POST when a data body is present and no explicit method was given.
    Raises ``ToolExecutionError`` when no URL can be found.
    """
    import shlex  # noqa: PLC0415

    try:
        tokens = shlex.split(command.strip())
    except ValueError as exc:
        raise ToolExecutionError(f"could not parse cURL command: {exc}") from exc
    if tokens and tokens[0] == "curl":
        tokens = tokens[1:]

    method: str | None = None
    url = ""
    headers: dict[str, str] = {}
    body: str | None = None
    data_flags = {"-d", "--data", "--data-raw", "--data-binary", "--data-ascii", "--data-urlencode"}

    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in {"-X", "--request"} and i + 1 < len(tokens):
            method = tokens[i + 1].upper()
            i += 2
            continue
        if tok in {"-H", "--header"} and i + 1 < len(tokens):
            raw = tokens[i + 1]
            if ":" in raw:
                key, _, value = raw.partition(":")
                headers[key.strip()] = value.strip()
            i += 2
            continue
        if tok in data_flags and i + 1 < len(tokens):
            body = tokens[i + 1] if body is None else f"{body}&{tokens[i + 1]}"
            i += 2
            continue
        if tok == "--url" and i + 1 < len(tokens):
            url = tokens[i + 1]
            i += 2
            continue
        if tok in {"-A", "--user-agent"} and i + 1 < len(tokens):
            headers.setdefault("User-Agent", tokens[i + 1])
            i += 2
            continue
        if not tok.startswith("-") and not url:
            url = tok
        i += 1

    if not url:
        raise ToolExecutionError("cURL command did not contain a URL")
    if method is None:
        method = "POST" if body is not None else "GET"
    return {"method": method, "url": url, "headers": headers, "body": body}


def _coerce_request_body(raw: Any) -> Any:
    """A JSON string body becomes a parsed object; everything else passes through."""
    if isinstance(raw, str) and raw.strip():
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
    return raw


async def _await_external_app_result(result: Any) -> Any:
    return await result


def external_app_entrypoint_allowed(spec: str, allowlist: str) -> bool:
    """Whether ``spec`` is permitted by the configured entrypoint allowlist.

    Fails closed on an empty allowlist. An ``external_app`` node imports and invokes
    installed Python **in the control-plane process** from a string an operator types
    into the Inspector, and configuration shipped allowlists for every other
    execution surface (MCP commands, Python modules/scripts, remote hosts) but not
    this one — so the default had to become "none permitted", not "any".

    A trailing ``*`` allows a module prefix (``mycompany.integrations.*``), which is
    what makes the allowlist usable for a team with many first-party entrypoints
    without degenerating into "allow everything".
    """
    candidate = _normalize_external_app_spec(spec)
    if not candidate:
        return False
    for raw in str(allowlist or "").split(","):
        pattern = _normalize_external_app_spec(raw)
        if not pattern:
            continue
        if pattern.endswith("*"):
            prefix = pattern[:-1]
            # A bare '*' would mean "allow everything", which defeats the control.
            if prefix and candidate.startswith(prefix):
                return True
            continue
        if candidate == pattern:
            return True
    return False


def _normalize_external_app_spec(spec: str) -> str:
    """Canonicalize to ``module:attr`` so ``a.b.c`` and ``a.b:c`` compare equal."""
    text = (spec or "").strip()
    if not text:
        return ""
    if ":" in text:
        module, _, attr = text.partition(":")
        return f"{module.strip()}:{attr.strip()}"
    if text.endswith("*"):
        return text
    module, _, attr = text.rpartition(".")
    return f"{module.strip()}:{attr.strip()}" if module else text


def _resolve_external_app_entrypoint(
    entrypoint: str, *, allowlist: str = ""
) -> tuple[Callable[..., Any], str]:
    spec = (entrypoint or "").strip()
    if not spec:
        raise ToolExecutionError("external_app entrypoint must not be empty")
    # Shape first: a malformed entrypoint deserves the shape error, which is the
    # actionable one. The allowlist is checked next, still before any import.
    if ":" in spec:
        module_name, attr_path = spec.split(":", 1)
    elif "." in spec:
        module_name, attr_path = spec.rsplit(".", 1)
    else:
        raise ToolExecutionError(
            "external_app entrypoint must use 'package.module:callable' "
            "or 'package.module.callable'"
        )
    if not external_app_entrypoint_allowed(spec, allowlist):
        raise ToolExecutionError(
            f"external_app entrypoint {spec!r} is not in "
            "CALIBER_EXTERNAL_APP_ENTRYPOINT_ALLOWLIST; this node imports and invokes "
            "installed Python in the CALIBER process, so it is allowlisted rather than "
            "open by default"
        )
    module_name = module_name.strip()
    attr_path = attr_path.strip()
    if not module_name or not attr_path:
        raise ToolExecutionError(
            f"external_app entrypoint {entrypoint!r} is invalid; expected 'package.module:callable'"
        )
    try:
        module = importlib.import_module(module_name)
    except Exception as exc:
        raise ToolExecutionError(
            f"could not import external_app module {module_name!r}: {type(exc).__name__}: {exc}"
        ) from exc
    target: Any = module
    for attr in [part.strip() for part in attr_path.split(".") if part.strip()]:
        if not hasattr(target, attr):
            raise ToolExecutionError(
                f"external_app entrypoint {entrypoint!r} could not resolve "
                f"attribute {attr!r} on {target!r}"
            )
        target = getattr(target, attr)
    if not callable(target):
        raise ToolExecutionError(
            f"external_app entrypoint {entrypoint!r} resolved to a non-callable object"
        )
    return target, f"{module_name}:{attr_path}"


def _external_app_single_argument(
    param_name: str,
    payload: dict[str, Any],
) -> Any:
    if param_name in payload:
        return payload[param_name]
    if param_name in {"payload", "data", "event", "request"}:
        return payload
    if param_name == "input":
        return payload.get("input", "")
    return payload


def _invoke_external_app_callable(
    fn: Callable[..., Any],
    payload: dict[str, Any],
) -> Any:
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        signature = None

    if signature is None:
        result = fn(payload)
    else:
        params = [
            param
            for param in signature.parameters.values()
            if param.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
                inspect.Parameter.VAR_KEYWORD,
            )
        ]
        if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params):
            result = fn(**payload)
        elif not params:
            result = fn()
        elif len(params) == 1 and params[0].kind != inspect.Parameter.KEYWORD_ONLY:
            result = fn(_external_app_single_argument(params[0].name, payload))
        else:
            args = [
                payload[param.name]
                for param in params
                if param.kind == inspect.Parameter.POSITIONAL_ONLY and param.name in payload
            ]
            kwargs = {
                param.name: payload[param.name]
                for param in params
                if param.kind
                not in {
                    inspect.Parameter.POSITIONAL_ONLY,
                    inspect.Parameter.VAR_KEYWORD,
                }
                and param.name in payload
            }
            result = fn(*args, **kwargs)

    if inspect.isawaitable(result):
        return asyncio.run(_await_external_app_result(result))
    return result


def _run_external_app_entrypoint(
    *,
    node: IRExternalApp,
    ir: IRWorkflow,
    nid: str,
    inputs: dict[str, Any],
    run_input: str,
    preview: bool,
    entrypoint_allowlist: str = "",
    config: Any | None = None,
) -> tuple[Any, dict[str, Any]]:
    # Imports and calls installed Python in the control-plane process: not supported in the reference profile (EP-012).
    refuse_in_reference_profile("EP-012", config)
    entrypoint, resolved_entrypoint = _resolve_external_app_entrypoint(
        node.entrypoint, allowlist=entrypoint_allowlist
    )
    run_ctx = current_run_context()
    payload = {
        "input": _select_input(inputs, run_input),
        "context": _json_compatible(inputs.get("context", dict(inputs))),
        "inputs": _json_compatible(dict(inputs)),
        "run_input": run_input,
        "entrypoint": resolved_entrypoint,
        "node_id": nid,
        "workflow_id": ir.workflow_id,
        "workflow_version": ir.version,
        "session_id": run_ctx.session_id if run_ctx is not None else None,
        "workflow_alias": run_ctx.workflow_alias if run_ctx is not None else None,
        "workflow_version_id": run_ctx.workflow_version_id if run_ctx is not None else None,
        "preview": preview,
    }
    timeout = node.execution_policy.timeout_seconds
    call_context = contextvars.copy_context()
    started = time.perf_counter()
    if timeout is None:
        result = call_context.run(_invoke_external_app_callable, entrypoint, payload)
    else:
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(
            call_context.run,
            _invoke_external_app_callable,
            entrypoint,
            payload,
        )
        timed_out = False
        try:
            result = future.result(timeout=timeout)
        except FuturesTimeout as exc:
            timed_out = True
            raise ToolExecutionError(
                f"external_app node {nid!r} timed out after {timeout}s"
            ) from exc
        finally:
            pool.shutdown(wait=not timed_out, cancel_futures=timed_out)
    duration_ms = round((time.perf_counter() - started) * 1000, 1)
    metadata: dict[str, Any] = {
        "entrypoint": resolved_entrypoint,
        "duration_ms": duration_ms,
        "preview": preview,
    }
    if run_ctx is not None:
        metadata["session_id"] = run_ctx.session_id
    if isinstance(result, dict) and isinstance(result.get("metadata"), dict):
        metadata = {
            **result["metadata"],
            **metadata,
        }
    return result, metadata


def execute(
    plan: RuntimePlan,
    input_text: str,
    *,
    executor: WorkflowExecutor,
    session_id: str | None = None,
    preview: bool = False,
    token_budget: TokenBudget | None = None,
    on_step: Callable[[NodeStep], None] | None = None,
    on_node_start: Callable[[str, IRNode, dict[str, Any]], None] | None = None,
    extra_tools: dict[str, Callable[..., Any]] | None = None,
    runtime_approvals_enabled: bool = False,
    approved_human_approval_nodes: set[str] | None = None,
    resume_checkpoint: RuntimeResumeCheckpoint | None = None,
    managed_file_resolver: ManagedFileResolver | None = None,
) -> WorkflowRunResult:
    """Execute a workflow plan against a single string input.

    Returns a :class:`WorkflowRunResult` regardless of success — guardrail
    blocks and runtime errors are captured as ``status`` rather than raised, so
    the caller (preview endpoint, eval replay) gets a structured outcome.

    ``extra_tools`` are pre-bound callables merged into every agent's resolved
    tool set by ``local_name`` (run-scoped file tools are injected this way by
    the run route; storage doc §4.4). It defaults to ``None`` — when unset the
    interpreter behaves exactly as before, so existing callers are unaffected.
    """
    ir = plan.ir
    root_ctx = CaliberRunContext(
        workflow_id=ir.workflow_id,
        workflow_version=ir.version,
        entry_node_id=ir.entry_node_id,
        workflow_alias=plan.workflow_alias,
        workflow_version_id=plan.workflow_version_id,
        compiler_version=plan.compiler_version,
        manifest_hash=ir.manifest_hash,
        default_model_ref=ir.default_model_ref,
        session_id=session_id,
        preview=preview,
        trace_group_tags=dict(ir.mlflow_trace_group_tags),
    )
    root_tags = run_tags(root_ctx)
    # The runtime entry of the reference profile's code refusal (B12.4a, #754): before the preview check, before any node.
    if code_refusals := ir_refusals(ir, plan.code_policy):
        return WorkflowRunResult(
            status="error",
            output="",
            steps=[
                NodeStep(
                    node_id=node_id,
                    node_type=ir.nodes[node_id].node_type.value,
                    status="error",
                    detail=f"{REFUSAL_CODE}: no workflow node ran",
                )
                for node_id in dict.fromkeys(refusal.node_id for refusal in code_refusals)
            ],
            tags=root_tags,
            error=summarize(code_refusals),
            error_code=REFUSAL_CODE,
        )
    if preview:
        unisolated_nodes = _preview_unisolated_nodes(ir)
        if unisolated_nodes:
            labels = ", ".join(
                f"{node.node_id} ({node.node_type.value})" for node in unisolated_nodes
            )
            error = (
                "preview blocked before execution because ordinary Preview cannot isolate "
                f"these capability nodes: {labels}"
            )
            return WorkflowRunResult(
                status="error",
                output="",
                steps=[
                    NodeStep(
                        node_id=node.node_id,
                        node_type=node.node_type.value,
                        status="error",
                        detail="preview cannot isolate this capability; no workflow node ran",
                    )
                    for node in unisolated_nodes
                ],
                tags=root_tags,
                error=error,
            )

    tracer = get_tracer()
    # Root MLflow run + span for the whole workflow execution (golden-path
    # roadmap, Wave 1). Opening a run is what *activates* the caliber.* tags —
    # ``run_with_caliber_context`` only sets them when a run is active. The root
    # span parents every per-agent / per-tool span into a single trace. An
    # already-active run (e.g. an enclosing refinement run) is reused, not nested.
    with (
        tracer.trace_run(
            "workflow.run",
            tags=root_tags,
            experiment=ir.mlflow_experiment_name,
        ) as run_handle,
        tracer.span("workflow.run", span_type="CHAIN", attributes=root_tags) as root_span,
        run_with_caliber_context(
            workflow_id=ir.workflow_id,
            workflow_version=ir.version,
            entry_node_id=ir.entry_node_id,
            session_id=session_id,
            workflow_alias=plan.workflow_alias,
            workflow_version_id=plan.workflow_version_id,
            compiler_version=plan.compiler_version,
            manifest_hash=ir.manifest_hash,
            default_model_ref=ir.default_model_ref,
            preview=preview,
            extra_tags=ir.mlflow_trace_group_tags,
        ) as ctx,
        _bind_managed_file_resolver(managed_file_resolver),
    ):
        # Stamp the trace with the session so multi-turn / retried runs group
        # into one MLflow session (surfaced in CALIBER Observability).
        if session_id:
            tracer.annotate_trace(session_id=session_id)
        root_span.set_io(inputs=input_text)
        result = _interpret(
            plan,
            input_text,
            executor=executor,
            preview=preview,
            base_tags=run_tags(ctx),
            token_budget=token_budget,
            on_step=on_step,
            on_node_start=on_node_start,
            extra_tools=extra_tools,
            runtime_approvals_enabled=runtime_approvals_enabled,
            approved_human_approval_nodes=approved_human_approval_nodes,
            resume_checkpoint=resume_checkpoint,
        )
        root_span.set_io(outputs=result.output or result.error)
        if result.status == "error":
            root_span.mark_error(result.error)
        if run_handle.run_id:
            result.mlflow_run_id = run_handle.run_id
        if root_span.mlflow_trace_id:
            result.mlflow_trace_id = root_span.mlflow_trace_id
        return result


def _interpret(  # noqa: PLR0911, PLR0912, PLR0915 - graph interpreter dispatch
    plan: RuntimePlan,
    input_text: str,
    *,
    executor: WorkflowExecutor,
    preview: bool,
    base_tags: dict[str, str],
    token_budget: TokenBudget | None,
    on_step: Callable[[NodeStep], None] | None,
    on_node_start: Callable[[str, IRNode, dict[str, Any]], None] | None,
    extra_tools: dict[str, Callable[..., Any]] | None = None,
    runtime_approvals_enabled: bool = False,
    approved_human_approval_nodes: set[str] | None = None,
    resume_checkpoint: RuntimeResumeCheckpoint | None = None,
) -> WorkflowRunResult:
    ir = plan.ir
    if plan.governed_agent_steps is not None and (
        refusal := _governed_plan_refusal(ir, plan.governed_agent_steps.governs)
    ):
        return WorkflowRunResult(
            status="error",
            output="",
            steps=[],
            tags=base_tags,
            tokens=0,
            error=f"ToolExecutionError: {refusal}",
        )
    connections = _connections(ir)
    out_conns: dict[str, list[_Connection]] = {nid: [] for nid in ir.nodes}
    in_conns: dict[str, list[_Connection]] = {nid: [] for nid in ir.nodes}
    for conn in connections:
        out_conns.setdefault(conn.from_node, []).append(conn)
        in_conns.setdefault(conn.to_node, []).append(conn)

    port_values: dict[tuple[str, str], Any] = {}
    delivered: dict[str, bool] = {c.edge_id: False for c in connections}
    dead: dict[str, bool] = {c.edge_id: False for c in connections}
    steps: list[NodeStep] = []
    guardrail_results: list[dict[str, Any]] = []
    total_tokens = 0
    final_output = ""
    pending_wait_event_step: NodeStep | None = None
    idempotent_cache: dict[tuple[str, str], tuple[NodeStep, dict[str, Any], int]] = {}

    # Seed start-node outputs unless we're resuming from a checkpoint boundary.
    start_ids = [nid for nid, n in ir.nodes.items() if n.node_type == NodeType.START]
    if resume_checkpoint is None:
        for sid in start_ids:
            start_node = ir.nodes[sid]
            out_ports = list(start_node.outputs) or ["output"]
            for port in out_ports:
                port_values[(sid, port)] = input_text

    ready: list[str] = list(start_ids) if resume_checkpoint is None else []
    processed: set[str] = set()
    resume_input_snapshot: dict[str, Any] | None = None
    resume_injected_inputs: dict[str, Any] | None = None

    def _node_ready(nid: str) -> bool:
        if (
            resume_checkpoint is not None
            and not resume_checkpoint.replay_output
            and nid == resume_checkpoint.node_id
        ):
            return True
        incoming = in_conns.get(nid, [])
        if not incoming:
            return True
        ir_node = ir.nodes.get(nid)
        if isinstance(ir_node, IRJoin) and ir_node.mode == "any":
            return any(delivered[c.edge_id] for c in incoming if not dead[c.edge_id])
        for conn in incoming:
            if dead[conn.edge_id]:
                continue
            if not delivered[conn.edge_id]:
                return False
        # ready when at least one live incoming connection delivered
        return any(delivered[c.edge_id] for c in incoming if not dead[c.edge_id])

    def _prune_unreachable_descendants(node_id: str) -> None:
        pending = [node_id]
        seen: set[str] = set()
        while pending:
            current = pending.pop()
            if current in seen or current in processed:
                continue
            seen.add(current)
            incoming = in_conns.get(current, [])
            if not incoming or any(not dead[conn.edge_id] for conn in incoming):
                continue
            if current in ready:
                ready[:] = [queued for queued in ready if queued != current]
            for conn in out_conns.get(current, []):
                if dead[conn.edge_id]:
                    continue
                dead[conn.edge_id] = True
                pending.append(conn.to_node)

    def _gather_inputs(nid: str) -> dict[str, Any]:
        values: dict[str, Any] = {}
        if (
            resume_checkpoint is not None
            and not resume_checkpoint.replay_output
            and nid == resume_checkpoint.node_id
        ):
            if resume_input_snapshot:
                values.update(dict(resume_input_snapshot))
            if resume_injected_inputs:
                values.update(dict(resume_injected_inputs))
        for conn in in_conns.get(nid, []):
            if not delivered[conn.edge_id]:
                continue
            for from_out, to_in in conn.mappings:
                if (conn.from_node, from_out) in port_values:
                    values[to_in] = port_values[(conn.from_node, from_out)]
        return values

    def _notify_node_start(nid: str, node: IRNode, gathered_inputs: dict[str, Any]) -> None:
        if on_node_start is None:
            return
        on_node_start(nid, node, dict(gathered_inputs))

    def _deliver_from(nid: str, *, only_to: str | None = None) -> None:
        for conn in out_conns.get(nid, []):
            if only_to is not None and conn.to_node != only_to:
                dead[conn.edge_id] = True
                _prune_unreachable_descendants(conn.to_node)
                continue
            if all((nid, fo) in port_values for fo, _ in conn.mappings):
                delivered[conn.edge_id] = True
                for from_out, to_in in conn.mappings:
                    port_values[(conn.to_node, to_in)] = port_values[(nid, from_out)]
                if _node_ready(conn.to_node) and conn.to_node not in processed:
                    ready.append(conn.to_node)

    def _parallel_batch_parent(nid: str) -> str | None:  # noqa: PLR0911 - explicit guard cascade
        if nid in processed:
            return None
        node = ir.nodes.get(nid)
        if node is None or not isinstance(node, _PARALLEL_BRANCH_BATCH_TARGET_TYPES):
            return None
        incoming = [conn for conn in in_conns.get(nid, []) if not dead[conn.edge_id]]
        if len(incoming) != 1:
            return None
        connection = incoming[0]
        if not delivered[connection.edge_id] or connection.from_node not in processed:
            return None
        parent = ir.nodes.get(connection.from_node)
        if not isinstance(parent, IRParallel):
            return None
        if not _node_ready(nid):
            return None
        return connection.from_node

    def _ready_parallel_batch() -> list[str]:
        if not ready:
            return []
        parent_id = _parallel_batch_parent(ready[0])
        if parent_id is None:
            return []
        batch = [nid for nid in ready if _parallel_batch_parent(nid) == parent_id]
        return batch if len(batch) > 1 else []

    def _finalize_step(
        *,
        nid: str,
        node: IRNode,
        gathered_inputs: dict[str, Any],  # noqa: ARG001 - threaded through a shared finalize signature
        step: NodeStep,
        tokens: int,
        cache_key: tuple[str, str] | None,
        node_outputs: dict[str, Any],
        extra_guardrail_results: list[dict[str, Any]],
        remaining_batch_results: int = 0,
    ) -> WorkflowRunResult | None:
        nonlocal total_tokens, final_output, pending_wait_event_step
        if extra_guardrail_results:
            guardrail_results.extend(extra_guardrail_results)
        total_tokens += tokens
        if token_budget is not None:
            token_budget.charge(tokens)
        steps.append(step)
        if on_step is not None:
            on_step(step)
        if cache_key is not None and step.status == "ok":
            idempotent_cache[cache_key] = (step, node_outputs, tokens)

        if (
            step.status == "blocked"
            and isinstance(node, IRGuardrail)
            and node.on_failure == "block_retry"
        ):
            retry_tokens, step = _retry_blocked_guardrail(
                node,
                ir,
                plan,
                executor=executor,
                preview=preview,
                in_conns=in_conns,
                port_values=port_values,
                run_input=input_text,
                guardrail_results=guardrail_results,
                steps=steps,
                on_step=on_step,
            )
            total_tokens += retry_tokens
            if token_budget is not None:
                token_budget.charge(retry_tokens)

        if step.status == "blocked":
            if (
                isinstance(step.detail, str)
                and step.detail.startswith("waiting_event:")
                and (ready or remaining_batch_results > 0)
            ):
                pending_wait_event_step = pending_wait_event_step or step
                return None
            return WorkflowRunResult(
                status="blocked",
                output="",
                steps=steps,
                tags=base_tags,
                tokens=total_tokens,
                error=step.detail,
                guardrail_results=guardrail_results,
            )

        if node.node_type == NodeType.OUTPUT:
            final_output = step.output

        only_to = step.handoff_target if isinstance(node, IRRouter) else None
        _deliver_from(nid, only_to=only_to)
        return None

    def _effect_waiting_step(exc: UnknownEffectError) -> NodeStep:
        """Turn an effect-gate refusal into a durable, input-snapshot checkpoint."""
        node_id = getattr(exc, "blocked_node_id", None)
        if not isinstance(node_id, str) or not node_id:
            node_id = exc.effect_details[0].node_id if exc.effect_details else "unknown"
        node = ir.nodes.get(node_id)
        node_type = node.node_type.value if node is not None else "unknown"
        input_by_port = getattr(exc, "blocked_inputs", None)
        if not isinstance(input_by_port, dict):
            input_by_port = {}
        details: list[dict[str, Any]] = [
            {
                "proposal_id": item.proposal_id,
                "node_id": item.node_id,
                "state": item.state,
                "logical_key": item.logical_key,
                "intent_hash": item.intent_hash,
            }
            for item in exc.effect_details
        ]
        if not details:
            details = [
                {
                    "proposal_id": proposal_id,
                    "node_id": effect_node_id,
                    "state": state,
                    "logical_key": None,
                    "intent_hash": None,
                }
                for proposal_id, effect_node_id, state in exc.open_effects
            ]
        checkpoint_state = {
            "kind": "effect_reconciliation",
            "node_id": node_id,
            "input_by_port": dict(input_by_port),
            "effects": details,
            "reason": "effect_dependency_unresolved",
            "resume_policy": "same_run_same_occurrence",
        }
        return NodeStep(
            node_id=node_id,
            node_type=node_type,
            status="blocked",
            detail=f"waiting_effect:{node_id}",
            checkpoint_state=checkpoint_state,
            input_by_port=dict(input_by_port),
        )

    def _effect_waiting_result(exc: UnknownEffectError) -> WorkflowRunResult:
        step = _effect_waiting_step(exc)
        return _finalize_step(
            nid=step.node_id,
            node=ir.nodes.get(step.node_id) or ir.nodes[ir.entry_node_id],
            gathered_inputs=dict(step.input_by_port or {}),
            step=step,
            tokens=0,
            cache_key=None,
            node_outputs={},
            extra_guardrail_results=[],
        ) or WorkflowRunResult(
            status="blocked",
            output="",
            steps=steps,
            tags=base_tags,
            tokens=total_tokens,
            error=step.detail,
            guardrail_results=guardrail_results,
        )

    def _approval_waiting_result(exc: _step_proposal.EffectApprovalPending) -> WorkflowRunResult:
        """Park the node whose governed effect is waiting for its exact approval (``waiting_approval``), or refuse the run.

        The checkpoint is the ``runtime_approval`` kind the decision and resume routes accept, naming the proposal and the
        approval request that already exist (identifiers and the intent hash, never the effect's arguments) plus the node's
        input snapshot, so a resume runs the same node again and finds its one logical effect.
        """
        node_id = exc.blocked_node_id
        node = ir.nodes.get(node_id) if node_id else None
        if (
            node_id is None
            or node is None
            or any(node_id in _inline_children(n) for n in ir.nodes.values())
        ):
            # Only a node of the graph can park the run: a target of a fan-out, loop or boundary runs inside its parent and
            # cannot carry the park, so its effect would wait on an approval nobody can resume.
            return WorkflowRunResult(
                status="error",
                output="",
                steps=steps,
                tags=base_tags,
                tokens=total_tokens,
                error=(
                    "ToolExecutionError: a governed effect waiting for approval cannot run inline in a "
                    f"fan-out, loop or boundary: node {node_id!r}"
                ),
                guardrail_results=guardrail_results,
            )
        input_by_port = dict(exc.blocked_inputs or {})
        step = NodeStep(
            node_id=node_id,
            node_type=node.node_type.value,
            status="blocked",
            detail=f"waiting_approval:{node_id}",
            input_by_port=input_by_port,
            checkpoint_state={
                "kind": "runtime_approval",
                "node_id": node_id,
                "proposal_id": exc.proposal_id,
                "approval_request_id": exc.approval_request_id,
                "intent_hash": exc.intent_hash,
                "reason": "effect_proposal_awaiting_approval",
                "resume_policy": "same_run_same_occurrence",
            },
        )
        return _finalize_step(
            nid=node_id,
            node=node,
            gathered_inputs=input_by_port,
            step=step,
            tokens=0,
            cache_key=None,
            node_outputs={},
            extra_guardrail_results=[],
        ) or WorkflowRunResult(
            status="blocked",
            output="",
            steps=steps,
            tags=base_tags,
            tokens=total_tokens,
            error=step.detail,
            guardrail_results=guardrail_results,
        )

    def _run_parallel_ready_batch(batch_ids: list[str]) -> list[dict[str, Any]]:
        base_port_values = dict(port_values)
        results_by_node_id: dict[str, dict[str, Any]] = {}
        pending_specs: list[tuple[str, IRNode, dict[str, Any], tuple[str, str] | None]] = []

        for nid in batch_ids:
            node = ir.nodes[nid]
            gathered_inputs = _gather_inputs(nid)
            cache_key = (
                (nid, _stable_json(gathered_inputs)) if node.execution_policy.idempotent else None
            )
            cached = idempotent_cache.get(cache_key) if cache_key is not None else None
            if cached is not None:
                cached_step, cached_outputs, cached_tokens = cached
                local_port_values = dict(base_port_values)
                for port, value in cached_outputs.items():
                    local_port_values[(nid, port)] = value
                step = NodeStep(
                    node_id=cached_step.node_id,
                    node_type=cached_step.node_type,
                    status=cached_step.status,
                    output=cached_step.output,
                    tokens=cached_step.tokens,
                    prompt_tokens=cached_step.prompt_tokens,
                    completion_tokens=cached_step.completion_tokens,
                    cached_prompt_tokens=cached_step.cached_prompt_tokens,
                    cost_usd=cached_step.cost_usd,
                    model=cached_step.model,
                    prompt_version=cached_step.prompt_version,
                    tool_calls=list(cached_step.tool_calls),
                    handoff_target=cached_step.handoff_target,
                    detail=(
                        f"{cached_step.detail}; idempotent cache hit"
                        if cached_step.detail
                        else "idempotent cache hit"
                    ),
                    duration_ms=cached_step.duration_ms,
                )
                step = _attach_step_port_snapshots(
                    step,
                    node=node,
                    input_by_port=gathered_inputs,
                    port_values=local_port_values,
                )
                results_by_node_id[nid] = {
                    "nid": nid,
                    "node": node,
                    "gathered_inputs": gathered_inputs,
                    "tokens": cached_tokens,
                    "step": step,
                    "node_outputs": dict(cached_outputs),
                    "extra_guardrail_results": [],
                    "cache_key": cache_key,
                }
                continue
            pending_specs.append((nid, node, gathered_inputs, cache_key))

        for nid, node, gathered_inputs, _cache_key in pending_specs:
            _notify_node_start(nid, node, gathered_inputs)

        def _worker(
            nid: str,
            node: IRNode,
            gathered_inputs: dict[str, Any],
            cache_key: tuple[str, str] | None,
        ) -> dict[str, Any]:
            local_port_values = dict(base_port_values)
            local_guardrail_results: list[dict[str, Any]] = []
            tokens, step = _run_node_with_policy(
                node,
                ir,
                plan,
                executor=executor,
                preview=preview,
                inputs=gathered_inputs,
                run_input=input_text,
                port_values=local_port_values,
                guardrail_results=local_guardrail_results,
                extra_tools=extra_tools,
                runtime_approvals_enabled=runtime_approvals_enabled,
                approved_human_approval_nodes=approved_human_approval_nodes or set(),
            )
            step = _attach_step_port_snapshots(
                step,
                node=node,
                input_by_port=gathered_inputs,
                port_values=local_port_values,
            )
            node_outputs = {
                port: value
                for (node_id, port), value in local_port_values.items()
                if node_id == nid
            }
            return {
                "nid": nid,
                "node": node,
                "gathered_inputs": gathered_inputs,
                "tokens": tokens,
                "step": step,
                "node_outputs": node_outputs,
                "extra_guardrail_results": local_guardrail_results,
                "cache_key": cache_key,
            }

        if len(pending_specs) > 1:
            # Bounded: sizing the pool to the branch count let a wide graph spawn
            # an unbounded number of threads. Excess branches queue rather than
            # being dropped, so results are identical.
            branch_workers = max(1, min(len(pending_specs), plan.parallel_branch_max_workers))
            with ThreadPoolExecutor(max_workers=branch_workers) as pool:

                def _run_pending_spec(
                    spec: tuple[str, IRNode, dict[str, Any], tuple[str, str] | None],
                ) -> dict[str, Any]:
                    return contextvars.copy_context().run(_worker, *spec)

                futures = {spec[0]: pool.submit(_run_pending_spec, spec) for spec in pending_specs}
                for nid, future in futures.items():
                    try:
                        results_by_node_id[nid] = future.result()
                    except UnknownEffectError as exc:
                        results_by_node_id[nid] = {
                            "nid": nid,
                            "node": ir.nodes[nid],
                            "gathered_inputs": next(
                                spec[2] for spec in pending_specs if spec[0] == nid
                            ),
                            "tokens": 0,
                            "step": None,
                            "node_outputs": {},
                            "extra_guardrail_results": [],
                            "cache_key": next(spec[3] for spec in pending_specs if spec[0] == nid),
                            "effect_error": exc,
                        }
        elif len(pending_specs) == 1:
            try:
                result = _worker(*pending_specs[0])
                results_by_node_id[result["nid"]] = result
            except UnknownEffectError as exc:
                nid, node, gathered_inputs, cache_key = pending_specs[0]
                results_by_node_id[nid] = {
                    "nid": nid,
                    "node": node,
                    "gathered_inputs": gathered_inputs,
                    "tokens": 0,
                    "step": None,
                    "node_outputs": {},
                    "extra_guardrail_results": [],
                    "cache_key": cache_key,
                    "effect_error": exc,
                }

        return [results_by_node_id[nid] for nid in batch_ids]

    if resume_checkpoint is not None and resume_checkpoint.node_id in ir.nodes:
        # A resumed run only executes what lies downstream of the checkpoint node; everything
        # upstream already ran (or was skipped by a router) before the pause. Without this, an
        # edge from a node that will never run in this segment stays "undelivered and not dead",
        # so a node that several branches converge on (the usual shape of an Output node after a
        # router) is never ready: the run "completes" after the resumed node with no final output.
        _reachable = {resume_checkpoint.node_id}
        _frontier = [resume_checkpoint.node_id]
        while _frontier:
            for conn in out_conns.get(_frontier.pop(), []):
                if conn.to_node not in _reachable:
                    _reachable.add(conn.to_node)
                    _frontier.append(conn.to_node)
        for conn in connections:
            if conn.from_node not in _reachable:
                dead[conn.edge_id] = True

    if resume_checkpoint is not None:
        if resume_checkpoint.node_id not in ir.nodes:
            return WorkflowRunResult(
                status="error",
                output="",
                steps=steps,
                tags=base_tags,
                tokens=total_tokens,
                error=(
                    "ToolExecutionError: "
                    f"resume checkpoint references missing node {resume_checkpoint.node_id!r} "
                    "in the current workflow plan"
                ),
                guardrail_results=guardrail_results,
            )
        node = ir.nodes[resume_checkpoint.node_id]
        if not _resume_checkpoint_kind_matches_node(
            resume_checkpoint.checkpoint_kind,
            node,
            governed_agent_steps=_governs_agent(plan, node),
            governed_effects=plan.governed_effects is not None,
        ):
            return WorkflowRunResult(
                status="error",
                output="",
                steps=steps,
                tags=base_tags,
                tokens=total_tokens,
                error=(
                    "ToolExecutionError: "
                    f"resume checkpoint kind {resume_checkpoint.checkpoint_kind!r} "
                    f"does not match current node {resume_checkpoint.node_id!r} "
                    f"type {getattr(getattr(node, 'node_type', None), 'value', getattr(node, 'node_type', None))!r}"
                ),
                guardrail_results=guardrail_results,
            )
        resume_checkpoint_error = _validate_resume_checkpoint_for_node(
            resume_checkpoint,
            node,
        )
        if resume_checkpoint_error is not None:
            return WorkflowRunResult(
                status="error",
                output="",
                steps=steps,
                tags=base_tags,
                tokens=total_tokens,
                error=resume_checkpoint_error,
                guardrail_results=guardrail_results,
            )
        if resume_checkpoint.replay_output:
            if isinstance(node, (IRWaitForEvent, IRWaitUntil, IRHumanApproval)) or (
                isinstance(node, IRTool)
                and node.binding is not None
                and node.binding.requires_approval
            ):
                return WorkflowRunResult(
                    status="error",
                    output="",
                    steps=steps,
                    tags=base_tags,
                    tokens=total_tokens,
                    error=(
                        "ToolExecutionError: "
                        f"resume checkpoint for gated node {resume_checkpoint.node_id!r} "
                        "cannot replay output past the gate; an input snapshot resume is required"
                    ),
                    guardrail_results=guardrail_results,
                )
            out_ports = list(node.outputs) or ["output"]
            if resume_checkpoint.output_by_port:
                for port in out_ports:
                    if port in resume_checkpoint.output_by_port:
                        port_values[(resume_checkpoint.node_id, port)] = (
                            resume_checkpoint.output_by_port[port]
                        )
            else:
                for port in out_ports:
                    port_values[(resume_checkpoint.node_id, port)] = resume_checkpoint.output
            if isinstance(node, IRKnowledgeQuery):
                replay_provenance = _knowledge_context_provenance(
                    plan, port_values.get((resume_checkpoint.node_id, "result"))
                )
                if plan.context_recorder is not None and (
                    not plan.context_source_guard_present or not replay_provenance
                ):
                    return WorkflowRunResult(
                        status="error",
                        output="",
                        steps=steps,
                        tags=base_tags,
                        tokens=total_tokens,
                        error=(
                            "ContextDenied: authority_unavailable: "
                            "a replayed knowledge result has no reusable source-read authority"
                        ),
                        guardrail_results=guardrail_results,
                    )
                _set_context_provenance(plan, node, replay_provenance)
            processed.add(resume_checkpoint.node_id)
            _deliver_from(resume_checkpoint.node_id)
        else:
            resume_input_snapshot = dict(resume_checkpoint.input_by_port or {})
            resume_injected_inputs = dict(resume_checkpoint.injected_inputs or {})
            ready.append(resume_checkpoint.node_id)

    try:
        while ready:
            batch_ids = _ready_parallel_batch()
            if batch_ids:
                ready = [queued for queued in ready if queued not in batch_ids]
                batch_results = _run_parallel_ready_batch(batch_ids)
                for index, result in enumerate(batch_results):
                    nid = result["nid"]
                    node = result["node"]
                    if nid in processed:
                        continue
                    effect_error = result.get("effect_error")
                    if isinstance(effect_error, UnknownEffectError):
                        return _effect_waiting_result(effect_error)
                    processed.add(nid)
                    for port, value in result["node_outputs"].items():
                        port_values[(nid, port)] = value
                    blocked_result = _finalize_step(
                        nid=nid,
                        node=node,
                        gathered_inputs=result["gathered_inputs"],
                        step=result["step"],
                        tokens=result["tokens"],
                        cache_key=result["cache_key"],
                        node_outputs=result["node_outputs"],
                        extra_guardrail_results=result["extra_guardrail_results"],
                        remaining_batch_results=len(batch_results) - index - 1,
                    )
                    if blocked_result is not None:
                        return blocked_result
                continue

            nid = ready.pop(0)
            if nid in processed:
                continue
            node = ir.nodes[nid]
            if not _node_ready(nid):
                continue
            processed.add(nid)
            gathered_inputs = _gather_inputs(nid)
            cache_key: tuple[str, str] | None = None
            if node.execution_policy.idempotent:
                cache_key = (nid, _stable_json(gathered_inputs))
                cached = idempotent_cache.get(cache_key)
                if cached is not None:
                    cached_step, cached_outputs, cached_tokens = cached
                    for port, value in cached_outputs.items():
                        port_values[(nid, port)] = value
                    step = NodeStep(
                        node_id=cached_step.node_id,
                        node_type=cached_step.node_type,
                        status=cached_step.status,
                        output=cached_step.output,
                        tokens=cached_step.tokens,
                        prompt_tokens=cached_step.prompt_tokens,
                        completion_tokens=cached_step.completion_tokens,
                        cached_prompt_tokens=cached_step.cached_prompt_tokens,
                        cost_usd=cached_step.cost_usd,
                        model=cached_step.model,
                        prompt_version=cached_step.prompt_version,
                        tool_calls=list(cached_step.tool_calls),
                        handoff_target=cached_step.handoff_target,
                        detail=(
                            f"{cached_step.detail}; idempotent cache hit"
                            if cached_step.detail
                            else "idempotent cache hit"
                        ),
                        duration_ms=cached_step.duration_ms,
                    )
                    tokens = cached_tokens
                else:
                    _notify_node_start(nid, node, gathered_inputs)
                    tokens, step = _run_node_with_policy(
                        node,
                        ir,
                        plan,
                        executor=executor,
                        preview=preview,
                        inputs=gathered_inputs,
                        run_input=input_text,
                        port_values=port_values,
                        guardrail_results=guardrail_results,
                        extra_tools=extra_tools,
                        runtime_approvals_enabled=runtime_approvals_enabled,
                        approved_human_approval_nodes=approved_human_approval_nodes or set(),
                    )
            else:
                _notify_node_start(nid, node, gathered_inputs)
                tokens, step = _run_node_with_policy(
                    node,
                    ir,
                    plan,
                    executor=executor,
                    preview=preview,
                    inputs=gathered_inputs,
                    run_input=input_text,
                    port_values=port_values,
                    guardrail_results=guardrail_results,
                    extra_tools=extra_tools,
                    runtime_approvals_enabled=runtime_approvals_enabled,
                    approved_human_approval_nodes=approved_human_approval_nodes or set(),
                )
            step = _attach_step_port_snapshots(
                step,
                node=node,
                input_by_port=gathered_inputs,
                port_values=port_values,
            )
            node_outputs = {
                port: value for (node_id, port), value in port_values.items() if node_id == nid
            }
            blocked_result = _finalize_step(
                nid=nid,
                node=node,
                gathered_inputs=gathered_inputs,
                step=step,
                tokens=tokens,
                cache_key=cache_key,
                node_outputs=node_outputs,
                extra_guardrail_results=[],
            )
            if blocked_result is not None:
                return blocked_result
    except UnknownEffectError as exc:
        return _effect_waiting_result(exc)
    except _step_proposal.EffectApprovalPending as approval_pending:
        return _approval_waiting_result(approval_pending)
    except GuardrailBlockedError as exc:  # pragma: no cover - converted to step above
        return WorkflowRunResult(
            status="blocked",
            output="",
            steps=steps,
            tags=base_tags,
            tokens=total_tokens,
            error=str(exc),
            guardrail_results=guardrail_results,
        )
    except Exception as exc:
        return WorkflowRunResult(
            status="error",
            output="",
            steps=steps,
            tags=base_tags,
            tokens=total_tokens,
            error=f"{type(exc).__name__}: {exc}",
            error_code=_refusal_code_of(exc),
            guardrail_results=guardrail_results,
        )

    # If no output node produced a value, fall back to the last agent step.
    if not final_output:
        agent_steps = [s for s in steps if s.node_type == NodeType.AGENT.value and s.output]
        if agent_steps:
            final_output = agent_steps[-1].output

    if pending_wait_event_step is not None and not final_output:
        return WorkflowRunResult(
            status="blocked",
            output="",
            steps=steps,
            tags=base_tags,
            tokens=total_tokens,
            error=pending_wait_event_step.detail,
            guardrail_results=guardrail_results,
        )

    return WorkflowRunResult(
        status="completed",
        output=final_output,
        steps=steps,
        tags=base_tags,
        tokens=total_tokens,
        guardrail_results=guardrail_results,
        artifacts=_collect_artifacts(port_values),
    )


def _collect_artifacts(port_values: dict[tuple[str, str], Any]) -> dict[str, str]:
    """Gather ``artifacts`` maps from node structured outputs into one dict.

    Any node output value shaped ``{"artifacts": {name: content}}`` contributes
    its entries (e.g. a ``python_code`` node's ``result`` port). Non-string
    content is JSON-encoded so it persists as a text file. Later writers win on
    duplicate names.
    """
    collected: dict[str, str] = {}

    def _take(arts: Any) -> None:
        if isinstance(arts, dict):
            for name, content in arts.items():
                collected[str(name)] = (
                    content if isinstance(content, str) else json.dumps(content, default=str)
                )

    for value in port_values.values():
        if not isinstance(value, dict):
            continue
        # Top-level (`return {"artifacts": {...}}`) and the python_code node's
        # shape, where the whole returned dict lands on the ``result`` port
        # (so artifacts sit under ``value["result"]["artifacts"]``).
        _take(value.get("artifacts"))
        inner = value.get("result")
        if isinstance(inner, dict):
            _take(inner.get("artifacts"))
    return collected


def _automatic_session_memory_active(plan: RuntimePlan) -> bool:
    ctx = current_run_context()
    return bool(
        plan.session_memory_store is not None
        and plan.ir.session_mode != "none"
        and ctx is not None
        and ctx.session_id
    )


def _session_memory_key(plan: RuntimePlan, agent: IRAgent) -> tuple[str, str, str] | None:
    if not _automatic_session_memory_active(plan):
        return None
    ctx = current_run_context()
    if ctx is None or not ctx.session_id:
        return None
    return (plan.ir.workflow_id, agent.node_id, ctx.session_id)


def _merge_message_history(
    stored: list[HistoryMessage],
    explicit: list[HistoryMessage],
) -> list[HistoryMessage]:
    if not stored:
        return [dict(item) for item in explicit]
    if not explicit:
        return [dict(item) for item in stored]
    overlap = 0
    max_overlap = min(len(stored), len(explicit))
    for size in range(max_overlap, 0, -1):
        if stored[-size:] == explicit[:size]:
            overlap = size
            break
    return [*(dict(item) for item in stored), *(dict(item) for item in explicit[overlap:])]


def _trim_message_history(
    history: list[HistoryMessage],
    *,
    max_messages: int = 40,
) -> list[HistoryMessage]:
    if max_messages <= 0:
        return []
    trimmed = history[-max_messages:]
    return [dict(item) for item in trimmed]


def _is_inline_target(ir: IRWorkflow, node_id: str) -> bool:
    """Whether a ``for_each`` / ``loop`` / ``error_boundary`` runs this node inline (its input is an item, not an edge)."""
    return any(
        node_id in (getattr(node, "target_node_id", None), getattr(node, "compensate_with", None))
        for node in ir.nodes.values()
    )


def _delivering_edge(
    ir: IRWorkflow,
    node_id: str,
    port: str,
    value: Any,
    port_values: dict[tuple[str, str], Any],
) -> IREdge | None:
    """The edge that delivered ``value`` to the port (the same value the node was given), or ``None``.

    Several edges may target one port (a fan-in); the value the node got is the one a delivered edge carried, so the
    edge is found by the value, not by being first in the graph.
    """
    candidates = [
        edge
        for edge in ir.edges
        if edge.to_node == node_id
        and edge.to_input == port
        and (edge.from_node, edge.from_output) in port_values
    ]
    for same in (lambda carried: carried is value, lambda carried: carried == value):
        hits = [e for e in candidates if same(port_values[(e.from_node, e.from_output)])]
        if hits:
            return hits[-1]
    return None


def _get_context_provenance(
    plan: RuntimePlan, node_id: str, output: str
) -> tuple[dict[str, Any], ...]:
    with plan.context_provenance_lock:
        return plan.context_provenance.get((node_id, output), ())


def _set_context_provenance(
    plan: RuntimePlan,
    node: IRNode,
    provenance: tuple[dict[str, Any], ...],
) -> None:
    if not provenance:
        return
    with plan.context_provenance_lock:
        for output in node.outputs:
            plan.context_provenance[(node.node_id, output)] = provenance


def _input_context_provenance(
    plan: RuntimePlan,
    ir: IRWorkflow,
    node: IRNode,
    inputs: dict[str, Any],
    port_values: dict[tuple[str, str], Any],
) -> tuple[dict[str, Any], ...]:
    """Collect the unique authority records carried by values delivered to ``node``."""
    collected: list[dict[str, Any]] = []
    for port, value in inputs.items():
        edge = _delivering_edge(ir, node.node_id, port, value, port_values)
        if edge is None:
            continue
        for item in _get_context_provenance(plan, edge.from_node, edge.from_output):
            if item not in collected:
                collected.append(item)
    return tuple(collected)


def _knowledge_context_provenance(plan: RuntimePlan, payload: Any) -> tuple[dict[str, Any], ...]:
    """Return embedded or freshly resolved decision ids for a knowledge result."""
    if not isinstance(payload, dict):
        return ()
    embedded = payload.get("context_provenance")
    if isinstance(embedded, list):
        embedded_refs = tuple(
            dict(item)
            for item in embedded
            if isinstance(item, dict)
            and isinstance(item.get("authorization_decision_id"), str)
            and isinstance(item.get("source"), dict)
        )
        if embedded_refs:
            return embedded_refs
    if plan.context_provenance_resolver is None:
        return ()
    refs: list[dict[str, Any]] = []
    for version_id in payload.get("resolved_version_ids") or ():
        decision_id = plan.context_provenance_resolver(str(version_id))
        if decision_id:
            refs.append(
                {
                    "authorization_decision_id": str(decision_id),
                    "source": {"kind": "knowledge_base", "version_id": str(version_id)},
                }
            )
    return tuple(refs)


def _agent_context_inputs(
    ir: IRWorkflow,
    node: IRAgent,
    inputs: dict[str, Any],
    port_values: dict[tuple[str, str], Any],
    agent_input: str,
    plan: RuntimePlan | None = None,
) -> tuple[AgentContextInput, ...]:
    """The one input the provider is given (``agent_input``) and where it came from.

    The provider is sent a single input (:func:`_select_input`), so that is the one recorded; the history is recorded
    separately and no other port reaches it.
    """
    port = _selected_input_port(inputs)
    if port is None:
        # Nothing was wired: a graph node is handed the run's own input, an inline target the item it is run on.
        origin_type = None if _is_inline_target(ir, node.node_id) else "start"
        step_provenance = _STEP_PROVENANCE.get()
        return (
            AgentContextInput(
                port="input",
                text=agent_input,
                origin_node_type=origin_type,
                transform=(
                    {"name": "derived", "version": "runtime-v1"} if step_provenance else None
                ),
                authorization_decision_ids=tuple(
                    str(item["authorization_decision_id"])
                    for item in step_provenance
                    if item.get("authorization_decision_id")
                ),
            ),
        )
    edge = _delivering_edge(ir, node.node_id, port, inputs[port], port_values)
    origin = ir.nodes.get(edge.from_node) if edge is not None else None
    result = port_values.get((edge.from_node, "result")) if edge is not None else None
    raw_provenance = result.get("context_provenance") if isinstance(result, dict) else None
    provenance: list[dict[str, Any]] = []
    if isinstance(raw_provenance, list):
        provenance.extend(item for item in raw_provenance if isinstance(item, dict))
    elif edge is not None and plan is not None:
        provenance.extend(_get_context_provenance(plan, edge.from_node, edge.from_output))
    else:
        provenance.extend(_STEP_PROVENANCE.get())
    transform = None
    decision_ids: tuple[str, ...] = ()
    if provenance:
        transform = {
            "name": str(origin.node_id if origin is not None else "derived"),
            "version": "runtime-v1",
        }
        decision_ids = tuple(
            str(item["authorization_decision_id"])
            for item in provenance
            if item.get("authorization_decision_id")
        )
    return (
        AgentContextInput(
            port=port,
            text=agent_input,
            origin_node_id=edge.from_node if edge is not None else None,
            origin_node_type=origin.node_type.value if origin is not None else None,
            origin_result=result if isinstance(result, dict) else None,
            transform=transform,
            authorization_decision_ids=decision_ids,
        ),
    )


def _context_provider_material(
    executor: WorkflowExecutor, agent: IRAgent
) -> tuple[tuple[dict[str, Any], ...], str]:
    """Resolve the exact provider-facing tool shape and suffix before the call.

    The legacy runtime has four provider executors with intentionally different
    tool wire shapes. Keeping this mapping at the assembly boundary makes the
    manifest digest the material that actually reaches each provider without
    coupling the manifest package to optional SDKs.
    """
    name = type(executor).__name__
    if name == "AnthropicChatWorkflowExecutor":
        specs = _anthropic_tool_specs(agent)
        suffix = _structured_output_prompt_suffix(agent)
    elif name in {"OpenAIResponsesWorkflowExecutor", "OpenAIAgentsWorkflowExecutor"}:
        specs = _openai_responses_tool_specs(agent)
        suffix = (
            _structured_output_prompt_suffix(agent)
            if name == "OpenAIAgentsWorkflowExecutor"
            else ""
        )
    else:
        specs = _openai_tool_specs(agent)
        suffix = ""
    return tuple(dict(spec) for spec in specs), suffix


def _run_agent_with_history(
    plan: RuntimePlan,
    executor: WorkflowExecutor,
    agent: IRAgent,
    input_text: str,
    *,
    context_inputs: tuple[AgentContextInput, ...] | None = None,
    explicit_history: list[HistoryMessage] | None = None,
    handoff_agents: ResolvedHandoffAgents | None = None,
    tool_callables: dict[str, Callable[..., Any]],
    preview: bool,
    governed_step: Callable[[str, list[HistoryMessage]], NoReturn] | None = None,
) -> tuple[AgentTurnResult, list[HistoryMessage], list[HistoryMessage]]:
    """Run one agent turn with its history, context record and session memory.

    ``governed_step`` (B08.2) replaces the provider call: it is given the step request id of the context just recorded and the
    history and does not return: it raises ``StepParked`` or an error (a governed step has no turn to hand back). It requires a context recorder, because a governed request
    names the digest of the context manifest the provider is given.
    """
    explicit = [dict(item) for item in (explicit_history or [])]
    key = _session_memory_key(plan, agent)
    stored: list[HistoryMessage] = []
    memory_reference: str | None = None
    memory_decision_id: str | None = None
    if key is not None and plan.session_memory_store is not None:
        if plan.context_recorder is not None:
            from caliber.workflows.session_memory import memory_reference_for  # noqa: PLC0415

            memory_reference = memory_reference_for(*key)
            if plan.memory_read_authorizer is None:
                raise ContextDenied("authority_unavailable")
            # The decision is recorded before the store is touched. A memory
            # backend therefore cannot become an authority source by itself.
            memory_decision_id = plan.memory_read_authorizer(memory_reference)
        stored = plan.session_memory_store.load_history(
            workflow_id=key[0],
            node_id=key[1],
            session_id=key[2],
        )
    history = _merge_message_history(stored, explicit)
    tool_specs, structured_suffix = _context_provider_material(executor, agent)
    occurrence_id = _STEP_OCCURRENCE.get() or agent.node_id
    attempt = _STEP_ATTEMPT.get()
    root_step_request_id: str | None = None
    root_assembly: AgentContextAssembly | None = None
    if plan.context_recorder is not None:
        # The context is recorded, authorized and validated before the provider sees any of it: a refusal raises here.
        # A native (Agents SDK) handoff runs the target inside the provider call, where the runtime cannot see it, so
        # every agent the step may hand off to is recorded first with the same input and history.
        recipients = [agent] + [
            target
            for node_id, (target, _) in (handoff_agents or {}).items()
            if node_id != agent.node_id
        ]
        for recipient in recipients:
            recipient_occurrence = (
                occurrence_id
                if recipient.node_id == agent.node_id
                else f"{occurrence_id}:handoff:{recipient.node_id}"
            )
            assembly = AgentContextAssembly(
                node_id=recipient.node_id,
                instructions=recipient.instructions,
                instruction_text=_base_instruction_text(recipient),
                skill_instructions=tuple(recipient.skill_instructions),
                history=tuple(explicit),
                inputs=context_inputs or (AgentContextInput(port="input", text=input_text),),
                occurrence_id=recipient_occurrence,
                attempt=attempt,
                memory_history=tuple(stored),
                memory_reference=memory_reference,
                memory_authorization_decision_id=memory_decision_id,
                tool_specs=tool_specs,
                structured_output_suffix=structured_suffix,
            )
            request_id = plan.context_recorder(assembly)
            if recipient.node_id == agent.node_id:
                root_step_request_id = request_id
                root_assembly = assembly
    if governed_step is not None:
        if not root_step_request_id:
            raise ToolExecutionError(
                f"governed agent step {agent.node_id!r} requires a recorded context manifest"
            )
        governed_step(root_step_request_id, history)  # parks or raises; never returns
    result = _run_agent_traced(
        executor,
        agent,
        input_text,
        history=history,
        handoff_agents=handoff_agents,
        tool_callables=tool_callables,
        preview=preview,
    )
    if plan.context_recorder is not None and root_step_request_id and root_assembly is not None:
        tool_results = tuple(item for item in result.tool_calls if isinstance(item, dict))
        if tool_results:
            from dataclasses import replace  # noqa: PLC0415

            plan.context_recorder(
                replace(
                    root_assembly,
                    step_request_id=root_step_request_id,
                    tool_results=tool_results,
                )
            )
    updated_history = _trim_message_history(
        [
            *history,
            {"role": "user", "content": input_text},
            {"role": "assistant", "content": result.final_output},
        ]
    )
    if key is not None and plan.session_memory_store is not None:
        plan.session_memory_store.save_history(
            workflow_id=key[0],
            node_id=key[1],
            session_id=key[2],
            history=updated_history,
        )
    return result, updated_history, history


#: An ``error_code`` a governed step's error may show: a fixed lowercase token (an adapter's code is never free text).
_SAFE_STEP_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _governed_tool_trust(
    context: _step_proposal.GovernedStepContext, agent: IRAgent
) -> dict[str, _step_proposal.ToolTrust]:
    """The platform's classification of every tool bound to ``agent``; a classifier that raises is the fail-closed class."""
    trust: dict[str, _step_proposal.ToolTrust] = {}
    for binding in agent.tools:
        try:
            classified = context.classify_tool(binding)
        except Exception:
            classified = _step_proposal.untrusted_tool(binding.local_name)
        if type(classified) is not _step_proposal.ToolTrust:
            classified = _step_proposal.untrusted_tool(binding.local_name)
        trust[binding.local_name] = classified
    return trust


def _governed_read_tools(
    trust: Mapping[str, _step_proposal.ToolTrust], callables: Mapping[str, Callable[..., Any]]
) -> dict[str, Callable[..., Any]]:
    """The callables of tools classified ``read``, the only ones an adapter is handed (fail closed, see ``filter_read_tools``)."""
    return _agent_step.filter_read_tools(
        callables, lambda name: name in trust and trust[name].effect_class == "read"
    )


def _handoff_closure(ir: IRWorkflow, agent_id: str) -> tuple[str, ...]:
    """``agent_id`` and every agent its handoffs can reach, directly or through others (cycle-safe, in discovery order).

    The executor follows handoffs recursively, so an agent that hands off to one that hands off to a mapped tool can reach it.
    """
    seen: dict[str, None] = {agent_id: None}
    pending = [agent_id]
    while pending:
        node = ir.nodes.get(pending.pop())
        if not isinstance(node, IRAgent):
            continue
        for handoff in node.handoffs:
            if handoff.target_node_id not in seen:
                seen[handoff.target_node_id] = None
                pending.append(handoff.target_node_id)
    return tuple(seen)


def _governs_agent(plan: RuntimePlan, node: IRNode) -> bool:
    """Whether ``node`` is an agent node that runs as a governed step in ``plan`` (the context names it, or names every agent)."""
    context = plan.governed_agent_steps
    return isinstance(node, IRAgent) and context is not None and context.governs(node.node_id)


def _governed_plan_refusal(ir: IRWorkflow, governs: Callable[[str], bool]) -> str | None:
    """Why a plan with governed agent steps cannot run, or ``None``: a payload-free reason naming only workflow node ids.

    ``governs`` says which agent nodes (by node id) run as governed steps; an agent it does not name runs as before and is not
    held to these rules, except that it may not hand off to one that is governed.

    A governed agent node parks the run behind an approval, and only a node of the graph can park it: a fan-out, loop or boundary
    runs its target inline and cannot carry the park, so an agent there would leave a proposal and an approval request nobody
    waits on. A handoff would let a *different* agent's protected tool propose through this node's step. Both are refused up
    front, before any node runs and so before any proposal exists.
    """
    for edge in ir.edges:
        if (
            isinstance(ir.nodes.get(edge.from_node), IRParallel)
            and isinstance(ir.nodes.get(edge.to_node), IRAgent)
            and governs(edge.to_node)
        ):
            return (
                f"governed agent steps cannot run in parallel branches: agent {edge.to_node!r} "
                f"follows parallel node {edge.from_node!r}"
            )
    for node in ir.nodes.values():
        if isinstance(node, IRAgent):
            if governs(node.node_id) and node.handoffs:
                return (
                    f"governed agent steps do not follow handoffs: agent {node.node_id!r} has some"
                )
            for handoff in node.handoffs:
                if governs(handoff.target_node_id):
                    return (
                        f"governed agent steps cannot run inline: agent {handoff.target_node_id!r} "
                        f"is a handoff target of agent {node.node_id!r}"
                    )
            continue
        for child_id in _inline_children(node):
            if isinstance(ir.nodes.get(child_id), IRAgent) and governs(child_id):
                return (
                    f"governed agent steps cannot run inline: agent {child_id!r} is a target of "
                    f"{node.node_type.value} node {node.node_id!r}"
                )
    return None


def _governed_stop_error(result: _agent_step.AgentStepResult) -> ToolExecutionError:
    """The run error for a step that stopped without completing or proposing: its stop reason and fixed error code, no payload.

    A limit stop (``max_turns``, ``max_tool_calls``, ``max_tokens``, ``deadline``) also records the usage the step is charged,
    as numbers, so the run's error says what the step spent (B08.4 settles it from the result, not from this text).
    """
    code = result.error_code
    detail = result.stop_reason
    if isinstance(code, str) and _SAFE_STEP_CODE.fullmatch(code):
        detail = f"{detail} ({code})"
    if result.stop_reason in _agent_step.LIMIT_STOPS:
        usage = result.usage
        detail = f"{detail} (turns={usage.turns} tool_calls={usage.tool_calls} tokens={usage.total_tokens})"
    return ToolExecutionError(f"governed agent step stopped: {detail}")


def _governed_limits(
    context: _step_proposal.GovernedStepContext, agent: IRAgent
) -> _agent_step.StepLimits:
    """The step's limits: the producer's, brought down to the platform's ceilings; a clock or a producer that fails is a fixed refusal."""
    try:
        now = _agent_step.read_instant(_agent_step.resolve_clock(context.clock))
    except Exception:
        raise ToolExecutionError("governed agent step clock unavailable: clock_invalid") from None
    try:
        return _agent_step.cap_limits(context.limits(agent), context.ceilings, now=now)
    except Exception:
        raise ToolExecutionError("governed agent step limits unavailable: limits_invalid") from None


def _reserve_step_tokens(
    context: _step_proposal.GovernedStepContext,
    executor: WorkflowExecutor,
    request: _agent_step.AgentStepRequest,
) -> _step_reservation.StepReservation | None:
    """Reserve the step's token ceiling before its adapter is called (B08.4), or ``None`` for a step that will not call one.

    A step the platform stops before work (:func:`~caliber.workflows.agent_step.before_work_stop`: no turn or token, a deadline
    already past) and an executor that is not an adapter never reach a provider, so they reserve nothing. Every other step does,
    and the reservation is taken here, before any adapter code runs. A budget that is exhausted, a store that fails and a
    reservation this step already holds (an earlier call of the same run, node, occurrence and attempt may have spent: the platform
    will not call the provider again for it) are fixed-code run errors, and the adapter is never called.
    """
    if (
        not isinstance(executor, _agent_step.AgentStepAdapter)
        or _agent_step.before_work_stop(request, clock=context.clock) is not None
    ):
        return None
    try:
        reservation = _step_reservation.reserve_step_tokens(
            context.session_factory, request, run_budget_tokens=context.run_token_budget
        )
    except _step_reservation.ReservationRefused as exc:
        raise ToolExecutionError(f"governed agent step token reservation refused: {exc}") from None
    except Exception:
        # A store failure carries the statement's parameters; none of it reaches the run error, only a fixed code.
        raise ToolExecutionError(
            "governed agent step token reservation refused: store_failed"
        ) from None
    if not reservation.created:
        raise ToolExecutionError(
            f"governed agent step token reservation refused: {_step_reservation.RESERVATION_REPLAYED}"
        )
    return reservation


def _settle_step_tokens(
    context: _step_proposal.GovernedStepContext,
    reservation: _step_reservation.StepReservation,
    result: _agent_step.AgentStepResult,
) -> None:
    """Settle the step's reservation from its result, once. A failure here keeps the reservation open (counted in full).

    The provider has been called and the result is in hand, so a settlement the store cannot take is not a reason to discard it:
    the reservation stays ``reserved`` and counts at its whole ceiling until the janitor finalizes it, which can only overcharge,
    never undercharge. Only the exception's class is logged.
    """
    try:
        _step_reservation.settle_step_tokens(context.session_factory, reservation, result)
    except Exception as exc:
        logger.warning(
            "governed agent step %s: token settlement failed (%s); the reservation stays open at its whole ceiling",
            reservation.step_request_id,
            type(exc).__name__,
        )


def _run_governed_step(
    context: _step_proposal.GovernedStepContext,
    executor: WorkflowExecutor,
    agent: IRAgent,
    input_text: str,
    *,
    step_request_id: str,
    history: list[HistoryMessage],
    tool_callables: dict[str, Callable[..., Any]],
    binder: ToolProposalBinder | None = None,
) -> NoReturn:
    """One agent turn as a governed step: the platform, not the adapter, decides what runs and what is recorded (B08.2).

    The adapter is handed only the read-class tools. A step that completes is refused (the v1 result carries no text, so
    there is no output to publish). A step that proposes a protected action has it bound to the inventory and recorded with its approval request, and
    ``StepParked`` is raised so the node parks. Anything else (a limit, a refusal, a provider failure) is a run error carrying
    only the stop reason and a fixed code.
    """
    trust = _governed_tool_trust(context, agent)
    request = _agent_step.AgentStepRequest(
        step_request_id=step_request_id,
        workflow_run_id=context.workflow_run_id,
        node_id=agent.node_id,
        occurrence_id=_STEP_OCCURRENCE.get() or agent.node_id,
        attempt=_STEP_ATTEMPT.get(),
        run_manifest_digest=context.run_manifest_digest,
        context_manifest_digest=context.context_manifest_digest(step_request_id),
        grant_id=context.grant_id,
        limits=_governed_limits(context, agent),
    )
    step_input = _agent_step.AgentStepInput(
        agent=agent,
        input_text=input_text,
        history=tuple(history),
        read_tools=_governed_read_tools(trust, tool_callables),
    )
    reservation = _reserve_step_tokens(context, executor, request)
    # Nothing between the reservation and the settlement may release it: an exception or a cancellation out of the adapter leaves
    # it open, counted in full, for the owner janitor to finalize (B08.4); only the result of a step that returned settles it.
    result = _agent_step.run_agent_step(executor, request, step_input, clock=context.clock)
    if reservation is not None:
        _settle_step_tokens(context, reservation, result)
    if result.stop_reason == "proposed_action":
        try:
            proposal = _step_proposal.record_step_proposal(
                context, request, result, trust, binder=binder, bindings=agent.tools
            )
        except EffectRefused as exc:
            raise ToolExecutionError(f"governed agent step proposal refused: {exc.code}") from None
        except Exception:
            # A store failure (a deadlock, a lock timeout) carries the statement's parameters, which are the model's arguments:
            # none of it reaches the run error or a log line, only a fixed code.
            raise ToolExecutionError("governed agent step proposal failed: store_failed") from None
        raise _step_proposal.StepParked(
            proposal, step_request_id=step_request_id, usage=result.usage
        )
    if result.stop_reason != "completed":
        raise _governed_stop_error(result)
    # The v1 result carries no text, so a step that completes without proposing has produced nothing the graph could use:
    # publishing an empty output would let the run go on as though the agent had answered. Fail closed until the contract has
    # an output field.
    raise ToolExecutionError(
        "governed agent step completed without an output channel: the v1 step result carries no text"
    )


def _governed_parked_step(
    node: IRAgent,
    proposal: _step_proposal.StepProposal,
    *,
    step_request_id: str | None,
    usage: _agent_step.StepUsage | None,
) -> tuple[int, NodeStep]:
    """The blocked step of a node waiting for approval of its proposed action: identifiers and usage, never the action.

    The worker turns it into a ``waiting_approval`` run whose checkpoint is the ``runtime_approval`` kind the decision and resume
    routes already accept, naming the approval request that already exists (it creates no second one).
    """
    tokens = usage.total_tokens if usage is not None else 0
    return tokens, NodeStep(
        node.node_id,
        node.node_type.value,
        "blocked",
        tokens=tokens,
        prompt_tokens=usage.prompt_tokens if usage is not None else 0,
        completion_tokens=usage.completion_tokens if usage is not None else 0,
        cached_prompt_tokens=(usage.cached_prompt_tokens or 0) if usage is not None else 0,
        detail=f"waiting_approval:{node.node_id}",
        checkpoint_state={
            "kind": "runtime_approval",
            "node_id": node.node_id,
            "proposal_id": proposal.proposal_id,
            "approval_request_id": proposal.approval_request_id,
            "intent_hash": proposal.intent_hash,
            "step_request_id": step_request_id,
            "reason": "effect_proposal_awaiting_approval",
            "resume_policy": "same_run_same_occurrence",
        },
    )


def _publish_governed_outputs(
    node: IRAgent,
    port_values: dict[tuple[str, str], Any],
    output: str,
    history: list[HistoryMessage],
) -> None:
    nid = node.node_id
    if not node.outputs:
        port_values[(nid, "final_output")] = output
        return
    _publish_declared_outputs(
        node,
        port_values,
        {"final_output": output, "tool_calls": [], "history": history},
        fallback=output,
    )
    port_values.setdefault((nid, "final_output"), output)


def _completed_governed_step(
    node: IRAgent, port_values: dict[tuple[str, str], Any], proposal_id: str
) -> tuple[int, NodeStep]:
    """The finished node of a governed step whose proposed effect committed: it names the effect, never its arguments."""
    committed = f"effect {proposal_id} committed"
    _publish_governed_outputs(node, port_values, committed, [])
    return 0, NodeStep(node.node_id, node.node_type.value, "ok", output=committed)


#: Proposal states in which the dispatcher (or the reconciler) holds the effect and the node must wait for it.
_OPEN_EFFECT_STATES = frozenset({"approved", "dispatched", "unknown", "reconciled"})


def _dispatch_approved_step(
    context: _step_proposal.GovernedStepContext,
    node: IRAgent,
    prior: _step_proposal.PriorProposal,
    port_values: dict[tuple[str, str], Any],
) -> tuple[int, NodeStep] | None:
    """Send a governed step's proposal once its exact approval is decided (#1051), or ``None`` to park behind the request.

    The producer's ``dispatch_approved`` does the whole crossing (the exact approval, the execution-owner lease, the backend and
    the downstream it is bound to) through the dispatcher, which consumes the approval and claims the dispatch in one
    transaction before anything is sent. A context without one sends nothing: the proposal stays ``proposed`` and the node keeps
    parking, as before the producer. A dispatcher refusal is a run error carrying only its fixed code. An outcome short of
    ``committed`` (the answer was ambiguous, the receipt could not be read back) is the existing effect-reconciliation park: the
    run waits for the reconciler, and the node is never sent a second time. If the dispatch raised after it may have sent, the
    proposal is read again before the run fails, so an effect that is now ``dispatched`` or ``unknown`` parks instead.
    """
    dispatcher = context.dispatch_approved
    if dispatcher is None:
        return None
    nid = node.node_id

    def settled_after_failure(failure: ToolExecutionError) -> tuple[int, NodeStep]:
        """What a dispatch that raised left behind: the proposal is read again before the run is failed.

        A refusal or an exception can come *after* the downstream was called (a settlement the lease no longer allows, a
        verifier that raised, a store that went away): the dispatcher then leaves the proposal ``dispatched`` or ``unknown``, and
        failing the run would hide an effect that was sent. So an open proposal parks the run for the reconciler, a committed one
        completes the node, and only a proposal that never moved (``proposed``: nothing was claimed) is the failure. None of the
        exception's text, which can carry the statement's parameters, reaches the run error.
        """
        try:
            current = _step_proposal.prior_proposal(
                context.session_factory, context.workflow_run_id, nid, _STEP_OCCURRENCE.get() or nid
            )
        except Exception:
            current = None
        if current is not None and current.state in _OPEN_EFFECT_STATES:
            raise UnknownEffectError([(current.proposal_id, nid, current.state)]) from None
        if current is not None and current.state == "committed":
            return _completed_governed_step(node, port_values, current.proposal_id)
        raise failure from None

    try:
        outcome = dispatcher(prior)
    except EffectRefused as exc:
        return settled_after_failure(
            ToolExecutionError(f"governed agent step dispatch refused: {exc.code}")
        )
    except Exception:
        return settled_after_failure(
            ToolExecutionError("governed agent step dispatch failed: store_failed")
        )
    if outcome is None:
        return None
    if outcome.state == "committed":
        return _completed_governed_step(node, port_values, prior.proposal_id)
    if outcome.state == "failed":
        raise ToolExecutionError("governed agent step: its proposed effect failed")
    raise UnknownEffectError([(prior.proposal_id, nid, outcome.state)])


def _run_governed_agent_node(
    node: IRAgent,
    ir: IRWorkflow,
    plan: RuntimePlan,
    executor: WorkflowExecutor,
    *,
    inputs: dict[str, Any],
    run_input: str,
    port_values: dict[tuple[str, str], Any],
    preview: bool,
    extra_tools: dict[str, Callable[..., Any]] | None,
) -> tuple[int, NodeStep]:
    """Run an agent node as a governed step (``plan.governed_agent_steps``); see :func:`_run_governed_step`.

    A node occurrence has one logical effect: if one is already recorded (the step was resumed or retried) the model is **not**
    asked again. ``committed`` completes the node, ``failed`` fails it, ``proposed`` parks it behind its (re-asked) approval
    request, and any state in between (``approved``, ``dispatched``, ``unknown``, ``reconciled``: the dispatcher has it) is the
    existing effect-reconciliation park.
    """
    context = plan.governed_agent_steps
    assert context is not None
    nid = node.node_id
    prior = _step_proposal.prior_proposal(
        context.session_factory,
        context.workflow_run_id,
        nid,
        _STEP_OCCURRENCE.get() or nid,
    )
    if prior is not None:
        if prior.state == "committed":
            return _completed_governed_step(node, port_values, prior.proposal_id)
        if prior.state == "failed":
            raise ToolExecutionError("governed agent step: its proposed effect failed")
        if prior.state == "proposed":
            dispatched = _dispatch_approved_step(context, node, prior, port_values)
            if dispatched is not None:
                return dispatched
            try:
                proposal = _step_proposal.reask_approval(context, prior)
            except EffectRefused as exc:
                raise ToolExecutionError(
                    f"governed agent step approval refused: {exc.code}"
                ) from None
            except Exception:
                raise ToolExecutionError(
                    "governed agent step approval failed: store_failed"
                ) from None
            return _governed_parked_step(node, proposal, step_request_id=None, usage=None)
        raise UnknownEffectError([(prior.proposal_id, nid, prior.state)])
    agent_input = _select_input(inputs, run_input)
    callables = _resolve_tool_callables(
        node,
        plan.resolver,
        preview=preview,
        egress_policy=plan.egress_policy,
        mcp_identity=plan.mcp_identity,
    )
    if extra_tools:
        callables = {**callables, **extra_tools}
    callables = _gate_model_callables(plan, node, callables, preview=preview)
    try:
        _run_agent_with_history(
            plan,
            executor,
            node,
            agent_input,
            context_inputs=(
                _agent_context_inputs(ir, node, inputs, port_values, agent_input, plan)
                if plan.context_recorder is not None
                else None
            ),
            explicit_history=_normalize_message_history(inputs.get("history")),
            tool_callables=callables,
            preview=preview,
            governed_step=lambda step_request_id, history: _run_governed_step(
                context,
                executor,
                node,
                agent_input,
                step_request_id=step_request_id,
                history=history,
                tool_callables=callables,
                binder=_RuntimeToolBinder(plan, nid),
            ),
        )
    except _step_proposal.StepParked as parked:
        return _governed_parked_step(
            node, parked.proposal, step_request_id=parked.step_request_id, usage=parked.usage
        )
    # A governed step parks (above) or raises: it never produces a turn the graph could use.
    raise AssertionError("a governed step returned a turn")  # pragma: no cover


def _collect_agent_handoff_specs(
    agent: IRAgent,
    ir: IRWorkflow,
    plan: RuntimePlan,
    *,
    preview: bool,
    root_tool_callables: dict[str, Callable[..., Any]],
    extra_tools: dict[str, Callable[..., Any]] | None = None,
) -> ResolvedHandoffAgents | None:
    if not agent.handoffs:
        return None
    specs: ResolvedHandoffAgents = {}

    def _callables_for(agent_def: IRAgent) -> dict[str, Callable[..., Any]]:
        if agent_def.node_id == agent.node_id:
            return dict(root_tool_callables)
        callables = _resolve_tool_callables(
            agent_def,
            plan.resolver,
            preview=preview,
            egress_policy=plan.egress_policy,
            mcp_identity=plan.mcp_identity,
        )
        if extra_tools:
            callables = {**callables, **extra_tools}
        return _gate_model_callables(plan, agent_def, callables, preview=preview)

    def _visit(agent_def: IRAgent) -> None:
        if agent_def.node_id in specs:
            return
        specs[agent_def.node_id] = (agent_def, _callables_for(agent_def))
        for handoff in agent_def.handoffs:
            target = ir.nodes.get(handoff.target_node_id)
            if isinstance(target, IRAgent):
                _visit(target)

    _visit(agent)
    if len(specs) <= 1:
        return None
    return specs


def _resolve_agent_handoff_target(  # noqa: PLR0911 - explicit priority order
    agent: IRAgent,
    result: AgentTurnResult,
    ir: IRWorkflow,
    *,
    input_text: str,
    history: list[HistoryMessage] | None = None,
) -> str | None:
    if result.handoffs_resolved_in_executor:
        return None
    raw_target = str(result.handoff_target or "").strip()
    if raw_target:
        declared = next(
            (handoff for handoff in agent.handoffs if handoff.target_node_id == raw_target),
            None,
        )
        if declared is None:
            logger.warning(
                "workflow agent %s returned undeclared handoff target %r; ignoring it",
                agent.node_id,
                raw_target,
            )
            return None
        if not _handoff_condition_enabled(
            declared.condition,
            input_text=input_text,
            history=history,
            extra={"turn_input": [*(history or []), {"role": "user", "content": input_text}]},
        ):
            logger.debug(
                "workflow agent %s selected disabled handoff target %r; ignoring it",
                agent.node_id,
                raw_target,
            )
            return None
        target_node = ir.nodes.get(raw_target)
        if not isinstance(target_node, IRAgent):
            logger.warning(
                "workflow agent %s selected non-agent handoff target %r; ignoring it",
                agent.node_id,
                raw_target,
            )
            return None
        return raw_target

    if len(agent.handoffs) != 1:
        return None

    fallback = agent.handoffs[0]
    if not _handoff_condition_enabled(
        fallback.condition,
        input_text=input_text,
        history=history,
        extra={"turn_input": [*(history or []), {"role": "user", "content": input_text}]},
    ):
        return None

    target_node = ir.nodes.get(fallback.target_node_id)
    if not isinstance(target_node, IRAgent):
        logger.warning(
            "workflow agent %s declares non-agent handoff target %r; ignoring it",
            agent.node_id,
            fallback.target_node_id,
        )
        return None

    return fallback.target_node_id


_INLINE_ORCHESTRATION_TARGET_LABEL = "agent, subworkflow, tool, mcp_resource, knowledge_query, knowledge_build, template, data_transform, review_queue_enqueue, python_code, external_app, webhook, or api_request"
_INLINE_ORCHESTRATION_TARGET_TYPES = (
    IRAgent,
    IRSubworkflow,
    IRTool,
    IRMcpResource,
    IRKnowledgeQuery,
    IRKnowledgeBuild,
    IRTemplate,
    IRDataTransform,
    IRReviewQueueEnqueue,
    IRPythonCode,
    IRExternalApp,
    IRWebhook,
    IRApiRequest,
)

# First safe slice of true branch concurrency for explicit ``parallel`` fan-out:
# only direct children with exactly one incoming edge from the processed
# ``parallel`` node are batch-executed, and only for node types whose execution
# is self-contained enough to run against a snapshot of the current port state.
_PARALLEL_BRANCH_BATCH_TARGET_TYPES = (
    IRAgent,
    IRSubworkflow,
    IRTool,
    IRMcpResource,
    IRKnowledgeQuery,
    IRKnowledgeBuild,
    IRTemplate,
    IRDataTransform,
    IRPythonCode,
    IRExternalApp,
    IRWebhook,
    IRApiRequest,
    IRWaitUntil,
    IRWaitForEvent,
    IRErrorBoundary,
    IRFileInput,
    IRFolderInput,
    IRInputBucket,
)


def _check_egress(plan: RuntimePlan, request: Any) -> None:
    """Apply outbound egress policy to a webhook/api_request destination.

    Raises :class:`ToolExecutionError` so the node reports a normal runtime failure
    with the reason, rather than an opaque connection error the operator has to guess
    at.
    """
    url = request.get("url") if isinstance(request, dict) else None
    if not isinstance(url, str) or not url:
        return
    from caliber.egress import EgressBlockedError, EgressPolicy, check_url  # noqa: PLC0415

    policy = plan.egress_policy if plan.egress_policy is not None else EgressPolicy()
    try:
        check_url(url, policy)
    except EgressBlockedError as exc:
        raise ToolExecutionError(str(exc)) from exc


def _perform_guarded_effect(
    plan: RuntimePlan,
    *,
    node_id: str,
    request: Any,
    perform: Callable[[], Any],
) -> Any:
    """Perform an external effect at most once per (run, node, inputs).

    Without a ledger this is a plain call, which is correct for the paths that
    cannot be restarted (preview/eval refuse effect nodes) — the guard exists for
    the queued worker, where an expired lease restarts the run from the beginning
    and every effectful node would otherwise re-fire.

    A definitive failure is recorded as ``failed`` so a retry is allowed. An
    *ambiguous* failure — one where the request may already have reached the remote
    system — deliberately leaves the claim ``in_progress``, so the next attempt
    reports it as indeterminate instead of silently repeating the effect.
    """
    # Egress policy first: a blocked destination must not consume a ledger claim,
    # or a refused request would look like an in-flight effect on the next attempt.
    _check_egress(plan, request)
    ledger = plan.effect_ledger
    if ledger is None:
        return perform()

    from caliber.workflows.effect_ledger import IndeterminateEffectError  # noqa: PLC0415

    try:
        claim = ledger.claim(node_id=node_id, payload=request)
    except IndeterminateEffectError as exc:
        raise ToolExecutionError(str(exc)) from exc
    if not claim.fresh:
        return claim.replayed_result

    try:
        result = perform()
    except _DEFINITIVE_EFFECT_FAILURES as exc:
        # The request never went out, so the effect did not take.
        ledger.record_failure(claim.key, f"{type(exc).__name__}: {exc}")
        raise
    ledger.record_success(claim.key, result if isinstance(result, dict) else {"response": result})
    return result


def _declared_http_effect_class(node: IRNode) -> str:
    """Read a node-declared class, using the B04.2 reversible default.

    ``submit_proposal`` still applies the protected-effect inventory's
    fail-closed classification. The runtime value is therefore a claim from
    the node, while the persisted proposal remains inventory-bound.
    """
    value = getattr(node, "effect_class", None)
    return value if isinstance(value, str) and value.strip() else "reversible_write"


def _legacy_effects_allowed(plan: RuntimePlan) -> bool:
    """Whether a protected node of this plan may take the pre-dispatcher (development-profile) path (#1068).

    The one place that decides it, for the HTTP, registered-tool and OpenAPI routes and for the legacy OpenAPI binding: the
    plan must carry ``legacy_guarded_effects`` (``build_plan`` sets it only for the development profile) **and** have no
    governed context bound. A bound context is always used, so this is never true next to one. It relaxes only the refusal
    for a missing context; every other refusal of a route is unchanged.
    """
    return plan.legacy_guarded_effects and plan.governed_effects is None


def _perform_http_effect(
    plan: RuntimePlan,
    *,
    node: IRNode,
    node_id: str,
    request: dict[str, Any],
) -> Any:
    """Route a manifest HTTP node through proposal, approval and dispatch.

    Plans without a workflow version are the legacy/test execution surface and
    retain the existing ledger behavior, and so does a manifest-backed plan with
    no context whose ``legacy_guarded_effects`` is set (the development profile,
    #1068; it is not the governed path). Otherwise a manifest-backed plan must
    provide a complete governed context; missing authority or receipt
    verification is a refusal, never an implicit allow or a raw sender call.
    """
    sender = plan.webhook_sender or partial(_default_webhook_sender, policy=plan.egress_policy)
    if plan.governed_effects is None:
        if plan.workflow_version_id is None or _legacy_effects_allowed(plan):
            return _perform_guarded_effect(
                plan, node_id=node_id, request=request, perform=lambda: sender(request)
            )
        raise ToolExecutionError(
            f"{node_id!r} requires a governed effect context for manifest HTTP execution"
        )

    context = plan.governed_effects
    if not callable(context.authorize) or not callable(context.receipt_verifier):
        raise ToolExecutionError("governed HTTP effect context is incomplete")
    _check_egress(plan, request)
    proposal_args = _json_compatible(request)
    if not isinstance(proposal_args, dict):
        raise ToolExecutionError("governed HTTP effect request is not JSON-compatible")
    occurrence_id = _STEP_OCCURRENCE.get() or node_id
    return _submit_authorize_dispatch(
        context,
        label="HTTP effect",
        proposal={
            "node_id": node_id,
            "occurrence_id": occurrence_id,
            "connector": HTTP_CONNECTOR,
            "connector_version": HTTP_VERSION,
            "effect_class_name": _declared_http_effect_class(node),
            "resource": request["url"],
            "args": proposal_args,
        },
        backend_kind="http",
        backend_config={"sender": sender, "receipt_verifier": context.receipt_verifier},
    )


def _submit_authorize_dispatch(
    context: GovernedEffectContext,
    *,
    label: str,
    proposal: dict[str, Any],
    backend_kind: str,
    backend_config: dict[str, Any],
) -> dict[str, Any]:
    """Record one proposal, obtain its exact authorization, dispatch it, and return the committed response.

    The one governed crossing shared by every runtime caller of the B04 dispatcher (HTTP nodes, registry tools). It
    refuses at each step with a payload-free ``ToolExecutionError`` naming only ``label`` and the dispatcher's refusal
    code: a missing or malformed authorization, a proposal the dispatcher refuses, a dispatch it refuses, or any outcome
    short of ``committed`` (the effect may still have happened; the proposal stays recoverable by the reconciler).

    While the exact approval is still pending, ``authorize`` raises :class:`~caliber.effects.step_proposal.EffectApprovalPending`;
    it propagates untouched (it is not a refusal) and the interpreter parks the node behind the bound request. A retry or a
    resume records the same proposal again (one logical effect per node occurrence) and asks again.
    """
    if (
        context.dispatchable_backends is not None
        and backend_kind not in context.dispatchable_backends
    ):
        raise ToolExecutionError(f"{label} refused: no_authoritative_verifier")
    try:
        with context.session_factory() as session:
            recorded = submit_proposal(
                session,
                workflow_run_id=context.workflow_run_id,
                **proposal,
                admission=admit_backend(backend_kind),
            )
            session.commit()
    except EffectRefused as exc:
        raise ToolExecutionError(f"{label} proposal refused: {exc.code}") from exc
    if recorded.state != "proposed":
        # A retry of a node whose effect was already claimed or settled (for example ``unknown``): asking for a new
        # approval and dispatching again could only refuse, and would hide what really happened to the first attempt.
        raise ToolExecutionError(f"{label} was already recorded as {recorded.state}; not run again")

    try:
        authorization = context.authorize(recorded)
    except _step_proposal.EffectApprovalPending:
        raise
    except EffectRefused as exc:
        # A fixed code (no lease, a rejected request, ...): the reason a person can act on, never a value.
        raise ToolExecutionError(f"{label} authorization refused: {exc.code}") from exc
    except Exception as exc:
        raise ToolExecutionError(f"{label} authorization failed: {type(exc).__name__}") from exc
    if not isinstance(authorization, EffectAuthorization):
        raise ToolExecutionError(f"{label} authorization was not granted")

    try:
        outcome = dispatch(
            context.session_factory,
            recorded.proposal_id,
            approval_id=authorization.approval_id,
            approver_scopes=authorization.approver_scopes,
            lease=authorization.lease,
            backend_kind=backend_kind,
            backend_config={**context.backend_config, **backend_config},
            current_resource_version=authorization.current_resource_version,
            evidence_hash=authorization.evidence_hash,
            approval_validator=approval_valid_at_commit,
        )
    except EffectRefused as exc:
        raise ToolExecutionError(f"{label} dispatch refused: {exc.code}") from exc
    if outcome.state != "committed":
        raise ToolExecutionError(f"{label} did not commit: {outcome.reason or outcome.state}")
    if outcome.response is None:
        raise ToolExecutionError(f"{label} committed without a transport response")
    return dict(outcome.response)


def _read_registry_identity(
    plan: RuntimePlan, binding: IRToolBinding
) -> RegisteredToolIdentity | None:
    """The registry row of the tool a binding names, read by ``(resolved_name, resolved_version)``, or ``None`` when it names none.

    The one reader of a tool's trusted identity for the node routes (registered, OpenAPI) and for a model's proposal: the row,
    never the manifest binding, says what a tool is. A reader that fails is a typed refusal that names only the exception's class.
    """
    lookup = plan.registered_tool_identity
    if lookup is None or not binding.resolved_name or not binding.resolved_version:
        return None
    try:
        return lookup(binding.resolved_name, binding.resolved_version)
    except Exception as exc:
        raise ToolRouteRefused(
            REGISTRY_ROW_UNREADABLE,
            f"registered tool {binding.local_name!r} identity could not be read: "
            f"{type(exc).__name__}",
        ) from exc


def _governed_tool_route(
    plan: RuntimePlan, binding: IRToolBinding, *, node_id: str, preview: bool
) -> ToolRoute | None:
    """The dispatcher route of a registered Python tool node, or ``None`` when it runs inline as before.

    Only a plan wired to the registry (an identity reader, as ``build_plan`` binds) or to an authority (a governed
    context) is governed: a plan that has neither, such as a standalone export or a test plan, keeps calling the tool
    directly, as do previews, which never reach a real effect. OpenAPI bindings are routed by
    :func:`_governed_openapi_route` (B04.4) and MCP bindings by their own B04 task. A protected tool with no governed
    context is refused, except in a plan whose ``legacy_guarded_effects`` is set (the development profile, #1068), where it
    runs inline as before the dispatcher. The decision is made from the
    tool's **registry row** (``plan.registered_tool_identity``), never from ``binding.side_effect_level`` or the
    binding's names, which a workflow or an export author wrote.
    """
    if (
        preview
        or binding.binding_type != "registered_function"
        or binding.execution_backend != "python_callable"
        or (plan.registered_tool_identity is None and plan.governed_effects is None)
    ):
        return None
    identity = _read_registry_identity(plan, binding)
    if identity is not None and not _registered_row_matches(binding, identity):
        # The code the binding would run is not the code the registry row describes.
        raise ToolRouteRefused(
            REGISTRY_ROW_MISMATCH,
            f"registered tool {binding.local_name!r} does not match its registry row",
        )
    route = route_registered_tool(identity)
    if route.route == REFUSE:
        raise ToolRouteRefused(
            UNSUPPORTED_EFFECT_PATH,
            f"registered tool {binding.local_name!r} refused: unsupported_effect_path",
        )
    if route.route == INLINE:
        return None
    if plan.governed_effects is None:
        if _legacy_effects_allowed(plan):
            # Development profile, no authority bound (#1068): the pre-dispatcher behavior. The tool runs inline behind the
            # node's own approval gate; there is no proposal, exact-intent approval or lease.
            return None
        raise ToolRouteRefused(
            GOVERNED_CONTEXT_REQUIRED,
            f"{node_id!r} requires a governed effect context to run protected registered tool "
            f"{binding.local_name!r}",
        )
    return route


def _perform_registered_tool_effect(
    plan: RuntimePlan,
    *,
    node_id: str,
    binding: IRToolBinding,
    route: ToolRoute,
    fn: Callable[..., Any],
    arguments: dict[str, Any],
    fallback_input: str,
) -> Any:
    """Run a protected registry tool through proposal, approval and dispatch (B04.3, #741).

    ``fn`` is the already-bound tool, without retries: a retried write could repeat its effect, and the dispatcher
    never sends an ambiguous effect twice. It is called by the ``registered_tool`` backend with the arguments recorded in
    the proposal, only after the exact approval and the execution-owner lease are rechecked.
    """
    context = plan.governed_effects
    assert context is not None  # _governed_tool_route guarantees it
    if not callable(context.authorize) or not callable(context.receipt_verifier):
        raise ToolExecutionError("governed registered-tool effect context is incomplete")
    name = binding.resolved_name or binding.local_name
    version = binding.resolved_version
    proposal_args = _json_compatible(arguments)

    def invoker(recorded_arguments: dict[str, Any], recorded_input: str) -> Any:
        return _call_tool(fn, recorded_arguments, fallback_input=recorded_input)

    return _submit_authorize_dispatch(
        context,
        label="registered tool effect",
        proposal={
            "node_id": node_id,
            "occurrence_id": _STEP_OCCURRENCE.get() or node_id,
            "connector": REGISTERED_TOOL_CONNECTOR,
            "connector_version": REGISTERED_TOOL_VERSION,
            "min_effect_class": route.effect_class,
            "resource": tool_resource(name, version),
            "args": tool_intent_args(
                name=name, version=version, arguments=proposal_args, input_text=fallback_input
            ),
            "tool_name": name,
            "caliber_defined": route.trusted,
        },
        backend_kind="registered_tool",
        backend_config={"invoker": invoker, "receipt_verifier": context.receipt_verifier},
    )["result"]


@dataclass(frozen=True)
class _GovernedOpenApi:
    """A protected OpenAPI call's route and its trusted operation (the registry row's own configuration)."""

    route: ToolRoute
    operation: _openapi_http.OpenApiOperation


@dataclass(frozen=True)
class _OpenApiDecision:
    """An OpenAPI call's trusted row and its route, before the question of who may carry a protected one."""

    identity: RegisteredToolIdentity
    route: ToolRoute


def _openapi_decision(
    plan: RuntimePlan, binding: IRToolBinding, *, arguments: dict[str, Any]
) -> _OpenApiDecision:
    """Classify one OpenAPI tool call from its registry row (B04.4, #750); the one place both a node and a model's call do it.

    The call is classified from the tool's **registry row** (``plan.registered_tool_identity``): its declared level and the
    method its own configuration selects, never the binding's level, names or configuration. The binding must carry exactly
    the row's execution configuration and input schema: a binding that differs would run something the registry does not
    describe, so it is refused, inline reads included. A tool whose row cannot be read cannot be proposed (there is no
    trusted operation to send) and is refused. Whether a *protected* call may then be carried is the caller's decision
    (:func:`_governed_openapi_route` for a node, the loop gate and the proposal binder for a model's call).
    """
    name = binding.local_name
    identity = _read_registry_identity(plan, binding)
    if identity is None:
        raise ToolRouteRefused(
            UNSUPPORTED_EFFECT_PATH, f"OpenAPI tool {name!r} refused: unsupported_effect_path"
        )
    if (
        identity.execution_backend != binding.execution_backend
        or identity.backend_config != binding.backend_config
        or identity.input_schema != binding.input_schema
    ):
        # The configuration the binding would send is not the one the registry row describes.
        raise ToolRouteRefused(
            REGISTRY_ROW_MISMATCH, f"registered tool {name!r} does not match its registry row"
        )
    try:
        method = openapi_operation_method(
            dict(identity.backend_config or {}), _json_compatible(arguments)
        )
    except OpenApiPreflightError as exc:
        raise ToolRouteRefused(exc.code, f"OpenAPI tool {name!r} refused: {exc.code}") from exc
    route = route_openapi_tool(identity, method=method)
    if route.route == REFUSE:
        raise ToolRouteRefused(
            UNSUPPORTED_EFFECT_PATH, f"OpenAPI tool {name!r} refused: unsupported_effect_path"
        )
    return _OpenApiDecision(identity, route)


def _governed_openapi_route(
    plan: RuntimePlan,
    binding: IRToolBinding,
    *,
    node_id: str,
    preview: bool,
    arguments: dict[str, Any],
    overridden: bool,
) -> _GovernedOpenApi | None:
    """The dispatcher route of an OpenAPI tool node, or ``None`` when it runs inline as before (B04.4, #750).

    As for a registered Python tool, only a plan wired to the registry (an identity reader) or to an authority (a governed
    context) is governed, and a preview never reaches a real effect. The classification is :func:`_openapi_decision`'s; a
    protected call that an injected callable replaces is refused, which no proposal could describe.
    """
    if preview or (plan.registered_tool_identity is None and plan.governed_effects is None):
        return None
    name = binding.local_name
    decision = _openapi_decision(plan, binding, arguments=arguments)
    identity, route = decision.identity, decision.route
    if route.route == INLINE:
        return None
    if plan.governed_effects is None:
        if _legacy_effects_allowed(plan):
            # Development profile, no authority bound (#1068): the pre-dispatcher behavior, the call runs inline.
            return None
        raise ToolRouteRefused(
            GOVERNED_CONTEXT_REQUIRED,
            f"{node_id!r} requires a governed effect context to run protected OpenAPI tool {name!r}",
        )
    if overridden:
        raise ToolRouteRefused(
            TOOL_REPLACED,
            f"OpenAPI tool {name!r} is replaced by an injected callable and cannot be governed",
        )
    return _GovernedOpenApi(route, _openapi_operation(plan, identity))


def _openapi_operation(
    plan: RuntimePlan, identity: RegisteredToolIdentity
) -> _openapi_http.OpenApiOperation:
    """The trusted operation a registry row describes: its own configuration and schema, the plan's egress policy."""
    return _openapi_http.OpenApiOperation(
        tool=identity.name,
        version=identity.version,
        execution_config=identity.backend_config or {},
        input_schema=identity.input_schema,
        egress_policy=plan.egress_policy,
    )


def _perform_openapi_tool_effect(
    plan: RuntimePlan,
    *,
    node_id: str,
    openapi: _GovernedOpenApi,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """Run a protected OpenAPI tool call through proposal, approval and dispatch (B04.4, #750).

    Everything that can be refused without sending is refused **before the proposal is recorded**, so no approval is
    requested or spent for a call that cannot be made: the input against the row's schema, the operation the row selects, the
    egress policy for the request and an OAuth token URL, a caller-supplied idempotency key and a conflicting
    precondition (:func:`~caliber.integrations.openapi.executor.preflight_governed_openapi_request`). The proposal records the
    operation from the **row** (method, server, path template) and the input; the ``openapi_http`` backend sends it, once,
    only after the exact approval and the execution-owner lease are rechecked, with the proposal's stable key and
    precondition. The returned dict is the executor's result, as an inline call returns it.
    """
    context = plan.governed_effects
    assert context is not None  # _governed_openapi_route guarantees it
    if not callable(context.authorize) or not callable(context.receipt_verifier):
        raise ToolExecutionError("governed OpenAPI effect context is incomplete")
    if not arguments:
        raise ToolExecutionError("openapi tools accept one object argument")
    operation = openapi.operation
    try:
        checked = _openapi_http.preflight(operation, _json_compatible(arguments))
    except OpenApiPreflightError as exc:
        raise ToolExecutionError(f"OpenAPI effect proposal refused: {exc.code}") from exc
    return _submit_authorize_dispatch(
        context,
        label="OpenAPI effect",
        proposal={
            "node_id": node_id,
            "occurrence_id": _STEP_OCCURRENCE.get() or node_id,
            "connector": _openapi_http.CONNECTOR,
            "connector_version": _openapi_http.VERSION,
            "min_effect_class": openapi.route.effect_class,
            "resource": _openapi_http.operation_resource(checked),
            "args": _openapi_http.intent_args(operation, checked),
            "expected_resource_version": checked.expected_resource_version,
            "tool_name": operation.tool,
            "caliber_defined": openapi.route.trusted,
        },
        backend_kind=_openapi_http.BACKEND_KIND,
        backend_config={"operation": operation, "receipt_verifier": context.receipt_verifier},
    )


@dataclass(frozen=True)
class _GovernedMcp:
    """A protected MCP call's route and its trusted operation (the server record and the administrator's tool policy)."""

    route: McpToolRoute
    call: _mcp_tool.McpToolCall
    prepared: _mcp_tool.PreparedMcpCall = field(repr=False)
    arguments: dict[str, Any] = field(repr=False)


def _governed_mcp_route(
    plan: RuntimePlan,
    *,
    node_id: str,
    server_id: str,
    tool_name: str,
    timeout_seconds: float,
    arguments: dict[str, Any],
    preview: bool,
    overridden: bool = False,
) -> _GovernedMcp | None:
    """The dispatcher route of an MCP tool call, or ``None`` when it runs inline as before (B04.5, #759).

    As for the other governed tools, only a plan wired to the registry (an identity reader) or to an authority (a governed
    context) is governed, and a preview never reaches a real effect. The call is classified from the **MCP server record**
    (read from the database, scoped to the run's identity), the administrator's explicit tool policy for exactly that tool
    and the protected-effect inventory: never from the binding's ``side_effect_level``, a description or annotation the
    server reported, or the model. A tool nobody classified, a blocked tool, a server that is not ready and a first-party
    database write tool (reference profile only) are refused here, before any proposal or approval exists. Only a ``read``-class
    tool runs inline; every other call needs the governed effect context, except in a plan whose ``legacy_guarded_effects`` is
    set (the development profile, #1068), where it runs inline as before the dispatcher; a protected call that an injected
    callable replaces is refused once governed (no proposal could describe it).
    """
    if preview or (plan.registered_tool_identity is None and plan.governed_effects is None):
        return None
    label = f"MCP tool {tool_name!r}"
    call, prepared = _mcp_decision(
        plan,
        server_id=server_id,
        tool_name=tool_name,
        timeout_seconds=timeout_seconds,
        arguments=arguments,
    )
    route = prepared.route
    if route.route == INLINE:
        return None
    if plan.governed_effects is None:
        if _legacy_effects_allowed(plan):
            # Development profile, no authority bound (#1068): the pre-dispatcher behavior. The call runs inline through the
            # gateway entry point (which lets it through outside the reference profile), behind the node's own approval gate;
            # there is no proposal, exact-intent approval, lease or receipt.
            return None
        raise ToolRouteRefused(
            GOVERNED_CONTEXT_REQUIRED,
            f"{node_id!r} requires a governed effect context to run protected {label}",
        )
    if overridden:
        raise ToolRouteRefused(
            TOOL_REPLACED, f"{label} is replaced by an injected callable and cannot be governed"
        )
    assert prepared.args is not None  # only a refused route has no proposal material
    return _GovernedMcp(route, call, prepared, dict(prepared.args["arguments"]))


def _mcp_decision(
    plan: RuntimePlan,
    *,
    server_id: str,
    tool_name: str,
    timeout_seconds: float,
    arguments: dict[str, Any],
) -> tuple[_mcp_tool.McpToolCall, _mcp_tool.PreparedMcpCall]:
    """Classify one MCP tool call and build its proposal material; the one place both a node and a model's call do it (B04.5, #759).

    The call is classified from the **MCP server record** (read from the database, scoped to the run's identity), the
    administrator's explicit tool policy for exactly that tool and the protected-effect inventory: never from the binding's
    ``side_effect_level``, a description or annotation the server reported, or the model. A tool nobody classified, a blocked
    tool, a server that is not ready and a first-party database write tool (reference profile only) are refused here, before any
    proposal or approval exists, with the gate's own code.
    """
    label = f"MCP tool {tool_name!r}"
    identity = plan.mcp_identity

    def load_server() -> Any:
        if identity is None:
            return load_server_config(server_id)
        return load_server_config(server_id, identity=identity)

    call = _mcp_tool.McpToolCall(server_id, tool_name, load_server, timeout_seconds)
    try:
        prepared = _mcp_tool.prepare(call, _json_compatible(arguments))
    except _mcp_tool.McpCallRefused as exc:
        raise ToolRouteRefused(exc.code, f"{label} refused: {exc.code}") from exc
    route = prepared.route
    if route.route == REFUSE:
        code = route.code or UNSUPPORTED_EFFECT_PATH
        raise ToolRouteRefused(code, f"{label} refused: {route.code}")
    return call, prepared


def _perform_mcp_tool_effect(
    plan: RuntimePlan,
    *,
    node_id: str,
    mcp: _GovernedMcp,
) -> Any:
    """Run a protected MCP tool call through proposal, approval and dispatch (B04.5, #759); the result is the server's, bounded.

    The proposal records the server record's id, the tool, a digest of the record and classification the decision was made
    from, and the arguments (``mcp/<server_id>/<tool>``). The ``mcp`` backend reads the record again and refuses a server or
    classification that changed, then sends **once**, only after the exact approval and the execution-owner lease are
    rechecked. A result the server returns is untrusted data and is only a claim: the run's receipt verifier decides
    whether the call is ``committed``; without a confirmation it is ``unknown`` and this raises.
    """
    context = plan.governed_effects
    assert context is not None  # _governed_mcp_route guarantees it
    if not callable(context.authorize) or not callable(context.receipt_verifier):
        raise ToolExecutionError("governed MCP effect context is incomplete")
    response = _submit_authorize_dispatch(
        context,
        label="MCP effect",
        proposal={
            "node_id": node_id,
            "occurrence_id": _STEP_OCCURRENCE.get() or node_id,
            "connector": _mcp_tool.CONNECTOR,
            "connector_version": _mcp_tool.VERSION,
            "min_effect_class": mcp.route.effect_class,
            "resource": mcp.prepared.resource,
            "args": mcp.prepared.args,
            "tool_name": mcp.call.tool_name,
            "caliber_defined": False,
        },
        backend_kind=_mcp_tool.BACKEND_KIND,
        backend_config={"call": mcp.call, "receipt_verifier": context.receipt_verifier},
    )
    return response["result"]


class _RuntimeToolBinder:
    """What a model's proposal of a bound tool is, from the tool's **trusted identity** and the routes a tool node uses (B04.6, #768).

    :func:`caliber.effects.step_proposal.record_step_proposal` asks it, for each protected tool of the agent, what the proposal
    for the model's arguments would be. The answer is built with the same helpers the node routes use
    (:func:`_read_registry_identity`, :func:`route_registered_tool`, :func:`_openapi_decision`, :func:`_mcp_decision`; the trusted
    registry row or the MCP server record and the administrator's tool policy; never the binding's names or level, the model's
    claims or the server's own annotations), so a model-chosen call is classified exactly as a node-bound one and has no weaker
    path. The proposal it describes is **the node's own**: the same connector, resource and intent arguments a tool node records
    for the same call, so the connector's backend can dispatch it once it is approved. The model's arguments are embedded only
    after the trusted input schema accepted them (the registry row's for a registered or OpenAPI tool; an MCP tool has no trusted
    schema, its arguments are only the bounded plain JSON every intent is). Every refusal is an ``EffectRefused`` with a fixed code.
    """

    def __init__(self, plan: RuntimePlan, node_id: str) -> None:
        self._plan = plan
        self._node_id = node_id

    def connector(self, binding: IRToolBinding) -> str | None:
        if binding.binding_type == "mcp_tool":
            return _mcp_tool.CONNECTOR
        if binding.binding_type == "registered_function":
            if binding.execution_backend == OPENAPI_BACKEND:
                return _openapi_http.CONNECTOR
            if binding.execution_backend == "python_callable":
                return REGISTERED_TOOL_CONNECTOR
        return None

    def static_resource(self, binding: IRToolBinding) -> str | None:
        if binding.binding_type == "mcp_tool":
            return _mcp_tool.mcp_resource(binding.mcp_server_id or "", binding.mcp_tool_name or "")
        if (
            binding.binding_type == "registered_function"
            and binding.execution_backend == "python_callable"
            and binding.resolved_name
            and binding.resolved_version
        ):
            return tool_resource(binding.resolved_name, binding.resolved_version)
        return None

    def bind(self, binding: IRToolBinding, arguments: Mapping[str, Any]) -> ToolProposal:
        plain = _json_compatible(dict(arguments))
        try:
            if binding.binding_type == "mcp_tool":
                return self._mcp(binding, plain)
            if binding.binding_type == "registered_function":
                if binding.execution_backend == OPENAPI_BACKEND:
                    return self._openapi(binding, plain)
                if binding.execution_backend == "python_callable":
                    return self._registered(binding, plain)
        except ToolRouteRefused as exc:
            if exc.code == UNSUPPORTED_EFFECT_PATH:
                raise UnsupportedEffectPath(exc.code) from None
            raise EffectRefused(exc.code) from None
        except OpenApiPreflightError as exc:
            raise EffectRefused(exc.code) from None
        raise UnsupportedEffectPath(UNSUPPORTED_EFFECT_PATH)

    def _registered(self, binding: IRToolBinding, arguments: dict[str, Any]) -> ToolProposal:
        identity = _read_registry_identity(self._plan, binding)
        if identity is None:
            raise ToolRouteRefused(REGISTRY_ROW_UNREADABLE, "registry row unreadable")
        if not _registered_row_matches(binding, identity):
            raise ToolRouteRefused(REGISTRY_ROW_MISMATCH, "registry row mismatch")
        route = route_registered_tool(identity)
        if route.route == REFUSE:
            raise UnsupportedEffectPath(UNSUPPORTED_EFFECT_PATH)
        if route.route == INLINE:
            raise EffectRefused(TOOL_NOT_PROTECTED)
        if not check_tool_arguments(identity, arguments):
            raise EffectRefused(TOOL_INPUT_INVALID)
        return ToolProposal(
            backend_kind="registered_tool",
            connector=REGISTERED_TOOL_CONNECTOR,
            resource=tool_resource(identity.name, identity.version),
            args=tool_intent_args(
                name=identity.name, version=identity.version, arguments=arguments, input_text=""
            ),
            tool_name=identity.name,
            caliber_defined=route.trusted,
            effect_floor=route.effect_class,
            registry=(identity.name, identity.version),
        )

    def _openapi(self, binding: IRToolBinding, arguments: dict[str, Any]) -> ToolProposal:
        decision = _openapi_decision(self._plan, binding, arguments=arguments)
        if decision.route.route == INLINE:
            raise EffectRefused(TOOL_NOT_PROTECTED)
        operation = _openapi_operation(self._plan, decision.identity)
        checked = _openapi_http.preflight(operation, arguments)
        return ToolProposal(
            backend_kind=_openapi_http.BACKEND_KIND,
            connector=_openapi_http.CONNECTOR,
            resource=_openapi_http.operation_resource(checked),
            args=_openapi_http.intent_args(operation, checked),
            tool_name=operation.tool,
            caliber_defined=decision.route.trusted,
            effect_floor=decision.route.effect_class,
            expected_resource_version=checked.expected_resource_version,
            registry=(decision.identity.name, decision.identity.version),
        )

    def _mcp(self, binding: IRToolBinding, arguments: dict[str, Any]) -> ToolProposal:
        server_id, tool_name = binding.mcp_server_id or "", binding.mcp_tool_name or ""
        call, prepared = _mcp_decision(
            self._plan,
            server_id=server_id,
            tool_name=tool_name,
            timeout_seconds=binding.timeout_seconds or _mcp_tool.DEFAULT_TIMEOUT_SECONDS,
            arguments=arguments,
        )
        if prepared.route.route == INLINE:
            raise EffectRefused(TOOL_NOT_PROTECTED)
        assert prepared.args is not None  # only a refused route has no proposal material
        return ToolProposal(
            backend_kind=_mcp_tool.BACKEND_KIND,
            connector=_mcp_tool.CONNECTOR,
            resource=prepared.resource,
            args=prepared.args,
            tool_name=call.tool_name,
            caliber_defined=False,
            effect_floor=prepared.route.effect_class,
            mcp=(server_id, tool_name),
        )


def _registered_row_matches(binding: IRToolBinding, identity: RegisteredToolIdentity) -> bool:
    """Whether the code a registered binding would run is the code its registry row describes (module, callable, backend)."""
    return (
        identity.module_path == binding.module_path
        and identity.callable_name == binding.callable_name
        and identity.execution_backend == binding.execution_backend
    )


#: Failures that prove the effect did **not** reach the remote system, so the
#: ledger row can be released for retry. Anything else (notably a timeout, where
#: the request may already have been processed) keeps the claim, which makes the
#: next attempt report an indeterminate effect rather than repeat it.
_DEFINITIVE_EFFECT_FAILURES = (ConnectionError, ConnectionRefusedError, FileNotFoundError)


def _stringify_inline_target_input(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, default=str)
    except TypeError:
        return str(value)


def _template_render_context(inputs: dict[str, Any], run_input: str) -> dict[str, Any]:
    context: dict[str, Any] = {
        "inputs": _json_compatible(dict(inputs)),
        "run_input": run_input,
    }
    for key, value in inputs.items():
        context.setdefault(str(key), value)
    if context.get("input") in (None, ""):
        context["input"] = run_input

    variables = inputs.get("variables")
    if isinstance(variables, dict):
        context["variables"] = variables
        for key, value in variables.items():
            context.setdefault(str(key), value)
    else:
        context.setdefault("variables", {})

    extra_context = inputs.get("context")
    if isinstance(extra_context, dict):
        context["context"] = extra_context
        for key, value in extra_context.items():
            context.setdefault(str(key), value)
    elif extra_context is not None:
        context["context"] = extra_context

    return context


def _template_text_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(_json_compatible(value), ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def _template_path_tokens(expression: str) -> list[str | int]:  # noqa: PLR0912 - tiny parser
    expr = expression.strip()
    if not expr:
        raise ValueError("template expression is empty")

    tokens: list[str | int] = []
    current: list[str] = []
    index = 0
    length = len(expr)
    while index < length:
        char = expr[index]
        if char == ".":
            if current:
                token = "".join(current).strip()
                if not token:
                    raise ValueError("template expression contains an empty path segment")
                tokens.append(token)
                current = []
            elif not tokens:
                raise ValueError("template expression cannot start with '.'")
            index += 1
            continue
        if char == "[":
            if current:
                token = "".join(current).strip()
                if not token:
                    raise ValueError("template expression contains an empty path segment")
                tokens.append(token)
                current = []
            closing = expr.find("]", index + 1)
            if closing < 0:
                raise ValueError("template expression is missing a closing ']'")
            raw = expr[index + 1 : closing].strip()
            if not raw:
                raise ValueError("template index access cannot be empty")
            if raw.isdigit():
                tokens.append(int(raw))
            elif (raw.startswith('"') and raw.endswith('"')) or (
                raw.startswith("'") and raw.endswith("'")
            ):
                tokens.append(raw[1:-1])
            else:
                tokens.append(raw)
            index = closing + 1
            continue
        current.append(char)
        index += 1

    if current:
        token = "".join(current).strip()
        if not token:
            raise ValueError("template expression contains an empty path segment")
        tokens.append(token)

    if not tokens:
        raise ValueError("template expression is empty")
    return tokens


def _template_lookup_value(  # noqa: PLR0911 - explicit lookup fallbacks
    context: dict[str, Any], expression: str
) -> tuple[bool, Any]:
    expr = expression.strip()
    if not expr:
        return False, None
    if expr in context:
        return True, context[expr]
    try:
        tokens = _template_path_tokens(expr)
    except ValueError:
        return False, None

    current: Any = context
    for token in tokens:
        if isinstance(token, int):
            if isinstance(current, (list, tuple)) and 0 <= token < len(current):
                current = current[token]
                continue
            return False, None
        if isinstance(current, dict):
            if token in current:
                current = current[token]
                continue
            return False, None
        if isinstance(current, (list, tuple)) and token.isdigit():
            index = int(token)
            if 0 <= index < len(current):
                current = current[index]
                continue
            return False, None
        if hasattr(current, token):
            current = getattr(current, token)
            continue
        return False, None
    return True, current


def _record_template_expression(
    expression: str,
    values: list[str],
    seen: set[str],
) -> None:
    if expression not in seen:
        values.append(expression)
        seen.add(expression)


def _render_text_template(
    template: str,
    *,
    context: dict[str, Any],
    missing_variable_mode: str,
) -> tuple[str, list[str], list[str]]:
    used_variables: list[str] = []
    missing_variables: list[str] = []
    used_seen: set[str] = set()
    missing_seen: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        expression = match.group(1).strip()
        found, value = _template_lookup_value(context, expression)
        if found:
            _record_template_expression(expression, used_variables, used_seen)
            return _template_text_value(value)
        _record_template_expression(expression, missing_variables, missing_seen)
        if missing_variable_mode == "error":
            raise ToolExecutionError(f"template references missing variable {expression!r}")
        if missing_variable_mode == "empty":
            return ""
        return match.group(0)

    rendered = _TEMPLATE_VARIABLE_PATTERN.sub(replace, template)
    return rendered, used_variables, missing_variables


def _template_json_inside_string(template: str, start_index: int) -> bool:
    inside_string = False
    escaped = False
    for char in template[:start_index]:
        if escaped:
            escaped = False
            continue
        if inside_string and char == "\\":
            escaped = True
            continue
        if char == '"':
            inside_string = not inside_string
    return inside_string


def _resolve_json_template_tokens(
    value: Any,
    token_values: dict[str, tuple[Any, bool]],
) -> Any:
    if isinstance(value, list):
        return [_resolve_json_template_tokens(item, token_values) for item in value]
    if isinstance(value, dict):
        resolved: dict[str, Any] = {}
        for key, item in value.items():
            resolved_key = _resolve_json_template_tokens(key, token_values)
            if not isinstance(resolved_key, str):
                resolved_key = _template_text_value(resolved_key)
            resolved[str(resolved_key)] = _resolve_json_template_tokens(item, token_values)
        return resolved
    if isinstance(value, str):
        token = token_values.get(value)
        if token is not None:
            replacement, inside_string = token
            if inside_string:
                return _template_text_value(replacement)
            return _json_compatible(replacement)
        resolved_text = value
        for token_value, (replacement, _inside_string) in token_values.items():
            if token_value in resolved_text:
                resolved_text = resolved_text.replace(
                    token_value, _template_text_value(replacement)
                )
        return resolved_text
    return value


def _render_json_template(
    template: str,
    *,
    context: dict[str, Any],
    missing_variable_mode: str,
) -> tuple[str, Any, list[str], list[str]]:
    used_variables: list[str] = []
    missing_variables: list[str] = []
    used_seen: set[str] = set()
    missing_seen: set[str] = set()
    token_values: dict[str, tuple[Any, bool]] = {}
    token_index = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal token_index
        expression = match.group(1).strip()
        found, value = _template_lookup_value(context, expression)
        if found:
            _record_template_expression(expression, used_variables, used_seen)
        else:
            _record_template_expression(expression, missing_variables, missing_seen)
            if missing_variable_mode == "error":
                raise ToolExecutionError(f"template references missing variable {expression!r}")
            value = "" if missing_variable_mode == "empty" else match.group(0)
        inside_string = _template_json_inside_string(template, match.start())
        token = f"__caliber_template_token_{token_index}__"
        token_index += 1
        token_values[token] = (value, inside_string)
        if inside_string:
            return token
        return json.dumps(token)

    prepared = _TEMPLATE_VARIABLE_PATTERN.sub(replace, template)
    try:
        parsed = json.loads(prepared)
    except json.JSONDecodeError as exc:
        raise ToolExecutionError(
            f"template rendered invalid JSON: {exc.msg} at line {exc.lineno} column {exc.colno}"
        ) from exc
    result = _resolve_json_template_tokens(parsed, token_values)
    rendered = json.dumps(_json_compatible(result), ensure_ascii=False)
    return rendered, result, used_variables, missing_variables


def _handoff_input_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, (list, tuple)):
        return []
    normalized: list[dict[str, Any]] = []
    for item in items:
        candidate = item
        if hasattr(candidate, "to_input_item"):
            try:
                candidate = candidate.to_input_item()
            except Exception as exc:
                logger.debug("workflow input item conversion failed: %s", exc)
                continue
        if isinstance(candidate, dict):
            normalized.append(_json_compatible(dict(candidate)))
    return normalized


def _normalize_handoff_messages(items: Any) -> list[HistoryMessage]:
    messages: list[HistoryMessage] = []
    for item in _handoff_input_items(items):
        role = item.get("role")
        if not isinstance(role, str) or not role:
            continue
        messages.append(
            {
                "role": role,
                "content": _template_text_value(item.get("content")),
            }
        )
    return messages


def _split_handoff_turn_input(turn_input: Any) -> tuple[str, list[HistoryMessage]]:
    if isinstance(turn_input, str):
        return turn_input, []
    messages = _normalize_handoff_messages(turn_input)
    if not messages:
        return "", []

    last_user_index = next(
        (
            index
            for index in range(len(messages) - 1, -1, -1)
            if messages[index].get("role") == "user"
        ),
        None,
    )
    if last_user_index is not None:
        return (
            str(messages[last_user_index].get("content") or ""),
            [dict(item) for item in messages[:last_user_index]],
        )
    return (
        str(messages[-1].get("content") or ""),
        [dict(item) for item in messages[:-1]],
    )


def _handoff_assistant_output(items: Any) -> str:
    messages = _normalize_handoff_messages(items)
    for item in reversed(messages):
        if item.get("role") != "assistant":
            continue
        content = str(item.get("content") or "")
        if content.strip():
            return content
    for item in reversed(messages):
        content = str(item.get("content") or "")
        if content.strip():
            return content
    raw_items = _handoff_input_items(items)
    if raw_items:
        return _template_text_value(raw_items)
    return ""


def _handoff_render_context(
    *,
    input_text: str,
    history: list[HistoryMessage] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_history = [dict(item) for item in (history or [])]
    inputs: dict[str, Any] = {
        "input": input_text,
        "history": normalized_history,
    }
    if extra:
        inputs.update(extra)
    context = _template_render_context(inputs, input_text)
    context["history"] = normalized_history
    context.setdefault("messages", normalized_history)
    if extra:
        for key, value in extra.items():
            context[str(key)] = value
    return context


def _evaluate_handoff_expression(  # noqa: PLR0911, PLR0912, PLR0915 - bounded AST evaluator
    node: ast.AST, context: dict[str, Any]
) -> Any:
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Name):
        if node.id in context:
            return context[node.id]
        raise ValueError(f"unknown name {node.id!r}")
    if isinstance(node, ast.List):
        return [_evaluate_handoff_expression(item, context) for item in node.elts]
    if isinstance(node, ast.Tuple):
        return tuple(_evaluate_handoff_expression(item, context) for item in node.elts)
    if isinstance(node, ast.Set):
        return {_evaluate_handoff_expression(item, context) for item in node.elts}
    if isinstance(node, ast.Dict):
        resolved: dict[Any, Any] = {}
        for key, value in zip(node.keys, node.values, strict=False):
            if key is None:
                raise ValueError("dict unpacking is not supported")
            resolved[_evaluate_handoff_expression(key, context)] = _evaluate_handoff_expression(
                value, context
            )
        return resolved
    if isinstance(node, ast.Attribute):
        value = _evaluate_handoff_expression(node.value, context)
        if node.attr.startswith("_"):
            raise ValueError("private attributes are not supported")
        if isinstance(value, dict) and node.attr in value:
            return value[node.attr]
        attr = getattr(value, node.attr)
        if callable(attr):
            raise ValueError("callable attributes are not supported")
        return attr
    if isinstance(node, ast.Subscript):
        value = _evaluate_handoff_expression(node.value, context)
        index = _evaluate_handoff_expression(node.slice, context)
        return value[index]
    if isinstance(node, ast.UnaryOp):
        operand = _evaluate_handoff_expression(node.operand, context)
        if isinstance(node.op, ast.Not):
            return not bool(operand)
        if isinstance(node.op, ast.USub):
            return -operand
        if isinstance(node.op, ast.UAdd):
            return +operand
        raise ValueError(f"unsupported unary operator {type(node.op).__name__}")
    if isinstance(node, ast.BoolOp):
        values = node.values
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in values:
                result = _evaluate_handoff_expression(value, context)
                if not result:
                    return result
            return result
        if isinstance(node.op, ast.Or):
            or_result: Any = False
            for value in values:
                or_result = _evaluate_handoff_expression(value, context)
                if or_result:
                    return or_result
            return or_result
        raise ValueError(f"unsupported boolean operator {type(node.op).__name__}")
    if isinstance(node, ast.BinOp):
        left = _evaluate_handoff_expression(node.left, context)
        right = _evaluate_handoff_expression(node.right, context)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Div):
            return left / right
        if isinstance(node.op, ast.FloorDiv):
            return left // right
        if isinstance(node.op, ast.Mod):
            return left % right
        raise ValueError(f"unsupported binary operator {type(node.op).__name__}")
    if isinstance(node, ast.Compare):
        left = _evaluate_handoff_expression(node.left, context)
        for op, comparator in zip(node.ops, node.comparators, strict=False):
            right = _evaluate_handoff_expression(comparator, context)
            if isinstance(op, ast.Eq):
                ok = left == right
            elif isinstance(op, ast.NotEq):
                ok = left != right
            elif isinstance(op, ast.Lt):
                ok = left < right
            elif isinstance(op, ast.LtE):
                ok = left <= right
            elif isinstance(op, ast.Gt):
                ok = left > right
            elif isinstance(op, ast.GtE):
                ok = left >= right
            elif isinstance(op, ast.In):
                ok = left in right
            elif isinstance(op, ast.NotIn):
                ok = left not in right
            elif isinstance(op, ast.Is):
                ok = left is right
            elif isinstance(op, ast.IsNot):
                ok = left is not right
            else:
                raise ValueError(f"unsupported comparison operator {type(op).__name__}")
            if not ok:
                return False
            left = right
        return True
    raise ValueError(f"unsupported expression node {type(node).__name__}")


def _evaluate_runtime_expression(expression: str, context: dict[str, Any]) -> bool:
    parsed = ast.parse(expression, mode="eval")
    return bool(_evaluate_handoff_expression(parsed.body, context))


def _handoff_condition_enabled(
    condition: str | None,
    *,
    input_text: str,
    history: list[HistoryMessage] | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    expression = str(condition or "").strip()
    if not expression:
        return True
    context = _handoff_render_context(
        input_text=input_text,
        history=history,
        extra=extra,
    )
    try:
        return _evaluate_runtime_expression(expression, context)
    except Exception as exc:
        logger.debug(
            "handoff condition %r evaluated to disabled: %s",
            expression,
            exc,
        )
        return False


def _render_handoff_filter_text(
    template: str | None,
    *,
    input_text: str,
    history: list[HistoryMessage] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    text_template = str(template or "")
    if not text_template:
        return input_text
    context = _handoff_render_context(
        input_text=input_text,
        history=history,
        extra=extra,
    )
    rendered, _used_variables, _missing_variables = _render_text_template(
        text_template,
        context=context,
        missing_variable_mode="preserve",
    )
    return rendered


def _correlation_value_from_object(  # noqa: PLR0911 - explicit extraction fallbacks
    value: Any,
    correlation_key: str,
) -> tuple[bool, Any]:
    if isinstance(value, dict):
        if correlation_key in value:
            return True, value[correlation_key]
        for nested in value.values():
            found, resolved = _correlation_value_from_object(nested, correlation_key)
            if found:
                return True, resolved
        return False, None
    if isinstance(value, list):
        for nested in value:
            found, resolved = _correlation_value_from_object(nested, correlation_key)
            if found:
                return True, resolved
        return False, None
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed.startswith("{") or trimmed.startswith("["):
            try:
                parsed = json.loads(trimmed)
            except (TypeError, ValueError, json.JSONDecodeError):
                return False, None
            return _correlation_value_from_object(parsed, correlation_key)
    return False, None


def _resolve_wait_for_event_correlation_value(
    *,
    inputs: dict[str, Any],
    run_input: str,
    correlation_key: str,
) -> Any | None:
    if not correlation_key.strip():
        return None
    found, value = _correlation_value_from_object(inputs, correlation_key)
    if not found and run_input:
        found, value = _correlation_value_from_object(run_input, correlation_key)
    if not found:
        return None
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def workflow_handoff_is_enabled(
    condition: str,
) -> Callable[[Any, Any], bool]:
    """Return an Agents SDK ``is_enabled`` callback for a handoff condition."""

    expression = str(condition or "").strip()
    if not expression:
        return lambda _run_context, _agent: True

    def _is_enabled(run_context: Any, _agent: Any) -> bool:
        turn_input = getattr(run_context, "turn_input", [])
        input_text, history = _split_handoff_turn_input(turn_input)
        return _handoff_condition_enabled(
            expression,
            input_text=input_text,
            history=history,
            extra={"turn_input": _handoff_input_items(turn_input)},
        )

    return _is_enabled


def workflow_handoff_input_filter(
    template: str,
) -> Callable[[Any], Any]:
    """Return an Agents SDK ``input_filter`` that narrows the next agent input."""

    text_template = str(template or "")

    def _filter(handoff_input_data: Any) -> Any:
        run_context = getattr(handoff_input_data, "run_context", None)
        turn_input = getattr(run_context, "turn_input", [])
        input_text, history = _split_handoff_turn_input(turn_input)
        input_history = getattr(handoff_input_data, "input_history", ())
        if not input_text and isinstance(input_history, str):
            input_text = input_history
        pre_handoff_items = _handoff_input_items(
            getattr(handoff_input_data, "pre_handoff_items", ())
        )
        new_items = _handoff_input_items(getattr(handoff_input_data, "new_items", ()))
        assistant_output = _handoff_assistant_output(getattr(handoff_input_data, "new_items", ()))
        rendered = _render_handoff_filter_text(
            text_template,
            input_text=input_text,
            history=history,
            extra={
                "turn_input": _handoff_input_items(turn_input),
                "input_history": (
                    _normalize_handoff_messages(input_history)
                    if not isinstance(input_history, str)
                    else input_history
                ),
                "pre_handoff_items": pre_handoff_items,
                "new_items": new_items,
                "assistant_output": assistant_output,
                "final_output": assistant_output,
            },
        )
        return handoff_input_data.clone(
            input_history=rendered,
            input_items=(),
        )

    return _filter


def _inline_target_inputs(node: IRNode, item: Any) -> tuple[dict[str, Any], str]:
    node_inputs = getattr(node, "inputs", {}) or {}
    payload: dict[str, Any] = {}
    if isinstance(item, dict):
        for port in node_inputs:
            if port in item:
                payload[port] = item[port]
    if not payload:
        if "arguments" in node_inputs and not isinstance(item, str):
            payload["arguments"] = item
        elif "input" in node_inputs:
            payload["input"] = item
        elif "question" in node_inputs:
            payload["question"] = _stringify_inline_target_input(item)
        elif "arguments" in node_inputs:
            payload["arguments"] = item
    return payload, _stringify_inline_target_input(item)


def _loop_next_state(child_step: NodeStep, child_outputs: dict[str, Any]) -> Any:
    result_value = child_outputs.get("result")
    if result_value is not None:
        if (
            isinstance(result_value, dict)
            and "result" in result_value
            and any(key in result_value for key in ("text", "output", "answer", "metadata"))
        ):
            nested_result = result_value.get("result")
            if nested_result is not None:
                return nested_result
        return result_value
    for key in ("output", "text", "answer", "final_output"):
        if key not in child_outputs:
            continue
        value = child_outputs[key]
        if value is None:
            continue
        if isinstance(value, str) and not value:
            continue
        return value
    if child_step.output:
        return child_step.output
    if child_outputs:
        return child_outputs
    return child_step.output


def _supports_inline_orchestration_target(node: IRNode | None) -> bool:
    return isinstance(node, _INLINE_ORCHESTRATION_TARGET_TYPES)


def _run_inline_target_node(
    target: IRNode,
    ir: IRWorkflow,
    plan: RuntimePlan,
    *,
    executor: WorkflowExecutor,
    preview: bool,
    item: Any,
    guardrail_results: list[dict[str, Any]],
    extra_tools: dict[str, Callable[..., Any]] | None = None,
    occurrence_id: str | None = None,
    inherited_provenance: tuple[dict[str, Any], ...] = (),
) -> tuple[int, NodeStep, dict[str, Any], dict[str, str]]:
    target_inputs, target_run_input = _inline_target_inputs(target, item)
    target_port_values: dict[tuple[str, str], Any] = {}
    tokens, step = _run_node_with_policy(
        target,
        ir,
        plan,
        executor=executor,
        preview=preview,
        inputs=target_inputs,
        run_input=target_run_input,
        port_values=target_port_values,
        guardrail_results=guardrail_results,
        extra_tools=extra_tools,
        occurrence_id=occurrence_id,
        inherited_provenance=inherited_provenance,
    )
    step = _attach_step_port_snapshots(
        step,
        node=target,
        input_by_port=target_inputs,
        port_values=target_port_values,
    )
    if step.status != "ok":
        raise ToolExecutionError(
            f"target node {target.node_id!r} returned unsupported status {step.status!r}"
        )
    outputs = {
        port: value
        for (node_id, port), value in target_port_values.items()
        if node_id == target.node_id
    }
    return tokens, step, outputs, _collect_artifacts(target_port_values)


def _run_node(  # noqa: PLR0911, PLR0912, PLR0915 - per-node-type dispatch
    node: IRNode,
    ir: IRWorkflow,
    plan: RuntimePlan,
    *,
    executor: WorkflowExecutor,
    preview: bool,
    inputs: dict[str, Any],
    run_input: str,
    port_values: dict[tuple[str, str], Any],
    guardrail_results: list[dict[str, Any]],
    extra_tools: dict[str, Callable[..., Any]] | None = None,
    runtime_approvals_enabled: bool = False,
    approved_human_approval_nodes: set[str] | None = None,
) -> tuple[int, NodeStep]:
    nid = node.node_id
    ntype = node.node_type.value

    if node.node_type == NodeType.START:
        return 0, NodeStep(nid, ntype, "ok", output=run_input)

    if isinstance(node, IRFileInput):
        if node.file_ref is not None:
            resolver = _MANAGED_FILE_RESOLVER.get()
            if resolver is None:
                raise ToolExecutionError(
                    "managed file input requires a scoped runtime file resolver"
                )
            text, file_metadata = resolver(node.file_ref, node.encoding, node.max_bytes)
            source_label = node.file_ref.file_ref
            published_ref: dict[str, Any] | None = {
                "file_id": node.file_ref.file_id,
                "file_ref": node.file_ref.file_ref,
                "sha256": node.file_ref.sha256,
                "name": node.file_ref.name,
                "size_bytes": node.file_ref.size_bytes,
                "media_type": node.file_ref.media_type,
                "object_version_id": node.file_ref.object_version_id,
            }
        else:
            file_path = _path_from_inputs(inputs, configured_path=node.path, run_input=run_input)
            text, file_metadata = _read_text_file_node(
                file_path,
                encoding=node.encoding,
                max_bytes=node.max_bytes,
            )
            source_label = str(file_path)
            published_ref = None
        _publish_declared_outputs(
            node,
            port_values,
            {
                "text": text,
                # Retained for existing graph mappings. For managed files this
                # is the safe caliber:// URI, never a materialized host path.
                "path": source_label,
                "file_ref": published_ref,
                "metadata": file_metadata,
            },
            fallback=text,
        )
        return 0, NodeStep(
            nid,
            ntype,
            "ok",
            output=text,
            detail=f"read {file_metadata['bytes']} byte(s) from {source_label}",
        )

    if isinstance(node, IRFolderInput):
        folder_path = _path_from_inputs(inputs, configured_path=node.path, run_input=run_input)
        text, files, metadata = _read_folder_input_node(
            folder_path,
            pattern=node.pattern,
            recursive=node.recursive,
            max_files=node.max_files,
            max_bytes_per_file=node.max_bytes_per_file,
            encoding=node.encoding,
        )
        _publish_declared_outputs(
            node,
            port_values,
            {"text": text, "files": files, "metadata": metadata},
            fallback=text,
        )
        return 0, NodeStep(
            nid,
            ntype,
            "ok",
            output=text,
            detail=f"read {metadata['file_count']} file(s) from {folder_path}",
        )

    if isinstance(node, IRInputBucket):
        # Runtime prefix override (input port) wins over the configured prefix.
        prefix = _first_str(inputs)
        prefix = prefix if prefix is not None else node.prefix
        text, files, metadata = _read_input_bucket_node(
            bucket=node.bucket,
            prefix=prefix,
            recursive=node.recursive,
            max_files=node.max_files,
            max_bytes_per_file=node.max_bytes_per_file,
            encoding=node.encoding,
        )
        _publish_declared_outputs(
            node,
            port_values,
            {"text": text, "files": files, "metadata": metadata},
            fallback=text,
        )
        return 0, NodeStep(
            nid,
            ntype,
            "ok",
            output=text,
            detail=f"read {metadata['object_count']} object(s) from {node.bucket}/{prefix}",
        )

    if isinstance(node, IROutputBucket):
        keys, metadata = _write_output_bucket_node(
            bucket=node.bucket,
            prefix=node.prefix,
            overwrite=node.overwrite,
            port_values=port_values,
            direct_input=_first_str(inputs),
        )
        _publish_declared_outputs(
            node,
            port_values,
            {"keys": keys, "metadata": metadata},
            fallback=json.dumps(keys),
        )
        return 0, NodeStep(
            nid,
            ntype,
            "ok",
            output=json.dumps(keys),
            detail=f"wrote {len(keys)} object(s) to {node.bucket}/{node.prefix}",
        )

    if isinstance(node, IROutputFolder):
        written, metadata = _write_output_folder_node(
            path=node.path,
            overwrite=node.overwrite,
            port_values=port_values,
            direct_input=_first_str(inputs),
            config=_operator_config(plan),
        )
        _publish_declared_outputs(
            node,
            port_values,
            {"files": written, "metadata": metadata},
            fallback=json.dumps(written),
        )
        return 0, NodeStep(
            nid,
            ntype,
            "ok",
            output=json.dumps(written),
            detail=f"wrote {len(written)} file(s) to {node.path}",
        )

    if isinstance(node, IRWaitUntil):
        value = _first_str(inputs) or run_input
        if _manual_resume_override_requested(inputs):
            _publish_declared_outputs(
                node,
                port_values,
                {"output": value},
                fallback=value,
            )
            return 0, NodeStep(nid, ntype, "ok", output=value)
        try:
            resume_at = _wait_until_deadline(node.wait_until, timezone_name=node.timezone)
        except ValueError as exc:
            raise ToolExecutionError(f"wait_until node {nid!r} is misconfigured: {exc}") from exc
        if resume_at is None:
            raise ToolExecutionError(
                f"wait_until node {nid!r} has invalid wait_until {node.wait_until!r}; "
                "use ISO-8601 or 'now'"
            )
        if _wait_until_ready(node.wait_until, timezone_name=node.timezone):
            _publish_declared_outputs(
                node,
                port_values,
                {"output": value},
                fallback=value,
            )
            return 0, NodeStep(nid, ntype, "ok", output=value)
        return 0, NodeStep(
            nid,
            ntype,
            "blocked",
            output=value,
            detail=f"waiting_event:{nid}",
            checkpoint_state={
                "input_by_port": dict(inputs),
                "resume_at": resume_at.isoformat(),
                "timezone": node.timezone,
                "wait_until": node.wait_until,
            },
        )

    if isinstance(node, IRWaitForEvent):
        value = _first_str(inputs) or run_input
        has_event = any(
            key in inputs and inputs[key] not in (None, "", {})
            for key in ("event", "event_payload", node.event_name, "resume_event")
        )
        if has_event:
            event_payload = None
            for key in ("event_payload", "resume_event", "event", node.event_name):
                if key in inputs and inputs[key] not in (None, "", {}):
                    event_payload = inputs[key]
                    break
            event_name = inputs.get("event_name")
            if not isinstance(event_name, str) or not event_name.strip():
                event_name = node.event_name
            _publish_declared_outputs(
                node,
                port_values,
                {
                    "output": value,
                    "event_payload": event_payload,
                    "event_name": event_name,
                },
                fallback=value,
            )
            return 0, NodeStep(nid, ntype, "ok", output=value)
        correlation_key = node.correlation_key.strip()
        correlation_value = _resolve_wait_for_event_correlation_value(
            inputs=dict(inputs),
            run_input=run_input,
            correlation_key=correlation_key,
        )
        checkpoint_state: dict[str, Any] = {
            "input_by_port": dict(inputs),
            "expected_event_name": node.event_name,
        }
        if correlation_key:
            checkpoint_state["correlation_key"] = correlation_key
        if correlation_value is not None:
            checkpoint_state["correlation_value"] = correlation_value
        if node.timeout_seconds is not None:
            checkpoint_state["timeout_seconds"] = node.timeout_seconds
        return 0, NodeStep(
            nid,
            ntype,
            "blocked",
            output=value,
            detail=f"waiting_event:{nid}",
            checkpoint_state=checkpoint_state,
        )

    if isinstance(node, IRParallel):
        value = _first_str(inputs) or run_input
        _publish_declared_outputs(node, port_values, {"output": value}, fallback=value)
        return 0, NodeStep(nid, ntype, "ok", output=value)

    if isinstance(node, IRJoin):
        merged = dict(inputs)
        text = _first_str(inputs) or run_input
        _publish_declared_outputs(
            node,
            port_values,
            {"output": text, "merged": merged},
            fallback=text,
        )
        return 0, NodeStep(nid, ntype, "ok", output=text)

    if isinstance(node, IRSubworkflow):
        sub_input = _select_input(inputs, run_input)
        if plan.subworkflow_runner is None:
            raise ToolExecutionError("subworkflow runner is not configured for this runtime plan")
        payload = dict(
            plan.subworkflow_runner(
                node.workflow_id,
                node.alias,
                sub_input,
                node.timeout_seconds,
                plan.subworkflow_depth + 1,
                executor,
                preview,
            )
        )
        payload.setdefault("workflow_id", node.workflow_id)
        payload.setdefault("alias", node.alias)
        status = str(payload.get("status", "error"))
        if status != "completed":
            message = (
                f"subworkflow {node.workflow_id!r}@{node.alias!r} failed: "
                f"{payload.get('error', 'unknown error')}"
            )
            if payload.get("error_code") == REFUSAL_CODE:
                raise ChildRefusedError(message)
            raise ToolExecutionError(message)
        output = str(payload.get("output", ""))
        _publish_declared_outputs(
            node,
            port_values,
            {"output": output, "result": payload},
            fallback=output,
        )
        return int(payload.get("tokens", 0) or 0), NodeStep(nid, ntype, "ok", output=output)

    if isinstance(node, IRForEach):
        raw_items = inputs.get(node.item_input_port)
        if raw_items is None:
            raw_items = inputs.get("items")
        if raw_items is None:
            raw_items = run_input
        items = _normalize_iterable(raw_items, max_items=node.max_items)
        results: list[Any] = []
        total_tokens = 0
        aggregated_artifacts: dict[str, str] = {}
        target = ir.nodes.get(node.target_node_id) if node.target_node_id else None
        inherited_provenance = _input_context_provenance(plan, ir, node, inputs, port_values)
        if target is not None and not _supports_inline_orchestration_target(target):
            raise ToolExecutionError(
                f"for_each node {nid!r} target {target.node_id!r} must be an executable "
                f"node ({_INLINE_ORCHESTRATION_TARGET_LABEL}), not {target.node_type.value!r}"
            )

        if isinstance(target, IRAgent):
            agent_target = target

            def _run_agent_item(
                index: int, item: Any
            ) -> tuple[int, dict[str, Any], dict[str, str]]:
                # One item's failure must not abort a large fan-out (e.g. a single
                # rate-limited chunk among hundreds): capture the error into the
                # item's record and continue. Downstream consumers see output="" +
                # an "error" field; the node reports how many items failed.
                try:
                    toks, child_step, child_outputs, child_artifacts = _run_inline_target_node(
                        agent_target,
                        ir,
                        plan,
                        executor=executor,
                        preview=preview,
                        item=item,
                        guardrail_results=guardrail_results,
                        extra_tools=extra_tools,
                        occurrence_id=f"{agent_target.node_id}:item:{index}",
                        inherited_provenance=inherited_provenance,
                    )
                    record: dict[str, Any] = {
                        "item": item,
                        "output": child_step.output,
                        "node_id": agent_target.node_id,
                        "node_type": agent_target.node_type.value,
                        "outputs": child_outputs,
                        "tool_calls": child_step.tool_calls,
                        "status": child_step.status,
                    }
                    if child_step.detail:
                        record["detail"] = child_step.detail
                    namespaced_artifacts = {
                        f"item-{index}/{name}": content for name, content in child_artifacts.items()
                    }
                    if child_artifacts:
                        record["artifacts"] = sorted(child_artifacts)
                    return toks, record, namespaced_artifacts
                except (ContextDenied, UnknownEffectError):
                    raise  # a refused context, or an effect nobody can vouch for, fails the node: not an item's own error
                except Exception as exc:  # per-item isolation for fan-out
                    logger.warning("for_each agent item failed: %s", exc, exc_info=True)
                    return (
                        0,
                        {
                            "item": item,
                            "output": "",
                            "node_id": agent_target.node_id,
                            "node_type": agent_target.node_type.value,
                            "tool_calls": [],
                            "error": str(exc),
                        },
                        {},
                    )

            workers = max(1, min(plan.foreach_max_workers, len(items)))
            if _automatic_session_memory_active(plan):
                # Shared session memory makes concurrent fan-out nondeterministic,
                # so preserve a stable turn order when agent memory is enabled.
                workers = 1
            if workers > 1:
                # Bounded fan-out: each item's agent call runs in a copied context
                # so its AGENT span nests under this ForEach's active span (MLflow's
                # fluent span stack is contextvar-based). Tokens are summed after the
                # join — the branch returns the total and the caller charges the
                # budget once, sequentially — so no lock is needed. Futures are read
                # in submission order to preserve output ordering; per-item errors
                # are captured by _run_agent_item, so one failure can't abort the node.
                futures = []
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    for index, item in enumerate(items):
                        worker_ctx = contextvars.copy_context()
                        futures.append(pool.submit(worker_ctx.run, _run_agent_item, index, item))
                    for future in futures:
                        toks, record, item_artifacts = future.result()
                        total_tokens += toks
                        results.append(record)
                        aggregated_artifacts.update(item_artifacts)
            else:
                for index, item in enumerate(items):
                    toks, record, item_artifacts = _run_agent_item(index, item)
                    total_tokens += toks
                    results.append(record)
                    aggregated_artifacts.update(item_artifacts)
        else:
            for index, item in enumerate(items):
                if target is None:
                    results.append({"item": item, "output": str(item)})
                    continue
                try:
                    toks, child_step, child_outputs, child_artifacts = _run_inline_target_node(
                        target,
                        ir,
                        plan,
                        executor=executor,
                        preview=preview,
                        item=item,
                        guardrail_results=guardrail_results,
                        extra_tools=extra_tools,
                        occurrence_id=f"{target.node_id}:item:{index}",
                        inherited_provenance=inherited_provenance,
                    )
                    total_tokens += toks
                    results.append(
                        {
                            "item": item,
                            "output": child_step.output,
                            "node_id": target.node_id,
                            "node_type": target.node_type.value,
                            "outputs": child_outputs,
                            "tool_calls": child_step.tool_calls,
                            "status": child_step.status,
                            **({"detail": child_step.detail} if child_step.detail else {}),
                            **({"artifacts": sorted(child_artifacts)} if child_artifacts else {}),
                        }
                    )
                    aggregated_artifacts.update(
                        {
                            f"item-{index}/{name}": content
                            for name, content in child_artifacts.items()
                        }
                    )
                except (
                    ContextDenied,
                    UnknownEffectError,
                    UnsupportedInReferenceProfile,
                    _step_proposal.EffectApprovalPending,
                ):
                    raise
                except Exception as exc:
                    logger.warning("for_each target item failed: %s", exc, exc_info=True)
                    results.append(
                        {
                            "item": item,
                            "output": "",
                            "node_id": target.node_id,
                            "node_type": target.node_type.value,
                            "tool_calls": [],
                            "error": str(exc),
                        }
                    )
        text = "\n".join(str(item.get("output", item.get("result", ""))) for item in results)
        failed_count = sum(1 for item in results if isinstance(item, dict) and item.get("error"))
        metadata = {
            "count": len(results),
            "failed": failed_count,
            "target_node_id": node.target_node_id,
            "target_node_type": target.node_type.value if target is not None else None,
        }
        if aggregated_artifacts:
            metadata["artifacts"] = aggregated_artifacts
        _publish_declared_outputs(
            node,
            port_values,
            {"results": results, "text": text, "metadata": metadata},
            fallback=text,
        )
        detail = f"processed {len(results)} item(s)"
        if target is not None:
            detail += f" via {target.node_type.value}"
        if failed_count:
            detail += f" ({failed_count} failed)"
        return total_tokens, NodeStep(nid, ntype, "ok", output=text, detail=detail)

    if isinstance(node, IRLoop):
        if not node.target_node_id:
            raise ToolExecutionError(f"loop node {nid!r} requires target_node_id")
        target = ir.nodes.get(node.target_node_id)
        if target is None:
            raise ToolExecutionError(
                f"loop node {nid!r} target {node.target_node_id!r} does not exist"
            )
        if not _supports_inline_orchestration_target(target):
            raise ToolExecutionError(
                f"loop node {nid!r} target {target.node_id!r} must be an executable "
                f"node ({_INLINE_ORCHESTRATION_TARGET_LABEL}), not {target.node_type.value!r}"
            )

        current_state = inputs.get("state")
        if current_state is None:
            current_state = inputs.get("input")
        if current_state is None:
            current_state = run_input

        iterations: list[dict[str, Any]] = []
        total_tokens = 0
        loop_artifacts: dict[str, str] = {}
        inherited_provenance = _input_context_provenance(plan, ir, node, inputs, port_values)
        final_output = _stringify_inline_target_input(current_state)
        stop_reason = "max_iterations_reached"
        stopped_on_condition = False

        for iteration_index in range(node.max_iterations):
            tokens, child_step, child_outputs, child_artifacts = _run_inline_target_node(
                target,
                ir,
                plan,
                executor=executor,
                preview=preview,
                item=current_state,
                guardrail_results=guardrail_results,
                extra_tools=extra_tools,
                occurrence_id=f"{target.node_id}:iteration:{iteration_index + 1}",
                inherited_provenance=inherited_provenance,
            )
            total_tokens += tokens
            next_state = _loop_next_state(child_step, child_outputs)
            final_output = child_step.output

            iteration_record: dict[str, Any] = {
                "iteration": iteration_index + 1,
                "item": current_state,
                "output": child_step.output,
                "result": next_state,
                "node_id": target.node_id,
                "node_type": target.node_type.value,
                "outputs": child_outputs,
                "tool_calls": child_step.tool_calls,
                "status": child_step.status,
            }
            if child_step.detail:
                iteration_record["detail"] = child_step.detail
            if child_artifacts:
                iteration_record["artifacts"] = sorted(child_artifacts)
                loop_artifacts.update(
                    {
                        f"iteration-{iteration_index + 1}/{name}": content
                        for name, content in child_artifacts.items()
                    }
                )
            iterations.append(iteration_record)

            current_state = next_state
            expression = node.stop_condition.strip()
            if expression:
                context = {
                    "iteration": iteration_index + 1,
                    "max_iterations": node.max_iterations,
                    "item": iteration_record["item"],
                    "output": child_step.output,
                    "result": next_state,
                    "outputs": child_outputs,
                    "state": next_state,
                }
                try:
                    stopped_on_condition = _evaluate_runtime_expression(expression, context)
                except Exception as exc:
                    raise ToolExecutionError(
                        f"loop node {nid!r} stop_condition is invalid: {exc}"
                    ) from exc
                if stopped_on_condition:
                    stop_reason = "stop_condition"
                    break

        loop_metadata = {
            "count": len(iterations),
            "max_iterations": node.max_iterations,
            "stop_condition": node.stop_condition,
            "stop_reason": stop_reason,
            "target_node_id": target.node_id,
            "target_node_type": target.node_type.value,
            "stopped_on_condition": stopped_on_condition,
        }
        if loop_artifacts:
            loop_metadata["artifacts"] = loop_artifacts

        _publish_declared_outputs(
            node,
            port_values,
            {
                "output": final_output,
                "result": current_state,
                "iterations": iterations,
                "metadata": loop_metadata,
            },
            fallback=final_output,
        )

        detail = f"iterated {len(iterations)} time(s) via {target.node_type.value}"
        if stopped_on_condition:
            detail += " until stop condition"
        else:
            detail += " (max reached)"
        return total_tokens, NodeStep(nid, ntype, "ok", output=final_output, detail=detail)

    if isinstance(node, IRErrorBoundary):
        boundary_input = _select_input(inputs, run_input)
        fallback_output = node.fallback_text or boundary_input
        target = ir.nodes.get(node.target_node_id) if node.target_node_id else None
        compensate = ir.nodes.get(node.compensate_with) if node.compensate_with else None
        if target is not None and not _supports_inline_orchestration_target(target):
            raise ToolExecutionError(
                f"error_boundary node {nid!r} target {target.node_id!r} must be an "
                f"executable node ({_INLINE_ORCHESTRATION_TARGET_LABEL}), not "
                f"{target.node_type.value!r}"
            )
        if compensate is not None and not _supports_inline_orchestration_target(compensate):
            raise ToolExecutionError(
                f"error_boundary node {nid!r} compensation target {compensate.node_id!r} "
                f"must be an executable node ({_INLINE_ORCHESTRATION_TARGET_LABEL}), not "
                f"{compensate.node_type.value!r}"
            )
        inherited_provenance = _input_context_provenance(plan, ir, node, inputs, port_values)
        if target is not None:
            try:
                tokens, target_step, _target_outputs, _target_artifacts = _run_inline_target_node(
                    target,
                    ir,
                    plan,
                    executor=executor,
                    preview=preview,
                    item=boundary_input,
                    guardrail_results=guardrail_results,
                    extra_tools=extra_tools,
                    occurrence_id=f"{target.node_id}:boundary",
                    inherited_provenance=inherited_provenance,
                )
                _publish_declared_outputs(
                    node,
                    port_values,
                    {"output": target_step.output, "error": {}},
                    fallback=target_step.output,
                )
                return tokens, NodeStep(
                    nid,
                    ntype,
                    "ok",
                    output=target_step.output,
                    tool_calls=target_step.tool_calls,
                    detail=target_step.detail,
                )
            except (
                ContextDenied,
                UnknownEffectError,
                UnsupportedInReferenceProfile,
                _step_proposal.EffectApprovalPending,
            ):
                raise  # a refused context, or an effect nobody can vouch for, is not an error a boundary may absorb
            except Exception as exc:
                compensation_tokens = 0
                compensation_tool_calls: list[dict[str, Any]] = []
                error_payload: dict[str, Any] = {
                    "message": str(exc),
                    "target_node_id": target.node_id,
                    "target_node_type": target.node_type.value,
                }
                if compensate is not None:
                    try:
                        compensation_tokens, comp_step, comp_outputs, comp_artifacts = (
                            _run_inline_target_node(
                                compensate,
                                ir,
                                plan,
                                executor=executor,
                                preview=preview,
                                item=boundary_input,
                                guardrail_results=guardrail_results,
                                extra_tools=extra_tools,
                                occurrence_id=f"{compensate.node_id}:compensation",
                                inherited_provenance=inherited_provenance,
                            )
                        )
                    except (ContextDenied, UnknownEffectError, UnsupportedInReferenceProfile):
                        raise
                    except Exception as compensation_exc:
                        raise ToolExecutionError(
                            f"error_boundary node {nid!r} compensation target "
                            f"{compensate.node_id!r} failed while handling target "
                            f"{target.node_id!r}: original error: {exc}; compensation "
                            f"error: {compensation_exc}"
                        ) from compensation_exc
                    fallback_output = comp_step.output or fallback_output
                    compensation_tool_calls = comp_step.tool_calls
                    error_payload["compensation_node_id"] = compensate.node_id
                    error_payload["compensation_node_type"] = compensate.node_type.value
                    if comp_outputs:
                        error_payload["compensation_outputs"] = comp_outputs
                    if comp_artifacts:
                        error_payload["artifacts"] = comp_artifacts
                _publish_declared_outputs(
                    node,
                    port_values,
                    {"output": fallback_output, "error": error_payload},
                    fallback=fallback_output,
                )
                return compensation_tokens, NodeStep(
                    nid,
                    ntype,
                    "ok",
                    output=fallback_output,
                    tool_calls=compensation_tool_calls,
                    detail=f"handled error: {exc}",
                )
        _publish_declared_outputs(
            node,
            port_values,
            {"output": fallback_output, "error": {}},
            fallback=fallback_output,
        )
        return 0, NodeStep(nid, ntype, "ok", output=fallback_output)

    if isinstance(node, IRTool):
        binding = node.binding
        if binding is None:
            raise ToolExecutionError(f"tool node {nid!r} has no bound tool configuration")
        fallback_source: Any | None = inputs.get("input")
        if fallback_source is None and "arguments" in inputs:
            fallback_source = inputs.get("arguments")
        if fallback_source is None and inputs:
            fallback_source = next(iter(inputs.values()))
        if fallback_source is None:
            fallback_source = run_input
        arguments = _tool_arguments_from_node_inputs(inputs)
        fallback_input = _stringify_inline_target_input(fallback_source)
        approved = approved_human_approval_nodes or set()
        if binding.requires_approval and runtime_approvals_enabled and nid not in approved:
            return 0, NodeStep(
                nid,
                ntype,
                "blocked",
                output=fallback_input,
                detail=f"waiting_approval:{nid}",
            )
        override = (extra_tools or {}).get(binding.local_name)
        openapi_effect: _GovernedOpenApi | None = None
        mcp_effect: _GovernedMcp | None = None
        if binding.binding_type == "mcp_tool":
            mcp_effect = _governed_mcp_route(
                plan,
                node_id=nid,
                server_id=binding.mcp_server_id or "",
                tool_name=binding.mcp_tool_name or "",
                timeout_seconds=binding.timeout_seconds or _mcp_tool.DEFAULT_TIMEOUT_SECONDS,
                arguments=_mcp_arguments_from_input(arguments or fallback_input),
                preview=preview,
                overridden=override is not None,
            )
            route = (
                None
                if mcp_effect is None
                else ToolRoute(mcp_effect.route.route, mcp_effect.route.effect_class, trusted=False)
            )
        elif (
            binding.binding_type == "registered_function"
            and binding.execution_backend == OPENAPI_BACKEND
        ):
            openapi_effect = _governed_openapi_route(
                plan,
                binding,
                node_id=nid,
                preview=preview,
                arguments=arguments,
                overridden=override is not None,
            )
            route = None if openapi_effect is None else openapi_effect.route
        else:
            route = _governed_tool_route(plan, binding, node_id=nid, preview=preview)
        # A governed call is never retried: a second attempt of a write could repeat its effect.
        run_binding = replace(binding, max_retries=0) if route is not None else binding
        fn = (
            _resilient_callable(run_binding, override)
            if override is not None
            else _resolve_bound_tool_callable(
                run_binding,
                plan.resolver,
                preview=preview,
                required=True,
                egress_policy=plan.egress_policy,
                mcp_identity=plan.mcp_identity,
                # Only the development profile with no authority bound sends a protected OpenAPI call inline (#1068);
                # a preview never reaches the downstream, and a bound context always routes through the dispatcher.
                legacy_openapi=(
                    openapi_effect is None
                    and binding.execution_backend == OPENAPI_BACKEND
                    and _legacy_effects_allowed(plan)
                    and not preview
                ),
            )
        )
        assert fn is not None
        tracer = get_tracer()
        started = time.perf_counter()
        with tracer.span(
            f"tool.{binding.local_name}",
            span_type="TOOL",
            attributes={
                "caliber.tool": binding.local_name,
                "caliber.tool.input": arguments or fallback_input,
            },
        ) as span:
            try:
                if route is None:
                    result = _call_tool(fn, arguments, fallback_input=fallback_input)
                elif openapi_effect is not None:
                    result = _perform_openapi_tool_effect(
                        plan, node_id=nid, openapi=openapi_effect, arguments=arguments
                    )
                elif mcp_effect is not None:
                    result = {
                        "server_id": mcp_effect.call.server_id,
                        "tool_name": mcp_effect.call.tool_name,
                        "arguments": mcp_effect.arguments,
                        "result": _perform_mcp_tool_effect(plan, node_id=nid, mcp=mcp_effect),
                    }
                else:
                    result = _perform_registered_tool_effect(
                        plan,
                        node_id=nid,
                        binding=binding,
                        route=route,
                        fn=fn,
                        arguments=arguments,
                        fallback_input=fallback_input,
                    )
            finally:
                span.set_attribute(
                    "caliber.tool.latency_ms",
                    round((time.perf_counter() - started) * 1000, 3),
                )
            span.set_attribute("caliber.tool.output", result)
        result_payload = result
        if binding.binding_type == "mcp_tool" and isinstance(result, dict):
            result_payload = result.get("result", result)
        text = _tool_node_result_text(binding, result)
        tool_metadata: dict[str, Any] = {
            "tool_name": binding.local_name,
            "registry_ref": binding.registry_ref,
            "binding_type": binding.binding_type,
            "requires_approval": binding.requires_approval,
            "side_effect_level": binding.side_effect_level,
            "arguments": arguments,
        }
        if binding.binding_type == "registered_function":
            tool_metadata["module_path"] = binding.module_path
            tool_metadata["callable_name"] = binding.callable_name
        else:
            tool_metadata["server_id"] = binding.mcp_server_id
            tool_metadata["remote_tool_name"] = binding.mcp_tool_name
        tool_call = {
            "tool": binding.local_name,
            "registry_ref": binding.registry_ref,
            "binding_type": binding.binding_type,
            "arguments": arguments,
            "result": result_payload,
        }
        _publish_declared_outputs(
            node,
            port_values,
            {
                "text": text,
                "result": result_payload,
                "metadata": tool_metadata,
                "tool_calls": [tool_call],
            },
            fallback=text,
        )
        detail = f"invoked {binding.local_name}"
        if binding.binding_type == "mcp_tool" and binding.mcp_server_id:
            detail += f" via {binding.mcp_server_id}"
        return 0, NodeStep(
            nid,
            ntype,
            "ok",
            output=text,
            tool_calls=[tool_call],
            detail=detail,
        )

    if isinstance(node, IRMcpResource):
        node_input: Any | None = inputs.get("arguments")
        if node_input is None and "input" in inputs:
            node_input = inputs["input"]
        if node_input is None and inputs:
            node_input = next(iter(inputs.values()))
        if node_input is None:
            node_input = run_input
        arguments = _mcp_arguments_from_input(node_input)
        node_mcp = _governed_mcp_route(
            plan,
            node_id=nid,
            server_id=node.server_id,
            tool_name=node.tool_name,
            timeout_seconds=node.timeout_seconds,
            arguments=arguments,
            preview=preview,
        )
        try:
            if node_mcp is not None:
                result = _perform_mcp_tool_effect(plan, node_id=nid, mcp=node_mcp)
            else:
                invoke_kwargs: dict[str, Any] = {
                    "server_id": node.server_id,
                    "tool_name": node.tool_name,
                    "arguments": arguments,
                    "timeout_seconds": node.timeout_seconds,
                }
                if plan.mcp_identity is not None:
                    invoke_kwargs["identity"] = plan.mcp_identity
                result = invoke_tool_by_server_id_sync(**invoke_kwargs)
        except McpGatewayError as exc:
            raise ToolExecutionError(
                f"MCP node {nid!r} failed for {node.server_id!r}/{node.tool_name!r}: {exc}"
            ) from exc
        except EffectRefused as exc:
            raise ToolExecutionError(
                f"MCP node {nid!r} refused for {node.server_id!r}/{node.tool_name!r}: {exc.code}"
            ) from exc
        text = _mcp_result_text(result)
        tool_call = {
            "tool": f"mcp:{node.server_id}/{node.tool_name}",
            "server_id": node.server_id,
            "tool_name": node.tool_name,
            "arguments": arguments,
            "result": result,
        }
        _publish_declared_outputs(
            node,
            port_values,
            {
                "text": text,
                "result": result,
                "metadata": {
                    "server_id": node.server_id,
                    "tool_name": node.tool_name,
                    "arguments": arguments,
                },
                "tool_calls": [tool_call],
            },
            fallback=text,
        )
        if "tool_calls" in node.outputs:
            port_values[(nid, "tool_calls")] = [tool_call]
        return 0, NodeStep(
            nid,
            ntype,
            "ok",
            output=text,
            tool_calls=[tool_call],
            detail=f"invoked {node.tool_name} on {node.server_id}",
        )

    if isinstance(node, IRWebhook):
        url = node.url.strip()
        if not url:
            raise ToolExecutionError(f"webhook node {nid!r} requires a url")
        body: Any | None = inputs.get("payload")
        if body is None and "input" in inputs:
            body = inputs["input"]
        if body is None and inputs:
            body = next(iter(inputs.values()))
        if body is None:
            body = run_input
        request = {
            "url": url,
            "method": node.method,
            "headers": dict(node.headers),
            "timeout_seconds": node.timeout_seconds,
            "body": body,
        }
        try:
            result = _perform_http_effect(plan, node=node, node_id=nid, request=request)
        except (ToolExecutionError, _step_proposal.EffectApprovalPending):
            raise  # a refusal is already worded; a pending approval is the node's park, not a failure
        except Exception as exc:
            raise ToolExecutionError(
                f"webhook node {nid!r} failed for {node.method} {url}: {type(exc).__name__}: {exc}"
            ) from exc
        result_dict = result if isinstance(result, dict) else {"response": result}
        status_code = result_dict.get("status_code")
        response_text = str(result_dict.get("text") or "")
        if not response_text and result_dict.get("json") is not None:
            response_text = json.dumps(result_dict["json"])
        metadata = {
            "url": url,
            "method": node.method,
            "status_code": status_code,
        }
        _publish_declared_outputs(
            node,
            port_values,
            {"text": response_text, "response": result_dict, "metadata": metadata},
            fallback=response_text,
        )
        return 0, NodeStep(
            nid,
            ntype,
            "ok",
            output=response_text,
            detail=f"{node.method} {url}"
            + (f" -> {status_code}" if status_code is not None else ""),
        )

    if isinstance(node, IRApiRequest):
        if node.mode == "curl":
            parsed = _parse_curl(node.curl)
            method = str(parsed["method"])
            req_url = str(parsed["url"])
            req_headers = {str(k): str(v) for k, v in parsed["headers"].items()}
            req_body: Any | None = _coerce_request_body(parsed["body"])
        else:
            req_url = node.url.strip()
            if not req_url:
                raise ToolExecutionError(f"api_request node {nid!r} requires a url")
            method = node.method
            req_headers = dict(node.headers)
            if node.body.strip():
                req_body = _coerce_request_body(node.body)
            else:
                req_body = inputs.get("payload")
                if req_body is None and "input" in inputs:
                    req_body = inputs["input"]
                if req_body is None and inputs:
                    req_body = next(iter(inputs.values()))
                if req_body is None:
                    req_body = run_input
        request = {
            "url": req_url,
            "method": method,
            "headers": req_headers,
            "timeout_seconds": node.timeout_seconds,
            "body": req_body,
        }
        try:
            api_result = _perform_http_effect(plan, node=node, node_id=nid, request=request)
        except (ToolExecutionError, _step_proposal.EffectApprovalPending):
            raise  # a refusal is already worded; a pending approval is the node's park, not a failure
        except Exception as exc:
            raise ToolExecutionError(
                f"api_request node {nid!r} failed for {method} {req_url}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        api_dict = api_result if isinstance(api_result, dict) else {"response": api_result}
        api_status = api_dict.get("status_code")
        api_text = str(api_dict.get("text") or "")
        if not api_text and api_dict.get("json") is not None:
            api_text = json.dumps(api_dict["json"])
        _publish_declared_outputs(
            node,
            port_values,
            {
                "text": api_text,
                "response": api_dict,
                "metadata": {
                    "url": req_url,
                    "method": method,
                    "mode": node.mode,
                    "status_code": api_status,
                },
            },
            fallback=api_text,
        )
        return 0, NodeStep(
            nid,
            ntype,
            "ok",
            output=api_text,
            detail=f"{method} {req_url}" + (f" -> {api_status}" if api_status is not None else ""),
        )

    if isinstance(node, IRKnowledgeQuery):
        if plan.knowledge_query_runner is None:
            raise ToolExecutionError(
                "knowledge query runner is not configured for this runtime plan"
            )
        question = str(
            inputs.get("question")
            if inputs.get("question") not in (None, "")
            else (_first_str(inputs) or run_input)
        ).strip()
        if not question:
            raise ToolExecutionError(f"knowledge_query node {nid!r} requires a question input")
        version_ids = _normalize_string_list(
            inputs.get("version_ids", node.version_ids),
            max_items=3,
        )
        retrieval_modes = _normalize_string_list(
            inputs.get("retrieval_modes", node.retrieval_modes),
            max_items=2,
        )
        graph_overrides = {
            **(node.graph_overrides or {}),
            **(_normalize_graph_overrides(inputs.get("graph_overrides")) or {}),
        }
        payload = plan.knowledge_query_runner(
            {
                "knowledge_base_id": node.knowledge_base_id or None,
                "version_ids": version_ids,
                "question": question,
                "history": _normalize_message_history(inputs.get("history")),
                "top_k": node.top_k,
                "chat_model": node.chat_model,
                "retrieval_modes": retrieval_modes,
                "graph_overrides": graph_overrides or None,
                "_context_node_id": nid,
                "_context_occurrence_id": _STEP_OCCURRENCE.get(),
                "_context_attempt": _STEP_ATTEMPT.get(),
            }
        )
        versions = payload.get("versions") if isinstance(payload, dict) else None
        primary = versions[0] if isinstance(versions, list) and versions else {}
        answer = ""
        citations: list[Any] = []
        chunks: list[Any] = []
        graph_context: dict[str, Any] = {}
        detail = f"queried {len(versions) if isinstance(versions, list) else 0} retrieval result(s)"
        if isinstance(primary, dict):
            if isinstance(primary.get("answer"), str) and primary.get("answer"):
                answer = str(primary["answer"])
            elif isinstance(primary.get("answer_error"), str):
                answer = str(primary["answer_error"])
            citations = list(primary.get("citations") or [])
            chunks = list(primary.get("retrieved_chunks") or [])
            graph_context = dict(primary.get("graph_context") or {})
            retrieval_mode = primary.get("retrieval_mode")
            if isinstance(retrieval_mode, str) and retrieval_mode:
                detail += f" via {retrieval_mode}"
            if citations:
                detail += f" · {len(citations)} citation{'s' if len(citations) != 1 else ''}"
            if chunks:
                detail += f" · {len(chunks)} chunk{'s' if len(chunks) != 1 else ''}"
            fallback_mode = graph_context.get("fallback_retrieval_mode")
            if isinstance(fallback_mode, str) and fallback_mode:
                detail += f" · fallback {fallback_mode}"
            age_seed_strategy = graph_context.get("age_seed_strategy")
            if age_seed_strategy == "query_text":
                detail += " · seeded from question text"
            elif age_seed_strategy == "query_entities":
                detail += " · seeded from entities"
        _publish_declared_outputs(
            node,
            port_values,
            {
                "text": answer,
                "answer": answer,
                "result": payload,
                "citations": citations,
                "chunks": chunks,
                "graph_context": graph_context,
            },
            fallback=answer,
        )
        return 0, NodeStep(nid, ntype, "ok", output=answer, detail=detail)

    if isinstance(node, IRKnowledgeBuild):
        if preview:
            preview_text = "Preview skipped the knowledge build launch."
            _publish_declared_outputs(
                node,
                port_values,
                {
                    "text": preview_text,
                    "result": {
                        "status": "preview_skipped",
                        "preview": True,
                        "knowledge_base_id": node.knowledge_base_id,
                    },
                    "status": "preview_skipped",
                },
                fallback=preview_text,
            )
            return 0, NodeStep(
                nid,
                ntype,
                "skipped",
                output=preview_text,
                detail="preview skipped knowledge build launch",
            )
        if plan.knowledge_build_runner is None:
            raise ToolExecutionError(
                "knowledge build runner is not configured for this runtime plan"
            )
        knowledge_base_id = str(node.knowledge_base_id or "").strip()
        if not knowledge_base_id:
            raise ToolExecutionError(f"knowledge_build node {nid!r} requires a knowledge_base_id")
        runtime_chunking_strategy = (
            str(inputs.get("chunking_strategy")).strip()
            if isinstance(inputs.get("chunking_strategy"), str)
            else ""
        )
        runtime_embedding_model = (
            str(inputs.get("embedding_model")).strip()
            if isinstance(inputs.get("embedding_model"), str)
            else ""
        )
        chunking_strategy = runtime_chunking_strategy or node.chunking_strategy
        embedding_model = runtime_embedding_model or node.embedding_model
        if not chunking_strategy:
            raise ToolExecutionError(f"knowledge_build node {nid!r} requires a chunking_strategy")
        if not embedding_model:
            raise ToolExecutionError(f"knowledge_build node {nid!r} requires an embedding_model")
        chunking_config = {
            **dict(node.chunking_config or {}),
            **(_normalize_object_payload(inputs.get("chunking_config")) or {}),
        }
        graph_config = {
            **(dict(node.graph_config or {}) if isinstance(node.graph_config, dict) else {}),
            **(_normalize_object_payload(inputs.get("graph_config")) or {}),
        }
        sources = _normalize_structured_list(inputs.get("sources"), max_items=256)
        payload = plan.knowledge_build_runner(
            {
                "knowledge_base_id": knowledge_base_id,
                "sources": sources,
                "chunking_strategy": chunking_strategy,
                "embedding_model": embedding_model,
                "chunking_config": chunking_config,
                "graph_config": graph_config or None,
                "activate_when_complete": node.activate_when_complete,
                "wait_for_completion": node.wait_for_completion,
                "wait_timeout_seconds": node.wait_timeout_seconds,
            }
        )
        knowledge_base = payload.get("knowledge_base") if isinstance(payload, dict) else None
        version = payload.get("version") if isinstance(payload, dict) else None
        run = payload.get("run") if isinstance(payload, dict) else None
        status = (
            str(payload.get("status")).strip()
            if isinstance(payload, dict) and isinstance(payload.get("status"), str)
            else ""
        )
        activation = (
            dict(payload.get("activation") or {})
            if isinstance(payload, dict) and isinstance(payload.get("activation"), dict)
            else {}
        )
        wait_meta = (
            dict(payload.get("await_completion") or {})
            if isinstance(payload, dict) and isinstance(payload.get("await_completion"), dict)
            else {}
        )
        version_id = (
            str(version.get("knowledge_base_version_id")).strip()
            if isinstance(version, dict)
            and isinstance(version.get("knowledge_base_version_id"), str)
            else ""
        )
        run_id = (
            str(run.get("knowledge_base_run_id")).strip()
            if isinstance(run, dict) and isinstance(run.get("knowledge_base_run_id"), str)
            else ""
        )
        raw_version_number = version.get("version_number") if isinstance(version, dict) else None
        version_number = raw_version_number if isinstance(raw_version_number, int) else None
        output_text = (
            str(payload.get("summary")).strip()
            if isinstance(payload, dict) and isinstance(payload.get("summary"), str)
            else ""
        )
        if not output_text:
            version_label = f"v{version_number}" if version_number is not None else "new version"
            status_label = status or (
                str(version.get("status")).strip()
                if isinstance(version, dict) and isinstance(version.get("status"), str)
                else "started"
            )
            output_text = f"Knowledge build {status_label} for {version_label}."
        detail_parts = []
        if version_number is not None:
            detail_parts.append(f"v{version_number}")
        if status:
            detail_parts.append(status)
        if activation.get("status") == "activated":
            detail_parts.append("activated")
        elif activation.get("status") == "pending":
            detail_parts.append("activation deferred")
        wait_status = activation.get("wait_status") or wait_meta.get("status")
        if isinstance(wait_status, str) and wait_status == "timeout":
            detail_parts.append("await timeout")
        if not detail_parts:
            detail_parts.append("launched knowledge build")
        _publish_declared_outputs(
            node,
            port_values,
            {
                "text": output_text,
                "result": payload,
                "knowledge_base": knowledge_base,
                "version": version,
                "run": run,
                "status": status or "",
                "version_id": version_id,
                "run_id": run_id,
            },
            fallback=output_text,
        )
        return 0, NodeStep(
            nid,
            ntype,
            "ok",
            output=output_text,
            detail=" · ".join(detail_parts),
        )

    if isinstance(node, IRTemplate):
        context = _template_render_context(inputs, run_input)
        if node.output_format == "json":
            text, result_payload, used_variables, missing_variables = _render_json_template(
                node.template,
                context=context,
                missing_variable_mode=node.missing_variable_mode,
            )
        else:
            text, used_variables, missing_variables = _render_text_template(
                node.template,
                context=context,
                missing_variable_mode=node.missing_variable_mode,
            )
            result_payload = {"rendered": text}
        metadata = {
            "output_format": node.output_format,
            "missing_variable_mode": node.missing_variable_mode,
            "used_variables": used_variables,
            "missing_variables": missing_variables,
            "rendered_bytes": len(text.encode("utf-8")),
        }
        _publish_declared_outputs(
            node,
            port_values,
            {"text": text, "result": result_payload, "metadata": metadata},
            fallback=text,
        )
        detail_parts = [f"rendered {node.output_format} template"]
        if used_variables:
            detail_parts.append(
                f"{len(used_variables)} variable{'s' if len(used_variables) != 1 else ''}"
            )
        if missing_variables:
            detail_parts.append(f"{len(missing_variables)} missing")
        if not used_variables and not missing_variables:
            detail_parts.append("static")
        return 0, NodeStep(nid, ntype, "ok", output=text, detail=" · ".join(detail_parts))

    if isinstance(node, IRDataTransform):
        from caliber.workflows.data_transform import (  # noqa: PLC0415
            DataTransformError,
            apply_data_transform,
            transform_text,
        )

        transform_value: Any = inputs.get("value")
        if transform_value is None:
            transform_value = inputs.get("text", run_input)
        if isinstance(transform_value, str):
            with contextlib.suppress(ValueError, TypeError):
                transform_value = json.loads(transform_value)
        try:
            transformed = apply_data_transform(node.operation, transform_value, node.config)
        except DataTransformError as exc:
            raise ToolExecutionError(f"data_transform node {nid!r} failed: {exc}") from exc
        if not transformed["valid"] and node.fail_on_invalid:
            errors = transformed.get("metadata", {}).get("errors", [])
            detail = errors[0]["message"] if errors else "input did not match the configured schema"
            raise ToolExecutionError(f"data_transform node {nid!r} validation failed: {detail}")
        text = transform_text(transformed["result"])
        _publish_declared_outputs(
            node,
            port_values,
            {
                "text": text,
                "result": transformed["result"],
                "valid": transformed["valid"],
                "metadata": transformed["metadata"],
            },
            fallback=text,
        )
        return 0, NodeStep(
            nid,
            ntype,
            "ok",
            output=text,
            detail=f"applied {node.operation.replace('_', ' ')}",
        )

    if isinstance(node, IRReviewQueueEnqueue):
        if plan.review_queue_enqueuer is None:
            raise ToolExecutionError(
                f"review_queue_enqueue node {nid!r} requires the CALIBER database runtime"
            )
        raw_trace_ids = inputs.get("trace_ids")
        if isinstance(raw_trace_ids, list):
            trace_ids = [str(item).strip() for item in raw_trace_ids if str(item).strip()]
        else:
            trace_id = str(inputs.get("trace_id") or run_input).strip()
            trace_ids = [trace_id] if trace_id else []
        if not trace_ids:
            raise ToolExecutionError(f"review_queue_enqueue node {nid!r} requires a trace ID")
        payload = plan.review_queue_enqueuer(
            {
                "queue_id": node.queue_id,
                "trace_ids": list(dict.fromkeys(trace_ids)),
                "experiment_id": node.experiment_id,
                "assigned_to": node.assigned_to,
            }
        )
        created_count = int(payload.get("created_count", 0))
        text = f"Enqueued {created_count} trace{'s' if created_count != 1 else ''} for review."
        _publish_declared_outputs(
            node,
            port_values,
            {"text": text, "result": payload, "created_count": created_count},
            fallback=text,
        )
        return 0, NodeStep(nid, ntype, "ok", output=text, detail=text)

    if isinstance(node, IRPythonCode):
        # Arbitrary authored Python: not supported in the reference profile, whatever sandbox backend is configured (EP-011).
        refuse_in_reference_profile("EP-011", _operator_config(plan))
        python_input = _select_input(inputs, run_input)
        sandbox_kwargs: dict[str, Any] = {"default_timeout_seconds": node.timeout_seconds}
        configured_limits: tuple[tuple[str, int | None], ...] = (
            ("max_output_bytes", plan.max_output_bytes),
            ("max_memory_bytes", plan.sandbox_max_memory_bytes),
            ("max_file_bytes", plan.sandbox_max_file_bytes),
            ("max_open_files", plan.sandbox_max_open_files),
        )
        for kwarg, limit in configured_limits:
            if limit is not None:
                sandbox_kwargs[kwarg] = limit
        # Through the configured factory, not a hardcoded class: an operator who points
        # CALIBER_TOOL_SANDBOX_BACKEND at a container-backed sandbox was getting it for
        # registered tools and *not* here — and this is the path that runs arbitrary
        # user-authored code, so it is the one that most needs the stronger boundary.
        # The node timeout still wins; the remaining resource limits come from the plan's
        # resolved operator configuration.
        sandbox = sandbox_from_optional_config(
            _operator_config(plan),
            default_timeout_seconds=node.timeout_seconds,
        )
        for _attr, _value in sandbox_kwargs.items():
            with contextlib.suppress(AttributeError):
                setattr(sandbox, _attr, _value)
        sandbox_request = ToolSandboxRunRequest(
            source_code=_python_node_source(node.code),
            callable_name=_PYTHON_NODE_CALLABLE,
            input={
                "input": python_input,
                "context": _json_compatible(inputs.get("context", dict(inputs))),
                "inputs": _json_compatible(dict(inputs)),
                "run_input": run_input,
            },
            timeout_seconds=node.timeout_seconds,
        )
        result = sandbox.run_tool(sandbox_request)
        if result.status == "timed_out":
            raise ToolExecutionError(
                f"python_code node {nid!r} timed out after {node.timeout_seconds}s"
            )
        if result.status != "completed":
            error_text = result.error or result.stderr or "sandbox execution failed"
            raise ToolExecutionError(f"python_code node {nid!r} failed: {error_text}")

        code_output = result.output
        text = _python_node_text(code_output)
        metadata = {
            "status": result.status,
            "duration_ms": result.duration_ms,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        published_outputs = {"text": text, "result": code_output, "metadata": metadata}
        if isinstance(code_output, dict):
            for key, value in code_output.items():
                published_outputs.setdefault(str(key), value)
        _publish_declared_outputs(
            node,
            port_values,
            published_outputs,
            fallback=text,
        )
        return 0, NodeStep(
            nid,
            ntype,
            "ok",
            output=text,
            detail=f"sandbox duration: {result.duration_ms:.1f} ms",
        )

    if isinstance(node, IRAgent):
        ungoverned = [
            agent_id
            for agent_id in _handoff_closure(ir, nid)
            if agent_id in plan.agents_requiring_governance
            and not (
                plan.governed_agent_steps is not None
                and plan.governed_agent_steps.governs(agent_id)
            )
        ]
        if ungoverned and not preview:
            raise ToolExecutionError(
                f"agent {ungoverned[0]!r} can reach a tool the operator mapped to a governed backend, and this plan has no "
                "governed step for it (a sub-workflow, a synchronous run, or a plan built outside the worker's own run): "
                "it does not run as a legacy agent"
            )
        if _governs_agent(plan, node):
            return _run_governed_agent_node(
                node,
                ir,
                plan,
                executor,
                inputs=inputs,
                run_input=run_input,
                port_values=port_values,
                preview=preview,
                extra_tools=extra_tools,
            )
        agent_input = _select_input(inputs, run_input)
        explicit_history = _normalize_message_history(inputs.get("history"))
        callables = _resolve_tool_callables(
            node,
            plan.resolver,
            preview=preview,
            egress_policy=plan.egress_policy,
            mcp_identity=plan.mcp_identity,
        )
        if extra_tools:
            callables = {**callables, **extra_tools}
        callables = _gate_model_callables(plan, node, callables, preview=preview)
        handoff_agents = _collect_agent_handoff_specs(
            node,
            ir,
            plan,
            preview=preview,
            root_tool_callables=callables,
            extra_tools=extra_tools,
        )
        result, published_history, source_history = _run_agent_with_history(
            plan,
            executor,
            node,
            agent_input,
            context_inputs=(
                _agent_context_inputs(ir, node, inputs, port_values, agent_input, plan)
                if plan.context_recorder is not None
                else None
            ),
            explicit_history=explicit_history,
            handoff_agents=handoff_agents,
            tool_callables=callables,
            preview=preview,
        )
        tokens = result.tokens
        prompt_tokens = result.prompt_tokens
        completion_tokens = result.completion_tokens
        cached_prompt_tokens = result.cached_prompt_tokens
        cost_usd = result.cost_usd
        output = result.final_output
        structured_output = result.structured_output
        model = result.model
        prompt_version = result.prompt_version
        current_agent = node
        current_result = result
        current_input = agent_input
        current_published_history = published_history
        current_source_history = source_history
        current_handoff_agents = handoff_agents
        initial_handoff_target: str | None = None
        all_tool_calls = list(result.tool_calls)
        handoff_hops = 0

        while True:
            handoff_target = _resolve_agent_handoff_target(
                current_agent,
                current_result,
                ir,
                input_text=current_input,
                history=current_source_history,
            )
            current_result.handoff_target = handoff_target
            if initial_handoff_target is None:
                initial_handoff_target = handoff_target
            if not handoff_target or not isinstance(ir.nodes.get(handoff_target), IRAgent):
                break
            if handoff_hops >= MAX_AGENT_HANDOFF_HOPS:
                logger.warning(
                    "workflow agent %s exceeded the runtime handoff hop cap (%s); returning the latest output",
                    nid,
                    MAX_AGENT_HANDOFF_HOPS,
                )
                break
            target = ir.nodes[handoff_target]
            assert isinstance(target, IRAgent)
            handoff_spec = next(
                (
                    handoff
                    for handoff in current_agent.handoffs
                    if handoff.target_node_id == handoff_target
                ),
                None,
            )
            if current_handoff_agents and target.node_id in current_handoff_agents:
                t_callables = dict(current_handoff_agents[target.node_id][1])
            else:
                t_callables = _resolve_tool_callables(
                    target,
                    plan.resolver,
                    preview=preview,
                    egress_policy=plan.egress_policy,
                    mcp_identity=plan.mcp_identity,
                )
                if extra_tools:
                    t_callables = {**t_callables, **extra_tools}
                t_callables = _gate_model_callables(plan, target, t_callables, preview=preview)
            t_handoff_agents = _collect_agent_handoff_specs(
                target,
                ir,
                plan,
                preview=preview,
                root_tool_callables=t_callables,
                extra_tools=extra_tools,
            )
            target_input = current_input
            target_explicit_history: list[HistoryMessage] | None = current_published_history
            if handoff_spec is not None and str(handoff_spec.input_filter or "").strip():
                target_input = _render_handoff_filter_text(
                    handoff_spec.input_filter,
                    input_text=current_input,
                    history=current_source_history,
                    extra={
                        "turn_input": [
                            *current_source_history,
                            {"role": "user", "content": current_input},
                        ],
                        "assistant_output": current_result.final_output,
                        "final_output": current_result.final_output,
                        "structured_output": current_result.structured_output,
                        "tool_calls": current_result.tool_calls,
                    },
                )
                target_explicit_history = []
            t_result, target_history, target_source_history = _run_agent_with_history(
                plan,
                executor,
                target,
                target_input,
                context_inputs=(
                    (
                        AgentContextInput(
                            port="input",
                            text=target_input,
                            origin_node_id=current_agent.node_id,
                            origin_node_type="agent",
                        ),
                    )
                    if plan.context_recorder is not None
                    else None
                ),
                explicit_history=target_explicit_history,
                handoff_agents=t_handoff_agents,
                tool_callables=t_callables,
                preview=preview,
            )
            handoff_hops += 1
            tokens += t_result.tokens
            prompt_tokens += t_result.prompt_tokens
            completion_tokens += t_result.completion_tokens
            cached_prompt_tokens += t_result.cached_prompt_tokens
            cost_usd += t_result.cost_usd
            output = t_result.final_output
            structured_output = t_result.structured_output
            if model and t_result.model and t_result.model != model:
                model = f"{model} -> {t_result.model}"
            else:
                model = t_result.model or model
            if (
                prompt_version
                and t_result.prompt_version
                and t_result.prompt_version != prompt_version
            ):
                prompt_version = f"{prompt_version} -> {t_result.prompt_version}"
            else:
                prompt_version = t_result.prompt_version or prompt_version
            all_tool_calls.extend(t_result.tool_calls)
            current_agent = target
            current_result = t_result
            current_input = target_input
            current_published_history = target_history
            current_source_history = target_source_history
            current_handoff_agents = t_handoff_agents

        result.tool_calls = all_tool_calls
        result.handoff_target = initial_handoff_target
        published_history = current_published_history
        # Publish outputs to declared ports.
        if not node.outputs:
            port_values[(nid, "final_output")] = output
        else:
            published_values: dict[str, Any] = {
                "final_output": output,
                "tool_calls": result.tool_calls,
                "history": published_history,
            }
            structured_port = _agent_structured_output_port(node)
            if structured_output is not None and structured_port is not None:
                published_values[structured_port] = structured_output
            _publish_declared_outputs(
                node,
                port_values,
                published_values,
                fallback=output,
            )
            port_values.setdefault((nid, "final_output"), output)
        return tokens, NodeStep(
            nid,
            ntype,
            "ok",
            output=output,
            tokens=tokens,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_prompt_tokens=cached_prompt_tokens,
            cost_usd=cost_usd,
            model=model,
            prompt_version=prompt_version,
            tool_calls=result.tool_calls,
            handoff_target=result.handoff_target,
        )

    if isinstance(node, IRGuardrail):
        response_text = _first_str(inputs) or ""
        tool_calls = _find_recent_tool_calls(port_values)
        ctx = GuardrailContext(
            response_text=response_text,
            tool_calls=tool_calls,
            node_id=nid,
            occurrence_id=_STEP_OCCURRENCE.get(),
            attempt=_STEP_ATTEMPT.get(),
            context_recorder=plan.context_recorder,
        )
        from caliber.workflows.guardrails import evaluate_guardrail  # noqa: PLC0415

        results = evaluate_guardrail(node, ctx)
        for r in results:
            guardrail_results.append(
                {"node_id": nid, "kind": r.kind, "passed": r.passed, "reason": r.reason}
            )
        failed = [r for r in results if not r.passed]
        if failed and node.on_failure in ("block", "block_retry", "escalate"):
            return 0, NodeStep(nid, ntype, "blocked", output="", detail=failed[0].reason)
        # ``redact``: scrub the matched spans from the text and continue, rather
        # than halting (block) or — the prior bug — passing the unscrubbed text
        # through. Non-redactable failures (e.g. max_length) leave text as-is.
        output_text = response_text
        detail = ""
        if failed:
            if node.on_failure == "warn":
                detail = failed[0].reason
            elif node.on_failure == "redact":
                from caliber.workflows.guardrails import redact_guardrail  # noqa: PLC0415

                output_text, redacted_kinds = redact_guardrail(node, ctx)
                detail = (
                    f"redacted: {', '.join(redacted_kinds)}"
                    if redacted_kinds
                    else "redact: no redactable matches"
                )
            else:
                detail = failed[0].reason
        # passthrough: publish the (possibly redacted) text on declared outputs.
        for port in node.outputs:
            port_values[(nid, port)] = output_text
        if not node.outputs:
            port_values[(nid, "passthrough")] = output_text
        return 0, NodeStep(nid, ntype, "ok", output=output_text, detail=detail)

    if isinstance(node, IRRouter):
        if not node.branches:
            raise ToolExecutionError(f"router node {nid!r} requires at least one branch")
        router_target = _route(node, inputs)
        for port in node.outputs:
            port_values[(nid, port)] = _first_str(inputs) or run_input
        return 0, NodeStep(nid, ntype, "ok", output="", handoff_target=router_target)

    if isinstance(node, IRHumanApproval):
        value = _first_str(inputs) or run_input
        approved = approved_human_approval_nodes or set()
        if runtime_approvals_enabled and nid not in approved:
            return 0, NodeStep(
                nid,
                ntype,
                "blocked",
                output=value,
                detail=f"waiting_approval:{nid}",
            )
        for port in node.outputs:
            port_values[(nid, port)] = value
        return 0, NodeStep(
            nid, ntype, "ok", output=value, detail="approval required (pass-through in MVP)"
        )

    if isinstance(node, IRExternalApp):
        try:
            external_output, metadata = _run_external_app_entrypoint(
                node=node,
                ir=ir,
                nid=nid,
                inputs=inputs,
                run_input=run_input,
                preview=preview,
                entrypoint_allowlist=plan.external_app_entrypoint_allowlist,
                config=_operator_config(plan),
            )
        except (ToolExecutionError, EffectRefused):
            raise
        except Exception as exc:
            raise ToolExecutionError(
                f"external_app node {nid!r} failed: {type(exc).__name__}: {exc}"
            ) from exc

        text = _python_node_text(external_output)
        published: dict[str, Any] = {}
        if isinstance(external_output, dict):
            published.update(external_output)
        published.setdefault("text", text)
        published.setdefault("result", external_output)
        published["metadata"] = metadata
        _publish_declared_outputs(node, port_values, published, fallback=text)
        return 0, NodeStep(
            nid,
            ntype,
            "ok",
            output=text,
            detail=f"invoked {metadata['entrypoint']} in {metadata['duration_ms']:.1f} ms",
        )

    if node.node_type == NodeType.OUTPUT:
        value = _first_str(inputs) or ""
        return 0, NodeStep(nid, ntype, "ok", output=value)

    # NoteNode / unknown: no-op.
    return 0, NodeStep(nid, ntype, "ok")


def _run_node_traced(
    node: IRNode,
    ir: IRWorkflow,
    plan: RuntimePlan,
    *,
    executor: WorkflowExecutor,
    preview: bool,
    inputs: dict[str, Any],
    run_input: str,
    port_values: dict[tuple[str, str], Any],
    guardrail_results: list[dict[str, Any]],
    extra_tools: dict[str, Callable[..., Any]] | None = None,
    runtime_approvals_enabled: bool = False,
    approved_human_approval_nodes: set[str] | None = None,
) -> tuple[int, NodeStep]:
    """Run one non-agent node inside a span named by node id + kind.

    Broadens trace coverage to every graph node — router, loop, parallel/join,
    guardrail, knowledge query/build, output, subworkflow, template, python_code,
    etc. — so a run trace shows the full picture rather than only the agent/tool
    spans. ``IRAgent`` is deliberately *not* wrapped here: it already emits its
    own ``AGENT`` span (with nested ``TOOL`` spans) via ``_run_agent_traced``, so
    wrapping it again would double-count. The span is additive and no-op-safe
    (an inert tracer returns immediately) and never alters control flow — the
    node's ``(tokens, step)`` result and any exception propagate unchanged.
    """
    if isinstance(node, IRAgent):
        # Agent nodes own their AGENT span (+ nested TOOL spans); don't double-wrap.
        return _run_node(
            node,
            ir,
            plan,
            executor=executor,
            preview=preview,
            inputs=inputs,
            run_input=run_input,
            port_values=port_values,
            guardrail_results=guardrail_results,
            extra_tools=extra_tools,
            runtime_approvals_enabled=runtime_approvals_enabled,
            approved_human_approval_nodes=approved_human_approval_nodes,
        )
    tracer = get_tracer()
    with tracer.span(
        f"node.{node.node_id}",
        span_type=_node_span_type(node),
        attributes={
            "caliber.node_id": node.node_id,
            "caliber.node_type": node.node_type.value,
        },
    ) as span:
        tokens, step = _run_node(
            node,
            ir,
            plan,
            executor=executor,
            preview=preview,
            inputs=inputs,
            run_input=run_input,
            port_values=port_values,
            guardrail_results=guardrail_results,
            extra_tools=extra_tools,
            runtime_approvals_enabled=runtime_approvals_enabled,
            approved_human_approval_nodes=approved_human_approval_nodes,
        )
        # The interpreter captures most failures as a non-"ok" step status rather
        # than raising, so surface that on the span (the context manager already
        # records "completed"; this overrides it for blocked/error/skipped steps).
        if step.status and step.status != "ok":
            span.set_attribute("caliber.node.status", step.status)
        if step.tokens:
            span.set_attribute("caliber.tokens", step.tokens)
        if step.handoff_target:
            span.set_attribute("caliber.handoff_target", step.handoff_target)
        if step.detail:
            span.set_attribute("caliber.node.detail", step.detail)
        return tokens, step


def _inline_children(node: IRNode) -> tuple[str, ...]:
    """The nodes ``node`` runs itself, with no edge to them: a fan-out, loop or boundary's target (and compensation) and an
    agent's handoff targets. Their effects are the node's effects."""
    if isinstance(node, IRForEach | IRLoop):
        return (node.target_node_id,) if node.target_node_id else ()
    if isinstance(node, IRErrorBoundary):
        return tuple(n for n in (node.target_node_id, node.compensate_with) if n)
    if isinstance(node, IRAgent):
        return tuple(h.target_node_id for h in node.handoffs)
    return ()


def _upstream_node_ids(ir: IRWorkflow, node_id: str) -> frozenset[str]:
    """``node_id`` and everything whose effects it may depend on: every node that feeds it, directly or through others, and
    the inline children (targets, compensations, handoffs) of each of those, which have no edges of their own."""
    feeders: dict[str, set[str]] = {}
    for edge in ir.edges:
        feeders.setdefault(edge.to_node, set()).add(edge.from_node)
    seen = {node_id}
    pending = [node_id]
    while pending:
        current = pending.pop()
        node = ir.nodes.get(current)
        reached = set(feeders.get(current, ())) | set(_inline_children(node) if node else ())
        for other in reached - seen:
            seen.add(other)
            pending.append(other)
    return frozenset(seen)


def _check_effect_gate(
    plan: RuntimePlan,
    ir: IRWorkflow,
    node_id: str,
    *,
    inputs: dict[str, Any] | None = None,
) -> None:
    """Refuse to run ``node_id`` while an effect it may depend on has no final outcome (B05.5b, #742)."""
    if plan.effect_gate is not None:
        try:
            plan.effect_gate(_upstream_node_ids(ir, node_id))
        except UnknownEffectError as exc:
            if getattr(exc, "blocked_node_id", None) is None:
                exc.blocked_node_id = node_id
            if getattr(exc, "blocked_inputs", None) is None and inputs is not None:
                exc.blocked_inputs = dict(inputs)
            raise


def _run_node_with_policy(  # noqa: PLR0912, PLR0915 - policy execution has explicit retry/error branches
    node: IRNode,
    ir: IRWorkflow,
    plan: RuntimePlan,
    *,
    executor: WorkflowExecutor,
    preview: bool,
    inputs: dict[str, Any],
    run_input: str,
    port_values: dict[tuple[str, str], Any],
    guardrail_results: list[dict[str, Any]],
    extra_tools: dict[str, Callable[..., Any]] | None = None,
    runtime_approvals_enabled: bool = False,
    approved_human_approval_nodes: set[str] | None = None,
    occurrence_id: str | None = None,
    inherited_provenance: tuple[dict[str, Any], ...] = (),
) -> tuple[int, NodeStep]:
    attempts = max(1, node.execution_policy.max_retries + 1)
    last_exc: Exception | None = None
    ledger = plan.effect_ledger
    for attempt in range(attempts):
        occurrence_token = _STEP_OCCURRENCE.set(occurrence_id or node.node_id)
        attempt_token = _STEP_ATTEMPT.set(attempt + 1)
        provenance_token = _STEP_PROVENANCE.set(
            inherited_provenance or _input_context_provenance(plan, ir, node, inputs, port_values)
        )
        # Reset effect-occurrence numbering for each attempt. Without this a retry
        # re-derives occurrence 1 for an effect the previous attempt claimed as 0,
        # producing a different idempotency key, so the ledger judges an effect it
        # already recorded to be fresh and the node re-sends it. The module
        # docstring already specifies "it resets per attempt" — this is where.
        scope = (
            ledger.attempt_scope()
            if ledger is not None and hasattr(ledger, "attempt_scope")
            else contextlib.nullcontext()
        )
        timeout = node.execution_policy.timeout_seconds
        try:
            # Asked at the top of every attempt: an earlier attempt may have left this node's own effect unknown.
            _check_effect_gate(plan, ir, node.node_id, inputs=inputs)
            with scope:

                def _execute() -> tuple[int, NodeStep]:
                    return _run_node_traced(
                        node,
                        ir,
                        plan,
                        executor=executor,
                        preview=preview,
                        inputs=inputs,
                        run_input=run_input,
                        port_values=port_values,
                        guardrail_results=guardrail_results,
                        extra_tools=extra_tools,
                        runtime_approvals_enabled=runtime_approvals_enabled,
                        approved_human_approval_nodes=approved_human_approval_nodes,
                    )

                if timeout is None:
                    tokens, step = _execute()
                else:
                    # The deadline used to be evaluated only *after* the node
                    # returned, which made it two different wrong things at once: a
                    # node that hung never reached the check, and a node that
                    # SUCCEEDED but overran was turned into a failure and retried —
                    # a penalize-slow-success mechanism rather than a deadline.
                    #
                    # Nodes already execute on worker threads for parallel branches
                    # (see ``_run_parallel_branches``), including the
                    # ``copy_context`` needed to carry tracing and run context, so
                    # this is an established execution mode rather than a new one.
                    #
                    # Python cannot kill the worker; the guarantee is that the
                    # workflow stops waiting on it. ``shutdown(wait=False)`` on the
                    # timeout path is what makes that true — the same correction
                    # made in ``_resilient_callable``.
                    # A backstop, not a pre-emption. Several node kinds bound
                    # themselves with this same value and raise a message naming
                    # the node kind ("external_app node 'x' timed out"), which is
                    # more actionable than a generic one. Enforcing the identical
                    # deadline out here turned that into a race the generic
                    # message could win. The grace lets a node that handles its
                    # own deadline report first, and still bounds one that does
                    # not — which is the case this exists for.
                    pool = ThreadPoolExecutor(max_workers=1)
                    timed_out = False
                    try:
                        future = pool.submit(contextvars.copy_context().run, _execute)
                        tokens, step = future.result(timeout=timeout + _NODE_DEADLINE_GRACE_SECONDS)
                    except FuturesTimeout as exc:
                        timed_out = True
                        raise ToolExecutionError(
                            f"node {node.node_id!r} timed out after {timeout}s"
                        ) from exc
                    finally:
                        pool.shutdown(wait=not timed_out, cancel_futures=timed_out)
            provenance = inherited_provenance or _input_context_provenance(
                plan, ir, node, inputs, port_values
            )
            if isinstance(node, IRKnowledgeQuery):
                provenance = provenance or _knowledge_context_provenance(
                    plan, port_values.get((node.node_id, "result"))
                )
                result_payload = port_values.get((node.node_id, "result"))
                if provenance and isinstance(result_payload, dict):
                    port_values[(node.node_id, "result")] = {
                        **result_payload,
                        "context_provenance": [dict(item) for item in provenance],
                    }
            _set_context_provenance(plan, node, provenance)
            return tokens, step
        except UnknownEffectError as exc:
            # The gate runs before the node (and inline child) starts. Keep the
            # exact boundary and gathered inputs on the exception so the outer
            # interpreter can persist an input-snapshot checkpoint rather than
            # treating the refusal as an opaque runtime failure.
            if getattr(exc, "blocked_node_id", None) is None:
                exc.blocked_node_id = node.node_id
            if getattr(exc, "blocked_inputs", None) is None:
                exc.blocked_inputs = dict(inputs)
            raise  # a retry cannot make an effect nobody can vouch for known
        except _step_proposal.EffectApprovalPending as exc:
            # The effect's approval has no decision yet: the node parks, it does not fail and is not retried (a retry would
            # only ask again). Keep the exact node and gathered inputs for the input-snapshot checkpoint, as for an open effect.
            if exc.blocked_node_id is None:
                exc.blocked_node_id = node.node_id
            if exc.blocked_inputs is None:
                exc.blocked_inputs = dict(inputs)
            raise
        except Exception as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                break
            logger.warning(
                "node %s failed (attempt %d/%d): %s",
                node.node_id,
                attempt + 1,
                attempts,
                exc,
            )
        finally:
            _STEP_PROVENANCE.reset(provenance_token)
            _STEP_ATTEMPT.reset(attempt_token)
            _STEP_OCCURRENCE.reset(occurrence_token)
    assert last_exc is not None
    raise last_exc


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _retry_blocked_guardrail(
    node: IRGuardrail,
    ir: IRWorkflow,
    plan: RuntimePlan,
    *,
    executor: WorkflowExecutor,
    preview: bool,
    in_conns: dict[str, list[_Connection]],
    port_values: dict[tuple[str, str], Any],
    run_input: str,
    guardrail_results: list[dict[str, Any]],
    steps: list[NodeStep],
    on_step: Callable[[NodeStep], None] | None = None,
) -> tuple[int, NodeStep]:
    """Re-run the agents feeding a blocked ``block_retry`` guardrail and re-check.

    Returns the extra tokens consumed and the final guardrail step (which may
    still be ``blocked`` if every retry also fails). Each retry appends its
    agent + guardrail steps to ``steps`` so the run trace shows the attempts.
    """
    extra_tokens = 0
    upstream_agents = [
        ir.nodes[c.from_node]
        for c in in_conns.get(node.node_id, [])
        if isinstance(ir.nodes.get(c.from_node), IRAgent)
    ]
    step: NodeStep | None = None
    for _attempt in range(node.max_retries):
        # Re-run each upstream agent to regenerate the guardrail's input.
        for agent in upstream_agents:
            _check_effect_gate(plan, ir, agent.node_id, inputs={})
            tokens, agent_step = _run_node(
                agent,
                ir,
                plan,
                executor=executor,
                preview=preview,
                inputs={},
                run_input=run_input,
                port_values=port_values,
                guardrail_results=guardrail_results,
            )
            agent_step = _attach_step_port_snapshots(
                agent_step,
                node=agent,
                input_by_port={},
                port_values=port_values,
            )
            extra_tokens += tokens
            steps.append(agent_step)
            if on_step is not None:
                on_step(agent_step)
            # Deliver the fresh agent output to the guardrail's input ports.
            for conn in in_conns.get(node.node_id, []):
                if conn.from_node == agent.node_id:
                    for from_out, to_in in conn.mappings:
                        if (agent.node_id, from_out) in port_values:
                            port_values[(node.node_id, to_in)] = port_values[
                                (agent.node_id, from_out)
                            ]
        tokens, step = _run_node(
            node,
            ir,
            plan,
            executor=executor,
            preview=preview,
            inputs={k: v for (n, k), v in port_values.items() if n == node.node_id},
            run_input=run_input,
            port_values=port_values,
            guardrail_results=guardrail_results,
        )
        step = _attach_step_port_snapshots(
            step,
            node=node,
            input_by_port={k: v for (n, k), v in port_values.items() if n == node.node_id},
            port_values=port_values,
        )
        extra_tokens += tokens
        steps.append(step)
        if on_step is not None:
            on_step(step)
        if step.status != "blocked":
            break
    return extra_tokens, step if step is not None else steps[-1]


def _selected_input_port(inputs: dict[str, Any]) -> str | None:
    if "input" in inputs:
        return "input"
    return next((port for port, value in inputs.items() if isinstance(value, str)), None)


def _select_input(inputs: dict[str, Any], run_input: str) -> str:
    port = _selected_input_port(inputs)
    return run_input if port is None else str(inputs[port])


def confine_to_file_root(path: Path, *, what: str) -> Path:
    """Resolve ``path`` and refuse it if it escapes ``CALIBER_WORKFLOW_FILE_ROOT``.

    Unmanaged host-filesystem nodes resolve a caller- or author-supplied string
    and open it with the server's own authority. The promotion gate in
    :mod:`caliber.workflows.promoter` keeps those nodes out of production
    aliases, but that leaves a single layer: anything reachable before promotion
    — a development alias, a preview that slipped the unisolated-node list, a
    future caller — has no boundary at all.

    Resolution happens **before** any existence check and covers symlinks, which
    is the whole point: ``~/safe/link -> /etc`` passes a string comparison and
    fails this one.

    **An unset root now refuses these nodes** rather than leaving them
    unconfined. That reverses the original default deliberately: shipping the
    permissive state left the promotion gate as the only thing between an
    operator and an arbitrary host read or write, and a single-layer defence at
    a policy boundary is the pattern this control exists to remove. Operators
    who need the old behaviour set ``CALIBER_WORKFLOW_FILE_ROOT=unconfined``,
    which is one line and, unlike the previous default, appears in the config.
    """
    from caliber.config import (  # noqa: PLC0415
        workflow_file_root,
        workflow_host_paths_allowed,
    )

    if not workflow_host_paths_allowed():
        raise ValueError(
            f"{what} path is refused: unmanaged host-filesystem nodes are disabled "
            "because CALIBER_WORKFLOW_FILE_ROOT is unset. Bind a managed project "
            "file instead, set CALIBER_WORKFLOW_FILE_ROOT to a directory these "
            "nodes may use, or set it to 'unconfined' to allow any path the "
            "server can reach."
        )
    root = workflow_file_root()
    if root is None:
        return path
    try:
        resolved = path.resolve()
    except OSError as exc:  # pragma: no cover - platform-specific resolution failure
        raise ValueError(f"{what} path cannot be resolved: {exc}") from exc
    if resolved != root and root not in resolved.parents:
        raise ValueError(
            f"{what} path {str(resolved)!r} is outside the configured workflow file "
            f"root {str(root)!r}. Bind a managed project file, or widen "
            f"CALIBER_WORKFLOW_FILE_ROOT."
        )
    return resolved


def _path_from_inputs(
    inputs: dict[str, Any],
    *,
    configured_path: str,
    run_input: str,
) -> Path:
    del run_input
    value = inputs.get("path")
    raw = str(value if value not in (None, "") else configured_path).strip()
    if not raw:
        raise ValueError(
            "file/folder input node requires a path from its path input or node configuration"
        )
    return confine_to_file_root(Path(raw).expanduser(), what="file/folder input")


def _read_text_file_node(
    path: Path,
    *,
    encoding: str,
    max_bytes: int,
) -> tuple[str, dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"file input path does not exist: {path}")
    if not path.is_file():
        raise IsADirectoryError(f"file input path is not a file: {path}")
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    truncated = len(raw) > max_bytes
    raw = raw[:max_bytes]
    text = raw.decode(encoding or "utf-8", errors="replace")
    return text, {
        "path": str(path),
        "bytes": len(raw),
        "truncated": truncated,
        "encoding": encoding or "utf-8",
    }


def _read_folder_input_node(
    path: Path,
    *,
    pattern: str,
    recursive: bool,
    max_files: int,
    max_bytes_per_file: int,
    encoding: str,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    if not path.exists():
        raise FileNotFoundError(f"folder input path does not exist: {path}")
    if not path.is_dir():
        raise NotADirectoryError(f"folder input path is not a folder: {path}")

    glob_pattern = pattern or ("**/*" if recursive else "*")
    if not recursive and "**" in glob_pattern:
        glob_pattern = glob_pattern.replace("**/", "").replace("**", "*")
    candidates = sorted((p for p in path.glob(glob_pattern) if p.is_file()), key=str)
    selected = candidates[:max_files]
    files: list[dict[str, Any]] = []
    chunks: list[str] = []
    for file_path in selected:
        text, metadata = _read_text_file_node(
            file_path,
            encoding=encoding,
            max_bytes=max_bytes_per_file,
        )
        rel = str(file_path.relative_to(path))
        files.append(
            {
                "path": str(file_path),
                "relative_path": rel,
                "bytes": metadata["bytes"],
                "truncated": metadata["truncated"],
                "text": text,
            }
        )
        chunks.append(f"--- {rel} ---\n{text}")

    metadata = {
        "path": str(path),
        "pattern": pattern,
        "recursive": recursive,
        "file_count": len(files),
        "matched_count": len(candidates),
        "max_files": max_files,
        "truncated_file_list": len(candidates) > len(selected),
        "encoding": encoding or "utf-8",
    }
    return "\n\n".join(chunks), files, metadata


# ---------------------------------------------------------------------------
# Object-storage bucket I/O (input_bucket / output_bucket / output_folder)
# ---------------------------------------------------------------------------


def _bucket_io(bucket: str, prefix: str) -> tuple[Any, str]:
    """Build a storage backend bound to ``bucket`` and the key prefix to use.

    For the s3/MinIO backend the chosen bucket overrides the configured one and
    the config prefix is cleared, so listed/read/written keys are the real object
    keys and the node's ``prefix`` is applied to keys directly. For other backends
    (local/dev) the bucket name is folded into the key prefix to namespace it.
    """
    from caliber.config import CaliberConfig  # noqa: PLC0415
    from caliber.storage.service import build_backend  # noqa: PLC0415

    cfg = CaliberConfig.load().workflow_storage
    norm = (prefix or "").strip("/")
    if cfg.backend == "s3":
        backend = build_backend(cfg.model_copy(update={"bucket": bucket, "prefix": ""}))
        return backend, norm
    backend = build_backend(cfg)
    key_prefix = "/".join(p for p in [bucket.strip("/"), norm] if p)
    return backend, key_prefix


def _join_key(prefix: str, name: str) -> str:
    base = (prefix or "").strip("/")
    leaf = (name or "").lstrip("/")
    return f"{base}/{leaf}" if base else leaf


def _read_input_bucket_node(
    *,
    bucket: str,
    prefix: str,
    recursive: bool,
    max_files: int,
    max_bytes_per_file: int,
    encoding: str,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """Read a bounded set of text objects from an object-storage bucket."""
    if not bucket:
        raise ValueError("input_bucket node requires a bucket name")
    from caliber.storage.base import StorageError  # noqa: PLC0415

    backend, key_prefix = _bucket_io(bucket, prefix)
    try:
        items, cursor = backend.list(key_prefix, recursive=recursive, limit=max_files)
    except StorageError as exc:
        target = f"{bucket}/{prefix}" if prefix else bucket
        raise ToolExecutionError(f"input_bucket list failed for {target!r}: {exc}") from exc

    files: list[dict[str, Any]] = []
    chunks: list[str] = []
    skipped_object_count = 0
    base = key_prefix.strip("/")
    for item in items:
        if len(files) >= max_files:
            break
        key = item.ref.key
        if key.endswith("/"):  # skip folder markers
            continue
        try:
            raw = backend.read_bytes(key)
        except StorageError:
            skipped_object_count += 1
            continue
        truncated = len(raw) > max_bytes_per_file
        raw = raw[:max_bytes_per_file]
        text = raw.decode(encoding or "utf-8", errors="replace")
        rel = key[len(base) + 1 :] if base and key.startswith(base + "/") else key
        files.append(
            {
                "key": key,
                "relative_path": rel,
                "bytes": len(raw),
                "truncated": truncated,
                "text": text,
            }
        )
        chunks.append(f"--- {rel} ---\n{text}")

    metadata = {
        "bucket": bucket,
        "prefix": prefix,
        "recursive": recursive,
        "object_count": len(files),
        "max_files": max_files,
        "skipped_object_count": skipped_object_count,
        "truncated_file_list": cursor is not None,
        "encoding": encoding or "utf-8",
    }
    return "\n\n".join(chunks), files, metadata


def _gather_run_artifacts(
    port_values: dict[tuple[str, str], Any], direct_input: str | None
) -> dict[str, str]:
    """Collect the run's artifacts (every upstream ``{"artifacts": {...}}`` map),
    falling back to the node's direct text input as a single ``output.txt``."""
    artifacts = _collect_artifacts(port_values)
    if not artifacts and direct_input:
        artifacts = {"output.txt": direct_input}
    return artifacts


def _write_output_bucket_node(
    *,
    bucket: str,
    prefix: str,
    overwrite: bool,
    port_values: dict[tuple[str, str], Any],
    direct_input: str | None,
) -> tuple[list[str], dict[str, Any]]:
    """Write the run's collected artifacts as objects in an object-storage bucket."""
    if not bucket:
        raise ValueError("output_bucket node requires a bucket name")
    from caliber.storage.base import StorageError, sniff_media_type  # noqa: PLC0415

    backend, key_prefix = _bucket_io(bucket, prefix)
    artifacts = _gather_run_artifacts(port_values, direct_input)
    keys: list[str] = []
    for name, content in artifacts.items():
        key = _join_key(key_prefix, name)
        data = content.encode("utf-8")
        try:
            backend.write_bytes(
                key, data, media_type=sniff_media_type(data, filename=name), overwrite=overwrite
            )
        except StorageError as exc:
            context = ""
            if keys:
                recent = keys[-3:]
                recent_label = ", ".join(repr(item) for item in recent)
                if len(keys) > len(recent):
                    recent_label = f"{recent_label}, ..."
                context = f" after writing {len(keys)} object(s) ({recent_label})"
            raise ToolExecutionError(
                f"output_bucket write failed for {key!r}{context}: {exc}"
            ) from exc
        keys.append(key)
    metadata = {
        "bucket": bucket,
        "prefix": prefix,
        "object_count": len(keys),
        "keys": keys,
    }
    return keys, metadata


def _write_output_folder_node(
    *,
    path: str,
    overwrite: bool,
    port_values: dict[tuple[str, str], Any],
    direct_input: str | None,
    config: Any | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """Write the run's collected artifacts as files under a local folder.

    Not supported in the reference profile (EP-014, B04.7, #774): it creates directories and files on the host, and its only
    guard is the file-root confinement an operator may or may not have configured.
    """
    refuse_in_reference_profile("EP-014", config)
    if not path:
        raise ValueError("output_folder node requires a folder path")
    # Confine before ``mkdir``: this node creates the directory tree it writes
    # into, so an unconfined path is a create-anywhere primitive, and the
    # artifact filenames come from upstream node output.
    base = confine_to_file_root(Path(path).expanduser(), what="output_folder")
    base.mkdir(parents=True, exist_ok=True)
    artifacts = _gather_run_artifacts(port_values, direct_input)
    written: list[str] = []
    for name, content in artifacts.items():
        # Sanitize the artifact name so it can't escape the target folder.
        safe = "/".join(
            part for part in name.replace("\\", "/").split("/") if part not in ("", ".", "..")
        )
        if not safe:
            continue
        target = base / safe
        if target.exists() and not overwrite:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written.append(str(target))
    metadata = {
        "path": str(base),
        "file_count": len(written),
        "files": written,
    }
    return written, metadata


def _publish_declared_outputs(
    node: IRNode,
    port_values: dict[tuple[str, str], Any],
    values: dict[str, Any],
    *,
    fallback: Any,
) -> None:
    for port, spec in node.outputs.items():
        if port in values:
            port_values[(node.node_id, port)] = values[port]
        elif spec.name == "structured":
            port_values[(node.node_id, port)] = values.get("metadata", {})
        else:
            port_values[(node.node_id, port)] = fallback


def _agent_structured_output_port(agent: IRAgent) -> str | None:
    structured_ports = [
        port
        for port, spec in agent.outputs.items()
        if spec.name == "structured" and port not in {"history", "tool_calls"}
    ]
    for preferred in ("structured_output", "result"):
        if preferred in structured_ports:
            return preferred
    if len(structured_ports) == 1:
        return structured_ports[0]
    return None


def _wait_until_deadline(
    raw: str,
    *,
    timezone_name: str = "UTC",
) -> datetime | None:
    value = (raw or "").strip()
    if not value:
        return None
    if value.lower() in {"now", "immediate"}:
        return datetime.now(timezone.utc)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        zone = (timezone_name or "UTC").strip() or "UTC"
        try:
            parsed = parsed.replace(tzinfo=ZoneInfo(zone))
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone {zone!r}") from exc
    return parsed.astimezone(timezone.utc)


def _wait_until_ready(
    raw: str,
    *,
    timezone_name: str = "UTC",
) -> bool:
    deadline = _wait_until_deadline(raw, timezone_name=timezone_name)
    if deadline is None:
        return False
    return datetime.now(timezone.utc) >= deadline


def _normalize_iterable(value: Any, *, max_items: int) -> list[Any]:  # noqa: PLR0911 (flat type-dispatch reads clearer than nesting)
    if isinstance(value, list):
        return value[:max_items]
    if isinstance(value, tuple):
        return list(value)[:max_items]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return parsed[:max_items]
        except json.JSONDecodeError:
            pass
        return [line for line in text.splitlines() if line.strip()][:max_items]
    if value is None:
        return []
    return [value]


def _maybe_json_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text:
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def _normalize_message_history(value: Any) -> list[dict[str, str]]:
    parsed = _maybe_json_value(value)
    if not isinstance(parsed, list):
        return []
    history: list[dict[str, str]] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if not isinstance(role, str) or role not in {"user", "assistant"}:
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        history.append({"role": role, "content": content})
    return history


def _normalize_string_list(value: Any, *, max_items: int) -> list[str]:
    parsed = _maybe_json_value(value)
    if isinstance(parsed, str) and "," in parsed:
        parsed = [part.strip() for part in parsed.split(",")]
    items = _normalize_iterable(parsed, max_items=max_items)
    normalized = [str(item).strip() for item in items if str(item).strip()]
    return list(dict.fromkeys(normalized))


def _normalize_graph_overrides(value: Any) -> dict[str, Any] | None:
    parsed = _maybe_json_value(value)
    if not isinstance(parsed, dict):
        return None
    return {str(key): item for key, item in parsed.items() if item is not None}


def _normalize_object_payload(value: Any) -> dict[str, Any] | None:
    parsed = _maybe_json_value(value)
    if not isinstance(parsed, dict):
        return None
    return {str(key): item for key, item in parsed.items()}


def _normalize_structured_list(value: Any, *, max_items: int) -> list[Any] | None:
    parsed = _maybe_json_value(value)
    if not isinstance(parsed, list):
        return None
    return list(parsed[:max_items])


def _first_str(inputs: dict[str, Any]) -> str | None:
    for value in inputs.values():
        if isinstance(value, str):
            return value
    return None


def _find_recent_tool_calls(port_values: dict[tuple[str, str], Any]) -> list[dict[str, Any]]:
    for (_, port), value in reversed(list(port_values.items())):
        if port == "tool_calls" and isinstance(value, list):
            return value
    return []


def _route(node: IRRouter, inputs: dict[str, Any]) -> str | None:
    """Pick a branch target. The first branch whose condition matches wins;
    a branch with ``condition is None`` is the fallback/else branch."""
    fallback: str | None = None
    for branch in node.branches:
        if branch.condition is None:
            fallback = branch.to
            continue
        if _condition_matches(branch.condition, inputs):
            return branch.to
    return fallback or (node.branches[0].to if node.branches else None)


def _condition_matches(  # noqa: PLR0911, PLR0912, PLR0915 (flat operator dispatch table)
    condition: dict[str, Any], context: dict[str, Any] | str
) -> bool:
    if not condition:
        return False

    if "all" in condition:
        clauses = condition.get("all")
        if not isinstance(clauses, list) or not clauses:
            return False
        return all(isinstance(item, dict) and _condition_matches(item, context) for item in clauses)

    if "any" in condition:
        clauses = condition.get("any")
        if not isinstance(clauses, list) or not clauses:
            return False
        return any(isinstance(item, dict) and _condition_matches(item, context) for item in clauses)

    if "not" in condition:
        clause = condition.get("not")
        if not isinstance(clause, dict) or not clause:
            return False
        return not _condition_matches(clause, context)

    raw_op = condition.get("op")
    expected: Any = condition.get("value")
    if raw_op is None:
        # Accept shorthand operators such as {"contains": "refund"}.
        for shorthand in (
            "contains",
            "mentions",
            "not_contains",
            "equals",
            "not_equals",
            "starts_with",
            "ends_with",
            "regex",
            "exists",
            "gt",
            "gte",
            "lt",
            "lte",
            "in",
        ):
            if shorthand in condition:
                raw_op = shorthand
                expected = condition.get(shorthand)
                break
    if raw_op is None:
        return False

    op = str(raw_op).lower()
    case_sensitive = bool(condition.get("case_sensitive", False))

    if isinstance(context, dict):
        field = str(condition.get("field", "input"))
        candidate = _value_for_field(context, field)
    else:
        candidate = context

    if op == "exists":
        return candidate not in (None, "", [], {})

    if expected is None:
        return False

    if op in {"gt", "gte", "lt", "lte"}:
        try:
            left = float(candidate)
            right = float(expected)
        except (TypeError, ValueError):
            return False
        if op == "gt":
            return left > right
        if op == "gte":
            return left >= right
        if op == "lt":
            return left < right
        return left <= right

    if op == "in":
        if isinstance(expected, list):
            if isinstance(candidate, str) and not case_sensitive:
                lowered = [item.lower() if isinstance(item, str) else item for item in expected]
                return candidate.lower() in lowered
            return candidate in expected
        text = str(candidate)
        wanted = str(expected)
        if not case_sensitive:
            text = text.lower()
            wanted = wanted.lower()
        return text in wanted

    text = "" if candidate is None else str(candidate)
    wanted = str(expected)
    if not case_sensitive:
        text = text.lower()
        wanted = wanted.lower()

    if op in {"contains", "mentions"}:
        return wanted in text
    if op == "not_contains":
        return wanted not in text
    if op == "equals":
        return wanted == text
    if op == "not_equals":
        return wanted != text
    if op == "starts_with":
        return text.startswith(wanted)
    if op == "ends_with":
        return text.endswith(wanted)
    if op == "regex":
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            return re.search(str(expected), text, flags) is not None
        except re.error:
            return False
    return False


def _value_for_field(context: dict[str, Any], field: str) -> Any:
    if not field:
        return context
    parts = field.split(".")
    current: Any = context
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


__all__ = [
    "AgentTurnResult",
    "CaliberRunContext",
    "FakeWorkflowExecutor",
    "NodeStep",
    "OpenAIChatWorkflowExecutor",
    "RuntimePlan",
    "RuntimeResumeCheckpoint",
    "ToolExecutionError",
    "WorkflowExecutor",
    "WorkflowRunResult",
    "current_run_context",
    "execute",
    "run_tags",
    "run_with_caliber_context",
    "workflow_model",
]
