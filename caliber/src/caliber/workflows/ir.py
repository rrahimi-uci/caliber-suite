"""Typed intermediate representation for the workflow compiler (plan §10.6).

The IR sits between the parsed manifest and the generated SDK objects. It is a
plain dataclass tree so that:

* code generation and runtime construction read one stable shape, not the
  manifest's nested-union shape;
* semantic validation (types, reachability) happens against resolved
  references (tools bound, prompts located) rather than raw strings;
* a future second codegen backend (export package, different SDK) can target
  the same IR.

The IR is produced by :func:`caliber.workflows.compiler.build_ir`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal


class NodeType(str, Enum):
    START = "start"
    FILE_INPUT = "file_input"
    FOLDER_INPUT = "folder_input"
    INPUT_BUCKET = "input_bucket"
    OUTPUT_BUCKET = "output_bucket"
    OUTPUT_FOLDER = "output_folder"
    WAIT_UNTIL = "wait_until"
    WAIT_FOR_EVENT = "wait_for_event"
    PARALLEL = "parallel"
    JOIN = "join"
    FOR_EACH = "for_each"
    LOOP = "loop"
    ERROR_BOUNDARY = "error_boundary"
    SUBWORKFLOW = "subworkflow"
    TOOL = "tool"
    MCP_RESOURCE = "mcp_resource"
    KNOWLEDGE_QUERY = "knowledge_query"
    KNOWLEDGE_BUILD = "knowledge_build"
    TEMPLATE = "template"
    DATA_TRANSFORM = "data_transform"
    REVIEW_QUEUE_ENQUEUE = "review_queue_enqueue"
    PYTHON_CODE = "python_code"
    AGENT = "agent"
    GUARDRAIL = "guardrail"
    ROUTER = "router"
    HUMAN_APPROVAL = "human_approval"
    OUTPUT = "output"
    NOTE = "note"
    EXTERNAL_APP = "external_app"
    WEBHOOK = "webhook"
    API_REQUEST = "api_request"


@dataclass(frozen=True)
class IRType:
    """A resolved port type."""

    name: str  # one of the DataType literals
    schema: dict[str, Any] | None = None

    def assignable_from(self, other: IRType) -> bool:
        """Whether a value of ``other`` can flow into a port of ``self``.

        Rules (plan §9.3): equal types are assignable; ``string`` is assignable
        to ``messages`` (a single string can seed a message list); ``void``
        only accepts ``void``.
        """
        if self.name == other.name:
            return True
        # A single string can seed a message list.
        return self.name == "messages" and other.name == "string"


@dataclass(frozen=True)
class PromptRef:
    """A resolved prompt source for an agent."""

    kind: str  # "inline" | "mlflow_prompt"
    inline_text: str | None = None
    registry_name: str | None = None
    alias: str | None = None

    @property
    def mlflow_uri(self) -> str | None:
        if self.kind == "mlflow_prompt" and self.registry_name:
            return f"prompts:/{self.registry_name}@{self.alias or 'prod'}"
        return None


@dataclass(frozen=True)
class IRToolBinding:
    """A tool reference resolved against the registry."""

    local_name: str  # the name the agent uses (manifest tools key)
    registry_ref: str
    version_constraint: str
    requires_approval: bool
    side_effect_level: str  # read | write | external_action
    allow_in_preview: bool
    module_path: str
    callable_name: str
    binding_type: str = "registered_function"  # registered_function | mcp_tool
    mcp_server_id: str | None = None
    mcp_tool_name: str | None = None
    mcp_tool_schema_version: str = ""
    secret_refs: tuple[str, ...] = ()
    output_schema: dict[str, Any] | None = None
    #: JSON schema for the tool's arguments, threaded from the registry so the
    #: real agentic tool-calling loop can expose proper parameters to the model
    #: (golden-path roadmap, Wave 4). ``None`` → a generic single-input schema.
    input_schema: dict[str, Any] | None = None
    timeout_seconds: float | None = None
    max_retries: int = 0
    resolved_name: str = ""
    resolved_version: str = ""


@dataclass(frozen=True)
class IRExecutionPolicy:
    timeout_seconds: float | None = None
    max_retries: int = 0
    idempotent: bool = False


@dataclass(frozen=True)
class IRGuardrailCheck:
    kind: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class IRHandoff:
    target_node_id: str
    description: str = ""
    input_filter: str | None = None
    condition: str | None = None


@dataclass
class IRNode:
    node_id: str
    node_type: NodeType
    inputs: dict[str, IRType] = field(default_factory=dict)
    outputs: dict[str, IRType] = field(default_factory=dict)
    execution_policy: IRExecutionPolicy = field(default_factory=IRExecutionPolicy)


@dataclass
class IRAgent(IRNode):
    name: str = ""
    model: str = "inherit"
    instructions: PromptRef | None = None
    tools: list[IRToolBinding] = field(default_factory=list)
    handoffs: list[IRHandoff] = field(default_factory=list)
    output_type: dict[str, Any] | None = None
    eval_dataset: str | None = None
    #: Resolved skill content blocks composed into the system prompt at runtime
    #: (appended after the base instructions). Populated by the compiler from
    #: ``AgentNode.skills`` + the session-supplied skill source.
    skill_instructions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class IRManagedFileReference:
    file_id: str
    file_ref: str
    sha256: str
    name: str
    size_bytes: int
    media_type: str | None = None
    object_version_id: str | None = None


@dataclass
class IRFileInput(IRNode):
    file_ref: IRManagedFileReference | None = None
    path: str = ""
    encoding: str = "utf-8"
    max_bytes: int = 200_000


@dataclass
class IRFolderInput(IRNode):
    path: str = ""
    pattern: str = "**/*"
    recursive: bool = True
    max_files: int = 50
    max_bytes_per_file: int = 100_000
    encoding: str = "utf-8"


@dataclass
class IRInputBucket(IRNode):
    bucket: str = ""
    prefix: str = ""
    recursive: bool = True
    max_files: int = 50
    max_bytes_per_file: int = 100_000
    encoding: str = "utf-8"


@dataclass
class IROutputBucket(IRNode):
    bucket: str = ""
    prefix: str = ""
    overwrite: bool = True


@dataclass
class IROutputFolder(IRNode):
    path: str = ""
    overwrite: bool = True


@dataclass
class IRMcpResource(IRNode):
    server_id: str = ""
    tool_name: str = ""
    timeout_seconds: float = 45.0


@dataclass
class IRTool(IRNode):
    binding: IRToolBinding | None = None


@dataclass
class IRKnowledgeQuery(IRNode):
    knowledge_base_id: str = ""
    version_ids: list[str] = field(default_factory=list)
    retrieval_modes: list[str] = field(default_factory=list)
    top_k: int = 6
    chat_model: str | None = None
    graph_overrides: dict[str, Any] | None = None


@dataclass
class IRKnowledgeBuild(IRNode):
    knowledge_base_id: str = ""
    chunking_strategy: str = ""
    embedding_model: str = ""
    chunking_config: dict[str, Any] = field(default_factory=dict)
    graph_config: dict[str, Any] | None = None
    activate_when_complete: bool = False
    wait_for_completion: bool = False
    wait_timeout_seconds: float = 300.0


@dataclass
class IRTemplate(IRNode):
    template: str = ""
    output_format: str = "text"
    missing_variable_mode: str = "preserve"


@dataclass
class IRDataTransform(IRNode):
    operation: str = "mapping"
    config: dict[str, Any] = field(default_factory=dict)
    fail_on_invalid: bool = True


@dataclass
class IRReviewQueueEnqueue(IRNode):
    queue_id: str = ""
    experiment_id: str | None = None
    assigned_to: str | None = None


@dataclass
class IRPythonCode(IRNode):
    code: str = ""
    timeout_seconds: float = 5.0


@dataclass
class IRWaitUntil(IRNode):
    wait_until: str = ""
    timezone: str = "UTC"


@dataclass
class IRWaitForEvent(IRNode):
    event_name: str = ""
    correlation_key: str = ""
    timeout_seconds: float | None = None


@dataclass
class IRParallel(IRNode):
    pass


@dataclass
class IRJoin(IRNode):
    mode: str = "all"


@dataclass
class IRForEach(IRNode):
    target_node_id: str | None = None
    item_input_port: str = "items"
    max_items: int = 100


@dataclass
class IRLoop(IRNode):
    target_node_id: str | None = None
    max_iterations: int = 10
    stop_condition: str = ""


@dataclass
class IRErrorBoundary(IRNode):
    target_node_id: str | None = None
    fallback_text: str = ""
    compensate_with: str | None = None


@dataclass
class IRSubworkflow(IRNode):
    workflow_id: str = ""
    alias: str = "prod"
    timeout_seconds: float = 120.0


@dataclass
class IRGuardrail(IRNode):
    mode: str = "post_agent"
    checks: list[IRGuardrailCheck] = field(default_factory=list)
    on_failure: Literal["block", "block_retry", "warn", "redact", "escalate"] = "block"
    max_retries: int = 0


@dataclass
class IRRouterBranch:
    condition: dict[str, Any] | None
    to: str


@dataclass
class IRRouter(IRNode):
    branches: list[IRRouterBranch] = field(default_factory=list)


@dataclass
class IRHumanApproval(IRNode):
    required_role: str = "caliber.approver"
    approval_count: int = 1
    timeout_behavior: str = "block"


@dataclass
class IRExternalApp(IRNode):
    entrypoint: str = ""


@dataclass
class IRWebhook(IRNode):
    url: str = ""
    method: str = "POST"
    headers: dict[str, str] = field(default_factory=dict)
    timeout_seconds: float = 30.0


@dataclass
class IRApiRequest(IRNode):
    mode: str = "url"
    url: str = ""
    method: str = "GET"
    curl: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    body: str = ""
    timeout_seconds: float = 30.0


@dataclass
class IREdge:
    edge_id: str
    from_node: str
    from_output: str
    to_node: str
    to_input: str
    type_check: IRType


@dataclass
class IRDeployGate:
    name: str
    dataset_ref: str
    required_for_aliases: list[str]
    thresholds: dict[str, float]


@dataclass
class IRWorkflow:
    workflow_id: str
    version: str
    nodes: dict[str, IRNode]
    edges: list[IREdge]
    entry_node_id: str
    output_node_id: str
    deploy_gates: list[IRDeployGate] = field(default_factory=list)
    default_model_ref: str = "CALIBER_WORKFLOW_DEFAULT_MODEL"
    session_mode: str = "none"
    openai_workflow_api: str | None = None
    openai_parallel_tool_calls: str | None = None
    openai_prompt_cache_mode: str | None = None
    openai_prompt_cache_retention: str | None = None
    mlflow_experiment_name: str | None = None
    mlflow_trace_group_tags: dict[str, str] = field(default_factory=dict)
    manifest_hash: str = ""

    def agents(self) -> list[IRAgent]:
        return [n for n in self.nodes.values() if isinstance(n, IRAgent)]


__all__ = [
    "IRAgent",
    "IRApiRequest",
    "IRDataTransform",
    "IRDeployGate",
    "IREdge",
    "IRErrorBoundary",
    "IRExecutionPolicy",
    "IRExternalApp",
    "IRFileInput",
    "IRFolderInput",
    "IRForEach",
    "IRGuardrail",
    "IRGuardrailCheck",
    "IRHandoff",
    "IRHumanApproval",
    "IRJoin",
    "IRKnowledgeBuild",
    "IRKnowledgeQuery",
    "IRManagedFileReference",
    "IRMcpResource",
    "IRNode",
    "IRParallel",
    "IRPythonCode",
    "IRReviewQueueEnqueue",
    "IRRouter",
    "IRRouterBranch",
    "IRSubworkflow",
    "IRTemplate",
    "IRTool",
    "IRToolBinding",
    "IRType",
    "IRWaitForEvent",
    "IRWaitUntil",
    "IRWebhook",
    "IRWorkflow",
    "NodeType",
    "PromptRef",
]
