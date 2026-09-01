"""Server-backed workflow component catalog for the designer UI.

This module exposes the same node shapes the manifest parser/runtime validates
so the frontend can render documentation and schema hints from a shared source
of truth instead of duplicating them ad hoc.
"""

from __future__ import annotations

import inspect
import re
from functools import lru_cache
from typing import Any

from pydantic import BaseModel, ValidationError
from pydantic_core import PydanticUndefined

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
    InputBucketNode,
    JoinNode,
    KnowledgeBuildNode,
    KnowledgeQueryNode,
    LoopNode,
    McpResourceNode,
    NoteNode,
    OutputBucketNode,
    OutputFolderNode,
    OutputNode,
    ParallelNode,
    PortSpec,
    PythonCodeNode,
    ReviewQueueEnqueueNode,
    RouterNode,
    StartNode,
    SubworkflowNode,
    TemplateNode,
    ToolNode,
    WaitForEventNode,
    WaitUntilNode,
    WebhookNode,
)

_COMPONENT_ORDER: tuple[tuple[str, type[BaseModel]], ...] = (
    ("start", StartNode),
    ("file_input", FileInputNode),
    ("folder_input", FolderInputNode),
    ("input_bucket", InputBucketNode),
    ("output_bucket", OutputBucketNode),
    ("output_folder", OutputFolderNode),
    ("wait_until", WaitUntilNode),
    ("wait_for_event", WaitForEventNode),
    ("parallel", ParallelNode),
    ("join", JoinNode),
    ("for_each", ForEachNode),
    ("loop", LoopNode),
    ("error_boundary", ErrorBoundaryNode),
    ("subworkflow", SubworkflowNode),
    ("tool", ToolNode),
    ("mcp_resource", McpResourceNode),
    ("webhook", WebhookNode),
    ("api_request", ApiRequestNode),
    ("knowledge_query", KnowledgeQueryNode),
    ("knowledge_build", KnowledgeBuildNode),
    ("template", TemplateNode),
    ("data_transform", DataTransformNode),
    ("review_queue_enqueue", ReviewQueueEnqueueNode),
    ("external_app", ExternalAppNode),
    ("python_code", PythonCodeNode),
    ("output", OutputNode),
    ("agent", AgentNode),
    ("router", RouterNode),
    ("guardrail", GuardrailNode),
    ("human_approval", HumanApprovalNode),
    ("note", NoteNode),
)

_COMPONENT_LABELS: dict[str, str] = {
    "start": "Start",
    "file_input": "File Input",
    "folder_input": "Input Folder",
    "input_bucket": "Input Bucket",
    "output_bucket": "Output Bucket",
    "output_folder": "Output Folder",
    "wait_until": "Wait Until",
    "wait_for_event": "Wait For Event",
    "parallel": "Parallel",
    "join": "Join",
    "for_each": "For Each",
    "loop": "Loop",
    "error_boundary": "Error Boundary",
    "subworkflow": "Subworkflow",
    "tool": "Tool",
    "mcp_resource": "MCP Resource",
    "webhook": "Webhook",
    "api_request": "API Request",
    "knowledge_query": "Knowledge Query",
    "knowledge_build": "Knowledge Build",
    "template": "Template",
    "data_transform": "Data Transform",
    "review_queue_enqueue": "Review Queue Enqueue",
    "external_app": "External App",
    "python_code": "Python Code",
    "output": "Output",
    "agent": "Agent",
    "router": "Router",
    "guardrail": "Guardrail",
    "human_approval": "Human Approval",
    "note": "Note",
}

_COMPONENT_CATEGORIES: dict[str, str] = {
    "start": "Inputs & Outputs",
    "file_input": "Inputs & Outputs",
    "folder_input": "Inputs & Outputs",
    "input_bucket": "Inputs & Outputs",
    "output_bucket": "Inputs & Outputs",
    "output_folder": "Inputs & Outputs",
    "wait_until": "Orchestration",
    "wait_for_event": "Orchestration",
    "parallel": "Orchestration",
    "join": "Orchestration",
    "for_each": "Orchestration",
    "loop": "Orchestration",
    "error_boundary": "Safety",
    "subworkflow": "Orchestration",
    "tool": "Integrations",
    "mcp_resource": "Integrations",
    "webhook": "Integrations",
    "api_request": "Integrations",
    "knowledge_query": "Integrations",
    "knowledge_build": "Integrations",
    "template": "Utilities",
    "data_transform": "Logic",
    "review_queue_enqueue": "Governance",
    "external_app": "Integrations",
    "python_code": "Utilities",
    "output": "Inputs & Outputs",
    "agent": "Agents",
    "router": "Logic",
    "guardrail": "Safety",
    "human_approval": "Safety",
    "note": "Utilities",
}

_FALLBACK_DESCRIPTIONS: dict[str, str] = {
    "start": "Entry point of the flow.",
    "output": "Final response endpoint.",
    "parallel": "Fan out execution across multiple branches.",
    "join": "Merge incoming branches back into one control path.",
    "agent": "LLM-powered reasoning step with tools, skills, and handoffs.",
    "guardrail": "Apply validation or safety checks before or after an agent step.",
    "router": "Route execution conditionally across explicit branches.",
    "human_approval": "Pause the workflow until a human reviewer approves or rejects it.",
    "tool": "Invoke a registered tool binding directly from the workflow runtime.",
    "webhook": "Send an outbound HTTP request to an external URL.",
    "api_request": "Make HTTP requests using a URL or cURL command.",
    "template": "Render a no-code prompt or JSON payload from workflow variables.",
    "data_transform": "Map, validate, score, or route structured data without custom code.",
    "review_queue_enqueue": "Durably enqueue workflow traces for governed human review.",
    "note": "Canvas-only annotation for workflow authors.",
}

_COMPONENT_DOCS: dict[str, tuple[str, ...]] = {
    "file_input": (
        "Select a content-pinned project file for production runs. The runtime verifies its project ownership, size, object version, and SHA-256 before exposing text and lineage metadata.",
        "Legacy host paths remain available for mounted, operator-controlled development environments.",
    ),
    "folder_input": (
        "Useful for batch ingestion from a local workspace; pair it with For Each when every discovered file should run through the same agent or tool path.",
    ),
    "input_bucket": (
        "Scans object-store keys under a prefix and emits bounded decoded text plus per-object metadata so downstream nodes can trace every source artifact. Unreadable objects are skipped and reported in metadata, while bucket-list failures stop the run instead of looking like an empty input.",
    ),
    "output_bucket": (
        "Writes artifacts back to object storage and preserves lineage through returned keys and metadata that downstream nodes can inspect or persist.",
    ),
    "output_folder": (
        "Use this when run artifacts should land on a local workspace volume instead of object storage, especially during migration or debugging flows.",
    ),
    "start": (
        "Use event or cron triggers when runs should start automatically without an operator opening the designer.",
    ),
    "wait_until": (
        "Creates a resumable checkpoint until a wall-clock timestamp, which is ideal for SLA callbacks, follow-up nudges, and scheduled escalation paths.",
    ),
    "wait_for_event": (
        "Pauses the run until an external system or operator resumes it with a named event payload, making this the bridge for true asynchronous workflows.",
    ),
    "output": ("Map the final downstream field that should be returned as the workflow response.",),
    "parallel": (
        "Pair this with a Join node when you need a clear barrier before downstream execution continues.",
    ),
    "join": (
        "Use 'all' to wait for every branch, or 'any' to release the first successful branch early.",
        "Join inputs are edge-driven rather than fixed starter ports; each upstream branch wires directly into the barrier and the merged output carries branch-keyed payloads for downstream steps.",
    ),
    "for_each": (
        "Iterates a structured list through one executable target and collects each result, which is useful for per-document analysis, enrichment, or fan-out agent work.",
    ),
    "loop": (
        "Repeats one executable target until a stop condition matches or the maximum iteration cap is reached, which is useful for refinement, controlled retries, and bounded agent/tool loops.",
        "Stop conditions can reference iteration, state, output, result, and outputs; leave the condition blank when the loop should always run exactly up to the max iteration cap.",
    ),
    "error_boundary": (
        "Wrap a risky executable node with fallback text or compensation logic when the main path may fail but the overall workflow should keep moving.",
    ),
    "subworkflow": (
        "Invoke a published child workflow to keep large systems modular and reuse governed building blocks behind aliases such as dev, staging, or prod.",
    ),
    "tool": (
        "Runs a registered tool binding directly, reusing the same registry resolution, MCP-backed bindings, preview safety rules, and retry/timeout policies that agent tool calls use.",
        "Use this when the workflow should invoke a capability deterministically without asking an LLM to decide whether to call it.",
    ),
    "mcp_resource": (
        "Calls a registered MCP server tool with the same server and tool metadata the rest of CALIBER uses, so external systems stay typed and discoverable.",
    ),
    "webhook": (
        "Sends the upstream payload (or input) to an external URL as an HTTP request, JSON-encoding structured bodies, and publishes the response text, parsed body, and status metadata downstream.",
        "Use it to notify external systems, post to chat/incident webhooks, or call simple REST endpoints without writing a tool. Configure auth via headers (reference secrets by name rather than pasting them inline).",
    ),
    "api_request": (
        "Calls an HTTP API two ways: build the request from a URL + method + headers, or paste a cURL command and the node extracts the method, URL, headers, and body for you.",
        "When the body field is empty the upstream payload (or input) becomes the request body — structured bodies are sent as JSON. The response text, parsed body, and status metadata flow downstream. Reference auth secrets by name in headers rather than pasting them inline.",
    ),
    "agent": (
        "Agents can run with inline instructions or registered prompts and can persist conversation state through the shared workflow session ID.",
    ),
    "guardrail": (
        "Guardrails are best paired with agents or outputs where structured validation and retry behavior matter.",
    ),
    "router": (
        "Branches are evaluated top-to-bottom and the first matching condition wins; leave one branch empty as the fallback.",
        "Router control flow is expressed through outgoing edges instead of named output ports, so each branch destination becomes an explicit path on the canvas.",
    ),
    "human_approval": (
        "Runtime approvals appear in the run monitor and can be resolved directly from the workflow detail and editor surfaces.",
        "Every field is enforced at runtime: the required role gates who may decide, the approval count is a quorum of distinct approvers, and whoever started the run cannot approve it themselves by default.",
        "Timeout behavior is restricted to blocking. Escalation has no target to escalate to and auto-reject has no deadline to enforce, so both are rejected while you author rather than becoming a control that silently does nothing.",
    ),
    "knowledge_query": (
        "Supports dense retrieval, GraphRAG hybrid, and Apache AGE graph retrieval with query-time overrides.",
        "Leave retrieval modes empty to follow the knowledge base default, or wire a runtime retrieval_modes input when an upstream step should decide between dense, hybrid, or AGE per request.",
    ),
    "knowledge_build": (
        "Launches a new knowledge-base version build so workflows can refresh chunking, embeddings, and graph artifacts without leaving the canvas.",
        "Leave the sources and graph_config inputs unwired to reuse the knowledge base's saved source manifest and latest graph profile; wire them only when an upstream step should override the corpus selection or graph policy for this build.",
    ),
    "template": (
        "Use placeholders like {{input}}, {{variables.customer.name}}, or {{ticket.id}} to shape prompts, summaries, and structured payloads without writing Python.",
        "Text mode returns the rendered template verbatim; JSON mode validates the rendered payload and publishes the parsed object for downstream nodes.",
    ),
    "data_transform": (
        "Choose Fixture, Mapping, JSON Schema, Decision Table, or Confidence to replace common custom-Python glue with audited configuration.",
        "The result, validity flag, and diagnostic metadata are first-class outputs that can feed routers, approvals, and evidence views.",
    ),
    "review_queue_enqueue": (
        "Enqueues trace IDs into an active CALIBER Review Queue as an audited, idempotent workflow side effect.",
        "The database-backed runtime is required; exported standalone workflows fail closed until a queue enqueuer is supplied.",
    ),
    "external_app": (
        "Use this as a migration bridge while existing Python business logic is incrementally converted into first-class workflow nodes or tools.",
    ),
    "python_code": (
        "Runs sandboxed Python for lightweight glue logic, shaping payloads, or temporary adapters while a dedicated tool or node is still being formalized.",
    ),
    "note": (
        "Notes never execute at runtime, but they are helpful for documenting intent, handoff boundaries, or operator instructions directly on the canvas.",
    ),
}

_COMPONENT_SETUP_CHECKS: dict[str, tuple[dict[str, Any], ...]] = {
    "file_input": (
        {
            "label": "Select a managed file or provide a legacy path",
            "help": "Select a project file, set a host path, or map a path into the node.",
            "kind": "any_non_empty",
            "fields": ["file_ref", "path"],
        },
    ),
    "folder_input": (
        {
            "label": "Provide a folder path",
            "help": "Set the folder path directly or map one into the node's path input.",
            "kind": "non_empty_string",
            "field": "path",
        },
    ),
    "input_bucket": (
        {
            "label": "Select an input bucket",
            "help": "Choose the object-store bucket this node should scan. Unreadable objects are skipped and surfaced in metadata; bucket-list failures stop the run.",
            "kind": "non_empty_string",
            "field": "bucket",
        },
    ),
    "output_bucket": (
        {
            "label": "Select an output bucket",
            "help": "Choose where workflow artifacts should be written.",
            "kind": "non_empty_string",
            "field": "bucket",
        },
    ),
    "output_folder": (
        {
            "label": "Provide an output folder path",
            "help": "Choose the destination directory for emitted files.",
            "kind": "non_empty_string",
            "field": "path",
        },
    ),
    "wait_until": (
        {
            "label": "Set the target timestamp",
            "help": "Choose the time the workflow should resume.",
            "kind": "non_empty_string",
            "field": "wait_until",
        },
    ),
    "wait_for_event": (
        {
            "label": "Name the resume event",
            "help": "Set the event name operators or systems will use to resume this run.",
            "kind": "non_empty_string",
            "field": "event_name",
        },
    ),
    "parallel": (
        {
            "label": "Add at least two downstream branches",
            "help": "Connect this parallel node to at least two downstream branches before using it as a fan-out barrier.",
            "kind": "minimum_outgoing_edges",
            "minimum": 2,
        },
    ),
    "join": (
        {
            "label": "Connect at least two upstream branches",
            "help": "Feed this join from at least two upstream branches, or remove the join barrier.",
            "kind": "minimum_incoming_edges",
            "minimum": 2,
        },
        {
            "label": "Use distinct join input ports per branch",
            "help": "Map each incoming branch into a distinct join input port so the merge stays traceable.",
            "kind": "distinct_incoming_target_ports",
        },
    ),
    "for_each": (
        {
            "label": "Use an executable target when set",
            "help": "If you choose a target node for this loop, it must point to an executable step.",
            "kind": "target_node_executable_if_set",
            "field": "target_node_id",
        },
    ),
    "subworkflow": (
        {
            "label": "Select the workflow to invoke",
            "help": "Choose the child workflow this node should run.",
            "kind": "non_empty_string",
            "field": "workflow_id",
        },
        {
            "label": "Avoid calling this workflow recursively",
            "help": "Choose a different published child workflow instead of pointing this node back at the current workflow.",
            "kind": "not_current_workflow_id",
            "field": "workflow_id",
        },
    ),
    "tool": (
        {
            "label": "Select a tool binding",
            "help": "Choose the manifest tool binding this node should invoke directly.",
            "kind": "non_empty_string",
            "field": "tool_name",
        },
    ),
    "mcp_resource": (
        {
            "label": "Select an MCP server",
            "help": "Choose the active MCP server hosting the tool.",
            "kind": "non_empty_string",
            "field": "server_id",
        },
        {
            "label": "Select an MCP tool",
            "help": "Pick the tool to call on that server.",
            "kind": "non_empty_string",
            "field": "tool_name",
        },
    ),
    "webhook": (
        {
            "label": "Provide a request URL",
            "help": "Set the HTTP(S) endpoint this webhook should call.",
            "kind": "non_empty_string",
            "field": "url",
        },
    ),
    "api_request": (
        {
            "label": "Provide a URL or cURL command",
            "help": "Set the request URL (URL mode) or paste a cURL command (cURL mode).",
            "kind": "any_non_empty",
            "fields": ["url", "curl"],
        },
    ),
    "knowledge_query": (
        {
            "label": "Select a knowledge base or pinned versions",
            "help": "Choose the target knowledge base or pin explicit KB versions for this query.",
            "kind": "any_non_empty",
            "fields": ["knowledge_base_id", "version_ids"],
        },
    ),
    "knowledge_build": (
        {
            "label": "Select a knowledge base",
            "help": "Choose the existing knowledge base this node should refresh.",
            "kind": "non_empty_string",
            "field": "knowledge_base_id",
        },
        {
            "label": "Choose a chunking strategy",
            "help": "Set the chunker directly or map one into the chunking_strategy input.",
            "kind": "non_empty_string",
            "field": "chunking_strategy",
        },
        {
            "label": "Choose an embedding model",
            "help": "Set the embedding model directly or map one into the embedding_model input.",
            "kind": "non_empty_string",
            "field": "embedding_model",
        },
    ),
    "loop": (
        {
            "label": "Select a loop target",
            "help": "Choose the executable node this loop should repeat.",
            "kind": "non_empty_string",
            "field": "target_node_id",
        },
        {
            "label": "Choose an executable loop target",
            "help": "The selected loop target should point to an executable node in this workflow.",
            "kind": "target_node_executable_if_set",
            "field": "target_node_id",
        },
    ),
    "error_boundary": (
        {
            "label": "Protect an executable target when set",
            "help": "If this boundary wraps a target node, that target should be an executable step.",
            "kind": "target_node_executable_if_set",
            "field": "target_node_id",
        },
        {
            "label": "Use an executable compensation node when set",
            "help": "If you configure a compensation node, it should point to an executable recovery step.",
            "kind": "target_node_executable_if_set",
            "field": "compensate_with",
        },
    ),
    "template": (
        {
            "label": "Provide a template",
            "help": "Write the text or JSON template this node should render.",
            "kind": "non_empty_string",
            "field": "template",
        },
    ),
    "data_transform": (
        {
            "label": "Configure the transform",
            "help": "Provide the mapping, schema, rules, confidence signals, or fixture for the selected operation.",
            "kind": "non_empty_object",
            "field": "config",
        },
    ),
    "review_queue_enqueue": (
        {
            "label": "Select an active review queue",
            "help": "Paste the durable queue ID that should receive workflow traces.",
            "kind": "non_empty_string",
            "field": "queue_id",
        },
    ),
    "python_code": (
        {
            "label": "Provide Python code",
            "help": "Write or paste the code this node should execute.",
            "kind": "non_empty_string",
            "field": "code",
        },
    ),
    "agent": (
        {
            "label": "Provide instructions or a prompt reference",
            "help": "Set inline instructions or bind the agent to a registered prompt.",
            "kind": "instructions_present",
        },
    ),
    "guardrail": (
        {
            "label": "Configure at least one guardrail check",
            "help": "Choose the checks this node should apply to the response or input.",
            "kind": "non_empty_list",
            "field": "checks",
        },
    ),
    "router": (
        {
            "label": "Add at least one branch",
            "help": "Define the branch destinations and routing conditions.",
            "kind": "non_empty_list",
            "field": "branches",
        },
        {
            "label": "Connect every branch target with an outgoing edge",
            "help": "Each configured branch should point to a real node and also have a matching outgoing edge from this router.",
            "kind": "router_branch_edges_connected",
        },
    ),
    "external_app": (
        {
            "label": "Set the external app entrypoint",
            "help": "Provide the app entrypoint the runtime should invoke.",
            "kind": "non_empty_string",
            "field": "entrypoint",
        },
    ),
}

_DESIGNER_NODE_ID_MARKER = "__CALIBER_NODE_ID__"
_DESIGNER_NOW_PLUS_60S_ISO_MARKER = "__CALIBER_NOW_PLUS_60S_ISO__"


def _starter_port(type_name: str) -> dict[str, str]:
    return {"type": type_name}


def _wait_for_event_starter_outputs() -> dict[str, dict[str, str]]:
    return {
        "output": _starter_port("string"),
        "event_payload": _starter_port("structured"),
        "event_name": _starter_port("string"),
    }


_DESIGNER_STARTER_NODES: dict[str, dict[str, Any]] = {
    "start": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "start",
        "outputs": {
            "user_message": _starter_port("string"),
        },
    },
    "file_input": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "file_input",
        "path": "",
        "encoding": "utf-8",
        "max_bytes": 200000,
        "inputs": {"path": _starter_port("string")},
        "outputs": {
            "text": _starter_port("string"),
            "path": _starter_port("string"),
            "file_ref": _starter_port("structured"),
            "metadata": _starter_port("structured"),
        },
    },
    "folder_input": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "folder_input",
        "path": "",
        "pattern": "**/*",
        "recursive": True,
        "max_files": 50,
        "max_bytes_per_file": 100000,
        "encoding": "utf-8",
        "inputs": {"path": _starter_port("string")},
        "outputs": {
            "text": _starter_port("string"),
            "files": _starter_port("structured"),
            "metadata": _starter_port("structured"),
        },
    },
    "input_bucket": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "input_bucket",
        "bucket": "",
        "prefix": "",
        "recursive": True,
        "max_files": 50,
        "max_bytes_per_file": 100000,
        "encoding": "utf-8",
        "inputs": {"prefix": _starter_port("string")},
        "outputs": {
            "text": _starter_port("string"),
            "files": _starter_port("structured"),
            "metadata": _starter_port("structured"),
        },
    },
    "output_bucket": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "output_bucket",
        "bucket": "",
        "prefix": "",
        "overwrite": True,
        "inputs": {"input": _starter_port("string")},
        "outputs": {
            "keys": _starter_port("structured"),
            "metadata": _starter_port("structured"),
        },
    },
    "output_folder": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "output_folder",
        "path": "",
        "overwrite": True,
        "inputs": {"input": _starter_port("string")},
        "outputs": {
            "files": _starter_port("structured"),
            "metadata": _starter_port("structured"),
        },
    },
    "wait_until": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "wait_until",
        "wait_until": _DESIGNER_NOW_PLUS_60S_ISO_MARKER,
        "timezone": "UTC",
        "inputs": {"input": _starter_port("string")},
        "outputs": {"output": _starter_port("string")},
    },
    "wait_for_event": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "wait_for_event",
        "event_name": "resume_event",
        "correlation_key": "",
        "timeout_seconds": None,
        "inputs": {"input": _starter_port("string")},
        "outputs": _wait_for_event_starter_outputs(),
    },
    "parallel": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "parallel",
        "inputs": {"input": _starter_port("string")},
        "outputs": {"output": _starter_port("string")},
    },
    "join": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "join",
        "mode": "all",
        "inputs": {},
        "outputs": {
            "output": _starter_port("string"),
            "merged": _starter_port("structured"),
        },
    },
    "for_each": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "for_each",
        "target_node_id": None,
        "item_input_port": "items",
        "max_items": 100,
        "inputs": {"items": _starter_port("structured")},
        "outputs": {
            "results": _starter_port("structured"),
            "text": _starter_port("string"),
            "metadata": _starter_port("structured"),
        },
    },
    "loop": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "loop",
        "target_node_id": None,
        "max_iterations": 10,
        "stop_condition": "",
        "inputs": {
            "input": _starter_port("string"),
            "state": _starter_port("structured"),
        },
        "outputs": {
            "output": _starter_port("string"),
            "result": _starter_port("structured"),
            "iterations": _starter_port("structured"),
            "metadata": _starter_port("structured"),
        },
    },
    "error_boundary": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "error_boundary",
        "target_node_id": None,
        "fallback_text": "",
        "compensate_with": None,
        "inputs": {"input": _starter_port("string")},
        "outputs": {
            "output": _starter_port("string"),
            "error": _starter_port("structured"),
        },
    },
    "subworkflow": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "subworkflow",
        "workflow_id": "",
        "alias": "prod",
        "timeout_seconds": 120,
        "inputs": {"input": _starter_port("string")},
        "outputs": {
            "output": _starter_port("string"),
            "result": _starter_port("structured"),
        },
    },
    "tool": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "tool",
        "tool_name": "",
        "inputs": {
            "input": _starter_port("string"),
            "arguments": _starter_port("structured"),
        },
        "outputs": {
            "text": _starter_port("string"),
            "result": _starter_port("structured"),
            "metadata": _starter_port("structured"),
        },
    },
    "mcp_resource": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "mcp_resource",
        "server_id": "",
        "tool_name": "",
        "timeout_seconds": 45,
        "inputs": {"input": _starter_port("string")},
        "outputs": {
            "text": _starter_port("string"),
            "result": _starter_port("structured"),
            "metadata": _starter_port("structured"),
        },
    },
    "webhook": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "webhook",
        "url": "",
        "method": "POST",
        "headers": {},
        "timeout_seconds": 30,
        "inputs": {
            "payload": _starter_port("structured"),
            "input": _starter_port("string"),
        },
        "outputs": {
            "text": _starter_port("string"),
            "response": _starter_port("structured"),
            "metadata": _starter_port("structured"),
        },
    },
    "api_request": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "api_request",
        "mode": "url",
        "url": "",
        "method": "GET",
        "curl": "",
        "headers": {},
        "body": "",
        "timeout_seconds": 30,
        "inputs": {
            "payload": _starter_port("structured"),
            "input": _starter_port("string"),
        },
        "outputs": {
            "text": _starter_port("string"),
            "response": _starter_port("structured"),
            "metadata": _starter_port("structured"),
        },
    },
    "knowledge_query": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "knowledge_query",
        "knowledge_base_id": "",
        "version_ids": [],
        "retrieval_modes": [],
        "top_k": 6,
        "chat_model": None,
        "graph_overrides": None,
        "inputs": {
            "question": _starter_port("string"),
            "history": _starter_port("structured"),
            "retrieval_modes": _starter_port("structured"),
            "version_ids": _starter_port("structured"),
            "graph_overrides": _starter_port("structured"),
        },
        "outputs": {
            "text": _starter_port("string"),
            "answer": _starter_port("string"),
            "result": _starter_port("structured"),
            "citations": _starter_port("structured"),
            "chunks": _starter_port("structured"),
            "graph_context": _starter_port("structured"),
        },
    },
    "knowledge_build": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "knowledge_build",
        "knowledge_base_id": "",
        "chunking_strategy": "",
        "embedding_model": "",
        "chunking_config": {},
        "graph_config": None,
        "activate_when_complete": False,
        "wait_for_completion": False,
        "wait_timeout_seconds": 300.0,
        "inputs": {
            "input": _starter_port("string"),
            "sources": _starter_port("structured"),
            "chunking_strategy": _starter_port("string"),
            "embedding_model": _starter_port("string"),
            "chunking_config": _starter_port("structured"),
            "graph_config": _starter_port("structured"),
        },
        "outputs": {
            "text": _starter_port("string"),
            "result": _starter_port("structured"),
            "knowledge_base": _starter_port("structured"),
            "version": _starter_port("structured"),
            "run": _starter_port("structured"),
            "status": _starter_port("string"),
            "version_id": _starter_port("string"),
            "run_id": _starter_port("string"),
        },
    },
    "template": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "template",
        "template": "{{input}}",
        "output_format": "text",
        "missing_variable_mode": "preserve",
        "inputs": {
            "input": _starter_port("string"),
            "variables": _starter_port("structured"),
        },
        "outputs": {
            "text": _starter_port("string"),
            "result": _starter_port("structured"),
            "metadata": _starter_port("structured"),
        },
    },
    "data_transform": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "data_transform",
        "operation": "mapping",
        "config": {"fields": {}, "defaults": {}},
        "fail_on_invalid": True,
        "inputs": {
            "value": _starter_port("structured"),
            "text": _starter_port("string"),
        },
        "outputs": {
            "text": _starter_port("string"),
            "result": _starter_port("structured"),
            "valid": _starter_port("boolean"),
            "metadata": _starter_port("structured"),
        },
    },
    "review_queue_enqueue": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "review_queue_enqueue",
        "queue_id": "REVIEW-QUEUE-ID",
        "experiment_id": None,
        "assigned_to": None,
        "inputs": {
            "trace_id": _starter_port("string"),
            "trace_ids": _starter_port("structured"),
        },
        "outputs": {
            "text": _starter_port("string"),
            "result": _starter_port("structured"),
            "created_count": _starter_port("structured"),
        },
    },
    "external_app": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "external_app",
        "entrypoint": "",
        "inputs": {
            "input": _starter_port("string"),
            "context": _starter_port("structured"),
        },
        "outputs": {
            "text": _starter_port("string"),
            "result": _starter_port("structured"),
            "metadata": _starter_port("structured"),
        },
    },
    "python_code": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "python_code",
        "code": 'return {"text": input or run_input, "result": {"ok": True}}',
        "timeout_seconds": 5,
        "inputs": {
            "input": _starter_port("string"),
            "context": _starter_port("structured"),
        },
        "outputs": {
            "text": _starter_port("string"),
            "result": _starter_port("structured"),
            "metadata": _starter_port("structured"),
        },
    },
    "output": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "output",
        "inputs": {"response": _starter_port("string")},
    },
    "agent": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "agent",
        "name": _DESIGNER_NODE_ID_MARKER,
        "model": "inherit",
        "instructions": {"type": "inline", "text": "You are a helpful assistant."},
        "tools": [],
        "inputs": {
            "input": _starter_port("string"),
            "history": _starter_port("structured"),
        },
        "outputs": {
            "final_output": _starter_port("string"),
            "history": _starter_port("structured"),
        },
    },
    "router": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "router",
        "inputs": {"decision": _starter_port("string")},
        "outputs": {},
        "branches": [],
    },
    "guardrail": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "guardrail",
        "mode": "post_agent",
        "inputs": {"response": _starter_port("string")},
        "outputs": {"passthrough": _starter_port("string")},
        "on_failure": "block",
        "max_retries": 0,
        "checks": [{"non_empty_output": {}}],
    },
    "human_approval": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "human_approval",
        "required_role": "caliber.approver",
        "approval_count": 1,
        "timeout_behavior": "block",
        "inputs": {"request": _starter_port("string")},
        "outputs": {"request": _starter_port("string")},
    },
    "note": {
        "id": _DESIGNER_NODE_ID_MARKER,
        "type": "note",
        "text": "",
    },
}

_FIELD_DESCRIPTIONS: dict[str, str] = {
    "execution_policy": "Optional timeout, retry, and idempotency controls applied by the workflow runtime for this node.",
    "trigger": "Choose whether this workflow starts manually, from an external event, or on a cron schedule.",
    "path": "Local filesystem path used by this node.",
    "pattern": "Glob pattern used to select matching files inside the folder.",
    "recursive": "Whether nested folders or prefixes are included while scanning.",
    "max_bytes": "Maximum number of bytes read from a single file.",
    "max_files": "Upper bound on the number of files or objects this node processes.",
    "max_bytes_per_file": "Maximum number of bytes read from each file or object.",
    "encoding": "Text encoding used when decoding file content.",
    "bucket": "Object-store bucket used as the source or destination.",
    "prefix": "Object-store prefix scanned or written by this node.",
    "overwrite": "When disabled, existing files or objects are preserved instead of replaced.",
    "wait_until": "Timestamp or expression that determines when the run should resume.",
    "timezone": "Timezone applied when the wait timestamp omits an explicit offset.",
    "event_name": "Name of the external event that resumes the run.",
    "correlation_key": "Optional payload field used to correlate a resume event with the waiting run.",
    "timeout_seconds": "Upper bound on how long this node may wait or execute before timing out.",
    "mode": "Controls how this node behaves when coordinating branches or validation timing.",
    "target_node_id": "Optional node protected or invoked by this control component.",
    "item_input_port": "Input port on the target node that receives each iterated item.",
    "max_items": "Upper bound on items expanded by the loop to keep runs bounded.",
    "max_iterations": "Upper bound on loop repetitions to keep runs bounded even when the stop condition never matches.",
    "stop_condition": "Optional safe expression evaluated after each iteration. It can reference iteration, max_iterations, item, output, result, outputs, and state.",
    "fallback_text": "Text returned when the protected node fails and no compensation step handles the error.",
    "compensate_with": "Optional node to run when the protected node fails.",
    "workflow_id": "Published workflow invoked by this subworkflow step.",
    "alias": "Deployment alias resolved for subworkflow calls or trigger targets.",
    "server_id": "Registered MCP server used by this node.",
    "tool_name": "Tool invoked by this node. Tool nodes use the local manifest binding name; MCP resource nodes use the remote MCP tool name.",
    "url": "HTTP(S) endpoint this node sends its request to (URL mode).",
    "method": "HTTP method used for the outbound request (URL mode).",
    "headers": "Optional HTTP headers sent with the request. Reference secrets by name rather than pasting them inline.",
    "curl": "A cURL command parsed for the method, URL, headers, and body (cURL mode). No shell is executed.",
    "body": "Optional request body. A JSON string is sent as JSON; leave empty to use the upstream payload/input as the body.",
    "knowledge_base_id": "Knowledge base queried or refreshed by this node.",
    "chunking_strategy": "Chunking strategy used when the knowledge build launches.",
    "embedding_model": "Embedding model used for the launched knowledge-base version.",
    "chunking_config": "Optional chunking settings passed into the launched knowledge-base build.",
    "graph_config": "Optional graph extraction / GraphRAG build settings applied to the launched knowledge-base version.",
    "activate_when_complete": "When enabled, CALIBER promotes the new version to active once the build finishes successfully.",
    "wait_for_completion": "When enabled, the workflow waits for the launched build to reach a terminal status before continuing.",
    "wait_timeout_seconds": "Maximum time the workflow waits for a launched knowledge build before continuing with its current status.",
    "version_ids": "Optional pinned knowledge-base versions used instead of the active version.",
    "retrieval_modes": "Retrieval strategies applied when querying the knowledge base. Leave empty to follow the knowledge base default, or wire them from the retrieval_modes input port at runtime.",
    "top_k": "Maximum number of chunks retrieved before answer synthesis.",
    "chat_model": "Optional model override used only for the final answer generation step.",
    "graph_overrides": "Optional query-time GraphRAG and AGE retrieval settings.",
    "template": "Text or JSON template rendered from the current workflow inputs.",
    "output_format": "Whether the rendered template should stay as text or be parsed and validated as JSON.",
    "missing_variable_mode": "Controls whether unresolved placeholders stay intact, become empty strings, or raise an execution error.",
    "operation": "Closed-vocabulary no-code operation applied to the incoming value.",
    "config": "JSON configuration for mapping fields, a schema, decision rules, confidence signals, or fixture data.",
    "fail_on_invalid": "When enabled, JSON Schema failures stop the run instead of publishing valid=false for routing.",
    "queue_id": "Review queue that receives the workflow-generated item.",
    "experiment_id": "Optional MLflow experiment associated with the review item and trace.",
    "assigned_to": "Optional reviewer identity assigned when the queue item is created.",
    "code": "Python executed inside the workflow sandbox.",
    "name": "Human-friendly node or agent name shown in the canvas and run traces.",
    "model": "LLM model reference used by this agent.",
    "instructions": "Inline instructions or a registered MLflow prompt reference for this agent.",
    "tools": "Registered tools this agent may call.",
    "skills": "Reusable skills composed into the agent prompt.",
    "tool_constraints": "Optional per-tool usage rules applied by the compiler and runtime.",
    "handoffs": "Named delegation or escalation paths this agent can hand work to.",
    "output_type": "Structured output schema expected from the agent, when applicable.",
    "eval_dataset": "Optional evaluation dataset reference used during workflow calibration.",
    "checks": "Guardrail checks executed before or after the target agent step.",
    "on_failure": "Behavior applied when a guardrail check fails.",
    "max_retries": "Additional retry attempts allowed when the node is configured for Block + Retry.",
    "branches": "Ordered router branches evaluated from top to bottom.",
    "required_role": "Approver role required to release this manual gate.",
    "approval_count": "Number of approvals required before the run resumes.",
    "timeout_behavior": "Behavior applied if the approval wait times out.",
    "text": "Annotation text stored only for human readers on the canvas.",
    "entrypoint": "Python module path and callable used to bridge an existing application.",
}

_FIELD_EXAMPLES: dict[str, tuple[Any, ...]] = {
    "execution_policy": (
        {
            "timeout_seconds": 45,
            "max_retries": 2,
            "idempotent": True,
        },
    ),
    "path": ("/data/orders.csv", "/srv/support"),  # local examples across file/folder nodes
    "bucket": ("reports",),
    "prefix": ("docs/support/",),
    "wait_until": ("2026-07-01T09:00:00Z",),
    "event_name": ("ticket.approved",),
    "correlation_key": ("ticket_id",),
    "workflow_id": ("WF-support-triage",),
    "server_id": ("MCP-notion",),
    "tool_name": ("lookup_policy", "search_docs"),
    "knowledge_base_id": ("KB-ops",),
    "chunking_strategy": ("recursive",),
    "embedding_model": ("BAAI/bge-m3",),
    "max_iterations": (5,),
    "stop_condition": ("state.done or iteration >= 3",),
    "model": ("gpt-5.6-luna",),
    "template": (
        "Hello {{customer.name}}, your request is {{input}}.",
        '{"ticket_id":"{{variables.ticket.id}}","summary":"{{input}}","labels":{{variables.labels}}}',
    ),
    "config": ({"fields": {"customer_id": "customer.id"}, "defaults": {"status": "new"}},),
    "entrypoint": ("support.ticketing:handle_request",),
}

_EXCLUDED_FIELDS = {"id", "type", "inputs", "outputs", "label", "description"}

# Field keys treated as advanced/tuning rather than primary configuration. The
# designer collapses these behind a "Show advanced fields" toggle by default.
# Driven by field-key name so it applies uniformly across node types.
_ADVANCED_FIELD_KEYS: frozenset[str] = frozenset(
    {
        "encoding",
        "max_bytes",
        "max_bytes_per_file",
        "max_files",
        "recursive",
        "pattern",
        "prefix",
        "overwrite",
        "timeout_seconds",
        "timezone",
        "correlation_key",
        "max_items",
        "item_input_port",
        "max_iterations",
        "headers",
        "chunking_config",
        "graph_config",
        "graph_overrides",
        "activate_when_complete",
        "wait_for_completion",
        "wait_timeout_seconds",
        "tool_constraints",
        "eval_dataset",
        "output_type",
        "missing_variable_mode",
        "top_k",
        "chat_model",
        "version_ids",
        "approval_count",
        "timeout_behavior",
        "compensate_with",
    }
)

# Components kept for backward compatibility but de-emphasized in the palette.
# ``node_type -> suggested replacement`` (shown as the "Legacy" badge tooltip).
_LEGACY_COMPONENTS: dict[str, str] = {
    "external_app": "Tool, Python Code, or API Request",
}
_NODE_SPECIFIC_EXCLUDED_FIELDS: dict[str, set[str]] = {
    "start": {"execution_policy"},
    "output": {"execution_policy"},
}
_CAMEL_CASE_BOUNDARY = re.compile(r"(?<!^)(?=[A-Z])")


def build_workflow_component_catalog() -> dict[str, Any]:
    """Return the workflow component catalog consumed by the designer UI."""

    return _cached_component_catalog()


@lru_cache(maxsize=1)
def _cached_component_catalog() -> dict[str, Any]:
    components = [
        _build_component_item(node_type=node_type, model_cls=model_cls)
        for node_type, model_cls in _COMPONENT_ORDER
    ]
    return {"schema_version": 1, "components": components}


def _build_component_item(*, node_type: str, model_cls: type[BaseModel]) -> dict[str, Any]:
    label = _COMPONENT_LABELS[node_type]
    category = _COMPONENT_CATEGORIES[node_type]
    description, docs = _component_docs(node_type=node_type, model_cls=model_cls)
    inputs = _default_ports(node_type=node_type, model_cls=model_cls, field_name="inputs")
    outputs = _default_ports(node_type=node_type, model_cls=model_cls, field_name="outputs")
    fields = _component_fields(node_type=node_type, model_cls=model_cls)
    return {
        "type": node_type,
        "label": label,
        "category": category,
        "description": description,
        "docs": docs,
        "default_inputs": inputs,
        "default_outputs": outputs,
        "starter_node": _normalize_json_value(_DESIGNER_STARTER_NODES.get(node_type)),
        "fields": fields,
        "setup_checks": list(_COMPONENT_SETUP_CHECKS.get(node_type, ())),
        "legacy": node_type in _LEGACY_COMPONENTS,
        "legacy_replacement": _LEGACY_COMPONENTS.get(node_type),
    }


def _component_docs(*, node_type: str, model_cls: type[BaseModel]) -> tuple[str, list[str]]:
    # Only the model's own docstring may become designer copy. ``inspect.getdoc``
    # walks the MRO, so the six node models that carry no docstring of their own
    # published pydantic's ``BaseModel`` docstring as their palette description and
    # tips. A node without its own prose wants the curated fallback below instead.
    own_doc = model_cls.__dict__.get("__doc__")
    raw = inspect.cleandoc(own_doc) if isinstance(own_doc, str) else None
    # Tips come from ``_COMPONENT_DOCS`` alone. The docstring *body* used to be
    # appended one tip per physical source line, which turned engineering prose
    # into a stack of mid-sentence fragments carrying reST roles and literals
    # (``:class:`FolderInputNode```, ``` ``prefix`` ```) — copy written for someone
    # reading the module, rendered to someone configuring a node. Only the summary
    # line, which is a real one-sentence description, is published.
    docs = list(_COMPONENT_DOCS.get(node_type, ()))
    if raw:
        summary = next((line.strip() for line in raw.splitlines() if line.strip()), "")
        if summary:
            return summary, docs
    return _FALLBACK_DESCRIPTIONS.get(node_type, _humanize(node_type) + "."), docs


def _default_ports(
    *,
    node_type: str,
    model_cls: type[BaseModel],
    field_name: str,
) -> dict[str, dict[str, Any]]:
    starter_node = _DESIGNER_STARTER_NODES.get(node_type)
    if isinstance(starter_node, dict):
        starter_ports = _normalize_port_map(starter_node.get(field_name))
        if starter_ports:
            return starter_ports
    field = model_cls.model_fields.get(field_name)
    if field is None:
        return {}
    default = _field_default(field)
    return _normalize_port_map(default)


def _normalize_port_map(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        return {}
    ports: dict[str, dict[str, Any]] = {}
    for port_name, spec in value.items():
        if isinstance(spec, PortSpec):
            ports[str(port_name)] = spec.model_dump(by_alias=True)
            continue
        if not isinstance(spec, dict):
            continue
        try:
            parsed = PortSpec.model_validate(spec)
        except ValidationError:
            continue
        ports[str(port_name)] = parsed.model_dump(by_alias=True)
    return ports


def _component_fields(*, node_type: str, model_cls: type[BaseModel]) -> list[dict[str, Any]]:
    schema = model_cls.model_json_schema(by_alias=True)
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))
    definitions = schema.get("$defs", {})
    excluded_fields = _EXCLUDED_FIELDS | _NODE_SPECIFIC_EXCLUDED_FIELDS.get(node_type, set())
    declared_fields = [name for name in model_cls.model_fields if name not in excluded_fields]
    fields: list[dict[str, Any]] = []
    for field_name in declared_fields:
        model_field = model_cls.model_fields.get(field_name)
        if model_field is None:
            continue
        alias = model_field.alias or field_name
        property_schema = properties.get(alias) or properties.get(field_name)
        if not isinstance(property_schema, dict):
            continue
        resolved_schema, nullable = _resolve_schema(property_schema, definitions)
        constraints = _field_constraints(resolved_schema, nullable=nullable)
        default_value = resolved_schema.get("default", _field_default(model_field))
        if default_value is inspect._empty or default_value is PydanticUndefined:
            default_value = None
        fields.append(
            {
                "key": alias,
                "label": _humanize(alias),
                "type": _schema_type_label(property_schema, definitions),
                "required": alias in required or field_name in required,
                "default": _normalize_json_value(default_value),
                "description": _FIELD_DESCRIPTIONS.get(alias) or resolved_schema.get("description"),
                "advanced": alias in _ADVANCED_FIELD_KEYS,
                "constraints": constraints,
                "examples": [
                    _normalize_json_value(example) for example in _FIELD_EXAMPLES.get(alias, ())
                ],
            }
        )
    return fields


def _resolve_schema(
    schema: dict[str, Any], definitions: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    current = dict(schema)
    nullable = False
    if "$ref" in current:
        ref_name = str(current["$ref"]).split("/")[-1]
        target = definitions.get(ref_name)
        if isinstance(target, dict):
            current = dict(target)
    for union_key in ("anyOf", "oneOf"):
        options = current.get(union_key)
        if not isinstance(options, list):
            continue
        non_null: list[dict[str, Any]] = []
        for option in options:
            if not isinstance(option, dict):
                continue
            if option.get("type") == "null":
                nullable = True
                continue
            resolved, nested_nullable = _resolve_schema(option, definitions)
            nullable = nullable or nested_nullable
            non_null.append(resolved)
        if len(non_null) == 1:
            return non_null[0], nullable
    if "allOf" in current and isinstance(current["allOf"], list) and len(current["allOf"]) == 1:
        option = current["allOf"][0]
        if isinstance(option, dict):
            return _resolve_schema(option, definitions)
    return current, nullable


def _schema_type_label(schema: dict[str, Any], definitions: dict[str, Any]) -> str:
    union_labels, nullable = _union_type_labels(schema, definitions)
    if union_labels:
        base = " | ".join(union_labels)
        return f"{base} | null" if nullable else base
    resolved, nullable = _resolve_schema(schema, definitions)
    if "$ref" in schema:
        base = _humanize(str(schema["$ref"]).split("/")[-1])
    elif "enum" in resolved:
        base = "enum"
    else:
        schema_type = resolved.get("type")
        if isinstance(schema_type, list):
            cleaned = [str(item) for item in schema_type if item != "null"]
            base = " | ".join(cleaned) if cleaned else "object"
        elif schema_type == "array":
            item_schema = resolved.get("items")
            item_type = (
                _schema_type_label(item_schema, definitions)
                if isinstance(item_schema, dict)
                else "object"
            )
            base = f"list<{item_type}>"
        elif isinstance(schema_type, str) and schema_type:
            title = resolved.get("title") or schema.get("title")
            base = (
                _humanize(str(title))
                if schema_type == "object" and isinstance(title, str) and title.strip()
                else schema_type
            )
        elif isinstance(resolved.get("title"), str) and str(resolved["title"]).strip():
            base = _humanize(str(resolved["title"]))
        elif isinstance(schema.get("title"), str) and str(schema["title"]).strip():
            base = _humanize(str(schema["title"]))
        else:
            base = "object"
    return f"{base} | null" if nullable else base


def _union_type_labels(
    schema: dict[str, Any],
    definitions: dict[str, Any],
) -> tuple[list[str], bool]:
    nullable = False
    for union_key in ("oneOf", "anyOf"):
        options = schema.get(union_key)
        if not isinstance(options, list):
            continue
        labels: list[str] = []
        for option in options:
            if not isinstance(option, dict):
                continue
            if option.get("type") == "null":
                nullable = True
                continue
            labels.append(_schema_type_label(option, definitions))
        if labels:
            return list(dict.fromkeys(labels)), nullable
    return [], nullable


def _field_constraints(schema: dict[str, Any], *, nullable: bool) -> dict[str, Any]:
    constraints: dict[str, Any] = {}
    mapping = {
        "minimum": "minimum",
        "maximum": "maximum",
        "exclusiveMinimum": "exclusive_minimum",
        "exclusiveMaximum": "exclusive_maximum",
        "minLength": "min_length",
        "maxLength": "max_length",
        "minItems": "min_items",
        "maxItems": "max_items",
        "pattern": "pattern",
        "multipleOf": "multiple_of",
    }
    for source_key, target_key in mapping.items():
        if source_key in schema:
            constraints[target_key] = schema[source_key]
    if "enum" in schema and isinstance(schema["enum"], list):
        constraints["options"] = [_normalize_json_value(item) for item in schema["enum"]]
    if nullable:
        constraints["nullable"] = True
    return constraints


def _normalize_json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(by_alias=True)
    if isinstance(value, dict):
        return {str(key): _normalize_json_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_json_value(item) for item in value]
    if isinstance(value, tuple):
        return [_normalize_json_value(item) for item in value]
    if value is Ellipsis or value is inspect._empty:
        return None
    return value


def _field_default(model_field: Any) -> Any:
    default_factory = getattr(model_field, "default_factory", None)
    if callable(default_factory):
        return default_factory()
    default = getattr(model_field, "default", inspect._empty)
    if default is PydanticUndefined:
        return None
    return default


def _humanize(value: str) -> str:
    raw_parts = value.replace("-", "_").split("_")
    parts: list[str] = []
    for raw in raw_parts:
        if not raw:
            continue
        parts.extend(piece for piece in _CAMEL_CASE_BOUNDARY.sub("_", raw).split("_") if piece)
    words = []
    for part in parts:
        upper = part.upper()
        if upper in {"ID", "MCP", "LLM", "KB", "AGE"}:
            words.append(upper)
        else:
            words.append(part.capitalize())
    return " ".join(words)


__all__ = ["build_workflow_component_catalog"]
