"""Server-backed workflow creation templates for the Workflows gallery.

The create-new-workflow surface should not invent its own starter manifests.
This module keeps the gallery metadata and the initial canonical manifest
shapes together so the frontend can render cards and create drafts from one
backend-defined source of truth.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from functools import lru_cache
from typing import Any

from caliber.workflows.manifest import parse_manifest

WORKFLOW_TEMPLATE_ID_MARKER = "__CALIBER_WORKFLOW_ID__"
WORKFLOW_TEMPLATE_NAME_MARKER = "__CALIBER_WORKFLOW_NAME__"

_RUNTIME = {
    "sdk": "openai-agents-python",
    "sdk_version_policy": "runtime-pinned",
    "compiler_version": "caliber-workflow-compiler-v1",
    "default_model_ref": "CALIBER_WORKFLOW_DEFAULT_MODEL",
    "session": {"type": "none"},
}

_TEMPLATE_META: tuple[dict[str, str], ...] = (
    {
        "kind": "single_agent",
        "label": "Single Agent",
        "description": "One agent with tools and output.",
        "icon": "🤖",
        "gradient": "from-violet-500/10 to-caliber-500/10",
    },
    {
        "kind": "multi_agent_handoff",
        "label": "Multi-Agent Handoff",
        "description": "Coordinator agent delegates specialist work via handoff.",
        "icon": "🤝",
        "gradient": "from-fuchsia-500/10 to-rose-500/10",
    },
    {
        "kind": "guarded_pipeline",
        "label": "Guarded Pipeline",
        "description": "Agent → guardrail → output.",
        "icon": "🛡️",
        "gradient": "from-amber-500/10 to-orange-500/10",
    },
    {
        "kind": "parallel_fanout",
        "label": "Parallel Fan-Out",
        "description": "Fork work across two agents, then join the results.",
        "icon": "⚡",
        "gradient": "from-sky-500/10 to-indigo-500/10",
    },
    {
        "kind": "hitl_review",
        "label": "Human Review",
        "description": "Agent → PII redact → human approval → output.",
        "icon": "✋",
        "gradient": "from-emerald-500/10 to-teal-500/10",
    },
    {
        "kind": "for_each_loop",
        "label": "Batch Loop",
        "description": "Process a list of items through one reusable worker agent.",
        "icon": "🔁",
        "gradient": "from-cyan-500/10 to-teal-500/10",
    },
    {
        "kind": "refinement_loop",
        "label": "Refinement Loop",
        "description": "Iteratively improve one draft through the same worker agent.",
        "icon": "🌀",
        "gradient": "from-sky-500/10 to-emerald-500/10",
    },
    {
        "kind": "knowledge_rag",
        "label": "Knowledge Q&A",
        "description": "Start → knowledge query → output.",
        "icon": "📚",
        "gradient": "from-sky-500/10 to-cyan-500/10",
    },
    {
        "kind": "graph_hybrid_rag",
        "label": "GraphRAG Hybrid",
        "description": "Start → graph-hybrid knowledge query → output.",
        "icon": "🧠",
        "gradient": "from-cyan-500/10 to-emerald-500/10",
    },
    {
        "kind": "knowledge_age",
        "label": "AGE Graph Retrieval",
        "description": "Start → AGE-backed knowledge query → output.",
        "icon": "🕸️",
        "gradient": "from-emerald-500/10 to-blue-500/10",
    },
    {
        "kind": "knowledge_age_build",
        "label": "AGE Knowledge Build",
        "description": "Launch a graph-synced knowledge-base build for Apache AGE.",
        "icon": "🏗️",
        "gradient": "from-emerald-500/10 to-teal-500/10",
    },
    {
        "kind": "event_resume",
        "label": "Event Resume Gate",
        "description": "Pause for an external event, then continue with an agent.",
        "icon": "📨",
        "gradient": "from-amber-500/10 to-sky-500/10",
    },
    {
        "kind": "blank",
        "label": "Blank Canvas",
        "description": "Start from scratch.",
        "icon": "📄",
        "gradient": "from-slate-500/10 to-gray-500/10",
    },
)

_BAKEOFF_SCENARIOS: tuple[dict[str, Any], ...] = (
    {
        "id": "B1",
        "title": "Single-agent answer with tools",
        "starter_kind": "single_agent",
        "capabilities": ["agent execution", "tool wiring", "output inspection"],
        "evidence_to_capture": [
            "Time to first successful run",
            "Final output",
            "Step trace",
        ],
    },
    {
        "id": "B2",
        "title": "Multi-agent delegation",
        "starter_kind": "multi_agent_handoff",
        "capabilities": ["handoffs", "delegated terminal output", "run trace"],
        "evidence_to_capture": [
            "Handoff configuration effort",
            "Final delegated answer",
            "Node trace",
        ],
    },
    {
        "id": "B3",
        "title": "Human review gate",
        "starter_kind": "hitl_review",
        "capabilities": ["guardrails", "human approval", "pause and resume"],
        "evidence_to_capture": [
            "Approval UX",
            "Stored checkpoint",
            "Resume trace",
            "Post-approval output",
        ],
    },
    {
        "id": "B4",
        "title": "External event resume",
        "starter_kind": "event_resume",
        "capabilities": ["wait_for_event", "correlation", "long-running resume"],
        "evidence_to_capture": [
            "Paused state UX",
            "Resume-by-event path",
            "Recovery trail",
        ],
    },
    {
        "id": "B5",
        "title": "Parallel synthesis",
        "starter_kind": "parallel_fanout",
        "capabilities": ["parallel branches", "join", "merged result"],
        "evidence_to_capture": [
            "Branch visibility",
            "Join correctness",
            "Replay and debugger ergonomics",
        ],
    },
    {
        "id": "B6",
        "title": "Batch or iterative refinement",
        "starter_kind": "for_each_loop",
        "capabilities": ["loops", "repeated agent execution", "bounded iteration"],
        "evidence_to_capture": [
            "Iteration visibility",
            "Partial failure handling",
            "Output consistency",
        ],
    },
    {
        "id": "B7",
        "title": "GraphRAG hybrid query",
        "starter_kind": "graph_hybrid_rag",
        "capabilities": ["knowledge query", "graph-hybrid retrieval", "citations"],
        "evidence_to_capture": [
            "Retrieved chunks",
            "Graph context",
            "Citation traceability",
        ],
    },
    {
        "id": "B8",
        "title": "AGE-native graph build",
        "starter_kind": "knowledge_age_build",
        "capabilities": ["knowledge build", "graph extraction", "AGE sync config"],
        "evidence_to_capture": [
            "Build configuration effort",
            "Run logs",
            "Version metadata",
            "Graph sync readiness",
        ],
    },
    {
        "id": "B9",
        "title": "AGE graph retrieval",
        "starter_kind": "knowledge_age",
        "capabilities": ["Apache AGE retrieval mode", "graph-aware answer path"],
        "evidence_to_capture": [
            "Retrieval mode controls",
            "Graph evidence",
            "Fallback behavior",
        ],
    },
)

_OPERATOR_RUBRIC: tuple[dict[str, Any], ...] = (
    {
        "title": "Authoring friction",
        "checks": [
            "Time to create the workflow from a starter.",
            "Extra configuration needed before the first valid run.",
            "Whether missing setup appears as inline guidance or only as a runtime failure.",
        ],
    },
    {
        "title": "First-pass execution",
        "checks": [
            "Time to first successful run.",
            "Number of manual corrections before the workflow runs cleanly.",
            "Whether run inputs, outputs, and node-level state are inspectable without leaving the page.",
        ],
    },
    {
        "title": "Recovery and degraded-path handling",
        "checks": [
            "Whether paused runs expose a recoverable checkpoint trail.",
            "Whether retrieval or graph-sync fallbacks are visible to the operator.",
            "Whether retry, replay, or resume actions fail closed when state is incomplete or inconsistent.",
        ],
    },
    {
        "title": "Observability and evidence",
        "checks": [
            "Run history depth and searchability.",
            "Step trace clarity.",
            "Availability of final outputs, retrieved chunks, citations, and lineage metadata.",
        ],
    },
    {
        "title": "Reusability and deployment",
        "checks": [
            "Whether the workflow can be saved, versioned, exported, and rerun without re-authoring.",
            "Whether starter manifests can serve as reusable governed patterns instead of one-off demos.",
        ],
    },
)


def build_workflow_template_catalog() -> dict[str, Any]:
    """Return the workflow template catalog consumed by the Workflows page."""

    return _cached_template_catalog()


@lru_cache(maxsize=1)
def _cached_template_catalog() -> dict[str, Any]:
    templates: list[dict[str, Any]] = []
    for item in _TEMPLATE_META:
        kind = item["kind"]
        manifest = _template_manifest(kind)
        parse_manifest(manifest)
        templates.append(
            {
                **item,
                "manifest_template": manifest,
            }
        )
    return {
        "schema_version": 1,
        "templates": templates,
        "bakeoff_scenarios": list(_BAKEOFF_SCENARIOS),
        "operator_rubric": list(_OPERATOR_RUBRIC),
    }


def _base_manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow_id": WORKFLOW_TEMPLATE_ID_MARKER,
        "name": WORKFLOW_TEMPLATE_NAME_MARKER,
        "runtime": deepcopy(_RUNTIME),
        "nodes": {
            "start": {
                "id": "start",
                "type": "start",
                "outputs": {"user_message": {"type": "string"}},
            },
            "agent": {
                "id": "agent",
                "type": "agent",
                "name": "main-agent",
                "model": "inherit",
                "instructions": {
                    "type": "inline",
                    "text": "You are a helpful assistant.",
                },
                "tools": [],
                "inputs": {
                    "input": {"type": "string"},
                    "history": {"type": "structured"},
                },
                "outputs": {
                    "final_output": {"type": "string"},
                    "history": {"type": "structured"},
                },
            },
            "final": {
                "id": "final",
                "type": "output",
                "inputs": {"response": {"type": "string"}},
            },
        },
        "edges": [
            {
                "id": "e_start_agent",
                "from": "start",
                "to": "agent",
                "map": {"user_message": "input"},
            },
            {
                "id": "e_agent_final",
                "from": "agent",
                "to": "final",
                "map": {"final_output": "response"},
            },
        ],
        "tools": {},
    }


def _agent_node(*, node_id: str, name: str, instructions: str) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "agent",
        "name": name,
        "model": "inherit",
        "instructions": {
            "type": "inline",
            "text": instructions,
        },
        "tools": [],
        "inputs": {
            "input": {"type": "string"},
            "history": {"type": "structured"},
        },
        "outputs": {
            "final_output": {"type": "string"},
            "history": {"type": "structured"},
        },
    }


def _knowledge_query_manifest(*, retrieval_modes: list[str]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow_id": WORKFLOW_TEMPLATE_ID_MARKER,
        "name": WORKFLOW_TEMPLATE_NAME_MARKER,
        "runtime": deepcopy(_RUNTIME),
        "nodes": {
            "start": {
                "id": "start",
                "type": "start",
                "outputs": {"user_message": {"type": "string"}},
            },
            "knowledge": {
                "id": "knowledge",
                "type": "knowledge_query",
                "knowledge_base_id": "",
                "version_ids": [],
                "retrieval_modes": list(retrieval_modes),
                "top_k": 6,
                "chat_model": None,
                "graph_overrides": None,
                "inputs": {
                    "question": {"type": "string"},
                    "history": {"type": "structured"},
                    "retrieval_modes": {"type": "structured"},
                    "version_ids": {"type": "structured"},
                    "graph_overrides": {"type": "structured"},
                },
                "outputs": {
                    "text": {"type": "string"},
                    "answer": {"type": "string"},
                    "result": {"type": "structured"},
                    "citations": {"type": "structured"},
                    "chunks": {"type": "structured"},
                    "graph_context": {"type": "structured"},
                },
            },
            "final": {
                "id": "final",
                "type": "output",
                "inputs": {"response": {"type": "string"}},
            },
        },
        "edges": [
            {
                "id": "e_start_knowledge",
                "from": "start",
                "to": "knowledge",
                "map": {"user_message": "question"},
            },
            {
                "id": "e_knowledge_final",
                "from": "knowledge",
                "to": "final",
                "map": {"answer": "response"},
            },
        ],
        "tools": {},
    }


def _knowledge_age_build_manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow_id": WORKFLOW_TEMPLATE_ID_MARKER,
        "name": WORKFLOW_TEMPLATE_NAME_MARKER,
        "runtime": deepcopy(_RUNTIME),
        "nodes": {
            "start": {
                "id": "start",
                "type": "start",
                "outputs": {"user_message": {"type": "string"}},
            },
            "build_graph": {
                "id": "build_graph",
                "type": "knowledge_build",
                "knowledge_base_id": "",
                "chunking_strategy": "recursive",
                "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
                "chunking_config": {"chunk_size": 1200, "chunk_overlap": 180},
                "graph_config": {
                    "extractor_backend": "heuristic",
                    "spacy_model": None,
                    "max_entities_per_chunk": 12,
                    "entity_types": [],
                    "minimum_entity_mentions": 1,
                    "minimum_relationship_weight": 1.0,
                    "default_retrieval_mode": "age_graph",
                    "retrieval_strength": "balanced",
                    "output_target": "object_store_and_age",
                    "age_seed_mode": "entity_then_text",
                    "age_traversal_hops": 1,
                    "age_candidate_pool_size": 24,
                    "age_dense_rerank_weight": 0.35,
                    "strict_age_retrieval_default": False,
                },
                "activate_when_complete": True,
                "wait_for_completion": True,
                "wait_timeout_seconds": 900.0,
                "inputs": {
                    "input": {"type": "string"},
                    "sources": {"type": "structured"},
                    "chunking_strategy": {"type": "string"},
                    "embedding_model": {"type": "string"},
                    "chunking_config": {"type": "structured"},
                    "graph_config": {"type": "structured"},
                },
                "outputs": {
                    "text": {"type": "string"},
                    "result": {"type": "structured"},
                    "knowledge_base": {"type": "structured"},
                    "version": {"type": "structured"},
                    "run": {"type": "structured"},
                    "status": {"type": "string"},
                    "version_id": {"type": "string"},
                    "run_id": {"type": "string"},
                },
            },
            "final": {
                "id": "final",
                "type": "output",
                "inputs": {"response": {"type": "string"}},
            },
        },
        "edges": [
            {
                "id": "e_start_build",
                "from": "start",
                "to": "build_graph",
                "map": {"user_message": "input"},
            },
            {
                "id": "e_build_final",
                "from": "build_graph",
                "to": "final",
                "map": {"text": "response"},
            },
        ],
        "tools": {},
    }


def _event_resume_manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow_id": WORKFLOW_TEMPLATE_ID_MARKER,
        "name": WORKFLOW_TEMPLATE_NAME_MARKER,
        "runtime": deepcopy(_RUNTIME),
        "nodes": {
            "start": {
                "id": "start",
                "type": "start",
                "outputs": {"user_message": {"type": "string"}},
            },
            "wait_gate": {
                "id": "wait_gate",
                "type": "wait_for_event",
                "event_name": "documents.ready",
                "correlation_key": "document_id",
                "timeout_seconds": 3600,
                "inputs": {"input": {"type": "string"}},
                "outputs": {
                    "output": {"type": "string"},
                    "event_payload": {"type": "structured"},
                    "event_name": {"type": "string"},
                },
            },
            "agent": _agent_node(
                node_id="agent",
                name="release-agent",
                instructions=(
                    "Once the external readiness event arrives, summarize what changed, "
                    "what is ready now, and what the operator should do next."
                ),
            ),
            "final": {
                "id": "final",
                "type": "output",
                "inputs": {"response": {"type": "string"}},
            },
        },
        "edges": [
            {
                "id": "e_start_wait",
                "from": "start",
                "to": "wait_gate",
                "map": {"user_message": "input"},
            },
            {
                "id": "e_wait_agent",
                "from": "wait_gate",
                "to": "agent",
                "map": {"output": "input"},
            },
            {
                "id": "e_agent_final",
                "from": "agent",
                "to": "final",
                "map": {"final_output": "response"},
            },
        ],
        "tools": {},
    }


def _multi_agent_handoff_manifest() -> dict[str, Any]:
    manifest = _base_manifest()
    manifest["nodes"]["agent"] = _agent_node(
        node_id="agent",
        name="triage-agent",
        instructions=(
            "Handle general requests. When the request is about billing, "
            "invoices, or refunds, delegate it to the billing specialist."
        ),
    )
    manifest["nodes"]["agent"]["handoffs"] = [
        {
            "target": "billing",
            "description": "Handle billing, invoices, and refunds.",
            "condition": "'billing' in input or 'invoice' in input or 'refund' in input",
            "input_filter": (
                "Billing handoff\nCustomer request: {{input}}\nCoordinator draft: {{final_output}}"
            ),
        }
    ]
    manifest["nodes"]["billing"] = _agent_node(
        node_id="billing",
        name="billing-agent",
        instructions=("Resolve billing issues, refunds, invoices, and payment questions."),
    )
    return manifest


def _parallel_fanout_manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow_id": WORKFLOW_TEMPLATE_ID_MARKER,
        "name": WORKFLOW_TEMPLATE_NAME_MARKER,
        "runtime": deepcopy(_RUNTIME),
        "nodes": {
            "start": {
                "id": "start",
                "type": "start",
                "outputs": {"user_message": {"type": "string"}},
            },
            "parallel": {
                "id": "parallel",
                "type": "parallel",
                "inputs": {"input": {"type": "string"}},
                "outputs": {"output": {"type": "string"}},
            },
            "research": _agent_node(
                node_id="research",
                name="research-agent",
                instructions="Summarize the request from a research and evidence perspective.",
            ),
            "writer": _agent_node(
                node_id="writer",
                name="writer-agent",
                instructions="Draft a concise answer or action plan for the same request.",
            ),
            "join_all": {
                "id": "join_all",
                "type": "join",
                "mode": "all",
                "inputs": {
                    "research": {"type": "string"},
                    "draft": {"type": "string"},
                },
                "outputs": {
                    "output": {"type": "string"},
                    "merged": {"type": "structured"},
                },
            },
            "final": {
                "id": "final",
                "type": "output",
                "inputs": {"response": {"type": "string"}},
            },
        },
        "edges": [
            {
                "id": "e_start_parallel",
                "from": "start",
                "to": "parallel",
                "map": {"user_message": "input"},
            },
            {
                "id": "e_parallel_research",
                "from": "parallel",
                "to": "research",
                "map": {"output": "input"},
            },
            {
                "id": "e_parallel_writer",
                "from": "parallel",
                "to": "writer",
                "map": {"output": "input"},
            },
            {
                "id": "e_research_join",
                "from": "research",
                "to": "join_all",
                "map": {"final_output": "research"},
            },
            {
                "id": "e_writer_join",
                "from": "writer",
                "to": "join_all",
                "map": {"final_output": "draft"},
            },
            {
                "id": "e_join_final",
                "from": "join_all",
                "to": "final",
                "map": {"output": "response"},
            },
        ],
        "tools": {},
    }


def _for_each_loop_manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow_id": WORKFLOW_TEMPLATE_ID_MARKER,
        "name": WORKFLOW_TEMPLATE_NAME_MARKER,
        "runtime": deepcopy(_RUNTIME),
        "nodes": {
            "start": {
                "id": "start",
                "type": "start",
                "outputs": {"user_message": {"type": "string"}},
            },
            "for_each": {
                "id": "for_each",
                "type": "for_each",
                "target_node_id": "worker",
                "item_input_port": "items",
                "max_items": 100,
                "inputs": {"items": {"type": "structured"}},
                "outputs": {
                    "results": {"type": "structured"},
                    "text": {"type": "string"},
                    "metadata": {"type": "structured"},
                },
            },
            "worker": _agent_node(
                node_id="worker",
                name="item-worker",
                instructions=(
                    "Process one list item at a time. Return a short answer for "
                    "the current item only."
                ),
            ),
            "final": {
                "id": "final",
                "type": "output",
                "inputs": {"response": {"type": "string"}},
            },
        },
        "edges": [
            {
                "id": "e_start_loop",
                "from": "start",
                "to": "for_each",
                "map": {"user_message": "items"},
            },
            {
                "id": "e_loop_final",
                "from": "for_each",
                "to": "final",
                "map": {"text": "response"},
            },
        ],
        "tools": {},
    }


def _refinement_loop_manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow_id": WORKFLOW_TEMPLATE_ID_MARKER,
        "name": WORKFLOW_TEMPLATE_NAME_MARKER,
        "runtime": deepcopy(_RUNTIME),
        "nodes": {
            "start": {
                "id": "start",
                "type": "start",
                "outputs": {"user_message": {"type": "string"}},
            },
            "loop": {
                "id": "loop",
                "type": "loop",
                "target_node_id": "editor",
                "max_iterations": 3,
                "stop_condition": "iteration >= 2",
                "inputs": {
                    "input": {"type": "string"},
                    "state": {"type": "structured"},
                },
                "outputs": {
                    "output": {"type": "string"},
                    "result": {"type": "structured"},
                    "iterations": {"type": "structured"},
                    "metadata": {"type": "structured"},
                },
            },
            "editor": _agent_node(
                node_id="editor",
                name="editor-agent",
                instructions=(
                    "Refine the current draft once. Return only the improved "
                    "draft, without commentary."
                ),
            ),
            "final": {
                "id": "final",
                "type": "output",
                "inputs": {"response": {"type": "string"}},
            },
        },
        "edges": [
            {
                "id": "e_start_loop",
                "from": "start",
                "to": "loop",
                "map": {"user_message": "input"},
            },
            {
                "id": "e_loop_final",
                "from": "loop",
                "to": "final",
                "map": {"output": "response"},
            },
        ],
        "tools": {},
    }


def _blank_template_manifest() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "workflow_id": WORKFLOW_TEMPLATE_ID_MARKER,
        "name": WORKFLOW_TEMPLATE_NAME_MARKER,
        "runtime": deepcopy(_RUNTIME),
        "nodes": {
            "start": {
                "id": "start",
                "type": "start",
                "outputs": {"user_message": {"type": "string"}},
            },
            "final": {
                "id": "final",
                "type": "output",
                "inputs": {"response": {"type": "string"}},
            },
        },
        "edges": [],
        "tools": {},
    }


def _guarded_pipeline_manifest() -> dict[str, Any]:
    manifest = _base_manifest()
    manifest["nodes"]["guardrail"] = {
        "id": "guardrail",
        "type": "guardrail",
        "mode": "post_agent",
        "inputs": {"response": {"type": "string"}},
        "outputs": {"passthrough": {"type": "string"}},
        "on_failure": "block",
        "checks": [{"non_empty_output": {}}],
    }
    manifest["edges"] = [
        {
            "id": "e_start_agent",
            "from": "start",
            "to": "agent",
            "map": {"user_message": "input"},
        },
        {
            "id": "e_agent_guard",
            "from": "agent",
            "to": "guardrail",
            "map": {"final_output": "response"},
        },
        {
            "id": "e_guard_final",
            "from": "guardrail",
            "to": "final",
            "map": {"passthrough": "response"},
        },
    ]
    return manifest


def _hitl_review_manifest() -> dict[str, Any]:
    manifest = _base_manifest()
    manifest["nodes"]["pii_guard"] = {
        "id": "pii_guard",
        "type": "guardrail",
        "mode": "post_agent",
        "inputs": {"response": {"type": "string"}},
        "outputs": {"clean": {"type": "string"}},
        "on_failure": "redact",
        "checks": [
            {
                "pii_detection": {
                    "entities": ["email", "ssn", "phone", "credit_card"],
                }
            }
        ],
    }
    manifest["nodes"]["review"] = {
        "id": "review",
        "type": "human_approval",
        "inputs": {"response": {"type": "string"}},
        "outputs": {"approved": {"type": "string"}},
    }
    manifest["edges"] = [
        {
            "id": "e_start_agent",
            "from": "start",
            "to": "agent",
            "map": {"user_message": "input"},
        },
        {
            "id": "e_agent_guard",
            "from": "agent",
            "to": "pii_guard",
            "map": {"final_output": "response"},
        },
        {
            "id": "e_guard_review",
            "from": "pii_guard",
            "to": "review",
            "map": {"clean": "response"},
        },
        {
            "id": "e_review_final",
            "from": "review",
            "to": "final",
            "map": {"approved": "response"},
        },
    ]
    return manifest


_TEMPLATE_MANIFEST_BUILDERS: dict[str, Callable[[], dict[str, Any]]] = {
    "blank": _blank_template_manifest,
    "event_resume": _event_resume_manifest,
    "for_each_loop": _for_each_loop_manifest,
    "graph_hybrid_rag": lambda: _knowledge_query_manifest(retrieval_modes=["graph_hybrid"]),
    "guarded_pipeline": _guarded_pipeline_manifest,
    "hitl_review": _hitl_review_manifest,
    "knowledge_age": lambda: _knowledge_query_manifest(retrieval_modes=["age_graph"]),
    "knowledge_age_build": _knowledge_age_build_manifest,
    "knowledge_rag": lambda: _knowledge_query_manifest(retrieval_modes=[]),
    "multi_agent_handoff": _multi_agent_handoff_manifest,
    "parallel_fanout": _parallel_fanout_manifest,
    "refinement_loop": _refinement_loop_manifest,
}


def _template_manifest(kind: str) -> dict[str, Any]:
    builder = _TEMPLATE_MANIFEST_BUILDERS.get(kind)
    return builder() if builder is not None else _base_manifest()


__all__ = [
    "WORKFLOW_TEMPLATE_ID_MARKER",
    "WORKFLOW_TEMPLATE_NAME_MARKER",
    "build_workflow_template_catalog",
]
