"""Built-in, installable CALIBER Cookbook examples.

The documentation Cookbooks are product examples, not snippets that should
drift independently from the runtime.  This catalog gives every numbered
recipe a versioned, parseable workflow draft and enough metadata for the UI to
explain prerequisites before an operator installs it.

Installation is handled by :mod:`caliber.routes.cookbooks`; catalog builders
remain pure so they can be validated in tests and consumed by future CLI/SDK
surfaces without an application or database.
"""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from typing import Any

from caliber.workflows.manifest import parse_manifest
from caliber.workflows.template_catalog import (
    WORKFLOW_TEMPLATE_ID_MARKER,
    WORKFLOW_TEMPLATE_NAME_MARKER,
    build_workflow_template_catalog,
)

COOKBOOK_CATALOG_VERSION = "2026.08"

_RECIPES: tuple[dict[str, Any], ...] = (
    {
        "id": "01",
        "slug": "trustworthy-intake-classifier",
        "title": "Trustworthy Intake Classifier",
        "summary": "Classify intake requests with structured output, evaluation, and calibration evidence.",
        "icon": "📥",
        "template_kind": "guarded_pipeline",
        "capabilities": ["prompts", "structured output", "test sets", "calibration"],
        "prerequisites": ["Configured model provider for live inference"],
    },
    {
        "id": "02",
        "slug": "precision-skills",
        "title": "Precision Skills",
        "summary": "Author, render, trigger-test, package, calibrate, and bind a reusable skill.",
        "icon": "🧩",
        "template_kind": "single_agent",
        "capabilities": ["skills", "trigger tests", "packages", "calibration"],
        "prerequisites": [],
    },
    {
        "id": "03",
        "slug": "policy-safe-decision-tool",
        "title": "Policy-Safe Decision Tool",
        "summary": "Apply deterministic policy rules before a separately approved side effect.",
        "icon": "⚖️",
        "template_kind": "hitl_review",
        "capabilities": ["decision tables", "tools", "runtime approvals", "audit"],
        "prerequisites": ["Runtime approvals enabled for execution"],
    },
    {
        "id": "04",
        "slug": "document-to-json-pipeline",
        "title": "Document-to-JSON Pipeline",
        "summary": "Read a managed document, extract structured data, and validate its JSON Schema.",
        "icon": "📄",
        "template_kind": "guarded_pipeline",
        "capabilities": ["managed files", "structured output", "JSON Schema", "lineage"],
        "prerequisites": ["A supported document uploaded to a CALIBER project"],
    },
    {
        "id": "05",
        "slug": "governed-tool-connectivity",
        "title": "Governed Tool Connectivity",
        "summary": "Connect, discover, constrain, invoke, and calibrate an MCP tool.",
        "icon": "🔌",
        "template_kind": "single_agent",
        "capabilities": ["MCP", "tool policy", "secrets", "calibration"],
        "prerequisites": ["Reachable MCP server and any required secret references"],
    },
    {
        "id": "06",
        "slug": "grounded-knowledge-assistant",
        "title": "Grounded Knowledge Assistant",
        "summary": "Build a knowledge base, answer with citations, and route low-confidence responses.",
        "icon": "📚",
        "template_kind": "knowledge_rag",
        "capabilities": ["knowledge", "citations", "confidence", "review queues"],
        "prerequisites": ["Embedding and chat providers for live retrieval and answers"],
    },
    {
        "id": "07",
        "slug": "support-triage-copilot",
        "title": "Support Triage Copilot",
        "summary": "Ground support triage, route risk, and gate issue creation behind approval.",
        "icon": "🎧",
        "template_kind": "hitl_review",
        "capabilities": ["routing", "incident lookup", "issue creation", "evaluation"],
        "prerequisites": ["Configured incident source and issue tracker connector"],
    },
    {
        "id": "08",
        "slug": "incident-response-copilot",
        "title": "Incident Response Copilot",
        "summary": "Collect deployment and health evidence before proposing governed incident actions.",
        "icon": "🚨",
        "template_kind": "parallel_fanout",
        "capabilities": [
            "deployment health",
            "service health",
            "fact/hypothesis separation",
            "approval",
        ],
        "prerequisites": ["Configured deployment and service-health data sources"],
    },
    {
        "id": "09",
        "slug": "self-healing-workflows",
        "title": "Self-Healing Workflows",
        "summary": "Diagnose a failed run, recover from a checkpoint, patch configuration, and regress.",
        "icon": "🩹",
        "template_kind": "hitl_review",
        "capabilities": ["checkpoints", "recovery", "debugger", "versioning"],
        "prerequisites": ["Workflow worker when queued execution is enabled"],
    },
    {
        "id": "10",
        "slug": "trustworthy-evaluation",
        "title": "Trustworthy Evaluation",
        "summary": "Compare candidates, review difficult traces, and measure judge-human alignment.",
        "icon": "📊",
        "template_kind": "single_agent",
        "capabilities": ["test sets", "judges", "evaluations", "human alignment"],
        "prerequisites": ["Configured judge provider for model-backed grading"],
    },
    {
        "id": "11",
        "slug": "release-signoff-factory",
        "title": "Release Signoff Factory",
        "summary": "Assemble evidence, score release criteria, resolve blockers and waivers, and record signoff.",
        "icon": "🚀",
        "template_kind": "hitl_review",
        "capabilities": ["release candidates", "signoff", "waivers", "Allure evidence"],
        "prerequisites": ["Evaluation, review, and trace evidence for the release candidate"],
    },
    {
        "id": "12",
        "slug": "aria-evaluation-harness",
        "title": "Aria: Evaluation Harness from Intent",
        "summary": "Create a judge and Test Set through a typed, approval-aware Aria plan.",
        "icon": "🧪",
        "template_kind": "single_agent",
        "capabilities": ["Aria", "typed inputs", "judges", "test sets"],
        "prerequisites": [],
    },
    {
        "id": "13",
        "slug": "aria-review-governance-queue",
        "title": "Aria: Human-Review Queue from Intent",
        "summary": "Create and populate a governed review queue through an Aria plan.",
        "icon": "📝",
        "template_kind": "hitl_review",
        "capabilities": ["Aria", "review queues", "typed inputs", "dependency wiring"],
        "prerequisites": ["Trace IDs only when the example queue should be populated"],
    },
    {
        "id": "14",
        "slug": "aria-governance-starter-kit",
        "title": "Aria: Governance Starter Kit from Intent",
        "summary": "Create a judge, Test Set, and review queue as one governed plan.",
        "icon": "🛡️",
        "template_kind": "guarded_pipeline",
        "capabilities": ["Aria", "judges", "test sets", "review queues"],
        "prerequisites": [],
    },
    {
        "id": "15",
        "slug": "aria-triage-recalibrate-loop",
        "title": "Aria: Triage & Recalibrate Loop",
        "summary": "Triage flagged traces and launch a calibration job from a typed plan.",
        "icon": "🔄",
        "template_kind": "refinement_loop",
        "capabilities": ["Aria", "review queues", "calibration", "job polling"],
        "prerequisites": ["Existing workflow, agent, traces, provider, and refinement worker"],
    },
    {
        "id": "16",
        "slug": "production-observability-triage",
        "title": "Production Observability & Triage",
        "summary": "Capture production failures, review root cause, and retain regression evidence.",
        "icon": "🔭",
        "template_kind": "event_resume",
        "capabilities": ["observability", "test sets", "review queues", "evaluation"],
        "prerequisites": ["Existing successful and failed traces"],
    },
)


def _workflow_template(kind: str) -> dict[str, Any]:
    catalog = build_workflow_template_catalog()
    return deepcopy(
        next(item["manifest_template"] for item in catalog["templates"] if item["kind"] == kind)
    )


def _data_transform_node(
    node_id: str,
    *,
    operation: str,
    config: dict[str, Any],
    fail_on_invalid: bool = True,
) -> dict[str, Any]:
    return {
        "id": node_id,
        "type": "data_transform",
        "operation": operation,
        "config": config,
        "fail_on_invalid": fail_on_invalid,
        "inputs": {
            "value": {"type": "structured"},
            "text": {"type": "string"},
        },
        "outputs": {
            "text": {"type": "string"},
            "result": {"type": "structured"},
            "valid": {"type": "boolean"},
            "metadata": {"type": "structured"},
        },
    }


def _operational_connector_node(
    node_id: str,
    *,
    url: str,
    label: str,
) -> dict[str, Any]:
    """Return a credential-free API Request starter for a live data source.

    Deliberately invalid ``example.invalid`` hosts prevent an unreviewed draft
    from contacting a real system.  Operators replace the uppercase segments,
    configure egress, and use governed MCP bindings when authentication is
    required.  The paired fixture node keeps the installed Cookbook runnable
    without pretending that external connectivity was verified.
    """

    return {
        "id": node_id,
        "type": "api_request",
        "label": label,
        "mode": "url",
        "url": url,
        "method": "GET",
        "curl": "",
        "headers": {"Accept": "application/json"},
        "body": "",
        "timeout_seconds": 30,
        "inputs": {
            "payload": {"type": "structured"},
            "input": {"type": "string"},
        },
        "outputs": {
            "text": {"type": "string"},
            "response": {"type": "structured"},
            "metadata": {"type": "structured"},
        },
    }


def _replace_edge(
    manifest: dict[str, Any],
    edge_id: str,
    replacements: list[dict[str, Any]],
) -> None:
    edges = manifest.get("edges", [])
    index = next(index for index, edge in enumerate(edges) if edge["id"] == edge_id)
    edges[index : index + 1] = replacements


def _specialize_manifest(recipe_id: str, manifest: dict[str, Any]) -> None:
    """Make capability-focused Cookbooks exercise the real runtime component."""

    nodes = manifest["nodes"]
    if recipe_id == "03":
        nodes["policy_decision"] = _data_transform_node(
            "policy_decision",
            operation="decision_table",
            config={
                "rules": [
                    {
                        "name": "high-value-or-restricted",
                        "when": [
                            {
                                "path": "amount",
                                "operator": "greater_or_equal",
                                "value": 10_000,
                            }
                        ],
                        "result": "human_approval_required",
                    },
                    {
                        "name": "restricted-region",
                        "when": [{"path": "region", "operator": "in", "value": ["restricted"]}],
                        "result": "human_approval_required",
                    },
                ],
                "default": "policy_check_passed",
            },
        )
        _replace_edge(
            manifest,
            "e_start_agent",
            [
                {
                    "id": "e_start_policy",
                    "from": "start",
                    "to": "policy_decision",
                    "map": {"user_message": "text"},
                },
                {
                    "id": "e_policy_agent",
                    "from": "policy_decision",
                    "to": "agent",
                    "map": {"text": "input"},
                },
            ],
        )
    elif recipe_id == "04":
        nodes["validate_json"] = _data_transform_node(
            "validate_json",
            operation="json_schema",
            config={
                "schema": {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "type": "object",
                    "properties": {
                        "document_type": {"type": "string"},
                        "fields": {"type": "object"},
                        "source_file": {"type": "string"},
                    },
                    "required": ["document_type", "fields", "source_file"],
                    "additionalProperties": False,
                }
            },
        )
        _replace_edge(
            manifest,
            "e_agent_guard",
            [
                {
                    "id": "e_agent_schema",
                    "from": "agent",
                    "to": "validate_json",
                    "map": {"final_output": "text"},
                },
                {
                    "id": "e_schema_guard",
                    "from": "validate_json",
                    "to": "guardrail",
                    "map": {"text": "response"},
                },
            ],
        )
    elif recipe_id == "06":
        nodes["confidence"] = _data_transform_node(
            "confidence",
            operation="confidence",
            config={
                "bias": 0.1,
                "review_threshold": 0.65,
                "signals": [
                    {
                        "name": "has-citations",
                        "path": "citations",
                        "operator": "exists",
                        "value": True,
                        "weight": 0.55,
                    },
                    {
                        "name": "multiple-sources",
                        "path": "citation_count",
                        "operator": "greater_or_equal",
                        "value": 2,
                        "weight": 0.25,
                    },
                ],
            },
        )
        manifest["edges"].append(
            {
                "id": "e_knowledge_confidence",
                "from": "knowledge",
                "to": "confidence",
                "map": {"result": "value"},
            }
        )
    elif recipe_id == "07":
        nodes["incident_snapshot"] = _data_transform_node(
            "incident_snapshot",
            operation="fixture",
            config={
                "fixture": {
                    "source": "built-in-incident-fixture",
                    "incident_id": "INC-EXAMPLE-001",
                    "severity": "sev2",
                    "status": "investigating",
                    "summary": "Checkout latency exceeds the objective.",
                }
            },
        )
        nodes["incident_connector"] = _operational_connector_node(
            "incident_connector",
            label="Live incident / issue connector (configure before use)",
            url=("https://api.github.com/repos/OWNER/REPOSITORY/issues/ISSUE_NUMBER"),
        )
        _replace_edge(
            manifest,
            "e_start_agent",
            [
                {
                    "id": "e_start_incident",
                    "from": "start",
                    "to": "incident_snapshot",
                    "map": {"user_message": "text"},
                },
                {
                    "id": "e_incident_agent",
                    "from": "incident_snapshot",
                    "to": "agent",
                    "map": {"text": "input"},
                },
            ],
        )
    elif recipe_id == "08":
        nodes["research"] = _data_transform_node(
            "research",
            operation="fixture",
            config={
                "fixture": {
                    "source": "deployment-health-placeholder",
                    "status": "requires-connector-binding",
                    "facts": [],
                }
            },
        )
        nodes["writer"] = _data_transform_node(
            "writer",
            operation="fixture",
            config={
                "fixture": {
                    "source": "service-health-placeholder",
                    "status": "requires-connector-binding",
                    "facts": [],
                }
            },
        )
        nodes["deployment_connector"] = _operational_connector_node(
            "deployment_connector",
            label="Live deployment-health connector (configure before use)",
            url=("https://DEPLOYMENT_API.example.invalid/v1/deployments/DEPLOYMENT/health"),
        )
        nodes["service_health_connector"] = _operational_connector_node(
            "service_health_connector",
            label="Live service-health connector (configure before use)",
            url=("https://SERVICE_HEALTH.example.invalid/v1/services/SERVICE/health"),
        )
        for edge in manifest["edges"]:
            if edge["id"] in {"e_parallel_research", "e_parallel_writer"}:
                edge["map"] = {"output": "text"}
            elif edge["id"] == "e_research_join":
                edge["map"] = {"text": "research"}
            elif edge["id"] == "e_writer_join":
                edge["map"] = {"text": "draft"}


def _annotate_manifest(recipe: dict[str, Any]) -> dict[str, Any]:
    manifest = _workflow_template(str(recipe["template_kind"]))
    _specialize_manifest(str(recipe["id"]), manifest)
    nodes = manifest.setdefault("nodes", {})
    nodes["cookbook_guide"] = {
        "id": "cookbook_guide",
        "type": "note",
        "label": f"Cookbook {recipe['id']}",
        "text": (
            f"# {recipe['title']}\n\n{recipe['summary']}\n\n"
            "This built-in example is installed as a paused workflow and draft version. "
            "Review every binding and prerequisite before publishing or deploying it."
        ),
    }
    manifest["name"] = WORKFLOW_TEMPLATE_NAME_MARKER
    manifest["workflow_id"] = WORKFLOW_TEMPLATE_ID_MARKER
    manifest["cookbook"] = {
        "id": recipe["id"],
        "slug": recipe["slug"],
        "catalog_version": COOKBOOK_CATALOG_VERSION,
        "capabilities": list(recipe["capabilities"]),
        "prerequisites": list(recipe["prerequisites"]),
        "activation_requires_review": True,
    }
    # ``cookbook`` is product metadata, not part of the strict runtime manifest.
    # Keep it in the note for now and remove the envelope field before parsing.
    runtime_manifest = deepcopy(manifest)
    runtime_manifest.pop("cookbook", None)
    parse_manifest(runtime_manifest)
    return manifest


# ---------------------------------------------------------------------------
# Guided steps
#
# The recipe prose lives in ``docs-site/cookbooks/<slug>/README.md``. These are
# the same steps in addressable form: a stable id the UI can record progress
# against, a title, and the in-app route the step is performed on. The README
# stays the deep reference — it carries the field values, API fallbacks, and
# substitution notes that do not belong in a checklist.
#
# Authored rather than parsed, for two reasons the source makes obvious. The
# READMEs have three different shapes, not one: 01-10 and 12-15 use bold-titled
# numbered steps, 11 embeds its titles in prose, and 16 has no ``## Recipe``
# section at all. And a parsed step could not carry a route, which is the part
# that makes the checklist navigable.
#
# ``test_cookbook_steps_contract.py`` pins these against the READMEs so the two
# cannot drift apart silently.
# ---------------------------------------------------------------------------

#: Recipes whose README has no ``## Recipe`` section, so step count cannot be
#: checked against a numbered list. Named rather than skipped, so that a README
#: gaining one is a test failure rather than a silent gap in coverage.
RECIPES_WITHOUT_NUMBERED_RECIPE_SECTION = frozenset({"16"})

_RECIPE_STEPS: dict[str, tuple[dict[str, str], ...]] = {
    "01": (
        {"title": "Create the prompt", "route": "/prompts"},
        {"title": "Author", "route": "/prompts"},
        {"title": "Playground (live sanity check)", "route": "/prompts"},
        {"title": "Build & run the test set", "route": "/eval-datasets"},
        {"title": "(Optional) JSON/intent judge", "route": "/judges"},
        {"title": "Run the baseline", "route": "/prompts"},
        {"title": "Introduce a regression", "route": "/prompts"},
        {"title": "Compare", "route": "/prompts"},
        {"title": "Calibration (queued)", "route": "/prompts"},
        {"title": "Observe", "route": "/observability"},
    ),
    "02": (
        {"title": "Create the skill", "route": "/skills"},
        {"title": "Render Preview", "route": "/skills"},
        {"title": "Trigger Tests (positive)", "route": "/skills"},
        {"title": "Tighten + baseline", "route": "/skills"},
        {"title": "Export package", "route": "/skills"},
        {"title": "Import round-trip", "route": "/skills"},
        {"title": "Calibrate (queued)", "route": "/skills"},
        {"title": "Bind", "route": "/workflows"},
    ),
    "03": (
        {"title": "Register the lookup tools", "route": "/tools"},
        {"title": "Sandbox", "route": "/tools"},
        {"title": "Fixtures", "route": "/tools"},
        {"title": "Hardening (deterministic, inline)", "route": "/tools"},
        {"title": "The decision logic", "route": "/tools"},
        {"title": "Bind into a workflow with an approval gate", "route": "/workflows"},
        {"title": "Prove the gate in the run monitor", "route": "/workflows"},
        {"title": "Optional explanation", "route": "/prompts"},
    ),
    "04": (
        {"title": "Create a bucket + upload", "route": "/object-store"},
        {"title": "Verify extraction (preview)", "route": "/object-store"},
        {"title": "Register the extractor tool (optional)", "route": "/tools"},
        {"title": "Author the structuring prompt", "route": "/prompts"},
        {"title": "Build the workflow", "route": "/workflows"},
        {"title": "Preview + real runs", "route": "/workflows"},
        {"title": "Observe failures", "route": "/observability"},
        {"title": "Tune", "route": "/prompts"},
    ),
    "05": (
        {"title": "Quick-connect", "route": "/mcp-servers"},
        {"title": "Test connection + discovery", "route": "/mcp-servers"},
        {"title": "Invoke a read-only tool", "route": "/mcp-servers"},
        {"title": "Apply a policy overlay", "route": "/mcp-servers"},
        {"title": "Re-invoke the governed tool", "route": "/mcp-servers"},
        {"title": "Calibrate the read tool", "route": "/mcp-servers"},
        {"title": "Collect evidence", "route": "/observability"},
    ),
    "06": (
        {"title": "Build a KB version", "route": "/knowledge-bases"},
        {"title": "Explore → Chunks", "route": "/knowledge-bases"},
        {"title": "Explore → Query (mode comparison)", "route": "/knowledge-bases"},
        {"title": "Calibrate (inline metrics)", "route": "/knowledge-bases"},
        {"title": "Build the answer workflow", "route": "/workflows"},
        {"title": "Run scenario queries", "route": "/workflows"},
        {"title": "Evaluate faithfulness", "route": "/evaluations"},
        {"title": "Route to review", "route": "/review-queues"},
    ),
    "07": (
        {"title": "Confirm reusable assets", "route": "/tools"},
        {"title": "Build the workflow", "route": "/workflows"},
        {"title": "Encode the four outcomes", "route": "/workflows"},
        {"title": "Run the ticket cases", "route": "/workflows"},
        {"title": "Verify the safety branches", "route": "/workflows"},
        {"title": "Evaluate", "route": "/evaluations"},
        {"title": "Iterate", "route": "/prompts"},
    ),
    "08": (
        {"title": "Author the commander prompt", "route": "/prompts"},
        {"title": "Evidence nodes", "route": "/workflows"},
        {"title": "Build the workflow", "route": "/workflows"},
        {"title": "Run low-risk + high-risk cases", "route": "/workflows"},
        {"title": "Verify evidence separation", "route": "/workflows"},
        {"title": "Score + review", "route": "/evaluations"},
        {"title": "Harden", "route": "/workflows"},
    ),
    "09": (
        {"title": "Reproduce the failure", "route": "/workflows"},
        {"title": "Capture the run + open diagnostics", "route": "/workflows"},
        {"title": "Localize the root cause", "route": "/observability"},
        {"title": "Retry from checkpoint / lineage", "route": "/workflows"},
        {"title": "Apply a minimal patch (manual)", "route": "/workflows"},
        {"title": "Validate", "route": "/workflows"},
        {"title": "Compare", "route": "/workflows"},
    ),
    "10": (
        {"title": "Prepare the dataset", "route": "/eval-datasets"},
        {"title": "Define the judge", "route": "/judges"},
        {"title": "Run baseline + candidate", "route": "/evaluations"},
        {"title": "Inspect examples", "route": "/evaluations"},
        {"title": "Human review", "route": "/review-queues"},
        {"title": "Compute alignment", "route": "/judges"},
    ),
    "11": (
        {"title": "Open the Release Signoff Factory", "route": "/releases"},
        {"title": "Enter the candidate and artifact reference", "route": "/releases"},
        {"title": "Add criteria JSON with weights and observed scores", "route": "/releases"},
        {"title": "Create the candidate and inspect the weighted score", "route": "/releases"},
        {"title": "Re-evaluate after evidence changes", "route": "/releases"},
        {"title": "Record a criterion waiver if policy permits", "route": "/releases"},
        {"title": "Generate Allure evidence", "route": "/releases"},
        {"title": "Sign go or no-go with rationale", "route": "/releases"},
    ),
    "12": (
        {"title": "State the intent", "route": "/aria/plans"},
        {"title": "Review the plan", "route": "/aria/plans"},
        {"title": "Approve", "route": "/aria/plans"},
        {"title": "Execute the plan + create the artifacts", "route": "/aria/plans"},
        {"title": "Verify", "route": "/evaluations"},
        {"title": "(Follow-up)", "route": "/aria/plans"},
    ),
    "13": (
        {"title": "State the intent", "route": "/aria/plans"},
        {"title": "Review the plan", "route": "/aria/plans"},
        {"title": "Approve", "route": "/aria/plans"},
        {"title": "Execute the plan + create the queue", "route": "/aria/plans"},
        {"title": "Verify", "route": "/review-queues"},
        {"title": "(Follow-up)", "route": "/aria/plans"},
    ),
    "14": (
        {"title": "State the intent", "route": "/aria/plans"},
        {"title": "Review the plan", "route": "/aria/plans"},
        {"title": "Approve", "route": "/aria/plans"},
        {"title": "Execute the plan + create the three artifacts", "route": "/aria/plans"},
        {"title": "Verify", "route": "/review-queues"},
        {"title": "(Follow-up)", "route": "/aria/plans"},
    ),
    "15": (
        {"title": "State the intent", "route": "/aria/plans"},
        {"title": "Review the plan", "route": "/aria/plans"},
        {"title": "Approve", "route": "/aria/plans"},
        {"title": "Execute the plan + drive each step via its route", "route": "/aria/plans"},
        {"title": "Park + poll the async job", "route": "/aria/plans"},
        {"title": "Verify", "route": "/review-queues"},
        {"title": "(Follow-up)", "route": "/aria/plans"},
    ),
    # 16 has no ``## Recipe`` section. Its steps are derived from the four
    # quality gates its README does define, which are the checkable outcomes.
    "16": (
        {"title": "Capture every error trace in the window", "route": "/observability"},
        {"title": "Explain each flagged trace from its node tree", "route": "/observability"},
        {"title": "Label the triage queue and write assessments back", "route": "/review-queues"},
        {"title": "Record the prod-regression baseline", "route": "/evaluations"},
    ),
}


def _steps_for(recipe_id: str) -> list[dict[str, str]]:
    """Steps for one recipe, with ids assigned from position.

    Ids are derived (``01.1``, ``01.2``, ...) rather than hand-written so a step
    inserted in the middle cannot silently reuse an id that stored progress
    already points at.
    """
    return [
        {"id": f"{recipe_id}.{index}", **step}
        for index, step in enumerate(_RECIPE_STEPS.get(recipe_id, ()), start=1)
    ]


@lru_cache(maxsize=1)
def build_cookbook_catalog() -> dict[str, Any]:
    """Return all built-in Cookbooks with validated draft manifests."""

    recipes = []
    for raw in _RECIPES:
        recipe = deepcopy(raw)
        recipe["catalog_version"] = COOKBOOK_CATALOG_VERSION
        recipe["activation_requires_review"] = True
        recipe["steps"] = _steps_for(str(raw["id"]))
        recipe["manifest_template"] = _annotate_manifest(recipe)
        recipes.append(recipe)
    return {
        "schema_version": 1,
        "catalog_version": COOKBOOK_CATALOG_VERSION,
        "recipes": recipes,
    }


def materialize_cookbook_manifest(
    cookbook_id: str,
    *,
    workflow_id: str,
    workflow_name: str,
) -> dict[str, Any]:
    """Materialize one recipe's strict runtime manifest."""

    recipe = next(
        (item for item in build_cookbook_catalog()["recipes"] if item["id"] == cookbook_id),
        None,
    )
    if recipe is None:
        raise KeyError(cookbook_id)

    def replace(value: Any) -> Any:
        if value == WORKFLOW_TEMPLATE_ID_MARKER:
            return workflow_id
        if value == WORKFLOW_TEMPLATE_NAME_MARKER:
            return workflow_name
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, dict):
            return {key: replace(item) for key, item in value.items() if key != "cookbook"}
        return value

    manifest = replace(recipe["manifest_template"])
    assert isinstance(manifest, dict)
    parse_manifest(manifest)
    return manifest


__all__ = [
    "COOKBOOK_CATALOG_VERSION",
    "build_cookbook_catalog",
    "materialize_cookbook_manifest",
]
