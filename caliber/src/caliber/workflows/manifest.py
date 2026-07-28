"""Pydantic models for the CALIBER workflow manifest.

The manifest is the *source of truth* for a workflow (plan §7.1). The visual
editor edits it, the compiler reads it, and every published version stores a
frozen copy. These models give us:

* strict schema validation (unknown node types, duplicate ids, bad version
  constraints all fail at parse time);
* a deterministic canonical hash used for optimistic-locking (``If-Match``) and
  for the ``caliber.manifest_hash`` run tag;
* round-trip safety between YAML/JSON and the typed object.

Design notes
------------
* ``nodes`` is a mapping keyed by node id; each node also carries its own ``id``
  field, which must equal the key (caught by a model validator). This keeps the
  on-disk YAML readable while preserving stable ids for trace localization.
* Node variants are a discriminated union on ``type`` so a typo like
  ``type: agnet`` produces a clean validation error rather than silently
  dropping into a permissive base model.
* The model only accepts the *current* schema version. Older manifests are
  upgraded by :mod:`caliber.workflows.manifest_migrate` before validation, so
  the model never has to reason about historical shapes.

Plan references: §8 (manifest shape), §9 (node types), §9.3 (type system),
§19.2 (manifest tests).
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Any, Literal

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from caliber.knowledge.schemas import KnowledgeGraphConfigSchema

# The schema version this module understands. Bump alongside a new migration
# in :mod:`caliber.workflows.manifest_migrate` and a golden migration test.
CURRENT_SCHEMA_VERSION = 1

# Node/edge/tool ids must be valid, collision-free Python identifiers so the
# compiler can map them to variable names without escaping or aliasing (ext A1/A2).
ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_]*$"
_ID_RE = re.compile(ID_PATTERN)

# Resource caps (ext B1) — generous defaults that still bound a pathological or
# malicious manifest from exhausting memory / the cycle-DFS recursion limit.
MAX_NODES = 500
MAX_EDGES = 2000
MAX_TOOLS = 200
# Display strings that flow into generated code / UIs: reject control chars so a
# name can't smuggle newlines into the generated module (ext A1, defense-in-depth
# on top of codegen escaping).
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class WorkflowManifestError(ValueError):
    """Raised for manifest problems that aren't plain Pydantic validation.

    Subclasses :class:`ValueError` so callers that already catch validation
    errors as ``ValueError`` keep working.
    """


class UnsupportedSchemaVersionError(WorkflowManifestError):
    """Raised when a manifest declares a schema version we can't load/migrate."""


# ---------------------------------------------------------------------------
# Shared value objects
# ---------------------------------------------------------------------------


class ManagedFileReference(BaseModel):
    """Content-pinned reference to a CALIBER-managed file record.

    ``file_ref`` is the scoped logical address; ``file_id`` and ``sha256`` pin
    the exact DB row and bytes selected by the author.  Runtime resolution must
    verify all three before any content is exposed to a node or tool.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    file_id: str = Field(min_length=1, max_length=64)
    file_ref: str = Field(min_length=1, max_length=1024)
    sha256: str = Field(min_length=64, max_length=64)
    name: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(ge=0)
    media_type: str | None = Field(default=None, max_length=255)
    object_version_id: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def _valid_managed_ref(self) -> ManagedFileReference:
        if not self.file_ref.startswith("caliber://"):
            raise ValueError("managed file_ref must use the caliber:// scheme")
        normalized = self.sha256.lower()
        if not _SHA256_RE.fullmatch(normalized):
            raise ValueError("managed file sha256 must be 64 lowercase hex characters")
        if normalized != self.sha256:
            raise ValueError("managed file sha256 must be lowercase")
        return self


# The node-port type vocabulary (plan §9.3).
DataType = Literal["string", "structured", "messages", "boolean", "void"]
KnowledgeRetrievalMode = Literal["dense", "hybrid", "graph_hybrid", "age_graph"]
KnowledgeGraphRetrievalStrength = Literal["conservative", "balanced", "aggressive"]
KnowledgeAgeSeedMode = Literal[
    "entity_then_text",
    "query_entities_only",
    "query_text_only",
    "query_entities_and_text",
]
TemplateOutputFormat = Literal["text", "json"]
TemplateMissingVariableMode = Literal["preserve", "empty", "error"]


class PortSpec(BaseModel):
    """A single named input or output port on a node.

    ``schema_`` carries an optional JSON Schema for ``structured`` ports. It is
    aliased to ``schema`` on the wire to avoid shadowing pydantic's reserved
    ``model_json_schema`` machinery while keeping the manifest field name
    intuitive.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: DataType
    description: str = ""
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")


PortMap = dict[str, PortSpec]


class KnowledgeQueryGraphOverrides(BaseModel):
    """Optional query-time graph retrieval overrides for knowledge nodes."""

    model_config = ConfigDict(extra="forbid")

    retrieval_strength: KnowledgeGraphRetrievalStrength | None = None
    minimum_relationship_weight: float | None = Field(default=None, ge=0.0, le=10_000.0)
    age_seed_mode: KnowledgeAgeSeedMode | None = None
    age_traversal_hops: int | None = Field(default=None, ge=0, le=2)
    age_candidate_pool_size: int | None = Field(default=None, ge=4, le=200)
    age_dense_rerank_weight: float | None = Field(default=None, ge=0.0, le=3.0)
    strict_age_retrieval: bool = False


class InlineInstructions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["inline"]
    text: str = Field(min_length=1)


class PromptRefInstructions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["mlflow_prompt"]
    ref: str = Field(min_length=1)


InstructionSpec = Annotated[
    InlineInstructions | PromptRefInstructions,
    Field(discriminator="type"),
]


class HandoffSpec(BaseModel):
    """A delegation option exposed to an agent (plan §9.1.1, §10.6 IRHandoff).

    ``target`` is the node id of the specialist agent. ``condition`` is an
    optional CALIBER-side gate evaluated before the handoff is offered to the
    model; ``input_filter`` maps to the SDK handoff input filter.
    """

    model_config = ConfigDict(extra="forbid")

    target: str = Field(min_length=1)
    description: str = ""
    input_filter: str | None = None
    condition: str | None = None


class ExecutionPolicy(BaseModel):
    """Optional execution controls for a node run."""

    model_config = ConfigDict(extra="forbid")

    timeout_seconds: float | None = Field(default=None, gt=0, le=3600)
    max_retries: int = Field(default=0, ge=0, le=10)
    idempotent: bool = False


class GuardrailCheck(BaseModel):
    """A single guardrail check.

    Open-shaped: ``kind`` selects the adapter (see
    :mod:`caliber.workflows.guardrails`) and ``params`` carries its config. The
    YAML in plan §8 uses a nested-key form (``tool_required_before_claim: {...}``)
    which :meth:`from_loose` normalizes into this flat shape.
    """

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _accept_loose(cls, value: Any) -> Any:
        # Accept the {<kind>: {<params>}} sugar from the plan's YAML examples.
        if isinstance(value, dict) and "kind" not in value and len(value) == 1:
            ((kind, params),) = value.items()
            return {"kind": kind, "params": params or {}}
        return value


# ---------------------------------------------------------------------------
# Node variants
# ---------------------------------------------------------------------------


class _NodeBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=ID_PATTERN)
    execution_policy: ExecutionPolicy | None = None
    # Optional, presentation-only node metadata. ``label`` is the human-friendly
    # name shown on the canvas/inspector (the node ``id`` stays the stable
    # identifier edges reference); ``description`` is an author note that may
    # contain Markdown. Both default to ``None`` so ``to_dict`` (exclude_none)
    # omits them — manifests written before these existed keep their hash, and
    # they never reach the IR/compiler/runtime.
    label: str | None = Field(default=None, max_length=128)
    description: str | None = Field(default=None, max_length=4000)


class StartTrigger(BaseModel):
    """How a workflow run is initiated (plan: Start triggers).

    ``manual`` (the default) means runs are started on demand. ``event`` means an
    external caller starts a run by POSTing to the workflow's trigger endpoint
    (optionally matching ``event_name``). ``cron`` means the scheduler starts a
    run whenever ``cron`` fires in ``timezone``. ``alias`` is the deployment the
    event/schedule targets (which deployed version actually runs).
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["manual", "event", "cron"] = "manual"
    event_name: str = ""
    cron: str = ""
    timezone: str = "UTC"
    alias: str = Field(default="prod", min_length=1, max_length=64)
    enabled: bool = True

    @model_validator(mode="after")
    def _check_mode(self) -> StartTrigger:
        if self.mode == "event" and not self.event_name.strip():
            raise ValueError("event trigger requires a non-empty event_name")
        if self.mode == "cron":
            if not self.cron.strip():
                raise ValueError("cron trigger requires a cron expression")
            from caliber.workflows.cron import validate_cron  # noqa: PLC0415

            validate_cron(self.cron)
        return self


class StartNode(_NodeBase):
    type: Literal["start"]
    outputs: PortMap = Field(default_factory=dict)
    # Optional so manifests written before triggers existed keep their canonical
    # hash (``to_dict`` drops ``None`` via ``exclude_none``). Absent == manual.
    trigger: StartTrigger | None = None


class OutputNode(_NodeBase):
    type: Literal["output"]
    inputs: PortMap = Field(default_factory=dict)


class FileInputNode(_NodeBase):
    """Read one content-pinned managed file or legacy local path."""

    type: Literal["file_input"]
    file_ref: ManagedFileReference | None = None
    path: str = ""
    encoding: str = "utf-8"
    max_bytes: int = Field(default=200_000, ge=1, le=5_000_000)
    inputs: PortMap = Field(default_factory=lambda: {"path": PortSpec(type="string")})
    outputs: PortMap = Field(
        default_factory=lambda: {
            "text": PortSpec(type="string"),
            "path": PortSpec(type="string"),
            "file_ref": PortSpec(type="structured"),
            "metadata": PortSpec(type="structured"),
        }
    )

    @model_validator(mode="after")
    def _one_file_source(self) -> FileInputNode:
        if self.file_ref is not None and self.path.strip():
            raise ValueError("file_input must use either file_ref or path, not both")
        return self


class FolderInputNode(_NodeBase):
    """Read a bounded set of local text files from a folder."""

    type: Literal["folder_input"]
    path: str = ""
    pattern: str = "**/*"
    recursive: bool = True
    max_files: int = Field(default=50, ge=1, le=500)
    max_bytes_per_file: int = Field(default=100_000, ge=1, le=1_000_000)
    encoding: str = "utf-8"
    inputs: PortMap = Field(default_factory=lambda: {"path": PortSpec(type="string")})
    outputs: PortMap = Field(
        default_factory=lambda: {
            "text": PortSpec(type="string"),
            "files": PortSpec(type="structured"),
            "metadata": PortSpec(type="structured"),
        }
    )


class InputBucketNode(_NodeBase):
    """Read a bounded set of text objects from an object-storage bucket.

    The object-store counterpart of :class:`FolderInputNode`: instead of local
    disk it lists objects under ``prefix`` in ``bucket`` (S3/MinIO) and publishes
    their concatenated text plus per-object metadata into the graph. Unreadable
    objects are skipped while readable siblings still flow through, but true
    bucket-list failures fail closed instead of masquerading as an empty input.
    """

    type: Literal["input_bucket"]
    bucket: str = ""
    prefix: str = ""
    recursive: bool = True
    max_files: int = Field(default=50, ge=1, le=500)
    max_bytes_per_file: int = Field(default=100_000, ge=1, le=5_000_000)
    encoding: str = "utf-8"
    inputs: PortMap = Field(default_factory=lambda: {"prefix": PortSpec(type="string")})
    outputs: PortMap = Field(
        default_factory=lambda: {
            "text": PortSpec(type="string"),
            "files": PortSpec(type="structured"),
            "metadata": PortSpec(type="structured"),
        }
    )


class OutputBucketNode(_NodeBase):
    """Write the workflow's artifacts/outputs to an object-storage bucket.

    Collects every artifact produced upstream (the same ``{"artifacts": {...}}``
    map other nodes emit) plus any text on its input port, and writes each as an
    object under ``prefix`` in ``bucket`` (S3/MinIO).
    """

    type: Literal["output_bucket"]
    bucket: str = ""
    prefix: str = ""
    overwrite: bool = True
    inputs: PortMap = Field(default_factory=lambda: {"input": PortSpec(type="string")})
    outputs: PortMap = Field(
        default_factory=lambda: {
            "keys": PortSpec(type="structured"),
            "metadata": PortSpec(type="structured"),
        }
    )


class OutputFolderNode(_NodeBase):
    """Write the workflow's artifacts/outputs to a local folder.

    The local-disk counterpart of :class:`OutputBucketNode`: collects every
    artifact produced upstream plus any text on its input port and writes each
    as a file under ``path``.
    """

    type: Literal["output_folder"]
    path: str = ""
    overwrite: bool = True
    inputs: PortMap = Field(default_factory=lambda: {"input": PortSpec(type="string")})
    outputs: PortMap = Field(
        default_factory=lambda: {
            "files": PortSpec(type="structured"),
            "metadata": PortSpec(type="structured"),
        }
    )


class WaitUntilNode(_NodeBase):
    """Pause execution until resumed after a time/event boundary."""

    type: Literal["wait_until"]
    wait_until: str = Field(min_length=1)
    timezone: str = "UTC"
    inputs: PortMap = Field(default_factory=lambda: {"input": PortSpec(type="string")})
    outputs: PortMap = Field(default_factory=lambda: {"output": PortSpec(type="string")})


class WaitForEventNode(_NodeBase):
    """Pause execution until an external event resumes the run."""

    type: Literal["wait_for_event"]
    event_name: str = Field(min_length=1)
    correlation_key: str = ""
    timeout_seconds: float | None = Field(default=None, gt=0, le=2_592_000)
    inputs: PortMap = Field(default_factory=lambda: {"input": PortSpec(type="string")})
    outputs: PortMap = Field(
        default_factory=lambda: {
            "output": PortSpec(type="string"),
            "event_payload": PortSpec(type="structured"),
            "event_name": PortSpec(type="string"),
        }
    )


class ParallelNode(_NodeBase):
    """Explicit fan-out marker node."""

    type: Literal["parallel"]
    inputs: PortMap = Field(default_factory=lambda: {"input": PortSpec(type="string")})
    outputs: PortMap = Field(default_factory=lambda: {"output": PortSpec(type="string")})


class JoinNode(_NodeBase):
    """Explicit fan-in barrier node."""

    type: Literal["join"]
    mode: Literal["all", "any"] = "all"
    inputs: PortMap = Field(default_factory=dict)
    outputs: PortMap = Field(
        default_factory=lambda: {
            "output": PortSpec(type="string"),
            "merged": PortSpec(type="structured"),
        }
    )


class ForEachNode(_NodeBase):
    """Iterate over an input list, optionally invoking a target node per item."""

    type: Literal["for_each"]
    target_node_id: str | None = None
    item_input_port: str = "items"
    max_items: int = Field(default=100, ge=1, le=10_000)
    inputs: PortMap = Field(default_factory=lambda: {"items": PortSpec(type="structured")})
    outputs: PortMap = Field(
        default_factory=lambda: {
            "results": PortSpec(type="structured"),
            "text": PortSpec(type="string"),
            "metadata": PortSpec(type="structured"),
        }
    )


class LoopNode(_NodeBase):
    """Repeat a target node until a stop condition matches or the bound is reached."""

    type: Literal["loop"]
    target_node_id: str | None = None
    max_iterations: int = Field(default=10, ge=1, le=10_000)
    stop_condition: str = ""
    inputs: PortMap = Field(
        default_factory=lambda: {
            "input": PortSpec(type="string"),
            "state": PortSpec(type="structured"),
        }
    )
    outputs: PortMap = Field(
        default_factory=lambda: {
            "output": PortSpec(type="string"),
            "result": PortSpec(type="structured"),
            "iterations": PortSpec(type="structured"),
            "metadata": PortSpec(type="structured"),
        }
    )


class ErrorBoundaryNode(_NodeBase):
    """Execute a target node with fallback/compensation behavior on failure."""

    type: Literal["error_boundary"]
    target_node_id: str | None = None
    fallback_text: str = ""
    compensate_with: str | None = None
    inputs: PortMap = Field(default_factory=lambda: {"input": PortSpec(type="string")})
    outputs: PortMap = Field(
        default_factory=lambda: {
            "output": PortSpec(type="string"),
            "error": PortSpec(type="structured"),
        }
    )


class SubworkflowNode(_NodeBase):
    """Invoke another published workflow by id+alias."""

    type: Literal["subworkflow"]
    workflow_id: str = Field(min_length=1)
    alias: str = "prod"
    timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    inputs: PortMap = Field(default_factory=lambda: {"input": PortSpec(type="string")})
    outputs: PortMap = Field(
        default_factory=lambda: {
            "output": PortSpec(type="string"),
            "result": PortSpec(type="structured"),
        }
    )


class McpResourceNode(_NodeBase):
    """Invoke an MCP tool directly as a first-class workflow node."""

    type: Literal["mcp_resource"]
    server_id: str = Field(min_length=1, max_length=64)
    tool_name: str = Field(min_length=1, max_length=256)
    timeout_seconds: float = Field(default=45.0, gt=0, le=600)
    inputs: PortMap = Field(default_factory=lambda: {"input": PortSpec(type="string")})
    outputs: PortMap = Field(
        default_factory=lambda: {
            "text": PortSpec(type="string"),
            "result": PortSpec(type="structured"),
            "metadata": PortSpec(type="structured"),
        }
    )


class ToolNode(_NodeBase):
    """Invoke a registered workflow tool binding directly as a first-class workflow node."""

    type: Literal["tool"]
    tool_name: str = Field(min_length=1, max_length=256)
    inputs: PortMap = Field(
        default_factory=lambda: {
            "input": PortSpec(type="string"),
            "arguments": PortSpec(type="structured"),
        }
    )
    outputs: PortMap = Field(
        default_factory=lambda: {
            "text": PortSpec(type="string"),
            "result": PortSpec(type="structured"),
            "metadata": PortSpec(type="structured"),
        }
    )


class KnowledgeQueryNode(_NodeBase):
    """Query a knowledge base with dense, hybrid, or AGE-backed retrieval."""

    type: Literal["knowledge_query"]
    knowledge_base_id: str = ""
    version_ids: list[str] = Field(default_factory=list, max_length=3)
    retrieval_modes: list[KnowledgeRetrievalMode] = Field(
        default_factory=list,
        max_length=2,
    )
    top_k: int = Field(default=6, ge=1, le=20)
    chat_model: str | None = Field(default=None, max_length=256)
    graph_overrides: KnowledgeQueryGraphOverrides | None = None
    inputs: PortMap = Field(
        default_factory=lambda: {
            "question": PortSpec(type="string"),
            "history": PortSpec(type="structured"),
            "retrieval_modes": PortSpec(type="structured"),
            "version_ids": PortSpec(type="structured"),
            "graph_overrides": PortSpec(type="structured"),
        }
    )
    outputs: PortMap = Field(
        default_factory=lambda: {
            "text": PortSpec(type="string"),
            "answer": PortSpec(type="string"),
            "result": PortSpec(type="structured"),
            "citations": PortSpec(type="structured"),
            "chunks": PortSpec(type="structured"),
            "graph_context": PortSpec(type="structured"),
        }
    )

    @model_validator(mode="after")
    def _normalize(self) -> KnowledgeQueryNode:
        self.knowledge_base_id = self.knowledge_base_id.strip()
        self.version_ids = list(
            dict.fromkeys(item.strip() for item in self.version_ids if item.strip())
        )
        self.retrieval_modes = list(dict.fromkeys(item for item in self.retrieval_modes if item))
        if self.chat_model is not None:
            self.chat_model = self.chat_model.strip() or None
        return self


class KnowledgeBuildNode(_NodeBase):
    """Launch a new knowledge-base version build from inside a workflow."""

    type: Literal["knowledge_build"]
    knowledge_base_id: str = ""
    chunking_strategy: str = ""
    embedding_model: str = ""
    chunking_config: dict[str, Any] = Field(default_factory=dict)
    graph_config: KnowledgeGraphConfigSchema | None = None
    activate_when_complete: bool = False
    wait_for_completion: bool = False
    wait_timeout_seconds: float = Field(default=300.0, gt=0, le=86_400.0)
    inputs: PortMap = Field(
        default_factory=lambda: {
            "input": PortSpec(type="string"),
            "sources": PortSpec(type="structured"),
            "chunking_strategy": PortSpec(type="string"),
            "embedding_model": PortSpec(type="string"),
            "chunking_config": PortSpec(type="structured"),
            "graph_config": PortSpec(type="structured"),
        }
    )
    outputs: PortMap = Field(
        default_factory=lambda: {
            "text": PortSpec(type="string"),
            "result": PortSpec(type="structured"),
            "knowledge_base": PortSpec(type="structured"),
            "version": PortSpec(type="structured"),
            "run": PortSpec(type="structured"),
            "status": PortSpec(type="string"),
            "version_id": PortSpec(type="string"),
            "run_id": PortSpec(type="string"),
        }
    )

    @model_validator(mode="after")
    def _normalize(self) -> KnowledgeBuildNode:
        self.knowledge_base_id = self.knowledge_base_id.strip()
        self.chunking_strategy = self.chunking_strategy.strip()
        self.embedding_model = self.embedding_model.strip()
        return self


class TemplateNode(_NodeBase):
    """Render a no-code text or JSON payload from workflow inputs."""

    type: Literal["template"]
    template: str = Field(
        default="{{input}}",
        min_length=1,
        max_length=100_000,
    )
    output_format: TemplateOutputFormat = "text"
    missing_variable_mode: TemplateMissingVariableMode = "preserve"
    inputs: PortMap = Field(
        default_factory=lambda: {
            "input": PortSpec(type="string"),
            "variables": PortSpec(type="structured"),
        }
    )
    outputs: PortMap = Field(
        default_factory=lambda: {
            "text": PortSpec(type="string"),
            "result": PortSpec(type="structured"),
            "metadata": PortSpec(type="structured"),
        }
    )


class PythonCodeNode(_NodeBase):
    """Run custom Python in the isolated tool sandbox."""

    type: Literal["python_code"]
    code: str = Field(
        default='return {"text": input or run_input, "result": {"ok": True}}',
        min_length=1,
        max_length=200_000,
    )
    timeout_seconds: float = Field(default=5.0, gt=0, le=120)
    inputs: PortMap = Field(
        default_factory=lambda: {
            "input": PortSpec(type="string"),
            "context": PortSpec(type="structured"),
        }
    )
    outputs: PortMap = Field(
        default_factory=lambda: {
            "text": PortSpec(type="string"),
            "result": PortSpec(type="structured"),
            "metadata": PortSpec(type="structured"),
        }
    )


class AgentNode(_NodeBase):
    type: Literal["agent"]
    name: str = Field(min_length=1, max_length=256)

    @field_validator("name")
    @classmethod
    def _no_control_chars(cls, value: str) -> str:
        if _CONTROL_CHARS_RE.search(value):
            raise ValueError("agent name must not contain control characters/newlines")
        return value

    model: str = Field(min_length=1)
    instructions: InstructionSpec
    tools: list[str] = Field(default_factory=list)
    # Registered skill names whose content is composed into the agent's system
    # prompt at compile time (resolved via the session-backed skill source).
    skills: list[str] = Field(default_factory=list)
    # Optional per-tool usage constraints (e.g. {"lookup_policy": "required_before_claim"}).
    # Set by the ``update_tool_constraint`` semantic patch op (plan §17.3); the
    # compiler may surface these as guardrail hints. Kept out of the runtime graph.
    tool_constraints: dict[str, str] = Field(default_factory=dict)
    handoffs: list[HandoffSpec] = Field(default_factory=list)
    inputs: PortMap = Field(default_factory=dict)
    outputs: PortMap = Field(default_factory=dict)
    output_type: dict[str, Any] | None = None
    eval_dataset: str | None = None


class GuardrailNode(_NodeBase):
    type: Literal["guardrail"]
    mode: Literal["pre_agent", "post_agent"] = "post_agent"
    inputs: PortMap = Field(default_factory=dict)
    outputs: PortMap = Field(default_factory=dict)
    checks: list[GuardrailCheck] = Field(default_factory=list)
    on_failure: Literal["block", "block_retry", "warn", "redact", "escalate"] = "block"
    max_retries: int = Field(default=0, ge=0, le=10)


class RouterBranch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Free-shape visual condition (serialized from the UI condition builder).
    # ``None`` marks the fallback/else branch.
    condition: dict[str, Any] | None = None
    to: str = Field(min_length=1)


class RouterNode(_NodeBase):
    type: Literal["router"]
    inputs: PortMap = Field(default_factory=dict)
    outputs: PortMap = Field(default_factory=dict)
    branches: list[RouterBranch] = Field(default_factory=list)


class HumanApprovalNode(_NodeBase):
    type: Literal["human_approval"]
    inputs: PortMap = Field(default_factory=dict)
    outputs: PortMap = Field(default_factory=dict)
    required_role: str = "caliber.approver"
    approval_count: int = Field(default=1, ge=1)
    timeout_behavior: Literal["block", "escalate", "auto_reject"] = "block"


class NoteNode(_NodeBase):
    type: Literal["note"]
    text: str = ""


class ExternalAppNode(_NodeBase):
    """Migration bridge node (plan §26.1) — wraps existing hand-coded SDK apps."""

    type: Literal["external_app"]
    entrypoint: str = Field(min_length=1)
    inputs: PortMap = Field(default_factory=dict)
    outputs: PortMap = Field(default_factory=dict)


class WebhookNode(_NodeBase):
    """Send an outbound HTTP request (webhook) to an external URL.

    A deterministic, side-effecting integration node: the run's upstream
    ``payload`` (or ``input``) becomes the request body (JSON when structured),
    and the node publishes the response text, the parsed response object, and
    request/response metadata. ``url`` is left optional so a freshly-dropped node
    parses cleanly; validation/setup-checks flag an empty URL before compile.
    """

    type: Literal["webhook"]
    url: str = Field(default="", max_length=2048)
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"] = "POST"
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    inputs: PortMap = Field(
        default_factory=lambda: {
            "payload": PortSpec(type="structured"),
            "input": PortSpec(type="string"),
        }
    )
    outputs: PortMap = Field(
        default_factory=lambda: {
            "text": PortSpec(type="string"),
            "response": PortSpec(type="structured"),
            "metadata": PortSpec(type="structured"),
        }
    )


class ApiRequestNode(_NodeBase):
    """Make an HTTP request from a URL + method or a pasted cURL command.

    The user-facing HTTP component (plan: API Request). In ``url`` mode the
    request is built from ``url``/``method``/``headers``/``body``; in ``curl``
    mode the ``curl`` command string is parsed for the method, URL, headers, and
    body (no shell execution). When ``body`` is empty the upstream ``payload``
    (or ``input``) becomes the request body. Publishes the response text, parsed
    response object, and request/response metadata.
    """

    type: Literal["api_request"]
    mode: Literal["url", "curl"] = "url"
    url: str = Field(default="", max_length=2048)
    method: Literal["GET", "POST", "PATCH", "PUT", "DELETE"] = "GET"
    curl: str = Field(default="", max_length=8192)
    headers: dict[str, str] = Field(default_factory=dict)
    body: str = Field(default="", max_length=100_000)
    timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    inputs: PortMap = Field(
        default_factory=lambda: {
            "payload": PortSpec(type="structured"),
            "input": PortSpec(type="string"),
        }
    )
    outputs: PortMap = Field(
        default_factory=lambda: {
            "text": PortSpec(type="string"),
            "response": PortSpec(type="structured"),
            "metadata": PortSpec(type="structured"),
        }
    )


WorkflowNode = Annotated[
    StartNode
    | OutputNode
    | FileInputNode
    | FolderInputNode
    | InputBucketNode
    | OutputBucketNode
    | OutputFolderNode
    | WaitUntilNode
    | WaitForEventNode
    | ParallelNode
    | JoinNode
    | ForEachNode
    | LoopNode
    | ErrorBoundaryNode
    | SubworkflowNode
    | ToolNode
    | McpResourceNode
    | KnowledgeQueryNode
    | KnowledgeBuildNode
    | TemplateNode
    | PythonCodeNode
    | AgentNode
    | GuardrailNode
    | RouterNode
    | HumanApprovalNode
    | NoteNode
    | ExternalAppNode
    | WebhookNode
    | ApiRequestNode,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Edges, tools, deploy gates, top-level config
# ---------------------------------------------------------------------------


class WorkflowEdge(BaseModel):
    """Typed control/data flow between nodes (plan §9.3).

    ``map`` is required (even if trivial) — a missing map is a validation error
    so data flow is never implicit/invisible.
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=128, pattern=ID_PATTERN)
    from_: str = Field(min_length=1, alias="from")
    to: str = Field(min_length=1)
    # source-output-name -> target-input-name
    map: dict[str, str] = Field(min_length=1)


class RegisteredFunctionToolBinding(BaseModel):
    """A tool binding resolved against ``caliber_tool_registry``."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["registered_function"] = "registered_function"
    registry_ref: str = Field(min_length=1)
    version_constraint: str = ""
    requires_approval: bool = False
    secret_refs: list[str] = Field(default_factory=list)
    # Per-tool resilience (plan §16.4, §18.1 "timeouts and retry limits are
    # mandatory"). Enforced by the runtime tool wrapper (ext B2).
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    max_retries: int = Field(default=0, ge=0, le=5)

    @field_validator("version_constraint")
    @classmethod
    def _valid_specifier(cls, value: str) -> str:
        if value == "":
            return value
        try:
            SpecifierSet(value)
        except InvalidSpecifier as exc:
            raise ValueError(f"invalid version_constraint {value!r}: {exc}") from exc
        return value


class McpToolBinding(BaseModel):
    """A tool binding backed by an MCP server/tool pair."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["mcp_tool"] = "mcp_tool"
    server_id: str = Field(min_length=1, max_length=64)
    tool_name: str = Field(min_length=1, max_length=256)
    tool_schema_version: str = ""
    side_effect_level: Literal["read", "write", "external_action"] = "read"
    requires_approval: bool = False
    timeout_seconds: float | None = Field(default=None, gt=0, le=600)
    max_retries: int = Field(default=0, ge=0, le=5)


ToolBinding = Annotated[
    RegisteredFunctionToolBinding | McpToolBinding,
    Field(discriminator="type"),
]


class DeployGate(BaseModel):
    """A deploy-time quality gate (plan §9.4). NOT a runtime node."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["deploy_gate"] = "deploy_gate"
    dataset_ref: str = Field(min_length=1)
    required_for_aliases: list[str] = Field(default_factory=list)
    thresholds: dict[str, float] = Field(default_factory=dict)


class SessionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["none", "in_memory", "persistent"] = "none"


OpenAIWorkflowAPI = Literal["chat_completions", "responses", "agents_sdk"]
OpenAIParallelToolCallsMode = Literal["auto", "enabled", "disabled"]
OpenAIPromptCacheMode = Literal["auto", "enabled", "disabled"]
OpenAIPromptCacheRetention = Literal["default", "in_memory", "24h"]


class RuntimeOpenAIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow_api: OpenAIWorkflowAPI | None = None
    parallel_tool_calls: OpenAIParallelToolCallsMode | None = None
    prompt_cache_mode: OpenAIPromptCacheMode | None = None
    prompt_cache_retention: OpenAIPromptCacheRetention | None = None


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sdk: str = "openai-agents-python"
    sdk_version_policy: Literal["runtime-pinned"] = "runtime-pinned"
    compiler_version: str = "caliber-workflow-compiler-v1"
    default_model_ref: str = "CALIBER_WORKFLOW_DEFAULT_MODEL"
    session: SessionConfig = Field(default_factory=SessionConfig)
    openai: RuntimeOpenAIConfig | None = None

    @field_validator("sdk_version_policy")
    @classmethod
    def _runtime_pinned_only(cls, value: str) -> str:
        # Plan §1.1 + §19.4: manifest-pinned SDK policy is a compile error;
        # reject it at parse time so the message is precise.
        if value != "runtime-pinned":
            raise ValueError(
                "sdk_version_policy must be 'runtime-pinned'; the runtime pins "
                "the Agents SDK, not the manifest"
            )
        return value


class MLflowConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    experiment_name: str | None = None
    trace_group_tags: dict[str, str] = Field(default_factory=dict)


class PromptArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    registry_name: str = Field(min_length=1)
    alias: str = "prod"
    managed_by: str = "mlflow_prompt_registry"


class EvalDatasetArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_name: str = Field(min_length=1)
    min_overall_delta: float | None = None
    max_tone_regression: float | None = None


class ArtifactsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompts: dict[str, PromptArtifact] = Field(default_factory=dict)
    eval_datasets: dict[str, EvalDatasetArtifact] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# The manifest itself
# ---------------------------------------------------------------------------


class WorkflowManifest(BaseModel):
    """The complete, validated workflow manifest.

    Use :func:`parse_manifest` (which migrates first) rather than calling
    ``model_validate`` directly when the input may be an older schema version.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int
    workflow_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    description: str = ""
    owner: str = ""

    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    mlflow: MLflowConfig = Field(default_factory=MLflowConfig)
    artifacts: ArtifactsConfig = Field(default_factory=ArtifactsConfig)

    nodes: dict[str, WorkflowNode]
    edges: list[WorkflowEdge] = Field(default_factory=list)
    tools: dict[str, ToolBinding] = Field(default_factory=dict)
    deploy_gates: dict[str, DeployGate] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def _supported_version(cls, value: int) -> int:
        if value != CURRENT_SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"manifest schema_version {value} is not the current version "
                f"{CURRENT_SCHEMA_VERSION}; migrate it with "
                "caliber.workflows.manifest_migrate.migrate() before validating"
            )
        return value

    @model_validator(mode="before")
    @classmethod
    def _normalize_legacy_tool_bindings(cls, data: Any) -> Any:
        """Backfill ``tools.*.type`` for manifests written before MCP bindings.

        Existing manifests omit the discriminator entirely; treat them as
        ``registered_function`` bindings so legacy drafts and published versions
        remain valid without a migration rewrite.
        """
        if not isinstance(data, dict):
            return data
        raw_tools = data.get("tools")
        if not isinstance(raw_tools, dict):
            return data
        normalized: dict[str, Any] = {}
        changed = False
        for key, value in raw_tools.items():
            if isinstance(value, dict) and "type" not in value:
                changed = True
                normalized[key] = {"type": "registered_function", **value}
            else:
                normalized[key] = value
        if changed:
            data = dict(data)
            data["tools"] = normalized
        return data

    @model_validator(mode="after")
    def _structural_checks(self) -> WorkflowManifest:
        # Node key must equal node.id — keeps trace localization unambiguous.
        for key, node in self.nodes.items():
            if node.id != key:
                raise ValueError(f"node key {key!r} does not match node.id {node.id!r}")

        if not self.nodes:
            raise ValueError("manifest must declare at least one start and one output node")

        # Resource caps (ext B1).
        if len(self.nodes) > MAX_NODES:
            raise ValueError(f"too many nodes: {len(self.nodes)} > {MAX_NODES}")
        if len(self.edges) > MAX_EDGES:
            raise ValueError(f"too many edges: {len(self.edges)} > {MAX_EDGES}")
        if len(self.tools) > MAX_TOOLS:
            raise ValueError(f"too many tools: {len(self.tools)} > {MAX_TOOLS}")

        # Tool keys (local names) must be safe identifiers — they become Python
        # variable names in generated code (ext A1/A2).
        for tool_key in self.tools:
            if not _ID_RE.match(tool_key):
                raise ValueError(
                    f"tool key {tool_key!r} must match {ID_PATTERN} (a safe identifier)"
                )

        # Duplicate edge ids.
        edge_ids = [edge.id for edge in self.edges]
        dupes = {eid for eid in edge_ids if edge_ids.count(eid) > 1}
        if dupes:
            raise ValueError(f"duplicate edge ids: {sorted(dupes)}")

        return self

    # -- serialization helpers ---------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Return the wire-shaped dict (aliases applied, ``None`` dropped)."""
        return self.model_dump(mode="json", by_alias=True, exclude_none=True)

    def canonical_json(self) -> str:
        """Deterministic JSON: sorted keys, no extra whitespace (plan §11.1)."""
        return canonical_json(self.to_dict())

    def manifest_hash(self) -> str:
        """SHA-256 of the canonical JSON — key-order independent (plan §11.1)."""
        return compute_manifest_hash(self.to_dict())


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def canonical_json(data: dict[str, Any]) -> str:
    """Serialize a manifest-shaped dict deterministically."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_manifest_hash(manifest: dict[str, Any] | WorkflowManifest) -> str:
    """SHA-256 hex digest of a manifest's canonical JSON.

    Accepts either a raw dict or a parsed :class:`WorkflowManifest`. To make the
    hash *representation-independent*, a raw dict that parses successfully is
    normalized through the model first — so a manifest written with explicit
    defaults and the same manifest written without them hash identically, and a
    parsed manifest's :meth:`WorkflowManifest.manifest_hash` always matches
    ``compute_manifest_hash`` of the dict it came from. A dict that does not
    parse (an in-progress draft) is hashed by its raw canonical JSON, which is
    still stable for optimistic-locking as long as it isn't edited.
    """
    if isinstance(manifest, WorkflowManifest):
        payload = manifest.to_dict()
    else:
        try:
            payload = parse_manifest(manifest).to_dict()
        except Exception:
            payload = manifest
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def parse_manifest(data: dict[str, Any]) -> WorkflowManifest:
    """Migrate (if needed) then validate a raw manifest dict.

    This is the public entry point. It upgrades older ``schema_version``
    documents via :mod:`caliber.workflows.manifest_migrate`, then validates the
    result against :class:`WorkflowManifest`.
    """
    # Local import to avoid a cycle: manifest_migrate imports nothing from here
    # at module scope, but keeping the import lazy makes the dependency explicit.
    from caliber.workflows.manifest_migrate import migrate  # noqa: PLC0415

    if not isinstance(data, dict):
        raise WorkflowManifestError("manifest must be a JSON object")
    migrated = migrate(data)
    return WorkflowManifest.model_validate(migrated)


__all__ = [
    "CURRENT_SCHEMA_VERSION",
    "AgentNode",
    "ApiRequestNode",
    "ArtifactsConfig",
    "DataType",
    "DeployGate",
    "ErrorBoundaryNode",
    "EvalDatasetArtifact",
    "ExecutionPolicy",
    "ExternalAppNode",
    "FileInputNode",
    "FolderInputNode",
    "ForEachNode",
    "GuardrailCheck",
    "GuardrailNode",
    "HandoffSpec",
    "HumanApprovalNode",
    "InlineInstructions",
    "InputBucketNode",
    "InstructionSpec",
    "JoinNode",
    "KnowledgeAgeSeedMode",
    "KnowledgeGraphRetrievalStrength",
    "KnowledgeQueryGraphOverrides",
    "KnowledgeQueryNode",
    "KnowledgeRetrievalMode",
    "LoopNode",
    "MLflowConfig",
    "McpResourceNode",
    "McpToolBinding",
    "NoteNode",
    "OutputBucketNode",
    "OutputFolderNode",
    "OutputNode",
    "ParallelNode",
    "PortSpec",
    "PromptArtifact",
    "PromptRefInstructions",
    "PythonCodeNode",
    "RegisteredFunctionToolBinding",
    "RouterBranch",
    "RouterNode",
    "RuntimeConfig",
    "RuntimeOpenAIConfig",
    "SessionConfig",
    "StartNode",
    "StartTrigger",
    "SubworkflowNode",
    "ToolBinding",
    "ToolNode",
    "UnsupportedSchemaVersionError",
    "WaitForEventNode",
    "WaitUntilNode",
    "WebhookNode",
    "WorkflowEdge",
    "WorkflowManifest",
    "WorkflowManifestError",
    "WorkflowNode",
    "canonical_json",
    "compute_manifest_hash",
    "parse_manifest",
]
