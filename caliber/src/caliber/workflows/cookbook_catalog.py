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

from caliber.workflows.cookbook_document_pipeline import document_pipeline_graph
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
        "prerequisites": ["Configured model provider to score a prompt on the Evaluations page"],
    },
    {
        "id": "02",
        "slug": "precision-skills",
        "title": "Precision Skills",
        "summary": "Author a skill, test when it triggers, tighten it, calibrate and apply a candidate, roll back, and package it.",
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
        "summary": (
            "Read a text document from an object-store bucket, extract and validate invoice JSON, "
            "write it to an output bucket, and hold invalid documents for approval."
        ),
        "icon": "📄",
        "template_kind": "guarded_pipeline",
        "capabilities": ["object store", "JSON Schema", "run debugger", "approval"],
        "prerequisites": ["Invoice text documents uploaded to the doc-intake bucket"],
    },
    {
        "id": "05",
        "slug": "governed-tool-connectivity",
        "title": "Governed Tool Connectivity",
        "summary": "Connect an MCP server, classify its tools before use, prove a refused call never runs, calibrate the read tools, and gate a write behind approval.",
        "icon": "🔌",
        "template_kind": "single_agent",
        "capabilities": ["MCP", "tool policy", "approval gate", "calibration"],
        "prerequisites": [
            "A local MCP server CALIBER may run over stdio (the lab ships one) and the integrations preview flag"
        ],
    },
    {
        "id": "06",
        "slug": "grounded-knowledge-assistant",
        "title": "Grounded Knowledge Assistant",
        "summary": "Build a knowledge base, answer with citations, and route low-confidence responses.",
        "icon": "📚",
        "template_kind": "knowledge_rag",
        "capabilities": ["knowledge", "citations", "confidence", "review queues"],
        "prerequisites": [
            "An S3-compatible object store and the local embedding model "
            "(answer text and judged metrics are simulated without a chat provider)"
        ],
    },
    {
        "id": "07",
        "slug": "support-triage-copilot",
        "title": "Support Triage Copilot",
        "summary": "Ground support triage, route risk, and gate issue creation behind approval.",
        "icon": "🎧",
        "template_kind": "hitl_review",
        "capabilities": ["routing", "incident lookup", "issue creation", "evaluation"],
        "prerequisites": [
            "A workflow worker with the run queue enabled, and a second account holding the approver scope"
        ],
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
        "prerequisites": [
            "A workflow worker with the run queue enabled, a second approver, and the production deploy-gate policy variables from the README"
        ],
    },
    {
        "id": "09",
        "slug": "self-healing-workflows",
        "title": "Self-Healing Workflows",
        "summary": (
            "Find why a workflow fails on bad inputs, patch it as a new version, "
            "prove the fix on the same inputs, and roll an alias back."
        ),
        "icon": "🩹",
        "template_kind": "hitl_review",
        "capabilities": ["run debugger", "retry lineage", "versioning", "aliases"],
        "prerequisites": [
            "A workflow worker for queued runs, and CALIBER_EGRESS_ALLOWED_HOSTS naming the stand-in account service"
        ],
    },
    {
        "id": "10",
        "slug": "trustworthy-evaluation",
        "title": "Trustworthy Evaluation",
        "summary": "Compare candidates, review difficult traces, and measure judge-human alignment.",
        "icon": "📊",
        "template_kind": "single_agent",
        "capabilities": ["test sets", "judges", "evaluations", "human alignment"],
        "prerequisites": [
            "A real LLM provider with CALIBER_EVAL_PROVIDER=mlflow for judges and evaluations (the simulated default refuses them)"
        ],
    },
    {
        "id": "11",
        "slug": "release-signoff-factory",
        "title": "Release Signoff Factory",
        "summary": "Score release criteria on the server, resolve blockers with accountable waivers, and record an immutable go/no-go (record-only: nothing is deployed).",
        "icon": "🚀",
        "template_kind": "hitl_review",
        "capabilities": ["release candidates", "signoff", "waivers", "Allure evidence"],
        "prerequisites": [
            "Scores and evidence references to enter (CALIBER does not read or verify them) and the release-candidates preview flag"
        ],
    },
    {
        "id": "12",
        "slug": "aria-evaluation-harness",
        "title": "Aria: Evaluation Harness from Intent",
        "summary": "Create a judge and Test Set through a typed, approval-aware Aria plan, then fill the Test Set.",
        "icon": "🧪",
        "template_kind": "single_agent",
        "capabilities": ["Aria", "typed inputs", "judges", "test sets"],
        "prerequisites": [
            "Aria enabled (CALIBER_FEATURE_ARIA=true); a model provider only to run the judge in Evaluations"
        ],
    },
    {
        "id": "13",
        "slug": "aria-review-governance-queue",
        "title": "Aria: Human-Review Queue from Intent",
        "summary": "Create and populate a governed review queue through an Aria plan, then review traces.",
        "icon": "📝",
        "template_kind": "hitl_review",
        "capabilities": ["Aria", "review queues", "typed inputs", "dependency wiring"],
        "prerequisites": [
            "Aria enabled (CALIBER_FEATURE_ARIA=true); trace ids only when the queue should be populated"
        ],
    },
    {
        "id": "14",
        "slug": "aria-governance-starter-kit",
        "title": "Aria: Governance Starter Kit from Intent",
        "summary": "Create a judge, Test Set, and review queue as one governed plan, at two autonomy levels.",
        "icon": "🛡️",
        "template_kind": "guarded_pipeline",
        "capabilities": ["Aria", "judges", "test sets", "review queues"],
        "prerequisites": [
            "Aria enabled (CALIBER_FEATURE_ARIA=true); a model provider only to run the judge in Evaluations"
        ],
    },
    {
        "id": "15",
        "slug": "aria-triage-recalibrate-loop",
        "title": "Aria: Triage & Recalibrate Loop",
        "summary": "Triage flagged traces and launch a calibration job from a typed plan; the plan parks on the job and settles by itself.",
        "icon": "🔄",
        "template_kind": "refinement_loop",
        "capabilities": ["Aria", "review queues", "calibration", "job polling"],
        "prerequisites": [
            "Aria enabled (CALIBER_FEATURE_ARIA=true), background workers on, and an existing workflow with a deploy-gate test set, a registered agent and flagged traces; a model provider for a calibration to find a candidate"
        ],
    },
    {
        "id": "16",
        "slug": "production-observability-triage",
        "title": "Production Observability & Triage",
        "summary": "Capture production failures, review root cause, and retain regression evidence.",
        "icon": "🔭",
        "template_kind": "event_resume",
        "capabilities": ["observability", "test sets", "review queues", "evaluation"],
        "prerequisites": ["A workflow that is running and recording traces (the lab imports one)"],
    },
    # Numbered 18, not 17: ``cookbook_17_financial_analysis`` is a standalone SDK automation
    # with no catalog entry or documentation lab.
    {
        "id": "18",
        "slug": "projects-roles-tokens-audit",
        "title": "Projects, Roles, Tokens & Audit",
        "summary": (
            "Set up a team in a project with owner, editor, reviewer and viewer roles, bind an "
            "access token to it, and read the audit trail."
        ),
        "icon": "🔐",
        "template_kind": "hitl_review",
        "capabilities": ["projects", "roles", "access tokens", "audit log", "readiness"],
        "prerequisites": [
            "Accounts for the team, with the operator and approver platform scopes set for them",
        ],
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
        # A real intake pipeline (no agent, no model): see ``cookbook_document_pipeline``.
        nodes.clear()
        pipeline_nodes, pipeline_edges = document_pipeline_graph()
        nodes.update(pipeline_nodes)
        manifest["edges"] = pipeline_edges
    elif recipe_id == "06":
        nodes["confidence"] = _data_transform_node(
            "confidence",
            operation="confidence",
            config={
                "bias": 0.1,
                "review_threshold": 0.65,
                "signals": [
                    # The ``confidence`` node reads the whole ``knowledge_query`` result, which
                    # groups its citations under ``versions`` (one entry per queried version).
                    {
                        "name": "has-citations",
                        "path": "versions.0.citations.0",
                        "operator": "exists",
                        "value": True,
                        "weight": 0.55,
                    },
                    {
                        "name": "multiple-sources",
                        "path": "versions.0.citations.1",
                        "operator": "exists",
                        "value": True,
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
RECIPES_WITHOUT_NUMBERED_RECIPE_SECTION = frozenset({"18"})

_RECIPE_STEPS: dict[str, tuple[dict[str, str], ...]] = {
    "01": (
        {"title": "Create the prompt", "route": "/prompts"},
        {"title": "Save the weaker v2 as a draft", "route": "/prompts"},
        {"title": "Import the test set from the file", "route": "/prompts"},
        {"title": "Check the imported test set", "route": "/eval-datasets"},
        {"title": "Evaluate a prompt version", "route": "/evaluations"},
        {"title": "See the regression against the baseline", "route": "/evaluations"},
        {"title": "Calibrate and wait for the candidate", "route": "/prompts"},
        {"title": "Review the evidence and the diff, then Apply", "route": "/prompts"},
        {"title": "Verify the release on Releases", "route": "/releases"},
        {"title": "Verify the audit log", "route": "/audit-log"},
        {"title": "Roll back", "route": "/prompts"},
    ),
    "02": (
        {"title": "Open the gallery and create a workspace", "route": "/cookbooks"},
        {"title": "Build the skill", "route": "/skills"},
        {"title": "Render Preview", "route": "/skills"},
        {"title": "Check selection: positive, negative, near miss", "route": "/skills"},
        {"title": "Run the scenarios: round 1", "route": "/skills"},
        {"title": "Tighten the trigger phrases", "route": "/skills"},
        {"title": "Run round 2 and pin the baseline", "route": "/skills"},
        {"title": "Calibrate and review the candidate", "route": "/skills"},
        {"title": "Apply, then roll back", "route": "/skills"},
        {"title": "Export and re-import the package", "route": "/skills"},
        {"title": "Failure paths", "route": "/skills"},
        {"title": "Archive", "route": "/skills"},
    ),
    "03": (
        {"title": "Register the decision and refund tools", "route": "/tools"},
        {"title": "Sandbox", "route": "/tools"},
        {"title": "Fixtures", "route": "/tools"},
        {"title": "Hardening (run the suite)", "route": "/tools"},
        {"title": "Baseline and a regression", "route": "/tools"},
        {"title": "Import the workflow", "route": "/workflows"},
        {"title": "Approve as a second person", "route": "/workflows"},
        {"title": "Reject, deny and read the audit log", "route": "/audit-log"},
        {"title": "Make a new version of the tool", "route": "/tools"},
    ),
    "04": (
        {
            "title": "Create the reviewer account, the workspace and the membership",
            "route": "/administration",
        },
        {"title": "Create the two buckets", "route": "/object-store"},
        {"title": "Upload the sample invoices", "route": "/object-store"},
        {"title": "Preview a stored document", "route": "/object-store"},
        {"title": "Install the cookbook", "route": "/cookbooks"},
        {"title": "Resume the installed workflow", "route": "/workflows"},
        {"title": "Run a valid invoice", "route": "/workflows"},
        {"title": "Run the same invoice again", "route": "/workflows"},
        {"title": "Run an invoice that fails validation", "route": "/workflows"},
        {"title": "Approve and reject as the reviewer", "route": "/workflows"},
        {"title": "Check what reached the output bucket", "route": "/object-store"},
        {"title": "Publish version 1", "route": "/workflows"},
        {"title": "Change the rule and save a second version", "route": "/workflows"},
        {"title": "Re-run the same document on version 2", "route": "/workflows"},
        {"title": "Compare the versions", "route": "/workflows"},
    ),
    "05": (
        {"title": "Create the accounts and the workspace", "route": "/administration"},
        {"title": "Register the server", "route": "/mcp-servers"},
        {"title": "Test the connection", "route": "/mcp-servers"},
        {"title": "Open the Playground and find nothing callable", "route": "/mcp-servers"},
        {"title": "Classify every tool", "route": "/mcp-servers"},
        {"title": "Invoke the read tool", "route": "/mcp-servers"},
        {"title": "Meet the refusals", "route": "/mcp-servers"},
        {"title": "Calibrate the read tool", "route": "/mcp-servers"},
        {"title": "Gate the write behind a second person", "route": "/workflows"},
        {"title": "Version the workflow and meet the policy gate", "route": "/workflows"},
        {
            "title": "Servers CALIBER will not run, the audit log and the traces",
            "route": "/observability",
        },
    ),
    "06": (
        {"title": "Upload the policy documents to the Object Store", "route": "/object-store"},
        {"title": "Build knowledge base version 1", "route": "/knowledge-bases"},
        {"title": "Explore the chunks", "route": "/knowledge-bases"},
        {"title": "Ask the same question in three retrieval modes", "route": "/knowledge-bases"},
        {
            "title": "Store the answer prompt and import the calibration questions",
            "route": "/prompts",
        },
        {
            "title": "Calibrate: a baseline, then a better configuration",
            "route": "/knowledge-bases",
        },
        {"title": "Build the tuned version 2", "route": "/knowledge-bases"},
        {"title": "Set the active version", "route": "/knowledge-bases"},
        {"title": "Install the answer workflow", "route": "/cookbooks"},
        {"title": "Bind it to the knowledge base and run it", "route": "/workflows"},
        {"title": "Send the run's trace to a review queue", "route": "/review-queues"},
    ),
    "07": (
        {"title": "Create the accounts and the workspace", "route": "/administration"},
        {"title": "Load the tickets as a test set", "route": "/eval-datasets"},
        {"title": "Import the triage workflow", "route": "/workflows"},
        {"title": "Validate it; Preview is refused", "route": "/workflows"},
        {"title": "Publish it", "route": "/workflows"},
        {"title": "Run a how-to ticket and a bug ticket", "route": "/workflows"},
        {"title": "Try to approve your own run", "route": "/workflows"},
        {"title": "A second person approves and resumes", "route": "/workflows"},
        {"title": "A rejection releases nothing; an operator cannot decide", "route": "/workflows"},
        {"title": "Check who did what in the audit log", "route": "/audit-log"},
        {"title": "Modify the rule, publish a new version, re-run, compare", "route": "/workflows"},
        {"title": "Roll back by restoring version 1", "route": "/workflows"},
    ),
    "08": (
        {"title": "Create the accounts and the workspace", "route": "/administration"},
        {"title": "Import before the test set exists", "route": "/workflows"},
        {"title": "Create the test set from the file", "route": "/eval-datasets"},
        {"title": "Import the incident workflow", "route": "/workflows"},
        {"title": "Validate and Preview an incident", "route": "/workflows"},
        {"title": "Publish it", "route": "/workflows"},
        {"title": "Run incidents; the rollback waits", "route": "/workflows"},
        {"title": "A second person approves the rollback", "route": "/workflows"},
        {"title": "Loosen a rule, publish a new version, Preview, compare", "route": "/workflows"},
        {"title": "Roll back by restoring version 1", "route": "/workflows"},
        {"title": "Score the workflow version in Evaluations", "route": "/evaluations"},
        {"title": "Register an agent and Calibrate the workflow", "route": "/agents"},
    ),
    "09": (
        {"title": "Import version 1 of the workflow", "route": "/workflows"},
        {"title": "Publish it", "route": "/workflows"},
        {"title": "Run a healthy ticket", "route": "/workflows"},
        {"title": "Reproduce a schema failure", "route": "/workflows"},
        {"title": "Reproduce the poison record", "route": "/workflows"},
        {"title": "Reproduce the upstream failure", "route": "/workflows"},
        {"title": "Retry: the same code fails the same way", "route": "/workflows"},
        {"title": "Look for a patch", "route": "/workflows"},
        {"title": "Patch it as version 2", "route": "/workflows"},
        {"title": "Prove it on the same tickets", "route": "/workflows"},
        {"title": "Compare the versions, then try to promote", "route": "/workflows"},
    ),
    "10": (
        {"title": "Load the test set", "route": "/eval-datasets"},
        {"title": "Create the candidate prompt", "route": "/prompts"},
        {"title": "Define the judge", "route": "/judges"},
        {"title": "Run baseline + candidate", "route": "/evaluations"},
        {"title": "Inspect examples", "route": "/evaluations"},
        {"title": "Modify the prompt, save v2, re-run, compare", "route": "/prompts"},
        {"title": "Human review", "route": "/review-queues"},
        {"title": "Compute alignment", "route": "/judges"},
    ),
    "11": (
        {"title": "Open the Release signoff factory", "route": "/releases"},
        {"title": "Create a candidate with one blocker", "route": "/releases"},
        {"title": "Record a waiver", "route": "/releases"},
        {"title": "Re-evaluate the candidate", "route": "/releases"},
        {"title": "Generate Allure", "route": "/releases"},
        {"title": "Sign off GO", "route": "/releases"},
        {"title": "Sign off NO-GO on a blocked candidate", "route": "/releases"},
        {"title": "See that a waiver cannot raise a score", "route": "/releases"},
        {"title": "Create a new candidate for the fixed version", "route": "/releases"},
        {"title": "See that a signed GO deploys nothing", "route": "/prompts"},
        {"title": "Check who did what in the audit log", "route": "/audit-log"},
        {"title": "Reload and check what persists", "route": "/releases"},
    ),
    "12": (
        {"title": "State the intent", "route": "/aria/plans"},
        {"title": "Review the plan", "route": "/aria/plans"},
        {"title": "Approve the plan", "route": "/aria/plans"},
        {"title": "Execute and answer the judge's questions", "route": "/aria/plans"},
        {"title": "Answer the test set's questions", "route": "/aria/plans"},
        {"title": "Verify the judge", "route": "/judges"},
        {"title": "Verify the test set and load the 30 cases", "route": "/eval-datasets"},
        {"title": "Modify a row, which makes a new version", "route": "/eval-datasets"},
        {"title": "Run the judge in Evaluations", "route": "/evaluations"},
        {"title": "A sentence with no steps", "route": "/aria/plans"},
        {"title": "Skip a step", "route": "/aria/plans"},
        {"title": "A judge that cannot be created", "route": "/aria/plans"},
    ),
    "13": (
        {"title": "State the intent", "route": "/aria/plans"},
        {"title": "Review the plan", "route": "/aria/plans"},
        {"title": "Approve the plan", "route": "/aria/plans"},
        {"title": "Execute and answer the queue's questions", "route": "/aria/plans"},
        {"title": "Answer the enqueue step", "route": "/aria/plans"},
        {"title": "Find the queue", "route": "/review-queues"},
        {"title": "Review one trace", "route": "/review-queues"},
        {"title": "A review that does not fit", "route": "/review-queues"},
        {"title": "Skip the queue", "route": "/aria/plans"},
        {"title": "A question schema the queue refuses", "route": "/aria/plans"},
    ),
    "14": (
        {"title": "State the intent", "route": "/aria/plans"},
        {"title": "Review the plan", "route": "/aria/plans"},
        {"title": "Approve the plan", "route": "/aria/plans"},
        {"title": "Execute and answer the judge's questions", "route": "/aria/plans"},
        {"title": "Answer the queue's questions", "route": "/aria/plans"},
        {"title": "Answer the test set's questions", "route": "/aria/plans"},
        {"title": "Skip the enqueue step", "route": "/aria/plans"},
        {"title": "Verify the judge and the queue", "route": "/review-queues"},
        {"title": "Verify the test set and add rows", "route": "/eval-datasets"},
        {"title": "Ask for less", "route": "/aria/plans"},
        {"title": "Deny a step", "route": "/aria/plans"},
    ),
    "15": (
        {"title": "State the intent", "route": "/aria/plans"},
        {"title": "Review the plan", "route": "/aria/plans"},
        {"title": "Approve the plan", "route": "/aria/plans"},
        {"title": "Execute and answer the queue's questions", "route": "/aria/plans"},
        {"title": "Answer the enqueue step", "route": "/aria/plans"},
        {"title": "Answer the calibration step and watch the plan park", "route": "/aria/plans"},
        {"title": "Wait: the plan settles by itself", "route": "/aria/plans"},
        {"title": "Find the queue", "route": "/review-queues"},
        {"title": "Triage one flagged run", "route": "/review-queues"},
        {"title": "A workflow that does not exist", "route": "/aria/plans"},
        {"title": "Deny the calibration", "route": "/aria/plans"},
    ),
    "16": (
        {"title": "Import and publish the production workflow", "route": "/workflows"},
        {"title": "Send the 40 production tickets through the Run Monitor", "route": "/workflows"},
        {"title": "Filter the traces by status", "route": "/observability"},
        {"title": "Open a failing trace and find the node", "route": "/observability"},
        {"title": "Search with no match, open an unknown trace", "route": "/observability"},
        {"title": "Create the test set", "route": "/eval-datasets"},
        {
            "title": "Capture failures from Observability and from the test set",
            "route": "/observability",
        },
        {"title": "Put the gold outcome on a captured example", "route": "/eval-datasets"},
        {"title": "Create the triage queue", "route": "/review-queues"},
        {"title": "Enqueue two traces and review them", "route": "/review-queues"},
        {"title": "Read the answers on the trace", "route": "/observability"},
        {
            "title": "Fix the workflow: restore, edit three nodes, publish version 2",
            "route": "/workflows",
        },
        {
            "title": "Re-run the 16 tickets that mattered and compare the traces",
            "route": "/observability",
        },
        {"title": "Compare the two versions of the workflow", "route": "/workflows"},
    ),
    # 18's README is organised by the sections every lab README shares (API path, UI path,
    # verification), not by a ``## Recipe`` list. Its steps are the eleven numbered steps of
    # "Run it through the UI", and ``test_cookbook_steps_contract`` pins their count to that list.
    "18": (
        {"title": "Create the team's accounts", "route": "/administration"},
        {"title": "Create the workspace", "route": "/"},
        {"title": "Add the members with their roles", "route": "/administration"},
        {
            "title": "Prove a viewer and a reviewer are refused, an editor writes and releases v2",
            "route": "/prompts",
        },
        {"title": "Prove a non-member sees nothing", "route": "/"},
        {"title": "Change a role and remove a member", "route": "/administration"},
        {"title": "Issue a token bound to the workspace", "route": "/settings"},
        {"title": "Revoke the token", "route": "/settings"},
        {"title": "Read the audit log", "route": "/audit-log"},
        {"title": "Read the release timeline and the live board", "route": "/releases"},
        {"title": "Read readiness: the health dot and the simulated-provider banner", "route": "/"},
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
