"""Workflow graph validation (plan §10.3, §15.6, §19.3).

Validation is *static*: it runs on a parsed :class:`WorkflowManifest` plus
(optionally) a tool resolver, and produces a structured report of errors and
warnings. Errors block publish; warnings allow publish but require
acknowledgement for production (plan §16.5).

The checks, in order:

1. exactly one start node; at least one reachable output node;
2. acyclicity — arbitrary graph cycles are rejected, while pure handoff
   cycles are allowed with a warning because runtime execution is bounded
   by a handoff hop cap (plan §9.1.1, §10.3);
3. every edge's ``map`` references real ports and connects assignable types;
4. required inputs satisfied; orphaned / unreachable nodes flagged;
5. tool references resolve in the registry (when a resolver is supplied), and a
   ``write``/``external_action`` tool reaching output without a Human Approval
   node is warned;
6. agent prompt refs exist in ``artifacts.prompts``;
7. deploy-gate dataset refs exist in ``artifacts.eval_datasets``.

The report shape matches plan §15.6 so the route layer can return it verbatim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from caliber.workflows.guardrails import known_check_kinds
from caliber.workflows.ir import IRType
from caliber.workflows.manifest import (
    AgentNode,
    ApiRequestNode,
    ErrorBoundaryNode,
    ExternalAppNode,
    FileInputNode,
    FolderInputNode,
    ForEachNode,
    GuardrailNode,
    HumanApprovalNode,
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
from caliber.workflows.tools import ToolResolutionError, ToolResolver

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

# DFS colors for cycle detection.
_WHITE, _GRAY, _BLACK = 0, 1, 2

_EXECUTABLE_ORCHESTRATION_TARGET_LABEL = "agent, subworkflow, tool, mcp_resource, knowledge_query, knowledge_build, template, python_code, external_app, webhook, or api_request"
_EXECUTABLE_ORCHESTRATION_TARGET_TYPES = (
    AgentNode,
    SubworkflowNode,
    ToolNode,
    McpResourceNode,
    KnowledgeQueryNode,
    KnowledgeBuildNode,
    TemplateNode,
    PythonCodeNode,
    ExternalAppNode,
    WebhookNode,
    ApiRequestNode,
)
_MIN_PARALLEL_FANOUT = 2
_MIN_JOIN_FANIN = 2

# Keys that, when carrying an inline string value in an open manifest field
# (e.g. a guardrail check's ``params``), indicate a secret was pasted into the
# manifest instead of referenced by name (plan §18.2, ext A3). ``secret_refs``
# (a list of *names*) is allowed and skipped.
# Match a secret term only as a delimited segment of the key (start/end or
# separated by ``_``/``-``) so port names like ``passthrough`` or ``tokenize``
# don't false-positive. ``password``/``passwd`` (not bare ``pass``), ``secret``,
# ``api_key``, ``access_key``, ``private_key``, ``token``, ``bearer``, ``credential``.
_SECRET_KEY_RE = re.compile(
    r"(?:^|[_-])(password|passwd|secret|api[_-]?key|apikey|access[_-]?key|"
    r"private[_-]?key|token|bearer|credential)s?(?:$|[_-])",
    re.IGNORECASE,
)
_SECRET_ALLOWED_KEYS = frozenset({"secret_refs"})


def find_inline_secrets(data: Any, path: str = "") -> list[str]:
    """Return dotted paths of secret-looking inline values in a manifest dict.

    Walks the manifest recursively; flags a string value whose key name matches
    a secret pattern. The point is to keep raw secrets out of stored manifests
    and logs — tools reference secrets by name via ``secret_refs`` instead.
    """
    found: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            child = f"{path}.{key}" if path else str(key)
            if key in _SECRET_ALLOWED_KEYS:
                continue
            if isinstance(value, str) and value and _SECRET_KEY_RE.search(str(key)):
                found.append(child)
            else:
                found.extend(find_inline_secrets(value, child))
    elif isinstance(data, list):
        for i, item in enumerate(data):
            found.extend(find_inline_secrets(item, f"{path}[{i}]"))
    return found


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    path: str
    message: str
    severity: str = SEVERITY_ERROR

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "severity": self.severity,
        }


@dataclass
class ValidationReport:
    issues: list[ValidationIssue] = field(default_factory=list)

    def add(self, code: str, path: str, message: str, severity: str = SEVERITY_ERROR) -> None:
        self.issues.append(ValidationIssue(code, path, message, severity))

    @property
    def errors(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> list[ValidationIssue]:
        return [i for i in self.issues if i.severity != SEVERITY_ERROR]

    @property
    def valid(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "errors": [i.to_dict() for i in self.errors],
            "warnings": [i.to_dict() for i in self.warnings],
        }


# Ports exposed by each node, by direction. Used for map validation.
def _node_outputs(node: WorkflowNode) -> dict[str, IRType]:
    raw = getattr(node, "outputs", {}) or {}
    return {name: IRType(spec.type, spec.schema_) for name, spec in raw.items()}


def _node_inputs(node: WorkflowNode) -> dict[str, IRType]:
    raw = getattr(node, "inputs", {}) or {}
    return {name: IRType(spec.type, spec.schema_) for name, spec in raw.items()}


@dataclass
class _Adj:
    """An adjacency entry recording how two nodes are connected."""

    to: str
    kind: str  # "edge" | "handoff"
    edge_id: str | None = None


def validate_manifest(
    manifest: WorkflowManifest,
    *,
    resolver: ToolResolver | None = None,
    skill_names: set[str] | None = None,
) -> ValidationReport:
    """Validate a parsed manifest and return a structured report.

    ``skill_names`` (the registry's known skill names) enables agent-skill-ref
    resolution; ``None`` skips it (the compile path doesn't enforce skills).
    """
    report = ValidationReport()
    nodes = manifest.nodes

    start_ids = [nid for nid, n in nodes.items() if isinstance(n, StartNode)]
    output_ids = [nid for nid, n in nodes.items() if isinstance(n, OutputNode)]

    _check_start_output(report, start_ids, output_ids)
    adjacency = _build_adjacency(report, manifest)
    _check_reachability(report, manifest, start_ids, output_ids, adjacency)
    _check_cycles(report, manifest, adjacency)
    _check_edges(report, manifest)
    _check_handoffs(report, manifest)
    _check_orchestration_nodes(report, manifest)
    _check_node_setup(report, manifest)
    _check_knowledge_queries(report, manifest)
    _check_knowledge_builds(report, manifest)
    _check_tools(report, manifest, resolver)
    _check_prompts(report, manifest)
    _check_agent_eval_datasets(report, manifest)
    _check_agent_skills(report, manifest, skill_names)
    _check_guardrails(report, manifest)
    _check_deploy_gates(report, manifest)
    _check_write_tool_approval(report, manifest, resolver)

    for secret_path in find_inline_secrets(manifest.to_dict()):
        report.add(
            "inline_secret",
            secret_path,
            f"possible secret value at {secret_path!r}; reference secrets by name "
            "via tool secret_refs, never inline.",
        )

    return report


def _check_start_output(
    report: ValidationReport, start_ids: list[str], output_ids: list[str]
) -> None:
    if len(start_ids) == 0:
        report.add("no_start_node", "nodes", "Exactly one start node is required.")
    elif len(start_ids) > 1:
        report.add(
            "multiple_start_nodes",
            "nodes",
            f"Exactly one start node is required; found {len(start_ids)}: {sorted(start_ids)}.",
        )
    if len(output_ids) == 0:
        report.add("no_output_node", "nodes", "A reachable output node is required.")


def _build_adjacency(report: ValidationReport, manifest: WorkflowManifest) -> dict[str, list[_Adj]]:
    adjacency: dict[str, list[_Adj]] = {nid: [] for nid in manifest.nodes}
    for edge in manifest.edges:
        if edge.from_ not in manifest.nodes:
            report.add(
                "edge_bad_source",
                f"edges.{edge.id}.from",
                f"Edge {edge.id!r} source node {edge.from_!r} does not exist.",
            )
            continue
        if edge.to not in manifest.nodes:
            report.add(
                "edge_bad_target",
                f"edges.{edge.id}.to",
                f"Edge {edge.id!r} target node {edge.to!r} does not exist.",
            )
            continue
        adjacency[edge.from_].append(_Adj(to=edge.to, kind="edge", edge_id=edge.id))
    for nid, node in manifest.nodes.items():
        if isinstance(node, AgentNode):
            for handoff in node.handoffs:
                if handoff.target in manifest.nodes and handoff.target != nid:
                    adjacency[nid].append(_Adj(to=handoff.target, kind="handoff"))
    return adjacency


def _check_reachability(
    report: ValidationReport,
    manifest: WorkflowManifest,
    start_ids: list[str],
    output_ids: list[str],
    adjacency: dict[str, list[_Adj]],
) -> set[str]:
    if not start_ids:
        return set()
    reachable: set[str] = set()
    stack = list(start_ids)
    while stack:
        nid = stack.pop()
        if nid in reachable:
            continue
        reachable.add(nid)
        for adj in adjacency.get(nid, []):
            if adj.to not in reachable:
                stack.append(adj.to)

    if output_ids and not any(oid in reachable for oid in output_ids):
        report.add(
            "output_unreachable",
            "nodes",
            "A reachable output node is required; no output node is reachable from start.",
        )

    from caliber.workflows.manifest import NoteNode  # noqa: PLC0415

    for nid, node in manifest.nodes.items():
        if isinstance(node, (StartNode, NoteNode)):
            continue
        if nid not in reachable:
            report.add(
                "orphaned_node",
                f"nodes.{nid}",
                f"Node {nid!r} is not reachable from the start node.",
                severity=SEVERITY_WARNING,
            )
    return reachable


def _check_cycles(
    report: ValidationReport,
    manifest: WorkflowManifest,
    adjacency: dict[str, list[_Adj]],
) -> None:
    color = dict.fromkeys(manifest.nodes, _WHITE)
    reported_handoff_cycle = False
    reported_arbitrary_cycle = False
    # Parallel stacks: the nodes on the current DFS path, and the kind of edge
    # used to *enter* each one (entry kind of the root is irrelevant).
    path_nodes: list[str] = []
    path_kinds: list[str] = []

    def _classify(cycle_edges: list[str]) -> None:
        if cycle_edges and all(kind == "handoff" for kind in cycle_edges):
            nonlocal reported_handoff_cycle
            if reported_handoff_cycle:
                return
            reported_handoff_cycle = True
            report.add(
                "handoff_cycle",
                "nodes",
                (
                    "Handoff cycles are allowed, but runtime execution is bounded by "
                    "the handoff hop cap. Add conditions or prompts that let the "
                    "agents terminate cleanly."
                ),
                severity=SEVERITY_WARNING,
            )
        else:
            nonlocal reported_arbitrary_cycle
            if reported_arbitrary_cycle:
                return
            reported_arbitrary_cycle = True
            report.add(
                "arbitrary_cycle",
                "nodes",
                "Arbitrary cycles are not supported in MVP; the workflow graph must be acyclic.",
            )

    def _dfs(nid: str, entry_kind: str) -> None:
        color[nid] = _GRAY
        path_nodes.append(nid)
        path_kinds.append(entry_kind)
        for adj in adjacency.get(nid, []):
            if color[adj.to] == _GRAY:
                # Cycle: edges strictly within it are those entering the nodes
                # from adj.to onward, plus the closing back-edge.
                start = path_nodes.index(adj.to)
                cycle_edges = [*path_kinds[start + 1 :], adj.kind]
                _classify(cycle_edges)
            elif color[adj.to] == _WHITE:
                _dfs(adj.to, adj.kind)
        path_nodes.pop()
        path_kinds.pop()
        color[nid] = _BLACK

    for nid in manifest.nodes:
        if color[nid] == _WHITE:
            _dfs(nid, "")


def _check_edges(report: ValidationReport, manifest: WorkflowManifest) -> None:
    for edge in manifest.edges:
        source = manifest.nodes.get(edge.from_)
        target = manifest.nodes.get(edge.to)
        if source is None or target is None:
            continue  # already reported in adjacency build
        src_outputs = _node_outputs(source)
        tgt_inputs = _node_inputs(target)
        for src_port, tgt_port in edge.map.items():
            if src_port not in src_outputs:
                report.add(
                    "map_bad_source_port",
                    f"edges.{edge.id}.map",
                    f"Source output {src_port!r} not found on node {edge.from_!r}.",
                )
                continue
            if tgt_port not in tgt_inputs:
                report.add(
                    "map_bad_target_port",
                    f"edges.{edge.id}.map",
                    f"Target input {tgt_port!r} not found on node {edge.to!r}.",
                )
                continue
            src_type = src_outputs[src_port]
            tgt_type = tgt_inputs[tgt_port]
            if not tgt_type.assignable_from(src_type):
                report.add(
                    "type_mismatch",
                    f"edges.{edge.id}.map",
                    f"Type mismatch on edge {edge.id!r}: {edge.from_}.{src_port} "
                    f"({src_type.name}) is not assignable to {edge.to}.{tgt_port} "
                    f"({tgt_type.name}).",
                )


def _check_handoffs(report: ValidationReport, manifest: WorkflowManifest) -> None:
    for nid, node in manifest.nodes.items():
        if not isinstance(node, AgentNode):
            continue
        for index, handoff in enumerate(node.handoffs):
            path = f"nodes.{nid}.handoffs[{index}].target"
            if handoff.target == nid:
                report.add(
                    "handoff_self_target",
                    path,
                    f"Handoff {index + 1} on agent {nid!r} targets the same agent. Pick a different specialist.",
                )
                continue
            target = manifest.nodes.get(handoff.target)
            if target is None:
                report.add(
                    "handoff_bad_target",
                    path,
                    f"Handoff {index + 1} on agent {nid!r} points to unknown node {handoff.target!r}.",
                )
            elif not isinstance(target, AgentNode):
                report.add(
                    "handoff_non_agent",
                    path,
                    f"Handoff {index + 1} on agent {nid!r} can only target another agent; "
                    f"{handoff.target!r} is a {target.type} node.",
                )


def _check_tool_binding_reference(
    report: ValidationReport,
    manifest: WorkflowManifest,
    resolver: ToolResolver | None,
    *,
    local_name: str,
    path: str,
    owner_label: str,
) -> None:
    binding = manifest.tools.get(local_name)
    if binding is None:
        report.add(
            "unbound_tool",
            path,
            f"{owner_label} references tool {local_name!r} which has no binding "
            f"in the manifest 'tools' section.",
        )
        return
    if isinstance(binding, McpToolBinding) or resolver is None:
        return
    assert isinstance(binding, RegisteredFunctionToolBinding)
    try:
        resolution = resolver.resolve(binding.registry_ref, binding.version_constraint)
    except ToolResolutionError as exc:
        report.add("missing_tool", path, str(exc))
        return
    for warning in resolution.warnings:
        report.add(
            "deprecated_tool",
            path,
            warning,
            severity=SEVERITY_WARNING,
        )


def _binding_side_effect_level(
    binding: RegisteredFunctionToolBinding | McpToolBinding,
    resolver: ToolResolver | None,
) -> str | None:
    if isinstance(binding, McpToolBinding):
        return binding.side_effect_level
    if resolver is None:
        return None
    try:
        resolution = resolver.resolve(binding.registry_ref, binding.version_constraint)
    except ToolResolutionError:
        return None
    return resolution.entry.side_effect_level


def _check_tools(
    report: ValidationReport,
    manifest: WorkflowManifest,
    resolver: ToolResolver | None,
) -> None:
    for nid, node in manifest.nodes.items():
        if isinstance(node, AgentNode):
            for local_name in node.tools:
                _check_tool_binding_reference(
                    report,
                    manifest,
                    resolver,
                    local_name=local_name,
                    path=f"nodes.{nid}.tools",
                    owner_label=f"Agent {nid!r}",
                )
            continue
        if isinstance(node, ToolNode):
            _check_tool_binding_reference(
                report,
                manifest,
                resolver,
                local_name=node.tool_name,
                path=f"nodes.{nid}.tool_name",
                owner_label=f"Tool node {nid!r}",
            )


def _check_prompts(report: ValidationReport, manifest: WorkflowManifest) -> None:
    for nid, node in manifest.nodes.items():
        if not isinstance(node, AgentNode):
            continue
        instr = node.instructions
        if isinstance(instr, PromptRefInstructions) and instr.ref not in manifest.artifacts.prompts:
            report.add(
                "missing_prompt_ref",
                f"nodes.{nid}.instructions.ref",
                f"Prompt ref {instr.ref!r} is not declared in artifacts.prompts.",
            )


def _check_agent_eval_datasets(report: ValidationReport, manifest: WorkflowManifest) -> None:
    for nid, node in manifest.nodes.items():
        if not isinstance(node, AgentNode):
            continue
        dataset_ref = (node.eval_dataset or "").strip()
        if not dataset_ref:
            continue
        if dataset_ref not in manifest.artifacts.eval_datasets:
            report.add(
                "missing_eval_dataset",
                f"nodes.{nid}.eval_dataset",
                f"Agent {nid!r} references eval dataset {dataset_ref!r} "
                "that is not declared in artifacts.eval_datasets.",
            )


def _check_agent_skills(
    report: ValidationReport, manifest: WorkflowManifest, skill_names: set[str] | None
) -> None:
    """Flag agent skill refs that don't resolve to a registered skill.

    Only runs when the caller supplies the registry's skill names (the validate
    route does); the compile path leaves it ``None`` and composes best-effort.
    """
    if skill_names is None:
        return
    for nid, node in manifest.nodes.items():
        if not isinstance(node, AgentNode):
            continue
        for skill in node.skills:
            if skill not in skill_names:
                report.add(
                    "missing_skill_ref",
                    f"nodes.{nid}.skills",
                    f"Skill {skill!r} is not a registered skill.",
                )


def _check_orchestration_nodes(report: ValidationReport, manifest: WorkflowManifest) -> None:
    outgoing_edges: dict[str, list[Any]] = {}
    incoming_edges: dict[str, list[Any]] = {}
    for edge in manifest.edges:
        outgoing_edges.setdefault(edge.from_, []).append(edge)
        incoming_edges.setdefault(edge.to, []).append(edge)

    for nid, node in manifest.nodes.items():
        _check_single_orchestration_node(
            report=report,
            manifest=manifest,
            node_id=nid,
            node=node,
            outgoing_edges=outgoing_edges,
            incoming_edges=incoming_edges,
        )


def _check_single_orchestration_node(
    *,
    report: ValidationReport,
    manifest: WorkflowManifest,
    node_id: str,
    node: WorkflowNode,
    outgoing_edges: dict[str, list[Any]],
    incoming_edges: dict[str, list[Any]],
) -> None:
    if isinstance(node, (WaitUntilNode, WaitForEventNode)):
        return
    if isinstance(node, ParallelNode):
        _check_parallel_node(report, node_id, outgoing_edges)
    elif isinstance(node, JoinNode):
        _check_join_node(report, node_id, incoming_edges)
    elif isinstance(node, SubworkflowNode):
        _check_subworkflow_node(report, manifest, node_id, node)
    elif isinstance(node, ForEachNode):
        _check_optional_executable_target(
            report=report,
            manifest=manifest,
            node_id=node_id,
            target_node_id=node.target_node_id,
            missing_code="foreach_bad_target",
            unsupported_code="foreach_unsupported_target_type",
            node_label="ForEach",
        )
    elif isinstance(node, LoopNode):
        _check_loop_node(report, manifest, node_id, node)
    elif isinstance(node, ErrorBoundaryNode):
        _check_error_boundary_node(report, manifest, node_id, node)


def _check_parallel_node(
    report: ValidationReport,
    node_id: str,
    outgoing_edges: dict[str, list[Any]],
) -> None:
    edge_count = len(outgoing_edges.get(node_id, []))
    if edge_count < _MIN_PARALLEL_FANOUT:
        report.add(
            "parallel_insufficient_fanout",
            f"nodes.{node_id}",
            f"Parallel node {node_id!r} only fans out to {edge_count} downstream "
            "edge(s). Add at least two branches or replace it with a direct connection.",
            severity=SEVERITY_WARNING,
        )


def _check_join_node(
    report: ValidationReport,
    node_id: str,
    incoming_edges: dict[str, list[Any]],
) -> None:
    edges = incoming_edges.get(node_id, [])
    if len(edges) < _MIN_JOIN_FANIN:
        report.add(
            "join_insufficient_fanin",
            f"nodes.{node_id}",
            f"Join node {node_id!r} only receives {len(edges)} upstream edge(s). "
            "Connect at least two branches or remove the join barrier.",
            severity=SEVERITY_WARNING,
        )
    seen_ports: dict[str, str] = {}
    for edge in edges:
        for _from_port, to_port in edge.map.items():
            previous = seen_ports.get(to_port)
            if previous is None:
                seen_ports[to_port] = edge.id
                continue
            report.add(
                "join_duplicate_input_port",
                f"nodes.{node_id}.inputs.{to_port}",
                f"Join node {node_id!r} receives multiple upstream mappings into input "
                f"port {to_port!r} ({previous!r}, {edge.id!r}). The last delivered "
                "value wins, so use distinct input ports per branch for traceable merges.",
                severity=SEVERITY_WARNING,
            )
            break


def _check_subworkflow_node(
    report: ValidationReport,
    manifest: WorkflowManifest,
    node_id: str,
    node: SubworkflowNode,
) -> None:
    workflow_id = node.workflow_id.strip()
    if not workflow_id:
        report.add(
            "subworkflow_missing_workflow_id",
            f"nodes.{node_id}.workflow_id",
            f"Subworkflow node {node_id!r} requires workflow_id.",
        )
    elif workflow_id == manifest.workflow_id.strip():
        report.add(
            "subworkflow_self_reference",
            f"nodes.{node_id}.workflow_id",
            f"Subworkflow node {node_id!r} cannot invoke the current workflow "
            f"{manifest.workflow_id!r}. Publish a separate child workflow or choose a "
            "different workflow_id.",
        )


def _check_optional_executable_target(
    *,
    report: ValidationReport,
    manifest: WorkflowManifest,
    node_id: str,
    target_node_id: str | None,
    missing_code: str,
    unsupported_code: str,
    node_label: str,
) -> None:
    if not target_node_id:
        return
    target = manifest.nodes.get(target_node_id)
    if target is None:
        report.add(
            missing_code,
            f"nodes.{node_id}.target_node_id",
            f"{node_label} node {node_id!r} target {target_node_id!r} does not exist.",
        )
    elif not isinstance(target, _EXECUTABLE_ORCHESTRATION_TARGET_TYPES):
        report.add(
            unsupported_code,
            f"nodes.{node_id}.target_node_id",
            f"{node_label} node {node_id!r} target {target_node_id!r} must be an "
            f"executable node ({_EXECUTABLE_ORCHESTRATION_TARGET_LABEL}); "
            f"found {target.type!r}.",
        )


def _check_loop_node(
    report: ValidationReport,
    manifest: WorkflowManifest,
    node_id: str,
    node: LoopNode,
) -> None:
    if not node.target_node_id:
        report.add(
            "loop_missing_target",
            f"nodes.{node_id}.target_node_id",
            f"Loop node {node_id!r} requires target_node_id.",
        )
        return
    _check_optional_executable_target(
        report=report,
        manifest=manifest,
        node_id=node_id,
        target_node_id=node.target_node_id,
        missing_code="loop_bad_target",
        unsupported_code="loop_unsupported_target_type",
        node_label="Loop",
    )


def _check_error_boundary_node(
    report: ValidationReport,
    manifest: WorkflowManifest,
    node_id: str,
    node: ErrorBoundaryNode,
) -> None:
    _check_optional_executable_target(
        report=report,
        manifest=manifest,
        node_id=node_id,
        target_node_id=node.target_node_id,
        missing_code="error_boundary_bad_target",
        unsupported_code="error_boundary_unsupported_target_type",
        node_label="Error boundary",
    )
    if not node.compensate_with:
        return
    compensate = manifest.nodes.get(node.compensate_with)
    if compensate is None:
        report.add(
            "error_boundary_bad_compensation",
            f"nodes.{node_id}.compensate_with",
            f"Error boundary node {node_id!r} compensation target {node.compensate_with!r} does not exist.",
        )
    elif not isinstance(compensate, _EXECUTABLE_ORCHESTRATION_TARGET_TYPES):
        report.add(
            "error_boundary_unsupported_compensation_type",
            f"nodes.{node_id}.compensate_with",
            f"Error boundary node {node_id!r} compensation target {node.compensate_with!r} "
            f"must be an executable node ({_EXECUTABLE_ORCHESTRATION_TARGET_LABEL}); "
            f"found {compensate.type!r}.",
        )


def _check_knowledge_queries(report: ValidationReport, manifest: WorkflowManifest) -> None:
    incoming_ports = _incoming_ports(manifest)
    for nid, node in manifest.nodes.items():
        if not isinstance(node, KnowledgeQueryNode):
            continue
        if (
            node.knowledge_base_id
            or node.version_ids
            or "version_ids" in incoming_ports.get(nid, set())
        ):
            continue
        report.add(
            "missing_knowledge_target",
            f"nodes.{nid}.knowledge_base_id",
            "Select a knowledge base or pinned version.",
        )


def _check_knowledge_builds(report: ValidationReport, manifest: WorkflowManifest) -> None:
    incoming_ports = _incoming_ports(manifest)
    for nid, node in manifest.nodes.items():
        if not isinstance(node, KnowledgeBuildNode):
            continue
        if not node.knowledge_base_id:
            report.add(
                "missing_knowledge_build_target",
                f"nodes.{nid}.knowledge_base_id",
                "Select a knowledge base to refresh.",
            )
        if not node.chunking_strategy and "chunking_strategy" not in incoming_ports.get(nid, set()):
            report.add(
                "missing_knowledge_build_chunking_strategy",
                f"nodes.{nid}.chunking_strategy",
                "Choose a chunking strategy or map one into the node.",
            )
        if not node.embedding_model and "embedding_model" not in incoming_ports.get(nid, set()):
            report.add(
                "missing_knowledge_build_embedding_model",
                f"nodes.{nid}.embedding_model",
                "Choose an embedding model or map one into the node.",
            )


def _incoming_ports(manifest: WorkflowManifest) -> dict[str, set[str]]:
    incoming_ports: dict[str, set[str]] = {}
    for edge in manifest.edges:
        if edge.to in manifest.nodes:
            incoming_ports.setdefault(edge.to, set()).update(edge.map.values())
    return incoming_ports


def _check_node_setup(report: ValidationReport, manifest: WorkflowManifest) -> None:
    incoming_ports = _incoming_ports(manifest)
    outgoing_targets: dict[str, set[str]] = {}
    for edge in manifest.edges:
        if edge.from_ in manifest.nodes and edge.to in manifest.nodes:
            outgoing_targets.setdefault(edge.from_, set()).add(edge.to)

    for nid, node in manifest.nodes.items():
        _check_single_node_setup(
            report=report,
            manifest=manifest,
            node_id=nid,
            node=node,
            incoming_ports=incoming_ports,
            outgoing_targets=outgoing_targets,
        )


def _check_single_node_setup(
    *,
    report: ValidationReport,
    manifest: WorkflowManifest,
    node_id: str,
    node: WorkflowNode,
    incoming_ports: dict[str, set[str]],
    outgoing_targets: dict[str, set[str]],
) -> None:
    if isinstance(node, FileInputNode):
        _check_mapped_string_field(
            report=report,
            node_id=node_id,
            field_name="path",
            field_value=node.path,
            incoming_ports=incoming_ports,
            code="missing_file_path",
            message="Provide a file path before this node can run.",
        )
    elif isinstance(node, FolderInputNode):
        _check_mapped_string_field(
            report=report,
            node_id=node_id,
            field_name="path",
            field_value=node.path,
            incoming_ports=incoming_ports,
            code="missing_folder_path",
            message="Provide a folder path before this node can run.",
        )
    elif isinstance(node, InputBucketNode):
        _check_required_string_field(
            report=report,
            node_id=node_id,
            field_name="bucket",
            field_value=node.bucket,
            code="missing_input_bucket",
            message="Select an input bucket before this node can run.",
        )
    elif isinstance(node, OutputBucketNode):
        _check_required_string_field(
            report=report,
            node_id=node_id,
            field_name="bucket",
            field_value=node.bucket,
            code="missing_output_bucket",
            message="Select an output bucket before this node can run.",
        )
    elif isinstance(node, OutputFolderNode):
        _check_required_string_field(
            report=report,
            node_id=node_id,
            field_name="path",
            field_value=node.path,
            code="missing_output_folder_path",
            message="Provide an output folder path before this node can run.",
        )
    elif isinstance(node, GuardrailNode):
        if not node.checks:
            report.add(
                "missing_guardrail_checks",
                f"nodes.{node_id}.checks",
                "Configure at least one guardrail check.",
            )
    elif isinstance(node, RouterNode):
        _check_router_setup(report, manifest, node_id, node, outgoing_targets)


def _check_required_string_field(
    *,
    report: ValidationReport,
    node_id: str,
    field_name: str,
    field_value: str,
    code: str,
    message: str,
) -> None:
    if field_value.strip():
        return
    report.add(code, f"nodes.{node_id}.{field_name}", message)


def _check_mapped_string_field(
    *,
    report: ValidationReport,
    node_id: str,
    field_name: str,
    field_value: str,
    incoming_ports: dict[str, set[str]],
    code: str,
    message: str,
) -> None:
    if field_value.strip() or field_name in incoming_ports.get(node_id, set()):
        return
    report.add(code, f"nodes.{node_id}.{field_name}", message)


def _check_router_setup(
    report: ValidationReport,
    manifest: WorkflowManifest,
    node_id: str,
    node: RouterNode,
    outgoing_targets: dict[str, set[str]],
) -> None:
    if not node.branches:
        report.add(
            "missing_router_branches",
            f"nodes.{node_id}.branches",
            "Add at least one branch before this router can run.",
        )
        return
    router_targets = outgoing_targets.get(node_id, set())
    for index, branch in enumerate(node.branches):
        path = f"nodes.{node_id}.branches[{index}].to"
        if branch.to not in manifest.nodes:
            report.add(
                "router_bad_target",
                path,
                f"Router branch {index + 1} on node {node_id!r} points to unknown node {branch.to!r}.",
            )
            continue
        if branch.to not in router_targets:
            report.add(
                "router_missing_edge",
                path,
                f"Router branch {index + 1} on node {node_id!r} targets {branch.to!r}, but no outgoing edge from {node_id!r} reaches that node.",
            )


def _check_guardrails(report: ValidationReport, manifest: WorkflowManifest) -> None:
    valid_kinds = known_check_kinds()
    for nid, node in manifest.nodes.items():
        if not isinstance(node, GuardrailNode):
            continue
        for check in node.checks:
            if check.kind not in valid_kinds:
                report.add(
                    "unknown_guardrail_check",
                    f"nodes.{nid}.checks",
                    f"Guardrail {nid!r} uses unknown check kind {check.kind!r}; "
                    f"known kinds: {sorted(valid_kinds)}.",
                    severity=SEVERITY_WARNING,
                )


def _check_deploy_gates(report: ValidationReport, manifest: WorkflowManifest) -> None:
    for name, gate in manifest.deploy_gates.items():
        if gate.dataset_ref not in manifest.artifacts.eval_datasets:
            report.add(
                "missing_eval_dataset",
                f"deploy_gates.{name}.dataset_ref",
                f"Deploy gate {name!r} references eval dataset {gate.dataset_ref!r} "
                f"not declared in artifacts.eval_datasets.",
            )


def _check_write_tool_approval(
    report: ValidationReport,
    manifest: WorkflowManifest,
    resolver: ToolResolver | None,
) -> None:
    """Warn when a write/external tool reaches output without a Human Approval node."""
    has_approval_node = any(isinstance(n, HumanApprovalNode) for n in manifest.nodes.values())
    if has_approval_node:
        return
    for nid, node in manifest.nodes.items():
        references: list[tuple[str, str]] = []
        if isinstance(node, AgentNode):
            references = [(local_name, f"nodes.{nid}.tools") for local_name in node.tools]
        elif isinstance(node, ToolNode):
            references = [(node.tool_name, f"nodes.{nid}.tool_name")]
        else:
            continue
        for local_name, path in references:
            binding = manifest.tools.get(local_name)
            if binding is None:
                continue
            side_effect_level = _binding_side_effect_level(binding, resolver)
            if side_effect_level not in ("write", "external_action"):
                continue
            report.add(
                "write_tool_without_approval",
                path,
                f"Tool {local_name!r} has side_effect_level={side_effect_level!r} "
                "but the workflow has no Human Approval node.",
                severity=SEVERITY_WARNING,
            )


__all__ = [
    "SEVERITY_ERROR",
    "SEVERITY_INFO",
    "SEVERITY_WARNING",
    "ValidationIssue",
    "ValidationReport",
    "find_inline_secrets",
    "validate_manifest",
]
