"""Workflow compiler: manifest → IR → SDK objects + generated code (plan §10).

The compiler is deterministic. Given the same manifest, tool registry, and
compiler version it always produces byte-identical generated Python and an
identical compiler report — that property is what the golden tests lock down
(plan §19.5) and what makes the ``caliber.manifest_hash`` run tag meaningful.

Pipeline (plan §10.3):

1. parse + validate (errors block compilation);
2. resolve references (tools against the registry, prompts against artifacts);
3. build the typed IR (:mod:`caliber.workflows.ir`);
4. generate the Agents SDK Python module (an export/review artifact);
5. emit a compiler report.

In-server execution does not exec the generated code — it runs the IR through
:mod:`caliber.workflows.runtime`. The generated module exists for export,
review, and reproducibility (plan §10.5, §26 Q2).
"""

from __future__ import annotations

import hashlib
import json
import keyword
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field, fields, is_dataclass, replace
from enum import Enum
from typing import Any

from caliber.workflows.ir import (
    IRAgent,
    IRApiRequest,
    IRDataTransform,
    IRDeployGate,
    IREdge,
    IRErrorBoundary,
    IRExecutionPolicy,
    IRExternalApp,
    IRFileInput,
    IRFolderInput,
    IRForEach,
    IRGuardrail,
    IRGuardrailCheck,
    IRHandoff,
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
    IRRouterBranch,
    IRSubworkflow,
    IRTemplate,
    IRTool,
    IRToolBinding,
    IRType,
    IRWaitForEvent,
    IRWaitUntil,
    IRWebhook,
    IRWorkflow,
    NodeType,
    PromptRef,
)
from caliber.workflows.manifest import (
    AgentNode,
    ApiRequestNode,
    DataTransformNode,
    ErrorBoundaryNode,
    ExternalAppNode,
    FileInputNode,
    FolderInputNode,
    ForEachNode,
    GuardrailNode,
    HumanApprovalNode,
    InlineInstructions,
    InputBucketNode,
    JoinNode,
    KnowledgeBuildNode,
    KnowledgeQueryNode,
    LoopNode,
    McpResourceNode,
    McpToolBinding,
    OutputBucketNode,
    OutputFolderNode,
    OutputNode,
    ParallelNode,
    PromptRefInstructions,
    PythonCodeNode,
    RegisteredFunctionToolBinding,
    ReviewQueueEnqueueNode,
    RouterNode,
    StartNode,
    SubworkflowNode,
    TemplateNode,
    ToolNode,
    WaitForEventNode,
    WaitUntilNode,
    WebhookNode,
    WorkflowManifest,
    WorkflowNode,
)
from caliber.workflows.tools import (
    ToolResolutionError,
    ToolResolver,
)
from caliber.workflows.validation import validate_manifest

COMPILER_VERSION = "0.1.0"
_DIRECT_SDK_EXPORT_NODE_TYPES = {
    NodeType.START,
    NodeType.OUTPUT,
    NodeType.NOTE,
    NodeType.AGENT,
    NodeType.GUARDRAIL,
}

# Bounded LRU cache of compile results, keyed by manifest hash + version +
# resolved-tool fingerprint (ext D1). Opt-in via ``compile_workflow(use_cache=True)``
# on hot paths (preview, deploy gate) so repeated compiles of an unchanged
# version don't re-run the compiler. Invalidates automatically when the manifest
# or a referenced tool version/status changes (both are in the key).
_COMPILE_CACHE: OrderedDict[str, CompileResult] = OrderedDict()
_COMPILE_CACHE_MAX = 128


def clear_compile_cache() -> None:
    """Drop all cached compile results (tests + admin cache-bust)."""
    _COMPILE_CACHE.clear()


class CompileError(Exception):
    """Raised when a manifest cannot be compiled.

    Carries the validation report (when the failure is a validation error) so
    callers can surface structured diagnostics.
    """

    def __init__(self, message: str, *, report: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.report = report


@dataclass
class CompileResult:
    ir: IRWorkflow
    generated_python: str
    report: dict[str, Any]
    manifest_hash: str
    requirements: list[str] = field(default_factory=list)
    # Wall-clock compile time (ext C2). Kept off ``report`` so the report stays
    # deterministic for golden snapshots; observability reads it from here.
    compile_ms: float = 0.0
    cached: bool = False


# ---------------------------------------------------------------------------
# IR construction
# ---------------------------------------------------------------------------


def _ports(raw: dict[str, Any]) -> dict[str, IRType]:
    return {name: IRType(spec.type, spec.schema_) for name, spec in (raw or {}).items()}


def _execution_policy(node: WorkflowNode) -> IRExecutionPolicy:
    raw = getattr(node, "execution_policy", None)
    if raw is None:
        return IRExecutionPolicy()
    return IRExecutionPolicy(
        timeout_seconds=raw.timeout_seconds,
        max_retries=raw.max_retries,
        idempotent=raw.idempotent,
    )


def _prompt_ref(node: AgentNode, manifest: WorkflowManifest) -> PromptRef:
    instr = node.instructions
    if isinstance(instr, InlineInstructions):
        return PromptRef(kind="inline", inline_text=instr.text)
    if isinstance(instr, PromptRefInstructions):
        artifact = manifest.artifacts.prompts.get(instr.ref)
        if artifact is None:
            raise CompileError(
                f"agent {node.id!r} references prompt {instr.ref!r} not in artifacts.prompts"
            )
        return PromptRef(
            kind="mlflow_prompt",
            registry_name=artifact.registry_name,
            alias=artifact.alias,
        )
    raise CompileError(f"agent {node.id!r} has an unsupported instructions type")


def _tool_binding(
    local_name: str,
    manifest: WorkflowManifest,
    resolver: ToolResolver,
) -> IRToolBinding:
    binding = manifest.tools.get(local_name)
    if binding is None:
        raise CompileError(f"tool {local_name!r} has no binding in manifest.tools")
    if isinstance(binding, RegisteredFunctionToolBinding):
        try:
            resolution = resolver.resolve(binding.registry_ref, binding.version_constraint)
        except ToolResolutionError as exc:
            raise CompileError(str(exc)) from exc
        entry = resolution.entry
        secret_refs = tuple(dict.fromkeys((*binding.secret_refs, *entry.secret_refs)))
        return IRToolBinding(
            local_name=local_name,
            resolved_name=entry.name,
            resolved_version=entry.version,
            registry_ref=binding.registry_ref,
            version_constraint=binding.version_constraint,
            requires_approval=binding.requires_approval or entry.requires_approval,
            side_effect_level=entry.side_effect_level,
            allow_in_preview=entry.allow_in_preview,
            module_path=entry.module_path,
            callable_name=entry.callable_name,
            execution_backend=entry.execution_backend,
            backend_config=entry.backend_config,
            binding_type="registered_function",
            secret_refs=secret_refs,
            output_schema=entry.output_schema,
            input_schema=entry.input_schema,
            timeout_seconds=binding.timeout_seconds,
            max_retries=binding.max_retries,
        )
    assert isinstance(binding, McpToolBinding)
    return IRToolBinding(
        local_name=local_name,
        resolved_name=local_name,
        resolved_version="",
        registry_ref=f"mcp:{binding.server_id}/{binding.tool_name}",
        version_constraint="",
        requires_approval=binding.requires_approval,
        side_effect_level=binding.side_effect_level,
        allow_in_preview=False,
        module_path="<mcp>",
        callable_name="invoke",
        binding_type="mcp_tool",
        mcp_server_id=binding.server_id,
        mcp_tool_name=binding.tool_name,
        mcp_tool_schema_version=binding.tool_schema_version,
        timeout_seconds=binding.timeout_seconds,
        max_retries=binding.max_retries,
    )


def _build_node(  # noqa: PLR0911, PLR0912 - per-node-type IR builder dispatch
    node: WorkflowNode,
    manifest: WorkflowManifest,
    resolver: ToolResolver,
    skill_contents: dict[str, str] | None = None,
) -> IRNode:
    inputs = _ports(getattr(node, "inputs", {}))
    outputs = _ports(getattr(node, "outputs", {}))
    execution_policy = _execution_policy(node)

    if isinstance(node, StartNode):
        return IRNode(node.id, NodeType.START, inputs, outputs, execution_policy)
    if isinstance(node, OutputNode):
        return IRNode(node.id, NodeType.OUTPUT, inputs, outputs, execution_policy)
    if isinstance(node, FileInputNode):
        managed_ref = (
            IRManagedFileReference(**node.file_ref.model_dump(mode="python"))
            if node.file_ref is not None
            else None
        )
        return IRFileInput(
            node_id=node.id,
            node_type=NodeType.FILE_INPUT,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            file_ref=managed_ref,
            path=node.path,
            encoding=node.encoding,
            max_bytes=node.max_bytes,
        )
    if isinstance(node, FolderInputNode):
        return IRFolderInput(
            node_id=node.id,
            node_type=NodeType.FOLDER_INPUT,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            path=node.path,
            pattern=node.pattern,
            recursive=node.recursive,
            max_files=node.max_files,
            max_bytes_per_file=node.max_bytes_per_file,
            encoding=node.encoding,
        )
    if isinstance(node, InputBucketNode):
        return IRInputBucket(
            node_id=node.id,
            node_type=NodeType.INPUT_BUCKET,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            bucket=node.bucket,
            prefix=node.prefix,
            recursive=node.recursive,
            max_files=node.max_files,
            max_bytes_per_file=node.max_bytes_per_file,
            encoding=node.encoding,
        )
    if isinstance(node, OutputBucketNode):
        return IROutputBucket(
            node_id=node.id,
            node_type=NodeType.OUTPUT_BUCKET,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            bucket=node.bucket,
            prefix=node.prefix,
            overwrite=node.overwrite,
        )
    if isinstance(node, OutputFolderNode):
        return IROutputFolder(
            node_id=node.id,
            node_type=NodeType.OUTPUT_FOLDER,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            path=node.path,
            overwrite=node.overwrite,
        )
    if isinstance(node, WaitUntilNode):
        return IRWaitUntil(
            node_id=node.id,
            node_type=NodeType.WAIT_UNTIL,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            wait_until=node.wait_until,
            timezone=node.timezone,
        )
    if isinstance(node, WaitForEventNode):
        return IRWaitForEvent(
            node_id=node.id,
            node_type=NodeType.WAIT_FOR_EVENT,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            event_name=node.event_name,
            correlation_key=node.correlation_key,
            timeout_seconds=node.timeout_seconds,
        )
    if isinstance(node, ParallelNode):
        return IRParallel(
            node_id=node.id,
            node_type=NodeType.PARALLEL,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
        )
    if isinstance(node, JoinNode):
        return IRJoin(
            node_id=node.id,
            node_type=NodeType.JOIN,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            mode=node.mode,
        )
    if isinstance(node, ForEachNode):
        return IRForEach(
            node_id=node.id,
            node_type=NodeType.FOR_EACH,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            target_node_id=node.target_node_id,
            item_input_port=node.item_input_port,
            max_items=node.max_items,
        )
    if isinstance(node, LoopNode):
        return IRLoop(
            node_id=node.id,
            node_type=NodeType.LOOP,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            target_node_id=node.target_node_id,
            max_iterations=node.max_iterations,
            stop_condition=node.stop_condition,
        )
    if isinstance(node, ErrorBoundaryNode):
        return IRErrorBoundary(
            node_id=node.id,
            node_type=NodeType.ERROR_BOUNDARY,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            target_node_id=node.target_node_id,
            fallback_text=node.fallback_text,
            compensate_with=node.compensate_with,
        )
    if isinstance(node, SubworkflowNode):
        return IRSubworkflow(
            node_id=node.id,
            node_type=NodeType.SUBWORKFLOW,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            workflow_id=node.workflow_id,
            alias=node.alias,
            timeout_seconds=node.timeout_seconds,
        )
    if isinstance(node, ToolNode):
        return IRTool(
            node_id=node.id,
            node_type=NodeType.TOOL,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            binding=_tool_binding(node.tool_name, manifest, resolver),
        )
    if isinstance(node, McpResourceNode):
        return IRMcpResource(
            node_id=node.id,
            node_type=NodeType.MCP_RESOURCE,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            server_id=node.server_id,
            tool_name=node.tool_name,
            timeout_seconds=node.timeout_seconds,
        )
    if isinstance(node, KnowledgeQueryNode):
        return IRKnowledgeQuery(
            node_id=node.id,
            node_type=NodeType.KNOWLEDGE_QUERY,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            knowledge_base_id=node.knowledge_base_id,
            version_ids=list(node.version_ids),
            retrieval_modes=list(node.retrieval_modes),
            top_k=node.top_k,
            chat_model=node.chat_model,
            graph_overrides=(
                node.graph_overrides.model_dump(exclude_none=True)
                if node.graph_overrides is not None
                else None
            ),
        )
    if isinstance(node, KnowledgeBuildNode):
        return IRKnowledgeBuild(
            node_id=node.id,
            node_type=NodeType.KNOWLEDGE_BUILD,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            knowledge_base_id=node.knowledge_base_id,
            chunking_strategy=node.chunking_strategy,
            embedding_model=node.embedding_model,
            chunking_config=dict(node.chunking_config),
            graph_config=(
                node.graph_config.model_dump(exclude_none=True)
                if node.graph_config is not None
                else None
            ),
            activate_when_complete=node.activate_when_complete,
            wait_for_completion=node.wait_for_completion,
            wait_timeout_seconds=node.wait_timeout_seconds,
        )
    if isinstance(node, TemplateNode):
        return IRTemplate(
            node_id=node.id,
            node_type=NodeType.TEMPLATE,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            template=node.template,
            output_format=node.output_format,
            missing_variable_mode=node.missing_variable_mode,
        )
    if isinstance(node, DataTransformNode):
        return IRDataTransform(
            node_id=node.id,
            node_type=NodeType.DATA_TRANSFORM,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            operation=node.operation,
            config=dict(node.config),
            fail_on_invalid=node.fail_on_invalid,
        )
    if isinstance(node, ReviewQueueEnqueueNode):
        return IRReviewQueueEnqueue(
            node_id=node.id,
            node_type=NodeType.REVIEW_QUEUE_ENQUEUE,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            queue_id=node.queue_id,
            experiment_id=node.experiment_id,
            assigned_to=node.assigned_to,
        )
    if isinstance(node, PythonCodeNode):
        return IRPythonCode(
            node_id=node.id,
            node_type=NodeType.PYTHON_CODE,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            code=node.code,
            timeout_seconds=node.timeout_seconds,
        )
    if isinstance(node, AgentNode):
        return IRAgent(
            node_id=node.id,
            node_type=NodeType.AGENT,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            name=node.name,
            model=node.model,
            instructions=_prompt_ref(node, manifest),
            tools=[_tool_binding(t, manifest, resolver) for t in node.tools],
            skill_instructions=[
                skill_contents[name]
                for name in node.skills
                if skill_contents and name in skill_contents
            ],
            handoffs=[
                IRHandoff(
                    target_node_id=h.target,
                    description=h.description,
                    input_filter=h.input_filter,
                    condition=h.condition,
                )
                for h in node.handoffs
            ],
            output_type=node.output_type,
            eval_dataset=node.eval_dataset,
        )
    if isinstance(node, GuardrailNode):
        return IRGuardrail(
            node_id=node.id,
            node_type=NodeType.GUARDRAIL,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            mode=node.mode,
            checks=[IRGuardrailCheck(c.kind, dict(c.params)) for c in node.checks],
            on_failure=node.on_failure,
            max_retries=node.max_retries,
        )
    if isinstance(node, RouterNode):
        return IRRouter(
            node_id=node.id,
            node_type=NodeType.ROUTER,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            branches=[IRRouterBranch(b.condition, b.to) for b in node.branches],
        )
    if isinstance(node, HumanApprovalNode):
        return IRHumanApproval(
            node_id=node.id,
            node_type=NodeType.HUMAN_APPROVAL,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            required_role=node.required_role,
            approval_count=node.approval_count,
            timeout_behavior=node.timeout_behavior,
        )
    if isinstance(node, ExternalAppNode):
        return IRExternalApp(
            node_id=node.id,
            node_type=NodeType.EXTERNAL_APP,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            entrypoint=node.entrypoint,
        )
    if isinstance(node, WebhookNode):
        return IRWebhook(
            node_id=node.id,
            node_type=NodeType.WEBHOOK,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            url=node.url,
            method=node.method,
            headers=dict(node.headers),
            timeout_seconds=node.timeout_seconds,
        )
    if isinstance(node, ApiRequestNode):
        return IRApiRequest(
            node_id=node.id,
            node_type=NodeType.API_REQUEST,
            inputs=inputs,
            outputs=outputs,
            execution_policy=execution_policy,
            mode=node.mode,
            url=node.url,
            method=node.method,
            curl=node.curl,
            headers=dict(node.headers),
            body=node.body,
            timeout_seconds=node.timeout_seconds,
        )
    # NoteNode and anything else: a plain documentation node carries no ports.
    return IRNode(node.id, NodeType.NOTE, execution_policy=execution_policy)


def _entry_agent_id(manifest: WorkflowManifest, ir_nodes: dict[str, IRNode]) -> str:
    """Resolve the most meaningful runtime entry node for this workflow.

    We still prefer the first agent reachable from ``start`` because that keeps
    legacy agent-first exports stable. But production workflows are now allowed
    to be tool-first, subworkflow-first, or even a simple ``start -> output``
    pass-through graph, so the compiler must not reject agent-free manifests.
    """
    start_ids = [nid for nid, n in manifest.nodes.items() if isinstance(n, StartNode)]
    adjacency: dict[str, list[str]] = {nid: [] for nid in manifest.nodes}
    for edge in manifest.edges:
        if edge.from_ in adjacency:
            adjacency[edge.from_].append(edge.to)
    seen: set[str] = set()
    queue = list(start_ids)
    first_reachable_non_passthrough: str | None = None
    while queue:
        nid = queue.pop(0)
        if nid in seen:
            continue
        seen.add(nid)
        node = ir_nodes.get(nid)
        if isinstance(node, IRAgent):
            return nid
        if (
            first_reachable_non_passthrough is None
            and node is not None
            and node.node_type not in {NodeType.START, NodeType.NOTE, NodeType.OUTPUT}
        ):
            first_reachable_non_passthrough = nid
        queue.extend(adjacency.get(nid, []))
    if first_reachable_non_passthrough is not None:
        return first_reachable_non_passthrough
    # Fall back to any agent so codegen still has an entry point.
    for nid, node in ir_nodes.items():
        if isinstance(node, IRAgent):
            return nid
    for nid, node in ir_nodes.items():
        if node.node_type not in {NodeType.NOTE, NodeType.OUTPUT}:
            return nid
    raise CompileError(
        "workflow has no executable entry node; add an agent, tool, subworkflow, "
        "or connect the start node to a runnable path"
    )


def _check_identifier_collisions(ir_nodes: dict[str, IRNode]) -> None:
    """Reject distinct agent/tool ids that normalize to the same Python var (ext A2).

    Node/tool ids are already constrained to a safe identifier charset, but
    keyword normalization (``class`` → ``class_``) could still collide two
    distinct ids. Detecting it here keeps generated code unambiguous.
    """
    by_identifier: dict[str, str] = {}
    sources: list[str] = []
    for node in ir_nodes.values():
        if isinstance(node, IRAgent):
            sources.append(node.node_id)
            sources.extend(b.local_name for b in node.tools)
    for original in sources:
        ident = _py_identifier(original)
        existing = by_identifier.get(ident)
        if existing is not None and existing != original:
            raise CompileError(
                f"identifier collision: {existing!r} and {original!r} both map to "
                f"the generated variable {ident!r}"
            )
        by_identifier[ident] = original


def build_ir(
    manifest: WorkflowManifest,
    resolver: ToolResolver,
    *,
    version: str = "draft",
    skill_contents: dict[str, str] | None = None,
) -> IRWorkflow:
    """Build the typed IR from a validated manifest (raises on resolution errors).

    ``skill_contents`` (name → content) resolves each agent's ``skills`` into
    composed system-prompt blocks; ``None`` leaves agents skill-free.
    """
    ir_nodes: dict[str, IRNode] = {
        nid: _build_node(node, manifest, resolver, skill_contents)
        for nid, node in manifest.nodes.items()
    }
    _check_identifier_collisions(ir_nodes)
    edges: list[IREdge] = []
    for edge in manifest.edges:
        source = manifest.nodes.get(edge.from_)
        src_outputs = _ports(getattr(source, "outputs", {})) if source else {}
        for src_port, tgt_port in edge.map.items():
            type_check = src_outputs.get(src_port, IRType("void"))
            edges.append(
                IREdge(
                    edge_id=edge.id,
                    from_node=edge.from_,
                    from_output=src_port,
                    to_node=edge.to,
                    to_input=tgt_port,
                    type_check=type_check,
                )
            )

    output_ids = [nid for nid, n in manifest.nodes.items() if isinstance(n, OutputNode)]
    deploy_gates = [
        IRDeployGate(
            name=name,
            dataset_ref=gate.dataset_ref,
            required_for_aliases=list(gate.required_for_aliases),
            thresholds=dict(gate.thresholds),
        )
        for name, gate in sorted(manifest.deploy_gates.items())
    ]

    return IRWorkflow(
        workflow_id=manifest.workflow_id,
        version=version,
        nodes=ir_nodes,
        edges=edges,
        entry_node_id=_entry_agent_id(manifest, ir_nodes),
        output_node_id=output_ids[0] if output_ids else "",
        deploy_gates=deploy_gates,
        default_model_ref=manifest.runtime.default_model_ref,
        session_mode=manifest.runtime.session.type,
        openai_workflow_api=(
            manifest.runtime.openai.workflow_api if manifest.runtime.openai is not None else None
        ),
        openai_parallel_tool_calls=(
            manifest.runtime.openai.parallel_tool_calls
            if manifest.runtime.openai is not None
            else None
        ),
        openai_prompt_cache_mode=(
            manifest.runtime.openai.prompt_cache_mode
            if manifest.runtime.openai is not None
            else None
        ),
        openai_prompt_cache_retention=(
            manifest.runtime.openai.prompt_cache_retention
            if manifest.runtime.openai is not None
            else None
        ),
        mlflow_experiment_name=manifest.mlflow.experiment_name,
        mlflow_trace_group_tags=dict(manifest.mlflow.trace_group_tags),
        manifest_hash=manifest.manifest_hash(),
    )


# ---------------------------------------------------------------------------
# Code generation
# ---------------------------------------------------------------------------


def _py_identifier(node_id: str) -> str:
    """Map a node id to a safe, stable Python identifier."""
    ident = re.sub(r"\W", "_", node_id)
    if not ident or ident[0].isdigit():
        ident = f"n_{ident}"
    if keyword.iskeyword(ident):
        ident = f"{ident}_"
    return ident


def _topo_order_agents(ir: IRWorkflow) -> list[IRAgent]:
    """Order agents so handoff *targets* are defined before their sources."""
    agents = {a.node_id: a for a in ir.agents()}
    ordered: list[IRAgent] = []
    visited: set[str] = set()

    def _visit(node_id: str) -> None:
        if node_id in visited or node_id not in agents:
            return
        visited.add(node_id)
        for handoff in sorted(agents[node_id].handoffs, key=lambda h: h.target_node_id):
            _visit(handoff.target_node_id)
        ordered.append(agents[node_id])

    for node_id in sorted(agents):
        _visit(node_id)
    return ordered


def _str_literal(value: str) -> str:
    """A safe Python string literal for any value (ext A1).

    ``json.dumps`` produces a double-quoted literal with all control chars and
    quotes escaped, so attacker-controlled names/refs can't break out of the
    literal in the generated module. For ordinary identifiers/text this is
    byte-identical to a naive ``"..."`` interpolation.
    """
    return json.dumps(value)


def _instructions_literal(prompt: PromptRef | None) -> str:
    if prompt is None:
        return '""'
    if prompt.kind == "mlflow_prompt" and prompt.mlflow_uri:
        return f"mlflow.genai.load_prompt({_str_literal(prompt.mlflow_uri)}).template"
    return _str_literal(prompt.inline_text or "")


def _agent_instructions_arg(agent: IRAgent) -> str:
    """Instructions literal with resolved skills composed in (runtime parity).

    Both instruction forms are string-valued expressions, so skill blocks append
    uniformly: ``<base> + "\\n\\n## Skill\\n<content>"``.
    """
    base = _instructions_literal(agent.instructions)
    blocks = [b for b in agent.skill_instructions if b and b.strip()]
    if not blocks:
        return base
    composed = "".join(f"\n\n## Skill\n{b.strip()}" for b in blocks)
    return f"{base} + {_str_literal(composed)}"


def _runtime_export_required(ir: IRWorkflow, *, entry_agent: IRAgent | None) -> bool:
    if entry_agent is None:
        return True
    return any(node.node_type not in _DIRECT_SDK_EXPORT_NODE_TYPES for node in ir.nodes.values())


def _export_mode(ir: IRWorkflow) -> str:
    entry_node = ir.nodes.get(ir.entry_node_id)
    entry_agent = entry_node if isinstance(entry_node, IRAgent) else None
    return (
        "runtime_ir"
        if _runtime_export_required(ir, entry_agent=entry_agent)
        else "agents_sdk_direct"
    )


def generate_python(ir: IRWorkflow) -> str:  # noqa: PLR0912, PLR0915 - linear code emitter
    """Generate the deterministic Python export module for a workflow IR."""
    lines: list[str] = []
    entry_node = ir.nodes.get(ir.entry_node_id)
    entry_agent = entry_node if isinstance(entry_node, IRAgent) else None
    runtime_export = _runtime_export_required(ir, entry_agent=entry_agent)
    handoff_helpers_needed = any(
        str(handoff.condition or "").strip() or str(handoff.input_filter or "").strip()
        for agent in ir.agents()
        for handoff in agent.handoffs
    )
    lines.append('"""Generated by caliber-workflow-compiler. Do not edit by hand.')
    lines.append("")
    lines.append(f"workflow_id: {ir.workflow_id}")
    lines.append(f"version: {ir.version}")
    lines.append(f"manifest_hash: {ir.manifest_hash}")
    lines.append(f"compiler_version: {COMPILER_VERSION}")
    lines.append('"""')
    lines.append("")
    if runtime_export:
        lines.append("from typing import Any")
        lines.append("")
        lines.append("import caliber.workflows.ir as workflow_ir")
        lines.append("")
        lines.append(
            "from caliber.workflows.export_runtime import execute_exported_workflow, run_exported_workflow"
        )
        lines.append("")
        lines.append("# Full IR snapshot used by the export runtime path.")
        lines.append(f"_EXPORTED_IR = {_ir_expression_literal(ir)}")
        lines.append("")
        lines.append("def run(")
        lines.append("    input_text: str,")
        lines.append("    *,")
        lines.append("    session_id: str | None = None,")
        lines.append("    config: Any | None = None,")
        lines.append("    executor: Any | None = None,")
        lines.append("    resolver: Any | None = None,")
        lines.append("    preview: bool = False,")
        lines.append("    active_project_id: str | None = None,")
        lines.append("):")
        lines.append("    return run_exported_workflow(")
        lines.append("        _EXPORTED_IR,")
        lines.append("        input_text,")
        lines.append("        session_id=session_id,")
        lines.append("        config=config,")
        lines.append("        executor=executor,")
        lines.append("        resolver=resolver,")
        lines.append("        preview=preview,")
        lines.append("        active_project_id=active_project_id,")
        lines.append("    )")
        lines.append("")
        lines.append("def run_detailed(")
        lines.append("    input_text: str,")
        lines.append("    *,")
        lines.append("    session_id: str | None = None,")
        lines.append("    config: Any | None = None,")
        lines.append("    executor: Any | None = None,")
        lines.append("    resolver: Any | None = None,")
        lines.append("    preview: bool = False,")
        lines.append("    active_project_id: str | None = None,")
        lines.append("    identity: Any | None = None,")
        lines.append("    session_factory: Any | None = None,")
        lines.append("    extra_tools: dict[str, Any] | None = None,")
        lines.append("    workflow_alias: str | None = None,")
        lines.append("    workflow_version_id: str | None = None,")
        lines.append("    knowledge_query_runner: Any | None = None,")
        lines.append("    knowledge_build_runner: Any | None = None,")
        lines.append("    subworkflow_runner: Any | None = None,")
        lines.append("    session_memory_store: Any | None = None,")
        lines.append("):")
        lines.append("    return execute_exported_workflow(")
        lines.append("        _EXPORTED_IR,")
        lines.append("        input_text,")
        lines.append("        session_id=session_id,")
        lines.append("        config=config,")
        lines.append("        executor=executor,")
        lines.append("        resolver=resolver,")
        lines.append("        preview=preview,")
        lines.append("        extra_tools=extra_tools,")
        lines.append("        workflow_alias=workflow_alias,")
        lines.append("        workflow_version_id=workflow_version_id,")
        lines.append("        session_factory=session_factory,")
        lines.append("        identity=identity,")
        lines.append("        active_project_id=active_project_id,")
        lines.append("        knowledge_query_runner=knowledge_query_runner,")
        lines.append("        knowledge_build_runner=knowledge_build_runner,")
        lines.append("        subworkflow_runner=subworkflow_runner,")
        lines.append("        session_memory_store=session_memory_store,")
        lines.append("    )")
        lines.append("")
        return "\n".join(lines)

    # Tool bindings.
    registry_tool_bindings: dict[str, IRToolBinding] = {}
    mcp_tool_refs: dict[str, tuple[str, str]] = {}
    for agent in sorted(ir.agents(), key=lambda a: a.node_id):
        for binding in sorted(agent.tools, key=lambda b: b.local_name):
            if binding.binding_type == "registered_function":
                registry_tool_bindings.setdefault(binding.local_name, binding)
            elif binding.binding_type == "mcp_tool":
                mcp_tool_refs.setdefault(
                    binding.local_name,
                    (binding.mcp_server_id or "", binding.mcp_tool_name or ""),
                )
    lines.append("from typing import Any")
    lines.append("")
    lines.append("from agents import Agent, RunConfig, Runner, handoff")
    lines.append("import mlflow")
    lines.append("")
    if mcp_tool_refs:
        lines.append("from caliber.mcp_gateway import invoke_tool_by_server_id_sync")
    lines.append("from caliber.workflows.guardrails import enforce_guardrails")
    if handoff_helpers_needed:
        lines.append("from caliber.workflows.runtime import (")
        lines.append("    run_with_caliber_context,")
        lines.append("    workflow_handoff_input_filter,")
        lines.append("    workflow_handoff_is_enabled,")
        lines.append("    workflow_model,")
        lines.append(")")
    else:
        lines.append(
            "from caliber.workflows.runtime import run_with_caliber_context, workflow_model"
        )
    # The export binds tools the same way the platform does — subprocess by default,
    # allowlist enforced — so a workflow does not change behaviour by being exported.
    lines.append("from caliber.workflows.runtime import bind_exported_tool")
    lines.append("from caliber.workflows.tools import ToolRegistryEntry")
    lines.append("")
    if mcp_tool_refs:
        lines.append("# MCP tool bindings call the shared CALIBER MCP gateway directly.")
        lines.append("def _bind_mcp_tool(local_name: str, server_id: str, tool_name: str):")
        lines.append("    def _call(*args: Any, **kwargs: Any) -> dict[str, Any]:")
        lines.append("        if kwargs:")
        lines.append("            arguments: dict[str, Any] = dict(kwargs)")
        lines.append("        elif len(args) == 1:")
        lines.append("            arg = args[0]")
        lines.append("            if isinstance(arg, dict):")
        lines.append("                arguments = arg")
        lines.append('            elif arg in ("", None):')
        lines.append("                arguments = {}")
        lines.append("            else:")
        lines.append('                arguments = {"input": arg}')
        lines.append("        elif args:")
        lines.append('            arguments = {"args": list(args)}')
        lines.append("        else:")
        lines.append("            arguments = {}")
        lines.append("        result = invoke_tool_by_server_id_sync(")
        lines.append("            server_id=server_id,")
        lines.append("            tool_name=tool_name,")
        lines.append("            arguments=arguments,")
        lines.append("        )")
        lines.append("        return {")
        lines.append('            "tool": local_name,')
        lines.append('            "server_id": server_id,')
        lines.append('            "tool_name": tool_name,')
        lines.append('            "arguments": arguments,')
        lines.append('            "result": result,')
        lines.append("        }")
        lines.append("    return _call")
        for local_name in sorted(mcp_tool_refs):
            var = _py_identifier(local_name)
            server_id, tool_name = mcp_tool_refs[local_name]
            lines.append(
                f"{var} = _bind_mcp_tool({_str_literal(local_name)}, {_str_literal(server_id)}, {_str_literal(tool_name)})"
            )
        lines.append("")

    def _handoff_expr(handoff_spec: IRHandoff) -> str:
        args = [_py_identifier(handoff_spec.target_node_id)]
        if str(handoff_spec.description or "").strip():
            args.append(f"tool_description_override={_str_literal(handoff_spec.description)}")
        input_filter = handoff_spec.input_filter
        if isinstance(input_filter, str) and input_filter.strip():
            args.append(f"input_filter=workflow_handoff_input_filter({_str_literal(input_filter)})")
        condition = handoff_spec.condition
        if isinstance(condition, str) and condition.strip():
            args.append(f"is_enabled=workflow_handoff_is_enabled({_str_literal(condition)})")
        return f"handoff({', '.join(args)})"

    # Constructing the graph at run time is deliberate: a standalone caller may pass
    # explicit sandbox configuration to ``run(config=...)``. Binding registered tools at
    # module import made that configuration arrive too late and could even reject an
    # otherwise valid explicit configuration based on the ambient environment.
    lines.append("def _build_agent_graph(*, config: Any | None = None):")
    if registry_tool_bindings:
        lines.append("    # Tool bindings (resolved from the CALIBER tool registry).")
        for local_name in sorted(registry_tool_bindings):
            var = _py_identifier(local_name)
            binding = registry_tool_bindings[local_name]
            lines.append(
                f"    {var} = bind_exported_tool("
                f"{_tool_registry_entry_literal(binding)}, config=config)"
            )

    # Agent definitions in handoff-target-first order.
    for agent in _topo_order_agents(ir):
        var = _py_identifier(agent.node_id)
        lines.append(f"    # node: {agent.node_id}")
        lines.append(f"    {var} = Agent(")
        lines.append(f"        name={_str_literal(agent.name)},")
        lines.append(f"        model=workflow_model({_str_literal(agent.node_id)}),")
        lines.append(f"        instructions={_agent_instructions_arg(agent)},")
        tool_vars = ", ".join(
            _py_identifier(b.local_name) for b in sorted(agent.tools, key=lambda b: b.local_name)
        )
        lines.append(f"        tools=[{tool_vars}],")
        handoff_vars = ", ".join(
            _handoff_expr(h) for h in sorted(agent.handoffs, key=lambda h: h.target_node_id)
        )
        lines.append(f"        handoffs=[{handoff_vars}],")
        lines.append("    )")

    entry_var = _py_identifier(ir.entry_node_id)
    lines.append(f"    return {entry_var}")
    lines.append("")
    guardrails = sorted(
        (n for n in ir.nodes.values() if isinstance(n, IRGuardrail)),
        key=lambda g: g.node_id,
    )
    if guardrails:
        lines.append("# Guardrail specs enforced after the entry agent completes.")
        lines.append("_GUARDRAILS = [")
        for guard in guardrails:
            spec = {
                "node_id": guard.node_id,
                "mode": guard.mode,
                "on_failure": guard.on_failure,
                "checks": [{"kind": c.kind, "params": c.params} for c in guard.checks],
            }
            lines.append(f"    {_py_literal(spec)},")
        lines.append("]")
        lines.append("")

    lines.append(
        "def run(input_text: str, *, session_id: str | None = None, config: Any | None = None):"
    )
    lines.append("    with run_with_caliber_context(")
    lines.append(f"        workflow_id={_str_literal(ir.workflow_id)},")
    lines.append(f"        workflow_version={_str_literal(ir.version)},")
    lines.append(f"        entry_node_id={_str_literal(ir.entry_node_id)},")
    lines.append(f"        compiler_version={_str_literal(COMPILER_VERSION)},")
    lines.append(f"        manifest_hash={_str_literal(ir.manifest_hash)},")
    lines.append(f"        default_model_ref={_str_literal(ir.default_model_ref)},")
    lines.append(f"        extra_tags={_py_literal(ir.mlflow_trace_group_tags)},")
    lines.append("        session_id=session_id,")
    lines.append("    ):")
    # Agent construction calls ``workflow_model``. Build inside the context so inherited
    # models resolve to this workflow's pinned default rather than the process fallback.
    lines.append("        entry_agent = _build_agent_graph(config=config)")
    lines.append(
        "        result = Runner.run_sync(entry_agent, input_text, run_config=RunConfig(tracing_disabled=True))"
    )
    if guardrails:
        lines.append("        enforce_guardrails(result.final_output, [], _GUARDRAILS)")
    lines.append("        return result.final_output")
    lines.append("")

    return "\n".join(lines)


def _py_literal(value: Any) -> str:  # noqa: PLR0911 - deterministic literal dispatcher
    """Deterministic, valid-Python literal emitter for JSON-ish values."""
    if isinstance(value, str):
        return json.dumps(value)
    if value is True:
        return "True"
    if value is False:
        return "False"
    if value is None:
        return "None"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_py_literal(item) for item in value) + "]"
    if isinstance(value, tuple):
        if not value:
            return "()"
        inner = ", ".join(_py_literal(item) for item in value)
        if len(value) == 1:
            inner += ","
        return f"({inner})"
    if isinstance(value, dict):
        items = ", ".join(
            f"{_py_literal(key)}: {_py_literal(val)}"
            for key, val in sorted(value.items(), key=lambda item: str(item[0]))
        )
        return "{" + items + "}"
    raise TypeError(f"unsupported python literal value: {type(value).__name__}")


def _ir_expression_literal(  # noqa: PLR0911 - dataclass/enum/value encoder
    value: Any, module_alias: str = "workflow_ir"
) -> str:
    """Emit a constructor expression for workflow IR dataclass trees."""
    if isinstance(value, Enum):
        return f"{module_alias}.{type(value).__name__}.{value.name}"
    if is_dataclass(value) and not isinstance(value, type):
        args = ", ".join(
            f"{field_def.name}="
            f"{_ir_expression_literal(getattr(value, field_def.name), module_alias)}"
            for field_def in fields(value)
        )
        return f"{module_alias}.{type(value).__name__}({args})"
    if isinstance(value, list):
        return "[" + ", ".join(_ir_expression_literal(item, module_alias) for item in value) + "]"
    if isinstance(value, tuple):
        if not value:
            return "()"
        inner = ", ".join(_ir_expression_literal(item, module_alias) for item in value)
        if len(value) == 1:
            inner += ","
        return f"({inner})"
    if isinstance(value, dict):
        items = ", ".join(
            f"{_ir_expression_literal(key, module_alias)}: "
            f"{_ir_expression_literal(val, module_alias)}"
            for key, val in value.items()
        )
        return "{" + items + "}"
    return _py_literal(value)


def _tool_registry_entry_literal(binding: IRToolBinding) -> str:
    payload = {
        "name": binding.resolved_name,
        "version": binding.resolved_version,
        "module_path": binding.module_path,
        "callable_name": binding.callable_name,
        "execution_backend": binding.execution_backend,
        "backend_config": binding.backend_config,
        "side_effect_level": binding.side_effect_level,
        "requires_approval": binding.requires_approval,
        "allow_in_preview": binding.allow_in_preview,
        "input_schema": binding.input_schema,
        "output_schema": binding.output_schema,
        "secret_refs": binding.secret_refs,
    }
    return f"ToolRegistryEntry(**{_py_literal(payload)})"


# ---------------------------------------------------------------------------
# Top-level compile entry point
# ---------------------------------------------------------------------------


def _report(ir: IRWorkflow, validation: dict[str, Any]) -> dict[str, Any]:
    agents = ir.agents()
    return {
        "compiler_version": COMPILER_VERSION,
        "workflow_id": ir.workflow_id,
        "version": ir.version,
        "manifest_hash": ir.manifest_hash,
        "export_mode": _export_mode(ir),
        "entry_node_id": ir.entry_node_id,
        "output_node_id": ir.output_node_id,
        "node_count": len(ir.nodes),
        "agent_count": len(agents),
        "edge_count": len(ir.edges),
        "deploy_gates": sorted(g.name for g in ir.deploy_gates),
        "tool_refs": sorted({b.registry_ref for a in agents for b in a.tools}),
        "validation": validation,
    }


def _resolver_fingerprint(manifest: WorkflowManifest, resolver: ToolResolver) -> str:
    """A signature of how this manifest's tools resolve right now (ext D1).

    Included in the compile-cache key so a tool version/status change in the
    registry invalidates cached results even though the manifest is unchanged.
    """
    parts: list[str] = []
    for name in sorted(manifest.tools):
        binding = manifest.tools[name]
        if isinstance(binding, RegisteredFunctionToolBinding):
            try:
                res = resolver.resolve(binding.registry_ref, binding.version_constraint)
                parts.append(
                    f"{name}={res.entry.registry_ref}@{res.entry.version}:{res.entry.status}"
                )
            except Exception:
                # Unresolved tools still key distinctly so the cache stays correct.
                parts.append(f"{name}=UNRESOLVED")
            continue
        assert isinstance(binding, McpToolBinding)
        parts.append(
            f"{name}=MCP:{binding.server_id}/{binding.tool_name}:"
            f"{binding.side_effect_level}:approval={binding.requires_approval}:"
            f"schema={binding.tool_schema_version}"
        )
    return "|".join(parts)


def _cache_key(
    manifest: WorkflowManifest,
    resolver: ToolResolver,
    version: str,
    skill_contents: dict[str, str] | None = None,
) -> str:
    # Skill *content* is volatile (a skill can be improved without the manifest
    # changing), so its fingerprint is part of the key — otherwise a cached
    # compile could serve stale skill instructions.
    skills_fp = ""
    if skill_contents:
        skills_fp = hashlib.sha256(
            "|".join(f"{k}={v}" for k, v in sorted(skill_contents.items())).encode("utf-8")
        ).hexdigest()
    raw = (
        f"{manifest.manifest_hash()}|{version}|{COMPILER_VERSION}"
        f"|{_resolver_fingerprint(manifest, resolver)}|{skills_fp}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def compile_workflow(
    manifest: WorkflowManifest,
    *,
    resolver: ToolResolver,
    version: str = "draft",
    use_cache: bool = False,
    skill_contents: dict[str, str] | None = None,
) -> CompileResult:
    """Validate, build IR, generate code, and emit a report (plan §10.2).

    When ``use_cache`` is set, an identical (manifest, version, tool-resolution,
    skill-content) tuple returns a cached :class:`CompileResult` (ext D1).
    ``skill_contents`` (name → content) composes each agent's ``skills`` into
    its system prompt; ``None`` leaves agents skill-free.
    """
    if manifest.runtime.sdk_version_policy != "runtime-pinned":
        raise CompileError(
            f"unsupported sdk_version_policy {manifest.runtime.sdk_version_policy!r}; "
            "only 'runtime-pinned' is supported"
        )

    cache_key: str | None = None
    if use_cache:
        cache_key = _cache_key(manifest, resolver, version, skill_contents)
        hit = _COMPILE_CACHE.get(cache_key)
        if hit is not None:
            _COMPILE_CACHE.move_to_end(cache_key)
            # Return a copy so callers can't mutate the cached instance.
            return replace(hit, cached=True)

    started = time.perf_counter()
    validation = validate_manifest(manifest, resolver=resolver)
    if not validation.valid:
        raise CompileError(
            "manifest failed validation; cannot compile",
            report=validation.to_dict(),
        )

    ir = build_ir(manifest, resolver, version=version, skill_contents=skill_contents)
    generated = generate_python(ir)
    report = _report(ir, validation.to_dict())
    requirements = ["openai-agents>=0.1.0", "mlflow>=3.12,<4"]
    result = CompileResult(
        ir=ir,
        generated_python=generated,
        report=report,
        manifest_hash=ir.manifest_hash,
        requirements=requirements,
        compile_ms=round((time.perf_counter() - started) * 1000, 3),
        cached=False,
    )
    if cache_key is not None:
        _COMPILE_CACHE[cache_key] = result
        while len(_COMPILE_CACHE) > _COMPILE_CACHE_MAX:
            _COMPILE_CACHE.popitem(last=False)
    return result


__all__ = [
    "COMPILER_VERSION",
    "CompileError",
    "CompileResult",
    "build_ir",
    "clear_compile_cache",
    "compile_workflow",
    "generate_python",
]
