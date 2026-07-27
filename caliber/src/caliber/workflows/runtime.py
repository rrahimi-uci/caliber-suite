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
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from caliber.llm.models import is_reasoning_model
from caliber.mcp_gateway import McpGatewayError, invoke_tool_by_server_id_sync
from caliber.observability.mlflow_tracing import Tracer, get_tracer, model_cost_usd
from caliber.tool_sandbox.models import ToolSandboxRunRequest
from caliber.tool_sandbox.service import LocalSubprocessToolSandbox
from caliber.workflows.guardrails import (
    GuardrailBlockedError,
    GuardrailContext,
)
from caliber.workflows.ir import (
    IRAgent,
    IRApiRequest,
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
    IRMcpResource,
    IRNode,
    IROutputBucket,
    IROutputFolder,
    IRParallel,
    IRPythonCode,
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
        (node for node in ir.nodes.values() if node.node_type in _PREVIEW_UNISOLATED_NODE_TYPES),
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


def current_run_context() -> CaliberRunContext | None:
    return _RUN_CONTEXT.get()


def run_tags(ctx: CaliberRunContext, *, node_id: str | None = None) -> dict[str, str]:
    """Build the ``caliber.*`` run/span tag dict for a run (plan §11.1)."""
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
    if ctx.trace_group_tags:
        return {**ctx.trace_group_tags, **tags}
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
    """

    def __init__(self, *, follow_handoffs: bool = True, skip_tools: bool = False) -> None:
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
        client: Any = None,
        base_url: str | None = None,
        parallel_tool_calls: bool = False,
        prompt_cache_enabled: bool = False,
        prompt_cache_retention: str | None = None,
    ) -> None:
        if client is not None:
            self._client = client
        else:
            try:
                from openai import OpenAI  # noqa: PLC0415
            except ImportError as exc:
                raise RuntimeError(
                    "openai is not installed. Install with "
                    "`pip install caliber[llm]` to enable real workflow LLM runs."
                ) from exc
            # ``base_url`` routes calls through an OpenAI-compatible endpoint (e.g.
            # the MLflow AI Gateway at .../gateway/mlflow/v1) instead of api.openai.com.
            # ``None`` (the default) preserves the direct-OpenAI behaviour exactly.
            self._client = OpenAI(api_key=api_key, base_url=base_url or None)
        self._default_model = default_model
        self._parallel_tool_calls = parallel_tool_calls
        self._prompt_cache_enabled = prompt_cache_enabled
        self._prompt_cache_retention = prompt_cache_retention or None

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

        def _create(use_tools: bool) -> Any:
            kwargs: dict[str, Any] = {"model": model, "messages": messages}
            # Reasoning models (o-series, gpt-5*) only accept the default temperature
            # — sending a custom value is a 400. Plain chat models keep the low temp
            # for determinism.
            if not _is_reasoning_model(model):
                kwargs["temperature"] = 0.2
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
        client: Any = None,
        base_url: str | None = None,
        parallel_tool_calls: bool = False,
        prompt_cache_enabled: bool = False,
        prompt_cache_retention: str | None = None,
    ) -> None:
        if client is not None:
            self._client = client
        else:
            try:
                from openai import OpenAI  # noqa: PLC0415
            except ImportError as exc:
                raise RuntimeError(
                    "openai is not installed. Install with "
                    "`pip install caliber[llm]` to enable real workflow LLM runs."
                ) from exc
            self._client = OpenAI(api_key=api_key, base_url=base_url or None)
        self._default_model = default_model
        self._parallel_tool_calls = parallel_tool_calls
        self._prompt_cache_enabled = prompt_cache_enabled
        self._prompt_cache_retention = prompt_cache_retention or None

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
        base_url: str | None = None,
        parallel_tool_calls: bool = False,
        prompt_cache_enabled: bool = False,
        prompt_cache_retention: str | None = None,
    ) -> None:
        self._api_key = api_key
        self._default_model = default_model
        self._base_url = base_url or None
        self._parallel_tool_calls = parallel_tool_calls
        self._prompt_cache_enabled = prompt_cache_enabled
        self._prompt_cache_retention = prompt_cache_retention or None

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
                "`pip install caliber[llm]` to enable Agents SDK workflow runs."
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
    ) -> None:
        self._api_key = api_key
        self._default_model = default_model
        self._max_tokens = max_tokens
        self._client = client

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
        self._client = Anthropic(api_key=self._api_key)
        return self._client

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
) -> tuple[Any, str]:
    """Execute one model-chosen tool call inside a TOOL span.

    Side-effect gating for write/external tools in preview is already applied by
    ``_resolve_tool_callables`` (callables are preview-wrapped). ``requires_approval``
    tools are NOT executed in the autonomous loop — a gated marker is returned so
    the model can adapt rather than triggering an unapproved side effect.
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
    except Exception as exc:  # fail soft: a failing tool must not crash the whole run
        # The span already recorded failed status (the exception propagated through
        # it). Feed an error marker back so the model can adapt instead of aborting.
        marker = {"_error": f"{type(exc).__name__}: {exc}"[:500], "tool": name}
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


@dataclass
class RuntimePlan:
    """Everything the interpreter needs to execute a workflow version."""

    ir: IRWorkflow
    resolver: ToolResolver
    workflow_version_id: str | None = None
    workflow_alias: str | None = None
    compiler_version: str | None = None
    # Optional nested-workflow executor hook used by Subworkflow nodes.
    subworkflow_runner: (
        Callable[[str, str, str, float, int, WorkflowExecutor, bool], dict[str, Any]] | None
    ) = None
    knowledge_query_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None
    knowledge_build_runner: Callable[[dict[str, Any]], dict[str, Any]] | None = None
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
    # Bounded parallelism for a ForEach node whose target is an agent. 1 (default)
    # = sequential, byte-identical to pre-concurrency behavior. Set from
    # ``config.workflow_foreach_max_workers`` by ``build_plan``; other plan
    # builders (preview/eval/calibration) leave it at 1.
    foreach_max_workers: int = 1


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
) -> bool:
    if checkpoint_kind == "wait_for_event":
        return isinstance(node, IRWaitForEvent)
    if checkpoint_kind == "wait_until":
        return isinstance(node, IRWaitUntil)
    if checkpoint_kind == "human_approval":
        return isinstance(node, IRHumanApproval)
    if checkpoint_kind == "runtime_approval":
        return (
            isinstance(node, IRTool) and node.binding is not None and node.binding.requires_approval
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


def _resilient_callable(
    binding: IRToolBinding,
    fn: Callable[..., Any],
) -> Callable[..., Any]:
    """Wrap a tool callable with the binding's timeout + retry policy (ext B2).

    Retries up to ``max_retries`` additional attempts on exception/timeout.
    ``timeout_seconds`` is enforced by running the call in a worker thread and
    abandoning it if it overruns (the orphaned thread can't be force-killed in
    Python, but the workflow no longer blocks on it). Both are no-ops when unset.
    """
    timeout = binding.timeout_seconds
    attempts = max(1, binding.max_retries + 1)

    def _wrapped(*args: Any, **kwargs: Any) -> Any:
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                if timeout is None:
                    return fn(*args, **kwargs)
                with ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(fn, *args, **kwargs).result(timeout=timeout)
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

    return _wrapped


def _resolve_tool_callables(
    agent: IRAgent,
    resolver: ToolResolver,
    *,
    preview: bool,
) -> dict[str, Callable[..., Any]]:
    callables: dict[str, Callable[..., Any]] = {}
    for binding in agent.tools:
        chosen = _resolve_bound_tool_callable(
            binding,
            resolver,
            preview=preview,
            required=False,
        )
        if chosen is not None:
            callables[binding.local_name] = chosen
    return callables


def _resolve_bound_tool_callable(
    binding: IRToolBinding,
    resolver: ToolResolver,
    *,
    preview: bool,
    required: bool,
) -> Callable[..., Any] | None:
    real = _bind(binding, resolver)
    chosen = make_preview_callable(binding, real) if preview else real
    if chosen is None:
        if required:
            raise ToolExecutionError(
                f"tool {binding.local_name!r} could not be bound for execution"
            )
        return None
    return _resilient_callable(binding, chosen)


def _bind(binding: IRToolBinding, resolver: ToolResolver) -> Callable[..., Any] | None:
    if binding.binding_type == "mcp_tool":
        return _bind_mcp_tool(binding)
    override: Callable[..., Any] | None = None
    getter = getattr(resolver, "get_callable", None)
    if callable(getter):
        override = getter(binding.registry_ref)
    if override is not None:
        return override
    if binding.module_path == "<in-memory>":
        return None
    try:
        from caliber.workflows.tools import ToolRegistryEntry  # noqa: PLC0415

        entry = ToolRegistryEntry(
            name=binding.local_name,
            version="1.0",
            module_path=binding.module_path,
            callable_name=binding.callable_name,
        )
        return bind_registered_tool(entry)
    except Exception:
        return None


def _bind_mcp_tool(binding: IRToolBinding) -> Callable[..., Any]:
    """Bind an MCP tool as a runtime-managed callable.

    The workflow runtime resolves the target MCP server by ``server_id`` and
    invokes the tool through the shared MCP gateway.
    """
    tool_name = binding.mcp_tool_name or ""
    server_id = binding.mcp_server_id or ""

    def _invoke(arg: Any = "") -> dict[str, Any]:
        arguments = _mcp_arguments_from_input(arg)
        try:
            result = invoke_tool_by_server_id_sync(
                server_id=server_id,
                tool_name=tool_name,
                arguments=arguments,
            )
        except McpGatewayError as exc:
            raise ToolExecutionError(
                f"MCP tool {tool_name!r} invocation failed on server {server_id!r}: {exc}"
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


def _default_webhook_sender(request: dict[str, Any]) -> dict[str, Any]:
    """Perform a real outbound HTTP request for a Webhook node.

    ``request`` carries ``url``/``method``/``headers``/``timeout_seconds``/``body``.
    A structured (dict/list) body is sent as JSON; anything else as a text body.
    Returns ``status_code``/``text``/``json``/``headers`` for the node to publish.
    Injected via ``RuntimePlan.webhook_sender`` in tests so no network is hit.
    """
    import httpx  # noqa: PLC0415 - keep httpx out of the module import path

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

    with httpx.Client(timeout=timeout) as client:
        response = client.request(method, url, **kwargs)

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


def _resolve_external_app_entrypoint(entrypoint: str) -> tuple[Callable[..., Any], str]:
    spec = (entrypoint or "").strip()
    if not spec:
        raise ToolExecutionError("external_app entrypoint must not be empty")
    if ":" in spec:
        module_name, attr_path = spec.split(":", 1)
    elif "." in spec:
        module_name, attr_path = spec.rsplit(".", 1)
    else:
        raise ToolExecutionError(
            "external_app entrypoint must use 'package.module:callable' "
            "or 'package.module.callable'"
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
) -> tuple[Any, dict[str, Any]]:
    entrypoint, resolved_entrypoint = _resolve_external_app_entrypoint(node.entrypoint)
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
    ):
        # Stamp the trace with the session so multi-turn / retried runs group
        # into one MLflow session (surfaced in CALIBER Observability).
        if session_id:
            tracer.annotate_trace(session_id=session_id)
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
            with ThreadPoolExecutor(max_workers=len(pending_specs)) as pool:

                def _run_pending_spec(
                    spec: tuple[str, IRNode, dict[str, Any], tuple[str, str] | None],
                ) -> dict[str, Any]:
                    return contextvars.copy_context().run(_worker, *spec)

                futures = {spec[0]: pool.submit(_run_pending_spec, spec) for spec in pending_specs}
                for nid, future in futures.items():
                    results_by_node_id[nid] = future.result()
        elif len(pending_specs) == 1:
            result = _worker(*pending_specs[0])
            results_by_node_id[result["nid"]] = result

        return [results_by_node_id[nid] for nid in batch_ids]

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


def _run_agent_with_history(
    plan: RuntimePlan,
    executor: WorkflowExecutor,
    agent: IRAgent,
    input_text: str,
    *,
    explicit_history: list[HistoryMessage] | None = None,
    handoff_agents: ResolvedHandoffAgents | None = None,
    tool_callables: dict[str, Callable[..., Any]],
    preview: bool,
) -> tuple[AgentTurnResult, list[HistoryMessage], list[HistoryMessage]]:
    explicit = [dict(item) for item in (explicit_history or [])]
    key = _session_memory_key(plan, agent)
    stored: list[HistoryMessage] = []
    if key is not None and plan.session_memory_store is not None:
        stored = plan.session_memory_store.load_history(
            workflow_id=key[0],
            node_id=key[1],
            session_id=key[2],
        )
    history = _merge_message_history(stored, explicit)
    result = _run_agent_traced(
        executor,
        agent,
        input_text,
        history=history,
        handoff_agents=handoff_agents,
        tool_callables=tool_callables,
        preview=preview,
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
        callables = _resolve_tool_callables(agent_def, plan.resolver, preview=preview)
        if extra_tools:
            callables = {**callables, **extra_tools}
        return callables

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


_INLINE_ORCHESTRATION_TARGET_LABEL = "agent, subworkflow, tool, mcp_resource, knowledge_query, knowledge_build, template, python_code, external_app, webhook, or api_request"
_INLINE_ORCHESTRATION_TARGET_TYPES = (
    IRAgent,
    IRSubworkflow,
    IRTool,
    IRMcpResource,
    IRKnowledgeQuery,
    IRKnowledgeBuild,
    IRTemplate,
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
        file_path = _path_from_inputs(inputs, configured_path=node.path, run_input=run_input)
        text, file_metadata = _read_text_file_node(
            file_path,
            encoding=node.encoding,
            max_bytes=node.max_bytes,
        )
        _publish_declared_outputs(
            node,
            port_values,
            {"text": text, "path": str(file_path), "metadata": file_metadata},
            fallback=text,
        )
        return 0, NodeStep(
            nid,
            ntype,
            "ok",
            output=text,
            detail=f"read {file_metadata['bytes']} byte(s) from {file_path}",
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
            raise ToolExecutionError(
                f"subworkflow {node.workflow_id!r}@{node.alias!r} failed: "
                f"{payload.get('error', 'unknown error')}"
            )
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
                            )
                        )
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
        fn = _resolve_bound_tool_callable(
            binding,
            plan.resolver,
            preview=preview,
            required=True,
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
                result = _call_tool(fn, arguments, fallback_input=fallback_input)
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
        try:
            result = invoke_tool_by_server_id_sync(
                server_id=node.server_id,
                tool_name=node.tool_name,
                arguments=arguments,
                timeout_seconds=node.timeout_seconds,
            )
        except McpGatewayError as exc:
            raise ToolExecutionError(
                f"MCP node {nid!r} failed for {node.server_id!r}/{node.tool_name!r}: {exc}"
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
        sender = plan.webhook_sender or _default_webhook_sender
        request = {
            "url": url,
            "method": node.method,
            "headers": dict(node.headers),
            "timeout_seconds": node.timeout_seconds,
            "body": body,
        }
        try:
            result = sender(request)
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
        sender = plan.webhook_sender or _default_webhook_sender
        request = {
            "url": req_url,
            "method": method,
            "headers": req_headers,
            "timeout_seconds": node.timeout_seconds,
            "body": req_body,
        }
        try:
            api_result = sender(request)
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

    if isinstance(node, IRPythonCode):
        python_input = _select_input(inputs, run_input)
        sandbox_kwargs: dict[str, Any] = {"default_timeout_seconds": node.timeout_seconds}
        if plan.max_output_bytes is not None:
            sandbox_kwargs["max_output_bytes"] = plan.max_output_bytes
        sandbox = LocalSubprocessToolSandbox(**sandbox_kwargs)
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
        agent_input = _select_input(inputs, run_input)
        explicit_history = _normalize_message_history(inputs.get("history"))
        callables = _resolve_tool_callables(node, plan.resolver, preview=preview)
        if extra_tools:
            callables = {**callables, **extra_tools}
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
                t_callables = _resolve_tool_callables(target, plan.resolver, preview=preview)
                if extra_tools:
                    t_callables = {**t_callables, **extra_tools}
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
        ctx = GuardrailContext(response_text=response_text, tool_calls=tool_calls)
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
            )
        except ToolExecutionError:
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


def _run_node_with_policy(
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
    attempts = max(1, node.execution_policy.max_retries + 1)
    last_exc: Exception | None = None
    for attempt in range(attempts):
        started = time.monotonic()
        try:
            tokens, step = _run_node_traced(
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
            elapsed = time.monotonic() - started
            timeout = node.execution_policy.timeout_seconds
            if timeout is not None and elapsed > timeout:
                raise ToolExecutionError(f"node {node.node_id!r} timed out after {timeout}s")
            return tokens, step
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


def _select_input(inputs: dict[str, Any], run_input: str) -> str:
    if "input" in inputs:
        return str(inputs["input"])
    for value in inputs.values():
        if isinstance(value, str):
            return value
    return run_input


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
    return Path(raw).expanduser()


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
) -> tuple[list[str], dict[str, Any]]:
    """Write the run's collected artifacts as files under a local folder."""
    if not path:
        raise ValueError("output_folder node requires a folder path")
    base = Path(path)
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
