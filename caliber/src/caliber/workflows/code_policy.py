"""The reference profile's refusal of executable code, decided from the compiler's own kind registry (B12.4a, #754).

A workflow can run code in two ways: a **node kind** that executes code (``python_code`` runs author-supplied Python source,
``external_app`` imports and calls installed Python inside the control-plane process), and a **tool form** that does (a
registered ``python_callable`` tool imports an administrator-registered module). ADR 0001 ("unsupported paths are refused, not
degraded") and the protected-effect inventory (EP-011, EP-012, EP-015, EP-016, EP-019) mark these paths ``refuse``.

The kinds are not listed by hand here. :class:`caliber.workflows.ir.NodeType` is the compiler's IR kind registry, and every
member is classified exactly once below (``CODE_NODE_KINDS``, ``TOOL_NODE_KINDS`` or ``INERT_NODE_KINDS``);
``tests/test_profile_code_refusal.py`` fails when a kind is added to the registry without being classified, so a new
executable kind cannot arrive unrefused. A tool form is read from the binding's own ``execution_backend`` and ``binding_type``,
and an unknown form is refused too.

**One refusal code, four boundaries.** Every refusal carries :data:`REFUSAL_CODE` (the code ADR 0001 names for a refused
path) and is raised at validation (``validate_manifest``), at compile (``compile_workflow``), at promotion
(``require_alias_target_ready`` and ``promote``) and at runtime entry (``runtime.execute``, and the in-process tool binders). A
boundary reached without the others (a hand-built IR, a plan built outside ``build_plan``) is therefore still refused.

**A sandbox label is not isolation.** A ``python_code`` node is refused although it runs in the tool sandbox: the built-in
sandbox is a process boundary (separate interpreter, empty environment, rlimits) with ambient filesystem and network
authority, not a container, VM or seccomp boundary (``config.tool_sandbox_backend``), and the code is the workflow author's, not
the administrator's. A registered ``python_callable`` tool is administrator-registered code (trusted-code admission, not
containment of hostile code; the module allowlist is #763), so it is admitted when it runs where the platform puts it: in the
sandbox child, with ``CALIBER_REGISTERED_TOOL_SANDBOX_ENABLED`` on. With it off the tool is imported into the control plane and
refused. Whether a tool is "sandboxed" is decided by the platform's own binding path and configuration, never by a ``sandbox``
flag a tool, a manifest or a registry row carries.

The policy is process-wide state bound when the app starts (:func:`bind_code_policy`), like the registered-tool module
allowlist: the checks are reached from routes, the promoter, the compiler and the runtime, and a refusal only some of them
applied would not be one. A caller that holds a configuration passes an explicit policy, and the stricter of the two wins.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from caliber.workflows.ir import IRAgent, IRTool, IRToolBinding, IRWorkflow, NodeType
from caliber.workflows.manifest import (
    AgentNode,
    RegisteredFunctionToolBinding,
    ToolNode,
    WorkflowManifest,
)
from caliber.workflows.tools import ToolResolutionError, ToolResolver

#: The code ADR 0001 names for a path the reference profile refuses (``unsupported_in_reference_profile``).
REFUSAL_CODE: Final = "unsupported_in_reference_profile"

#: Node kinds that execute code, each with what it executes. These are refused outright in the reference profile.
CODE_NODE_KINDS: Final[Mapping[NodeType, str]] = {
    NodeType.PYTHON_CODE: (
        "runs author-supplied Python source in a sandbox that is a process boundary, not isolation (a sandbox label "
        "does not establish it)"
    ),
    NodeType.EXTERNAL_APP: "imports and calls installed Python inside the control-plane process",
}

#: Node kinds that carry tool bindings. They are not code themselves; each binding is checked for its execution form.
TOOL_NODE_KINDS: Final[frozenset[NodeType]] = frozenset({NodeType.AGENT, NodeType.TOOL})

#: Every other kind in the registry. None of them executes code: they route, wait, read or write through a governed path
#: (file and bucket nodes, webhooks and API requests, MCP resources, knowledge, templates, data transforms) or are
#: structural. Listing them is the point: a kind that is in none of the three sets fails the ratchet test.
INERT_NODE_KINDS: Final[frozenset[NodeType]] = frozenset(
    {
        NodeType.START,
        NodeType.FILE_INPUT,
        NodeType.FOLDER_INPUT,
        NodeType.INPUT_BUCKET,
        NodeType.OUTPUT_BUCKET,
        NodeType.OUTPUT_FOLDER,
        NodeType.WAIT_UNTIL,
        NodeType.WAIT_FOR_EVENT,
        NodeType.PARALLEL,
        NodeType.JOIN,
        NodeType.FOR_EACH,
        NodeType.LOOP,
        NodeType.ERROR_BOUNDARY,
        NodeType.SUBWORKFLOW,
        NodeType.MCP_RESOURCE,
        NodeType.KNOWLEDGE_QUERY,
        NodeType.KNOWLEDGE_BUILD,
        NodeType.TEMPLATE,
        NodeType.DATA_TRANSFORM,
        NodeType.REVIEW_QUEUE_ENQUEUE,
        NodeType.GUARDRAIL,
        NodeType.ROUTER,
        NodeType.HUMAN_APPROVAL,
        NodeType.OUTPUT,
        NodeType.NOTE,
        NodeType.WEBHOOK,
        NodeType.API_REQUEST,
    }
)

#: The tool execution backends that do not run registered Python: an OpenAPI operation is an HTTP call that the effect
#: dispatcher governs (B04.4).
NON_CODE_TOOL_BACKENDS: Final[frozenset[str]] = frozenset({"openapi_http"})

#: The one tool backend that imports and calls registered Python.
CODE_TOOL_BACKEND: Final = "python_callable"

#: An MCP tool binding is a call to a server, not code in this process; which servers are reachable is the MCP allowlist's
#: business (``REFERENCE_MCP_ALLOWLIST``, empty) and the dispatcher's.
MCP_BINDING_TYPE: Final = "mcp_tool"


@dataclass(frozen=True)
class CodePolicy:
    """Whether executable code is refused, and whether registered tools run in the sandbox child.

    ``reference`` is true for the reference profile, where code is refused. ``sandbox_enabled`` mirrors
    ``CALIBER_REGISTERED_TOOL_SANDBOX_ENABLED``: with it off a registered Python tool would be imported into the control plane.
    """

    reference: bool = False
    sandbox_enabled: bool = True

    @classmethod
    def from_config(cls, config: object) -> CodePolicy:
        """The policy a configuration implies (the reference profile refuses; any other profile does not)."""
        return cls(
            # An allow-list for the development profile: any other (or missing) profile gets the stricter answer.
            reference=getattr(config, "profile", "") != "development",
            sandbox_enabled=bool(getattr(config, "registered_tool_sandbox_enabled", True)),
        )


_BOUND: CodePolicy = CodePolicy()


def bind_code_policy(policy: CodePolicy | None) -> None:
    """Install the process-wide policy (``create_app`` binds it from the configuration; ``None`` clears it)."""
    global _BOUND  # noqa: PLW0603 - one process-wide policy by design, like the module allowlist
    _BOUND = policy if policy is not None else CodePolicy()


def bound_code_policy() -> CodePolicy:
    """The policy bound for this process (inactive until a reference-profile app binds one)."""
    return _BOUND


def resolve_code_policy(explicit: CodePolicy | None = None) -> CodePolicy:
    """The policy a check uses: the stricter of ``explicit`` and the process-wide one.

    A reference-profile process keeps refusing code when a caller hands in a development configuration, and a caller
    that holds a reference configuration refuses it in a process that was not bound. The sandbox counts as enabled only when
    both sources say so.
    """
    bound = _BOUND
    if explicit is None or not explicit.reference:
        return bound
    if not bound.reference:
        return explicit
    return CodePolicy(
        reference=True, sandbox_enabled=explicit.sandbox_enabled and bound.sandbox_enabled
    )


@dataclass(frozen=True)
class CodeRefusal:
    """One thing the reference profile refuses, with where it is and what to do about it."""

    path: str
    kind: str
    message: str
    node_id: str
    code: str = REFUSAL_CODE

    def to_dict(self) -> dict[str, str]:
        """The validation-report issue shape (``code``, ``path``, ``message``, ``severity``)."""
        return {"code": self.code, "path": self.path, "message": self.message, "severity": "error"}


class CodeRefusalError(Exception):
    """Raised where a refusal stops the caller. ``code`` is always :data:`REFUSAL_CODE`."""

    code: str = REFUSAL_CODE

    def __init__(self, refusals: list[CodeRefusal]) -> None:
        self.refusals = tuple(refusals)
        super().__init__(summarize(refusals))


def summarize(refusals: list[CodeRefusal]) -> str:
    """One line for a log, an error field or an HTTP detail: the code first, then each refusal."""
    return f"{REFUSAL_CODE}: " + "; ".join(r.message for r in refusals)


def report_has_refusal(report: object) -> bool:
    """Whether a validation/compile report (``{"errors": [{"code": ...}]}``) holds a refusal, tolerating any other shape."""
    errors = report.get("errors") if isinstance(report, dict) else None
    return isinstance(errors, list) and any(
        isinstance(e, dict) and e.get("code") == REFUSAL_CODE for e in errors
    )


def _node_refusal(node_id: str, kind: str, why: str) -> CodeRefusal:
    return CodeRefusal(
        path=f"nodes.{node_id}",
        kind=kind,
        node_id=node_id,
        message=(
            f"{kind} node {node_id!r} {why}; the reference profile does not support it, so remove the node "
            f"or run this workflow outside the reference profile"
        ),
    )


def _unknown_kind_refusal(node_id: str, kind: str) -> CodeRefusal:
    return CodeRefusal(
        path=f"nodes.{node_id}",
        kind=kind,
        node_id=node_id,
        message=(
            f"node {node_id!r} has kind {kind!r}, which the reference profile has not classified as free of code "
            f"(it is not in the compiler's kind registry, or not yet classified), so it refuses it"
        ),
    )


def node_kind_refusal(node_id: str, kind: str) -> CodeRefusal | None:
    """The refusal for a node of registry kind ``kind`` (a :class:`NodeType` value), or ``None``.

    The decision fails closed: a kind is admitted only when it is classified as inert or as a tool carrier. A kind the
    registry does not list, or lists but nobody classified, is refused: the profile cannot establish that it executes no code.
    """
    try:
        node_type = NodeType(kind)
    except ValueError:
        return _unknown_kind_refusal(node_id, kind)
    why = CODE_NODE_KINDS.get(node_type)
    if why is not None:
        return _node_refusal(node_id, node_type.value, why)
    if node_type in INERT_NODE_KINDS or node_type in TOOL_NODE_KINDS:
        return None
    # A registered kind nobody classified: fail closed until it is (the ratchet test is the early warning).
    return _unknown_kind_refusal(node_id, node_type.value)


def tool_form_refusal(
    tool: str,
    *,
    node_id: str,
    path: str,
    execution_backend: str,
    binding_type: str,
    policy: CodePolicy,
) -> CodeRefusal | None:
    """The refusal for a tool binding of this execution form under ``policy``, or ``None`` when it is admitted.

    * An MCP binding and an OpenAPI operation run no registered Python and are admitted here.
    * A ``python_callable`` tool is admitted when the sandbox is enabled, which is how the platform runs administrator-registered
      code out of process; with it disabled the tool would run unsandboxed in the control plane and is refused.
    * Any other backend is refused: the profile cannot establish what it runs.

    Nothing the tool carries (a ``sandbox`` entry in its ``backend_config``, a manifest field) is read: the decision is the
    execution form and the platform's own configuration.
    """
    if binding_type == MCP_BINDING_TYPE or execution_backend in NON_CODE_TOOL_BACKENDS:
        return None
    if execution_backend != CODE_TOOL_BACKEND:
        return CodeRefusal(
            path=path,
            kind="tool",
            node_id=node_id,
            message=(
                f"tool {tool!r} has execution backend {execution_backend!r}, which the reference profile "
                f"cannot establish does not run code, so it refuses it; use an openapi_http tool"
            ),
        )
    if policy.sandbox_enabled:
        return None
    return _unsandboxed_tool_refusal(tool, node_id=node_id, path=path)


def _unsandboxed_tool_refusal(tool: str, *, node_id: str, path: str) -> CodeRefusal:
    return CodeRefusal(
        path=path,
        kind="tool",
        node_id=node_id,
        message=(
            f"tool {tool!r} runs registered Python (execution backend python_callable) and "
            f"CALIBER_REGISTERED_TOOL_SANDBOX_ENABLED is false, so it would run unsandboxed inside the control plane, which "
            f"the reference profile does not support; set CALIBER_REGISTERED_TOOL_SANDBOX_ENABLED=true. A sandbox label on "
            f"the tool does not change this: isolation is decided by the platform's binding path, not by a flag the tool carries"
        ),
    )


def require_sandboxed_tool_binding(tool: str, policy: CodePolicy | None = None) -> None:
    """Raise :class:`CodeRefusalError` when the reference profile is active: the caller is about to import a tool in process.

    The in-process binders call this, so a tool that reaches them (a hand-built plan, a generated export) is refused whatever
    the boundaries above saw. Outside the reference profile it does nothing.
    """
    if resolve_code_policy(policy).reference:
        raise CodeRefusalError([_unsandboxed_tool_refusal(tool, node_id="", path=f"tools.{tool}")])


def _binding_refusal(
    node_id: str, binding: IRToolBinding, policy: CodePolicy
) -> CodeRefusal | None:
    return tool_form_refusal(
        binding.local_name,
        node_id=node_id,
        path=f"nodes.{node_id}.tools.{binding.local_name}",
        execution_backend=binding.execution_backend,
        binding_type=binding.binding_type,
        policy=policy,
    )


def ir_refusals(ir: IRWorkflow, policy: CodePolicy | None = None) -> list[CodeRefusal]:
    """Every refusal for a compiled workflow, in node-id order; empty outside the reference profile.

    Walks the whole IR, not the nodes reachable from the entry: a graph edit must not make a dormant code node live.
    """
    effective = resolve_code_policy(policy)
    if not effective.reference:
        return []
    found: list[CodeRefusal] = []
    for node_id in sorted(ir.nodes):
        node = ir.nodes[node_id]
        if (refusal := node_kind_refusal(node_id, node.node_type.value)) is not None:
            found.append(refusal)
        bindings: list[IRToolBinding] = []
        if isinstance(node, IRAgent):
            bindings.extend(node.tools)
        elif isinstance(node, IRTool) and node.binding is not None:
            bindings.append(node.binding)
        for binding in bindings:
            if (refusal := _binding_refusal(node_id, binding, effective)) is not None:
                found.append(refusal)
    return found


def require_no_code(ir: IRWorkflow, policy: CodePolicy | None = None) -> None:
    """Raise :class:`CodeRefusalError` when :func:`ir_refusals` finds anything."""
    refusals = ir_refusals(ir, policy)
    if refusals:
        raise CodeRefusalError(refusals)


def manifest_refusals(
    manifest: WorkflowManifest, resolver: ToolResolver | None, policy: CodePolicy | None = None
) -> list[CodeRefusal]:
    """Every refusal for a parsed manifest, in node-id order; empty outside the reference profile.

    Node kinds are read through the same registry as :func:`ir_refusals`. A tool binding's execution form lives in the
    tool registry, not the manifest, so it is read from ``resolver``; a binding the resolver cannot resolve (reported
    as ``missing_tool`` by validation, and refused by the compile boundary) is skipped here, and so is every registered
    binding when there is no resolver.
    """
    effective = resolve_code_policy(policy)
    if not effective.reference:
        return []
    found: list[CodeRefusal] = []
    for node_id in sorted(manifest.nodes):
        node = manifest.nodes[node_id]
        if (refusal := node_kind_refusal(node_id, str(node.type))) is not None:
            found.append(refusal)
        if isinstance(node, AgentNode):
            referenced = list(node.tools)
        elif isinstance(node, ToolNode):
            referenced = [node.tool_name]
        else:
            continue
        for local_name in referenced:
            binding = manifest.tools.get(local_name)
            if resolver is None or not isinstance(binding, RegisteredFunctionToolBinding):
                continue
            try:
                entry = resolver.resolve(binding.registry_ref, binding.version_constraint).entry
            except ToolResolutionError:
                continue
            refusal = tool_form_refusal(
                local_name,
                node_id=node_id,
                path=f"nodes.{node_id}.tools.{local_name}",
                execution_backend=str(entry.execution_backend or CODE_TOOL_BACKEND),
                binding_type="registered_function",
                policy=effective,
            )
            if refusal is not None:
                found.append(refusal)
    return found


__all__ = [
    "CODE_NODE_KINDS",
    "CODE_TOOL_BACKEND",
    "INERT_NODE_KINDS",
    "MCP_BINDING_TYPE",
    "NON_CODE_TOOL_BACKENDS",
    "REFUSAL_CODE",
    "TOOL_NODE_KINDS",
    "CodePolicy",
    "CodeRefusal",
    "CodeRefusalError",
    "bind_code_policy",
    "bound_code_policy",
    "ir_refusals",
    "manifest_refusals",
    "node_kind_refusal",
    "report_has_refusal",
    "require_no_code",
    "require_sandboxed_tool_binding",
    "resolve_code_policy",
    "summarize",
    "tool_form_refusal",
]
