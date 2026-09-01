#!/usr/bin/env python3
"""Generate build-time Markdown fragments for the CALIBER SDK docs.

Two fragments are produced:

* ``reference``  — the comprehensive Python SDK API reference
* ``cookbooks``  — the SDK-only cookbook implementations page

The generator is intentionally stdlib-only so ``docs-site/build-docs.mjs`` can
invoke it without adding a Node or Python package dependency.
"""

from __future__ import annotations

import ast
import re
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SDK_ROOT = REPO_ROOT / "sdk" / "caliber-sdk" / "src" / "caliber_sdk"
EXAMPLES_ROOT = REPO_ROOT / "sdk" / "caliber-sdk" / "examples"


def slugify(text: str) -> str:
    text = text.lower().replace("`", "")
    text = re.sub(r"[^\w\s-]", "", text).strip()
    text = re.sub(r"\s+", "-", text)
    return re.sub(r"-+", "-", text)


def heading_link(text: str) -> str:
    return f"#{slugify(text)}"


def clean_docstring(value: str | None) -> str:
    if not value:
        return ""
    return textwrap.dedent(value).strip()


def first_paragraph(value: str | None) -> str:
    if not value:
        return ""
    cleaned = clean_docstring(value)
    return cleaned.split("\n\n", 1)[0].replace("\n", " ").strip()


#: Internal aliases the SDK uses to work around Python's own scoping, mapped
#: back to what a reader should see. ``_List = list`` exists in the resource
#: modules because a class that defines its own ``list()`` method shadows the
#: builtin for every annotation written after it -- a real constraint, and one a
#: caller of the published API has no reason to know about. Left unmapped, 48
#: signatures advertised a ``_List`` type that does not exist.
INTERNAL_TYPE_ALIASES: dict[str, str] = {"_List": "list"}


def normalize_annotation(text: str) -> str:
    for alias, real in INTERNAL_TYPE_ALIASES.items():
        text = re.sub(rf"\b{re.escape(alias)}\b", real, text)
    return text


def format_annotation(node: ast.AST | None) -> str:
    if node is None:
        return "Any"
    return normalize_annotation(ast.unparse(node))


def format_default(node: ast.AST | None) -> str:
    if node is None:
        return "—"
    return ast.unparse(node)


#: Type names that have their own section in the generated reference, and are
#: therefore worth linking to. One definition, consumed by both
#: :func:`linkify_types` (prose) and :func:`render_type` (signatures and tables).
LINKABLE_TYPES: tuple[str, ...] = (
    "Account",
    "AriaInteraction",
    "AriaPlan",
    "AriaPlanDetail",
    "AriaPlanStep",
    "AsyncCaliberClient",
    "AuditEntry",
    "Bucket",
    "CaliberAPIError",
    "CaliberAuthenticationError",
    "CaliberClient",
    "CaliberConfigError",
    "CaliberConflictError",
    "CaliberError",
    "CaliberNotFoundError",
    "CaliberPermissionError",
    "CaliberRateLimitError",
    "CaliberServerError",
    "CaliberTransportError",
    "CaliberValidationError",
    "CalibrationJob",
    "Capabilities",
    "CookbookRecipe",
    "ErrorBody",
    "EvalDataset",
    "EvalExample",
    "Evaluation",
    "Extensibility",
    "FieldError",
    "Identity",
    "IssuedToken",
    "Job",
    "Judge",
    "JudgeAlignment",
    "KnowledgeBase",
    "LlmSetupStatus",
    "McpServer",
    "OptimizerPlugin",
    "Page",
    "PersonalAccessToken",
    "Project",
    "ProjectFile",
    "ProjectFolder",
    "Prompt",
    "RegisteredOptimizer",
    "ReleaseCandidate",
    "ReviewQueue",
    "RuntimeSettings",
    "RuntimeSettingsSummary",
    "SessionInfo",
    "Skill",
    "SkillRender",
    "SkillSelection",
    "SkillVersion",
    "Stability",
    "StoredObject",
    "Tool",
    "Trace",
    "WaitFailed",
    "WaitTimeout",
    "Workflow",
    "WorkflowRun",
    "WorkflowRunCapabilities",
    "WorkflowRunFailed",
    "WorkflowService",
    "WorkflowVersion",
)

#: Name -> in-page anchor, built once.
TYPE_ANCHORS: dict[str, str] = {
    name: heading_link(f"`{name}`") for name in LINKABLE_TYPES
}


def linkify_types(text: str) -> str:
    """Link documented type names inside a prose string."""

    def replace(match: "re.Match[str]") -> str:
        anchor = TYPE_ANCHORS.get(match.group(0))
        return f"[{match.group(0)}]({anchor})" if anchor else match.group(0)

    return re.sub(r"\b[A-Z][A-Za-z0-9_]+\b", replace, text)


def render_type(text: str) -> str:
    """Render a type expression as one code span, linked to its own docs.

    The previous form was ``f"`{linkify_types(text)}`"``, which put markdown
    link syntax *inside* a code span -- so 82 return types published as the
    literal text ``[CaliberClient](#caliberclient)`` instead of a link. The goal
    of cross-linked types was defeated by the wrapping.

    Linking the *whole* span rather than the type name inside it is deliberate:
    inline ``code`` carries a background and padding, so splitting
    ``list[WorkflowRun]`` into three adjacent spans to link only the middle one
    would render as three separate pills.

    When a compound type references exactly one documented name, the span links
    to it -- ``list[WorkflowRun]`` points at ``WorkflowRun``, which is where a
    reader wants to go. When it references several, the span stays unlinked and
    the names follow it, because guessing which one the reader meant would send
    them to the wrong place half the time.
    """
    stripped = (text or "").strip()
    if not stripped:
        return "`—`"

    exact = TYPE_ANCHORS.get(stripped)
    if exact:
        return f"[`{stripped}`]({exact})"

    referenced = [
        name
        for name in dict.fromkeys(re.findall(r"\b[A-Z][A-Za-z0-9_]*\b", stripped))
        if name in TYPE_ANCHORS
    ]
    if len(referenced) == 1:
        return f"[`{stripped}`]({TYPE_ANCHORS[referenced[0]]})"
    if referenced:
        links = ", ".join(f"[`{name}`]({TYPE_ANCHORS[name]})" for name in referenced)
        return f"`{stripped}` — see {links}"
    return f"`{stripped}`"


@dataclass
class ParamInfo:
    name: str
    kind: str
    annotation: str
    default: str


@dataclass
class MemberInfo:
    name: str
    signature: str
    params: list[ParamInfo]
    returns: str
    doc: str
    summary: str
    decorators: list[str] = field(default_factory=list)
    raises: list[str] = field(default_factory=list)


@dataclass
class AttributeInfo:
    name: str
    inferred_type: str
    note: str = ""


@dataclass
class FieldInfo:
    name: str
    annotation: str
    default: str


@dataclass
class ClassInfo:
    name: str
    signature: str
    doc: str
    summary: str
    bases: list[str]
    attributes: list[AttributeInfo] = field(default_factory=list)
    fields: list[FieldInfo] = field(default_factory=list)
    properties: list[MemberInfo] = field(default_factory=list)
    methods: list[MemberInfo] = field(default_factory=list)
    constructor: MemberInfo | None = None


@dataclass
class ConstantInfo:
    name: str
    value: str


@dataclass
class ModuleInfo:
    name: str
    doc: str
    summary: str
    classes: list[ClassInfo]
    functions: list[MemberInfo]
    constants: list[ConstantInfo]
    exports: list[str]


SPECIAL_METHODS = {"__enter__", "__exit__", "__aenter__", "__aexit__", "__repr__"}
RESOURCE_CLASS_NAMES = {
    "RawAPI",
    "AuthAPI",
    "TokensAPI",
    "AccountsAPI",
    "MeAPI",
    "CapabilitiesAPI",
    "SettingsAPI",
    "ProjectsAPI",
    "ProjectFilesAPI",
    "PromptsAPI",
    "SkillsAPI",
    "ToolsAPI",
    "WorkflowsAPI",
    "WorkflowVersionsAPI",
    "WorkflowRunsAPI",
    "WorkflowServicesAPI",
    "EvalDatasetsAPI",
    "JudgesAPI",
    "EvaluationsAPI",
    "McpServersAPI",
    "OpenApiIntegrationsAPI",
    "GatewayAPI",
    "KnowledgeBasesAPI",
    "ObjectStoreAPI",
    "JobsAPI",
    "ReviewQueuesAPI",
    "AriaAPI",
    "ReleasesAPI",
    "ObservabilityAPI",
    "AuditAPI",
    "EventsAPI",
    "CookbooksAPI",
    "SecretsAPI",
}

CLIENT_ATTRIBUTE_NOTES = {
    "raw": "Low-level route access through the SDK transport.",
    "auth": "Session inspection plus token and account sub-resources.",
    "me": "The caller identity surface.",
    "capabilities_info": "Runtime stability tiers and deployment capabilities.",
    "settings": "Runtime and LLM configuration inventory.",
    "projects": "Projects plus the managed file registry.",
    "prompts": "Prompt registry authoring and promotion.",
    "skills": "Skill registry, render tests, selection tests, and versions.",
    "tools": "Tool registry, schemas, and deterministic calibration.",
    "workflows": "Workflow registry plus versions, runs, and services.",
    "eval_datasets": "Evaluation datasets and examples.",
    "judges": "Model-backed graders and alignment scoring.",
    "evaluations": "Scored dataset runs.",
    "mcp_servers": "Managed MCP server registry and governed tool invocation.",
    "openapi_integrations": "Governed OpenAPI import, curation, dependency review, and tool-draft publication.",
    "gateway": "Gateway discovery, usage, and guardrails.",
    "knowledge_bases": "RAG corpora, versions, retrieval, and calibration.",
    "object_store": "Buckets and objects under the storage substrate.",
    "jobs": "Long-running background jobs.",
    "review_queues": "Human review queues and queue items.",
    "aria": "The approval-aware plan and interaction loop.",
    "releases": "Release candidates, waivers, signoff, and reports.",
    "observability": "Traces, experiments, and metrics.",
    "audit": "The audit log.",
    "events": "Server-sent event stream.",
    "cookbooks": "The built-in cookbook catalog and installer.",
    "secrets": "Write-only secret references.",
}

MODULE_EXAMPLES = {
    "caliber_sdk": "sdk/caliber-sdk/examples/quickstart.py#quickstart",
    "caliber_sdk.client": "sdk/caliber-sdk/examples/quickstart.py#quickstart",
    "caliber_sdk.auth": "sdk/caliber-sdk/examples/tokens.py#issue_scoped_token",
    "caliber_sdk.transport": "sdk/caliber-sdk/examples/quickstart.py#quickstart",
    "caliber_sdk.errors": "sdk/caliber-sdk/examples/quickstart.py#quickstart",
    "caliber_sdk.waiters": "sdk/caliber-sdk/examples/workflow_run.py#run_and_wait",
    "caliber_sdk.resources.auth": "sdk/caliber-sdk/examples/tokens.py#issue_scoped_token",
    "caliber_sdk.resources.system": "sdk/caliber-sdk/examples/quickstart.py#quickstart",
    "caliber_sdk.resources.projects": "sdk/caliber-sdk/examples/prompt_lifecycle.py#prompt_lifecycle",
    "caliber_sdk.resources.assets": "sdk/caliber-sdk/examples/prompt_lifecycle.py#prompt_lifecycle",
    "caliber_sdk.resources.quality": "sdk/caliber-sdk/examples/evaluation.py#build_and_score",
    "caliber_sdk.resources.workflows": "sdk/caliber-sdk/examples/workflow_run.py#run_and_wait",
    "caliber_sdk.resources.integrations": "sdk/caliber-sdk/examples/agentic.py#install_ready_cookbook",
    "caliber_sdk.resources.operations": "sdk/caliber-sdk/examples/agentic.py#plan_from_intent",
    "caliber_sdk.resources.raw": "sdk/caliber-sdk/examples/agentic.py#plan_from_intent",
    "caliber_sdk.models": "sdk/caliber-sdk/examples/quickstart.py#quickstart",
    "caliber_sdk.models.common": "sdk/caliber-sdk/examples/quickstart.py#quickstart",
    "caliber_sdk.models.core": "sdk/caliber-sdk/examples/quickstart.py#quickstart",
    "caliber_sdk.models.assets": "sdk/caliber-sdk/examples/prompt_lifecycle.py#prompt_lifecycle",
    "caliber_sdk.models.quality": "sdk/caliber-sdk/examples/evaluation.py#build_and_score",
    "caliber_sdk.models.integrations": "sdk/caliber-sdk/examples/agentic.py#install_ready_cookbook",
    "caliber_sdk.models.operations": "sdk/caliber-sdk/examples/agentic.py#plan_from_intent",
    "caliber_sdk.models.workflows": "sdk/caliber-sdk/examples/workflow_run.py#run_and_wait",
    "caliber_sdk.models.errors": "sdk/caliber-sdk/examples/quickstart.py#quickstart",
    "caliber_sdk.aio": "sdk/caliber-sdk/examples/workflow_run.py#run_and_wait",
    "caliber_sdk.aio.client": "sdk/caliber-sdk/examples/workflow_run.py#run_and_wait",
    "caliber_sdk.aio.transport": "sdk/caliber-sdk/examples/workflow_run.py#run_and_wait",
    "caliber_sdk.aio.waiters": "sdk/caliber-sdk/examples/workflow_run.py#run_and_wait",
}

CLASS_EXAMPLES = {
    "CaliberClient": MODULE_EXAMPLES["caliber_sdk.client"],
    "AsyncCaliberClient": MODULE_EXAMPLES["caliber_sdk.aio.client"],
    "WorkflowRunsAPI": MODULE_EXAMPLES["caliber_sdk.resources.workflows"],
    "WorkflowsAPI": MODULE_EXAMPLES["caliber_sdk.resources.workflows"],
    "PromptsAPI": MODULE_EXAMPLES["caliber_sdk.resources.assets"],
    "EvalDatasetsAPI": MODULE_EXAMPLES["caliber_sdk.resources.quality"],
    "JudgesAPI": MODULE_EXAMPLES["caliber_sdk.resources.quality"],
    "EvaluationsAPI": MODULE_EXAMPLES["caliber_sdk.resources.quality"],
    "CookbooksAPI": "sdk/caliber-sdk/examples/agentic.py#install_ready_cookbook",
    "AriaAPI": MODULE_EXAMPLES["caliber_sdk.resources.operations"],
}

MODULE_GROUPS = [
    (
        "Package index",
        [
            "caliber_sdk",
            "caliber_sdk.client",
            "caliber_sdk.auth",
            "caliber_sdk.transport",
            "caliber_sdk.errors",
            "caliber_sdk.waiters",
        ],
    ),
    (
        "Resource modules",
        [
            "caliber_sdk.resources",
            "caliber_sdk.resources.auth",
            "caliber_sdk.resources.system",
            "caliber_sdk.resources.projects",
            "caliber_sdk.resources.assets",
            "caliber_sdk.resources.workflows",
            "caliber_sdk.resources.quality",
            "caliber_sdk.resources.integrations",
            "caliber_sdk.resources.operations",
            "caliber_sdk.resources.raw",
        ],
    ),
    (
        "Model modules",
        [
            "caliber_sdk.models",
            "caliber_sdk.models.common",
            "caliber_sdk.models.core",
            "caliber_sdk.models.assets",
            "caliber_sdk.models.quality",
            "caliber_sdk.models.integrations",
            "caliber_sdk.models.operations",
            "caliber_sdk.models.workflows",
            "caliber_sdk.models.errors",
        ],
    ),
    (
        "Async client",
        [
            "caliber_sdk.aio",
            "caliber_sdk.aio.client",
            "caliber_sdk.aio.transport",
            "caliber_sdk.aio.waiters",
        ],
    ),
]

COOKBOOKS = [
    {
        "id": "01",
        "title": "Trustworthy Intake Classifier",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_01_trustworthy_intake_classifier.py",
        "summary": "Install the versioned recipe, then run the prompt workspace's own regression loop: author and promote the prompt, build the test set, pin a baseline run, introduce a regression, and read the Vs. baseline diff before queuing calibration.",
        "surfaces": ["cookbooks", "prompts", "datasets", "judges"],
        "steps": [
            "Inspect cookbook readiness and acknowledge prerequisites before installation.",
            "Install the built-in recipe, then author and promote the strict-JSON prompt version through the typed prompts resource.",
            "Build the regression test set, pin a baseline test run, introduce a deliberately weaker version, and diff the comparison run against that baseline before queuing a calibration run.",
        ],
    },
    {
        "id": "02",
        "title": "Precision Skills",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_02_precision_skills.py",
        "summary": "Install the recipe, register the reusable skill, run render and selection tests, and trigger skill calibration through the typed skills resource.",
        "surfaces": ["cookbooks", "skills"],
        "steps": [
            "Materialize the cookbook draft through the built-in installer.",
            "Create the skill and immediately prove its variable rendering and trigger-selection behavior.",
            "Start the server-side calibration job through `client.skills.calibrate()`, tagging the run with the installed workflow's id.",
        ],
    },
    {
        "id": "03",
        "title": "Policy-Safe Decision Tool",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_03_policy_safe_decision_tool.py",
        "summary": "Install the recipe, register the deterministic tool, persist hardening cases, and run a calibration pass against those fixtures.",
        "surfaces": ["cookbooks", "tools"],
        "steps": [
            "Install the versioned cookbook artifact after verifying readiness.",
            "Register the decision tool with explicit input and output schemas.",
            "Persist deterministic hardening fixtures and run a calibration pass to capture pass-rate evidence.",
        ],
    },
    {
        "id": "04",
        "title": "Document-to-JSON Pipeline",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_04_document_to_json_pipeline.py",
        "summary": "Install the recipe, upload a source document into a project, and preview-run the generated workflow draft against the uploaded managed file.",
        "surfaces": ["cookbooks", "projects", "workflows"],
        "steps": [
            "Create a project-scoped home for the source documents and upload a managed file through the SDK.",
            "Install the cookbook draft so the platform materializes the maintained workflow manifest for you.",
            "Preview-run the installed workflow version against the uploaded file (real tool bindings are not used in preview mode) and return the file/workflow identities needed for the next execution step.",
        ],
    },
    {
        "id": "05",
        "title": "Governed Tool Connectivity",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_05_governed_tool_connectivity.py",
        "summary": "Install the recipe, connect an MCP server, test reachability, discover tools, invoke the read tool, enforce a policy block on the write tool, then re-invoke it to capture the structured refusal.",
        "surfaces": ["cookbooks", "mcp_servers"],
        "steps": [
            "Install the official recipe and connect the target MCP server through the registry surface.",
            "Probe the server, refresh the discovered inventory, and invoke a read tool to prove it works before anything is locked down.",
            "Apply a policy block to the write tool, save calibration cases for the read tool, then re-invoke the now-blocked tool to capture its structured refusal as evidence.",
        ],
    },
    {
        "id": "06",
        "title": "Grounded Knowledge Assistant",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_06_grounded_knowledge_assistant.py",
        "summary": "Install the recipe, read the deployment's real chunking/embedding catalog, create the knowledge base and a version from it, query the corpus by version, and calibrate against a dedicated eval dataset.",
        "surfaces": ["cookbooks", "knowledge_bases", "datasets"],
        "steps": [
            "Install the recipe as the versioned workflow scaffold for the scenario.",
            "Read the deployment's real chunking-strategy and embedding-model catalog, then create the knowledge base and its first version from those choices.",
            "Query the corpus by version id, build a small eval dataset, and launch a calibration run scored against it.",
        ],
    },
    {
        "id": "07",
        "title": "Support Triage Copilot",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_07_support_triage_copilot.py",
        "summary": "Install the recipe, create its evaluation and review assets, then drive the approval-gated escalation branch both ways: one run approved through to completion, a matching run rejected before its external write.",
        "surfaces": ["cookbooks", "review_queues", "datasets", "judges", "evaluations", "workflows"],
        "steps": [
            "Install the maintained recipe instead of copying a workflow manifest into the script.",
            "Create the human-review queue, the support dataset, the grounding judge, and the evaluation run that score this loop.",
            "Publish the installed draft, then submit two escalation runs: approve and resume one to completion, and reject the other to prove no external write occurs.",
        ],
    },
    {
        "id": "08",
        "title": "Incident Response Copilot",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_08_incident_response_copilot.py",
        "summary": "Install the recipe, collect observability evidence, create the incident review queue, and return the traces and queue needed for human approval.",
        "surfaces": ["cookbooks", "observability", "review_queues"],
        "steps": [
            "Install the recipe so the workflow draft and governance wiring come from the catalog, not the docs page.",
            "Pull the current trace set and operational metrics through the observability surface.",
            "Create the incident review queue and enqueue the trace ids that require human decision-making.",
        ],
    },
    {
        "id": "09",
        "title": "Self-Healing Workflows",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_09_self_healing_workflows.py",
        "summary": "Install the recipe, publish the version, drive a run to a reproducible failure, capture its checkpoints and trace, retry it from that checkpoint, then approve the retry through to recovery and propose a patch candidate from the evidence.",
        "surfaces": ["cookbooks", "workflows"],
        "steps": [
            "Install the cookbook draft and promote the version from draft to runnable.",
            "Submit a run, reject its pending approval to reproduce a reliable failure, then capture its checkpoints and debugger trace.",
            "Retry from the last checkpoint, approve and resume the retried attempt to a recovered terminal state, and generate a patch candidate from the failure evidence.",
        ],
    },
    {
        "id": "10",
        "title": "Trustworthy Evaluation",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_10_trustworthy_evaluation.py",
        "summary": "Install the recipe, build the dataset/judge/evaluation, enqueue a real trace for human review, then compute judge/human alignment (Cohen's kappa) against that completed review.",
        "surfaces": ["cookbooks", "datasets", "judges", "evaluations", "review_queues", "observability"],
        "steps": [
            "Install the versioned recipe through the SDK so the platform owns the scaffold.",
            "Create the evaluation dataset, the custom judge, the evaluation run, and the disagreement-review queue.",
            "Enqueue a real trace, answer it, and compute the judge's alignment against that human label -- the recipe's defining evidence.",
        ],
    },
    {
        "id": "11",
        "title": "Release Signoff Factory",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_11_release_signoff_factory.py",
        "summary": "Install the recipe, create a release candidate, re-evaluate it from current evidence, generate the durable report, and record the signoff.",
        "surfaces": ["cookbooks", "releases"],
        "steps": [
            "Install the maintained recipe instead of forking its workflow definition into the docs.",
            "Create the candidate with weighted criteria, evidence references, and rollback metadata.",
            "Re-evaluate, generate the report job, and record the final go/no-go decision with rationale.",
        ],
    },
    {
        "id": "12",
        "title": "Aria: Evaluation Harness from Intent",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_12_aria_evaluation_harness.py",
        "summary": "Install the recipe, drive the Aria plan through approval, and create the judge and eval dataset the plan's own interactions ask for -- the shipped planner leaves their inputs empty, so the real artifacts come from their typed calls.",
        "surfaces": ["cookbooks", "aria", "judges", "datasets"],
        "steps": [
            "Install the recipe so the workflow scaffold stays aligned with the product catalog.",
            "Create the Aria plan from a typed intent, approve it, and execute it.",
            "Answer each pending interaction and create the judge/dataset it asks for through their own typed calls -- the documented execution gap means the plan alone would create neither.",
        ],
    },
    {
        "id": "13",
        "title": "Aria: Human-Review Queue from Intent",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_13_aria_review_queue.py",
        "summary": "Install the recipe, drive the Aria plan through approval, and create the real review queue its interaction asks for, denying the add-items step since no traces exist yet.",
        "surfaces": ["cookbooks", "aria", "review_queues"],
        "steps": [
            "Install the catalog-managed recipe for the review-governance scenario.",
            "Create the Aria plan, approve it, and execute it.",
            "Create the review queue through its own typed call when the plan's interaction asks for it, and deny the add-items interaction since no traces exist yet.",
        ],
    },
    {
        "id": "14",
        "title": "Aria: Governance Starter Kit from Intent",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_14_aria_governance_starter_kit.py",
        "summary": "Install the recipe, drive the Aria plan through approval, and create all three governance artifacts -- judge, eval dataset, review queue -- its interactions ask for.",
        "surfaces": ["cookbooks", "aria", "judges", "datasets", "review_queues"],
        "steps": [
            "Install the recipe that binds the governance starter-kit scenario to the live platform catalog.",
            "Create the Aria plan, approve it, and execute it.",
            "Create the judge, eval dataset, and review queue through their own typed calls as each interaction asks for them, denying add-items since no traces exist yet.",
        ],
    },
    {
        "id": "15",
        "title": "Aria: Triage & Recalibrate Loop",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_15_aria_triage_recalibrate_loop.py",
        "summary": "Install the recipe, drive the Aria plan through approval, create the triage queue and enqueue flagged traces, kick off a real workflow calibration, then poll the plan through its first async capability to completion.",
        "surfaces": ["cookbooks", "aria", "review_queues", "observability", "workflows"],
        "steps": [
            "Install the recipe through the supported cookbook installer.",
            "Create the Aria plan, approve it, and execute it against this recipe's own installed workflow as the remediation subject.",
            "Create the queue, enqueue the flagged traces, and start the workflow calibration through their own typed calls, then poll the plan -- parked in `waiting_job` -- until the async job resolves.",
        ],
    },
    {
        "id": "16",
        "title": "Production Observability & Triage",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_16_observability_triage.py",
        "summary": "Install the recipe, collect traces, create the regression dataset from a trace, and stand up the triage queue used to classify failures.",
        "surfaces": ["cookbooks", "observability", "datasets", "review_queues"],
        "steps": [
            "Install the recipe so the production-triage workflow draft comes from the versioned catalog.",
            "Collect traces from the observability surface and promote one failing trace into the regression dataset.",
            "Create the triage queue and enqueue the failure set that needs human classification.",
        ],
    },
    {
        "id": "17",
        "title": "Monthly Financial Analysis",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_17_financial_analysis.py",
        "summary": "Create a project and object-store bucket, generate and persist realistic monthly financial CSV data, register and execute a statistics prompt, then persist and verify the structured analysis -- entirely through typed SDK resources. This recipe requires an admin-scoped SDK token for object-store mutations and a configured model provider for live prompt execution.",
        "surfaces": ["projects", "object_store", "prompts", "aria"],
        "steps": [
            "Create the financial-analysis project and select it as the temporary SDK project scope.",
            "Create a dedicated blob bucket, generate the CSV in memory, upload and download-verify it, then import the object into the project's governed file registry for lineage.",
            "Create and re-read the registered prompt, execute it through the typed assistant-session surface, validate its mean/median/percentile/extrema JSON, then upload and download-verify the result in blob storage.",
        ],
    },
]

RESOURCE_SURFACE_LABELS = {
    "AccountsAPI": "user accounts",
    "AriaAPI": "Aria plans and interaction state",
    "AuditAPI": "audit log entries",
    "AuthAPI": "authentication and session state",
    "CapabilitiesAPI": "runtime capabilities",
    "CookbooksAPI": "built-in cookbook recipes",
    "EvalDatasetsAPI": "evaluation datasets",
    "EvaluationsAPI": "evaluation runs",
    "EventsAPI": "event streams",
    "GatewayAPI": "gateway policies and usage",
    "JobsAPI": "background jobs",
    "JudgesAPI": "judges and alignment assets",
    "KnowledgeBasesAPI": "knowledge bases",
    "McpServersAPI": "MCP servers and governed tools",
    "MeAPI": "caller identity",
    "ObjectStoreAPI": "object store buckets and objects",
    "OpenApiIntegrationsAPI": "OpenAPI integrations, tool drafts, and dependency graph",
    "ObservabilityAPI": "observability traces and metrics",
    "ProjectFilesAPI": "project files and folders",
    "ProjectsAPI": "projects",
    "PromptsAPI": "prompts and prompt versions",
    "RawAPI": "low-level management API routes",
    "ReleasesAPI": "release candidates and signoffs",
    "ReviewQueuesAPI": "review queues and queue items",
    "SecretsAPI": "secret references",
    "SettingsAPI": "runtime settings",
    "SkillsAPI": "skills",
    "TokensAPI": "personal access tokens",
    "ToolsAPI": "tools and calibration cases",
    "WorkflowRunsAPI": "workflow runs",
    "WorkflowServicesAPI": "workflow services",
    "WorkflowVersionsAPI": "workflow versions",
    "WorkflowsAPI": "workflows",
}

RELATED_CLASS_LINKS = {
    "AriaAPI": ["JobsAPI", "ReviewQueuesAPI", "JudgesAPI", "EvalDatasetsAPI"],
    "CookbooksAPI": ["WorkflowsAPI", "ProjectsAPI", "ReviewQueuesAPI", "AriaAPI"],
    "EvalDatasetsAPI": ["EvaluationsAPI", "JudgesAPI", "ReviewQueuesAPI"],
    "EvaluationsAPI": ["EvalDatasetsAPI", "JudgesAPI", "ReviewQueuesAPI"],
    "KnowledgeBasesAPI": ["ProjectsAPI", "EvaluationsAPI"],
    "McpServersAPI": ["ToolsAPI", "GatewayAPI"],
    "OpenApiIntegrationsAPI": ["ToolsAPI", "McpServersAPI"],
    "ProjectsAPI": ["ProjectFilesAPI"],
    "PromptsAPI": ["EvaluationsAPI", "ReviewQueuesAPI"],
    "ReleasesAPI": ["EvaluationsAPI", "ReviewQueuesAPI", "WorkflowsAPI"],
    "ReviewQueuesAPI": ["JudgesAPI", "ObservabilityAPI", "EvalDatasetsAPI"],
    "SkillsAPI": ["JudgesAPI", "EvaluationsAPI"],
    "WorkflowRunsAPI": ["WorkflowsAPI", "WorkflowVersionsAPI", "WorkflowServicesAPI"],
    "WorkflowServicesAPI": ["WorkflowsAPI", "WorkflowVersionsAPI"],
    "WorkflowVersionsAPI": ["WorkflowsAPI", "WorkflowRunsAPI", "WorkflowServicesAPI"],
    "WorkflowsAPI": ["WorkflowVersionsAPI", "WorkflowRunsAPI", "WorkflowServicesAPI"],
}


def parse_exports(tree: ast.Module) -> list[str]:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        values: list[str] = []
                        for element in node.value.elts:
                            if isinstance(element, ast.Constant) and isinstance(
                                element.value, str
                            ):
                                values.append(element.value)
                        return values
    return []


def signature_from_function(
    node: ast.FunctionDef | ast.AsyncFunctionDef, *, bound: bool
) -> tuple[str, list[ParamInfo], str]:
    args = node.args
    params: list[ParamInfo] = []
    parts: list[str] = []
    positional = list(args.posonlyargs) + list(args.args)
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)

    def add_param(arg: ast.arg, default: ast.AST | None, *, kind: str) -> None:
        if bound and arg.arg in {"self", "cls"}:
            return
        annotation = format_annotation(arg.annotation)
        default_text = format_default(default)
        token = arg.arg
        if annotation != "Any":
            token += f": {annotation}"
        if default is not None:
            token += f" = {default_text}"
        parts.append(token)
        params.append(ParamInfo(arg.arg, kind, annotation, default_text))

    for index, arg in enumerate(args.posonlyargs):
        add_param(arg, defaults[index], kind="positional-only")
    if args.posonlyargs:
        parts.append("/")
    for index, arg in enumerate(args.args, start=len(args.posonlyargs)):
        add_param(arg, defaults[index], kind="positional-or-keyword")
    if args.vararg is not None:
        annotation = format_annotation(args.vararg.annotation)
        token = f"*{args.vararg.arg}"
        if annotation != "Any":
            token += f": {annotation}"
        parts.append(token)
        params.append(ParamInfo(args.vararg.arg, "var-positional", annotation, "—"))
    elif args.kwonlyargs:
        parts.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        add_param(arg, default, kind="keyword-only")
    if args.kwarg is not None:
        annotation = format_annotation(args.kwarg.annotation)
        token = f"**{args.kwarg.arg}"
        if annotation != "Any":
            token += f": {annotation}"
        parts.append(token)
        params.append(ParamInfo(args.kwarg.arg, "var-keyword", annotation, "—"))
    returns = format_annotation(node.returns)
    return f"({', '.join(parts)}) -> {returns}", params, returns


def parse_attributes(
    init_node: ast.FunctionDef | ast.AsyncFunctionDef | None,
) -> list[AttributeInfo]:
    if init_node is None:
        return []
    attributes: list[AttributeInfo] = []
    for stmt in init_node.body:
        if isinstance(stmt, ast.Assign):
            if len(stmt.targets) != 1:
                continue
            target = stmt.targets[0]
            if not (
                isinstance(target, ast.Attribute)
                and isinstance(target.value, ast.Name)
                and target.value.id == "self"
            ):
                continue
            inferred = "Any"
            if isinstance(stmt.value, ast.Call):
                func = stmt.value.func
                if isinstance(func, ast.Name):
                    inferred = func.id
                elif isinstance(func, ast.Attribute):
                    inferred = func.attr
            if target.attr.startswith("_"):
                # A published reference exists so a reader never has to open the
                # source; listing the internals they must not touch works against
                # that. ``_transport`` and friends are implementation, and naming
                # them here invited callers to reach for them.
                continue
            note = CLIENT_ATTRIBUTE_NOTES.get(target.attr, "")
            attributes.append(AttributeInfo(target.attr, inferred, note))
    return attributes


def parse_dataclass_fields(node: ast.ClassDef) -> list[FieldInfo]:
    fields: list[FieldInfo] = []
    for stmt in node.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            fields.append(
                FieldInfo(
                    stmt.target.id,
                    format_annotation(stmt.annotation),
                    format_default(stmt.value),
                )
            )
    return fields


#: Methods whose call means "this went over the network". ``request`` covers the
#: transports; the ``_``-prefixed pair covers resource classes routing through
#: :class:`Resource`.
_REQUEST_METHODS = frozenset(
    {
        "request",
        "get",
        "post",
        "put",
        "patch",
        "delete",
        "download",
        "stream_lines",
        "paginate",
        "_get",
        "_post",
        "_put",
        "_patch",
        "_delete",
    }
)


#: Callables that build an exception for a ``raise`` expression, mapped to what
#: they can produce. ``error_for_response`` picks a subclass from the status code
#: (see ``caliber_sdk.errors``), so the base class is what a caller can rely on
#: catching -- and it is a real, documented, linkable type.
EXCEPTION_FACTORIES: dict[str, set[str]] = {"error_for_response": {"CaliberAPIError"}}


def performs_request(member: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when the body calls something that reaches the server.

    Matched on ``self.x(...)`` / ``self._transport.x(...)`` rather than any call
    named ``get``, so a dictionary ``.get()`` is not mistaken for an HTTP GET.
    """
    for node in ast.walk(member):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr not in _REQUEST_METHODS:
            continue
        target = func.value
        if isinstance(target, ast.Name) and target.id == "self":
            return True
        if (
            isinstance(target, ast.Attribute)
            and target.attr in {"_transport", "_client"}
            and isinstance(target.value, ast.Name)
            and target.value.id == "self"
        ):
            return True
    return False


def swallows_exceptions(member: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when a handler absorbs the failure instead of re-raising it.

    Absorbing means the handler returns, passes, or breaks without a bare
    ``raise``; a handler that re-raises or wraps still surfaces something to the
    caller and must stay documented.
    """
    for node in ast.walk(member):
        if not isinstance(node, ast.ExceptHandler):
            continue
        reraises = any(
            isinstance(inner, ast.Raise)
            for inner in ast.walk(ast.Module(body=node.body, type_ignores=[]))
        )
        if not reraises:
            return True
    return False


def infer_raises(
    member: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    class_name: str | None,
) -> list[str]:
    names: set[str] = set()
    if class_name in RESOURCE_CLASS_NAMES or class_name in {
        "AsyncRawAPI",
        "AsyncMeAPI",
        "AsyncCapabilitiesAPI",
        "AsyncWorkflowRunsAPI",
        "AsyncJobsAPI",
        "AsyncEventsAPI",
    }:
        if member.name not in {
            "__init__",
            "__enter__",
            "__exit__",
            "__aenter__",
            "__aexit__",
            "__repr__",
        }:
            names.update({"CaliberAPIError", "CaliberTransportError"})
    elif performs_request(member) and not swallows_exceptions(member):
        # The exceptions a caller actually has to handle are the ones the
        # transport raises, and they propagate rather than appearing as a
        # ``raise`` in the method body -- so a body scan alone reported nothing
        # for CaliberClient.whoami(), Transport.get(), and seventeen others.
        # Every one of them performs a request and every one of them can fail.
        #
        # ``swallows_exceptions`` is what keeps this honest: bootstrap_csrf
        # catches and returns None on purpose, because a deployment with CSRF
        # disabled serves no token, and claiming it raises would be wrong.
        names.update({"CaliberAPIError", "CaliberTransportError"})
    for node in ast.walk(member):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            func = node.exc.func
            raised = ""
            if isinstance(func, ast.Name):
                raised = func.id
            elif isinstance(func, ast.Attribute):
                raised = func.attr
            # ``raise error_for_response(...)`` raises whatever the factory
            # built, not a class called "error_for_response". Recording the
            # callable's own name documented an exception type that does not
            # exist and linked to an anchor that never would.
            names.update(EXCEPTION_FACTORIES.get(raised, {raised} if raised else set()))
        if isinstance(node, ast.Call):
            func = node.func
            func_name = ""
            if isinstance(func, ast.Name):
                func_name = func.id
            elif isinstance(func, ast.Attribute):
                func_name = func.attr
            if func_name == "wait_for":
                names.update({"WaitTimeout", "WaitFailed"})
    if class_name == "WorkflowRunsAPI" and member.name == "wait":
        names.add("WorkflowRunFailed")
    if class_name == "CaliberClient" and member.name == "__init__":
        names.add("CaliberConfigError")
    if class_name == "AsyncCaliberClient" and member.name == "__init__":
        names.add("CaliberConfigError")
    return sorted(names)


def parse_member(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    bound: bool,
    class_name: str | None,
) -> MemberInfo:
    signature, params, returns = signature_from_function(node, bound=bound)
    decorators: list[str] = []
    for decorator in node.decorator_list:
        decorators.append(ast.unparse(decorator))
    return MemberInfo(
        name=node.name,
        signature=signature,
        params=params,
        returns=returns,
        doc=clean_docstring(ast.get_docstring(node)),
        summary=first_paragraph(ast.get_docstring(node)),
        decorators=decorators,
        raises=infer_raises(node, class_name=class_name),
    )


def parse_class(node: ast.ClassDef) -> ClassInfo:
    doc = clean_docstring(ast.get_docstring(node))
    init_node: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    methods: list[MemberInfo] = []
    properties: list[MemberInfo] = []
    constructor: MemberInfo | None = None
    for stmt in node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if stmt.name == "__init__":
                init_node = stmt
                constructor = parse_member(stmt, bound=True, class_name=node.name)
                continue
            if stmt.name.startswith("_") and stmt.name not in SPECIAL_METHODS:
                continue
            member = parse_member(stmt, bound=True, class_name=node.name)
            if "property" in member.decorators:
                properties.append(member)
            else:
                methods.append(member)
    if constructor is None:
        signature = "()"
    else:
        signature = constructor.signature.replace(" -> None", "")
    return ClassInfo(
        name=node.name,
        signature=f"class {node.name}{signature}",
        doc=doc,
        summary=first_paragraph(doc),
        bases=[normalize_annotation(ast.unparse(base)) for base in node.bases],
        attributes=parse_attributes(init_node),
        fields=parse_dataclass_fields(node),
        properties=properties,
        methods=methods,
        constructor=constructor,
    )


def parse_constants(tree: ast.Module, exports: list[str]) -> list[ConstantInfo]:
    results: list[ConstantInfo] = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
                continue
            name = node.targets[0].id
            if (
                not exports
                and (name.startswith("_") or not name.isupper())
                and name != "__version__"
            ):
                continue
            if exports and name not in exports:
                continue
            if isinstance(
                node.value, (ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set)
            ):
                results.append(ConstantInfo(name, ast.unparse(node.value)))
    return results


def parse_module(module_name: str) -> ModuleInfo:
    if module_name == "caliber_sdk":
        path = SDK_ROOT / "__init__.py"
    else:
        relative = module_name.replace("caliber_sdk.", "").replace(".", "/")
        path = SDK_ROOT / f"{relative}.py"
        if not path.exists():
            path = SDK_ROOT / relative / "__init__.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    doc = clean_docstring(ast.get_docstring(tree))
    exports = parse_exports(tree)
    public = set(exports) if exports else None
    classes: list[ClassInfo] = []
    functions: list[MemberInfo] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            if public is not None and node.name not in public:
                continue
            if public is None and node.name.startswith("_"):
                continue
            classes.append(parse_class(node))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if public is not None and node.name not in public:
                continue
            if public is None and node.name.startswith("_"):
                continue
            functions.append(parse_member(node, bound=False, class_name=None))
    return ModuleInfo(
        name=module_name,
        doc=doc,
        summary=first_paragraph(doc),
        classes=classes,
        functions=functions,
        constants=parse_constants(tree, exports),
        exports=exports,
    )


def humanize_camel(name: str) -> str:
    text = re.sub(r"(?<!^)(?=[A-Z])", " ", name).strip().lower()
    return text.replace("mcp", "MCP").replace("aria", "Aria")


def surface_label(class_name: str | None) -> str:
    if not class_name:
        return "SDK surface"
    normalized = class_name.removeprefix("Async")
    if normalized in RESOURCE_SURFACE_LABELS:
        return RESOURCE_SURFACE_LABELS[normalized]
    if normalized.endswith("API"):
        return humanize_camel(normalized[:-3])
    return humanize_camel(normalized)


def inferred_class_summary(info: ClassInfo) -> str:
    if info.doc:
        return info.doc
    if info.name in {"CaliberClient", "AsyncCaliberClient"}:
        mode = "synchronous" if info.name == "CaliberClient" else "asynchronous"
        return (
            f"The {mode} entry point that binds authentication, transport, and "
            "typed CALIBER resource APIs into one client."
        )
    if info.name.endswith("API"):
        return f"Typed access to the {surface_label(info.name)} surface."
    if (
        info.name.endswith("Error")
        or info.name.endswith("Failed")
        or info.name.endswith("Timeout")
    ):
        return "SDK exception type used to report a specific failure mode."
    if info.fields:
        return (
            f"Data model returned by the SDK for {humanize_camel(info.name)} records."
        )
    return f"SDK type for {humanize_camel(info.name)}."


def inferred_member_doc(member: MemberInfo, *, class_name: str | None) -> str:
    if member.doc:
        return member.doc
    resource = surface_label(class_name)
    id_param = member.params[0].name if member.params else "id"
    behaviors = {
        "__enter__": "Return this instance so it can be used inside a context manager.",
        "__aenter__": "Return this instance so it can be used inside an async context manager.",
        "__exit__": "Close any owned resources when leaving the context manager.",
        "__aexit__": "Close any owned resources when leaving the async context manager.",
        "close": "Close the underlying HTTP client or transport owned by this object.",
        "headers": "Build the authentication headers added to outgoing HTTP requests.",
        "uses_cookie_auth": "Report whether this auth strategy relies on cookie-backed authentication.",
        "health": "Fetch the lightweight health/readiness view exposed by the deployment.",
        "openapi": "Return the live management OpenAPI document from the connected deployment.",
        "whoami": "Return the identity and scopes that CALIBER resolved for the current credential.",
        "capabilities": "Fetch the runtime capability inventory and SDK stability tiers.",
        "runtime": "Return the current runtime settings snapshot for the deployment.",
        "list": f"Return the current collection of {resource}, applying any supported filters.",
        "get": f"Fetch one record from the {resource} surface identified by `{id_param}`.",
        "create": f"Create a new record on the {resource} surface and return the server-normalized result.",
        "update": f"Patch an existing record on the {resource} surface and return the updated result.",
        "delete": f"Delete a record on the {resource} surface and return the server acknowledgement.",
        "render": "Render the templated asset with the supplied variables without running the full workflow.",
        "test_selection": "Score whether this asset would be selected for the supplied input.",
        "test_connection": "Probe the remote integration endpoint and return the connection result.",
        "discover_tools": "Refresh the tool inventory exposed by the connected integration.",
        "update_tool_policy": "Write the policy overlay that governs one discovered tool.",
        "save_test_cases": "Persist deterministic calibration cases for the targeted integration tool.",
        "calibrate_tool": "Start or run the calibration pass for the targeted integration tool.",
        "calibrate": f"Start the calibration flow exposed by the {resource} surface.",
        "compile": "Ask the server to compile the draft workflow or asset into its executable form.",
        "validate": "Run the server-side validation pass against the targeted draft.",
        "publish": "Promote the draft or version into the published state used by operators or runtime callers.",
        "unpublish": "Remove the published state from the targeted runtime asset.",
        "submit": "Create a new execution run on the server and return its initial state.",
        "wait": "Poll until the targeted run or job reaches a terminal state, then return the final record.",
        "query": "Run a query against the server-managed corpus or knowledge surface and return the response.",
        "create_version": "Create a new version under the targeted top-level asset.",
        "add_example": "Append one labeled example row to the targeted evaluation dataset.",
        "add_from_trace": "Promote one trace into an evaluation example row with the supplied expectations.",
        "examples": "Return the example rows currently stored for the targeted evaluation dataset.",
        "install": "Materialize the built-in cookbook recipe as CALIBER assets and return the created identifiers.",
        "create_candidate": "Create a release candidate with its decision criteria, evidence, and rollback metadata.",
        "evaluate": "Recompute the current release or evaluation verdict from the latest stored evidence.",
        "generate_report": "Start the durable report-generation job for the targeted release candidate.",
        "sign": "Record the final release decision together with operator rationale.",
        "enqueue": "Add the supplied items to the targeted review queue.",
        "history": "Return the recorded history for the targeted managed integration.",
        "guardrails": "Return the configured gateway guardrails for the connected deployment.",
        "create_guardrail": "Create a new gateway guardrail from the supplied configuration payload.",
        "delete_guardrail": "Delete the targeted gateway guardrail definition.",
        "bootstrap_csrf": "Fetch a CSRF token up front so browser-style authenticated writes can reuse it.",
    }
    if member.name in behaviors:
        detail = behaviors[member.name]
    elif class_name == "Transport":
        detail = "Send a prepared request through the shared transport and decode the typed response wrapper."
    elif class_name == "AsyncTransport":
        detail = "Send a prepared request asynchronously through the shared transport and decode the typed response wrapper."
    elif member.name.startswith("wait_for_"):
        detail = "Poll until the named condition becomes true or the timeout budget is exhausted."
    else:
        detail = f"Operate on the {resource} surface with the supplied arguments and return the server response."
    if class_name == "WorkflowRunsAPI" and member.name == "wait":
        detail += " If the run ends in a failure state and `raise_on_failure=True`, the SDK raises `WorkflowRunFailed`."
    if class_name in RESOURCE_CLASS_NAMES and member.name in {
        "create",
        "update",
        "delete",
    }:
        detail += " Validation and permission failures are surfaced through the standard CALIBER error hierarchy."
    return detail


def related_class_links(class_name: str | None) -> list[str]:
    if not class_name:
        return []
    related = RELATED_CLASS_LINKS.get(class_name.removeprefix("Async"), [])
    return [f"[`{name}`]({heading_link(f'`{name}`')})" for name in related]


def render_params(params: list[ParamInfo]) -> list[str]:
    if not params:
        return ["This callable takes no public parameters.", ""]
    lines = [
        "| Parameter | Kind | Type | Default |",
        "| --- | --- | --- | --- |",
    ]
    for param in params:
        lines.append(
            f"| `{param.name}` | {param.kind} | {render_type(param.annotation)} | `{param.default}` |"
        )
    lines.append("")
    return lines


def render_member(
    member: MemberInfo,
    *,
    label_prefix: str = "",
    class_name: str | None = None,
) -> list[str]:
    title = f"`{label_prefix}{member.name}{member.signature}`"
    lines = [f"###### {title}", ""]
    lines.append(inferred_member_doc(member, class_name=class_name))
    lines.append("")
    lines.extend(render_params(member.params))
    lines.append(f"**Returns:** {render_type(member.returns)}")
    lines.append("")
    if member.raises:
        lines.append("**Raises:**")
        lines.append("")
        for name in member.raises:
            # Linked only when the reference has a section for it. Linking
            # unconditionally produced dead ends: ``ValueError`` is a builtin this
            # reference does not document, and the link pointed at an anchor that
            # does not exist -- worse than plain text, because it invites a click
            # that goes nowhere.
            anchor = TYPE_ANCHORS.get(name)
            lines.append(f"- [`{name}`]({anchor})" if anchor else f"- `{name}`")
        lines.append("")
    return lines


#: Base classes that carry no public surface. ``Resource`` and ``_AsyncResource``
#: exist so every resource class shares one envelope-unwrapping request path --
#: their entire membership is ``_get``/``_post``/..., so a section for them would
#: document nothing a caller can call. Naming them in a **Bases:** line was worse
#: than omitting them: 38 classes pointed at a type the reference never defines,
#: which is a dead end for the reader rather than a hint.
INTERNAL_BASES = frozenset({"Resource", "_AsyncResource"})


def render_class(info: ClassInfo) -> list[str]:
    lines = [f"##### `{info.name}`", "", f"`{info.signature}`", ""]
    published_bases = [base for base in info.bases if base not in INTERNAL_BASES]
    if published_bases:
        lines.append(
            f"**Bases:** {', '.join(render_type(base) for base in published_bases)}"
        )
        lines.append("")
    lines.append(inferred_class_summary(info))
    lines.append("")
    class_example = CLASS_EXAMPLES.get(info.name)
    if class_example:
        lines.append("**Usage example**")
        lines.append("")
        lines.append("```python-example")
        lines.append(class_example)
        lines.append("```")
        lines.append("")
    related = related_class_links(info.name)
    if related:
        lines.append(f"**Related APIs:** {', '.join(related)}")
        lines.append("")
    if info.constructor:
        lines.append("**Constructor**")
        lines.append("")
        lines.extend(
            render_member(info.constructor, label_prefix="", class_name=info.name)
        )
    if info.attributes:
        lines.append("**Attributes**")
        lines.append("")
        lines.append("| Attribute | Type | Notes |")
        lines.append("| --- | --- | --- |")
        for attribute in info.attributes:
            note = attribute.note or "—"
            lines.append(
                f"| `{attribute.name}` | {render_type(attribute.inferred_type)} | {note} |"
            )
        lines.append("")
    if info.fields:
        lines.append("**Dataclass fields**")
        lines.append("")
        lines.append("| Field | Type | Default |")
        lines.append("| --- | --- | --- |")
        for field_info in info.fields:
            lines.append(
                f"| `{field_info.name}` | {render_type(field_info.annotation)} | `{field_info.default}` |"
            )
        lines.append("")
    if info.properties:
        lines.append("**Properties**")
        lines.append("")
        for member in info.properties:
            lines.extend(render_member(member, label_prefix="", class_name=info.name))
    if info.methods:
        lines.append("**Methods**")
        lines.append("")
        for member in info.methods:
            lines.extend(render_member(member, label_prefix="", class_name=info.name))
    return lines


def render_module(info: ModuleInfo) -> list[str]:
    title = f"Module `{info.name}`"
    lines = [f"### {title}", ""]
    if info.summary:
        lines.append(info.summary)
        lines.append("")
    example = MODULE_EXAMPLES.get(info.name)
    if example:
        lines.append("**Tested example**")
        lines.append("")
        lines.append("```python-example")
        lines.append(example)
        lines.append("```")
        lines.append("")
    if info.exports:
        lines.append("**Public exports**")
        lines.append("")
        export_parts = [f"`{name}`" for name in info.exports]
        lines.append(", ".join(export_parts))
        lines.append("")
    if info.constants:
        lines.append("**Module constants**")
        lines.append("")
        lines.append("| Name | Value |")
        lines.append("| --- | --- |")
        for constant in info.constants:
            lines.append(f"| `{constant.name}` | `{constant.value}` |")
        lines.append("")
    if info.functions:
        lines.append("#### Functions")
        lines.append("")
        for function in info.functions:
            lines.extend(render_member(function, class_name=None))
    if info.classes:
        lines.append("#### Classes")
        lines.append("")
        for class_info in info.classes:
            lines.extend(render_class(class_info))
    return lines


def render_symbol_index(modules: dict[str, ModuleInfo]) -> list[str]:
    """A flat A-Z index of every documented class and top-level function.

    The module index answers "what is in the package"; this answers the question
    a reader actually arrives with, which is "where is ``WorkflowRun``". Without
    it, looking up a symbol meant already knowing which of 29 modules defines it
    -- exactly the source-diving the reference exists to replace.

    Grouped by initial letter because a single 160-row table is a wall, and a
    reader scanning for one name wants to land near it.
    """
    entries: dict[str, tuple[str, str]] = {}
    for module_name, info in modules.items():
        for class_info in info.classes:
            entries.setdefault(
                class_info.name, (heading_link(f"`{class_info.name}`"), module_name)
            )
        for function in info.functions:
            entries.setdefault(
                function.name,
                (heading_link(f"`{function.name}{function.signature}`"), module_name),
            )

    if not entries:
        return []

    lines = ["## Symbol index", ""]
    lines.append(
        "Every documented class and module-level function, with the module that "
        "defines it. Members hang off their class, so start here and follow the link."
    )
    lines.append("")
    by_letter: dict[str, list[str]] = {}
    for name in sorted(entries, key=str.lower):
        by_letter.setdefault(name[0].upper(), []).append(name)

    for letter in sorted(by_letter):
        lines.append(f"**{letter}**")
        lines.append("")
        lines.append("| Symbol | Defined in |")
        lines.append("| --- | --- |")
        for name in by_letter[letter]:
            anchor, module_name = entries[name]
            module_link = heading_link(f"Module `{module_name}`")
            lines.append(f"| [`{name}`]({anchor}) | [`{module_name}`]({module_link}) |")
        lines.append("")
    return lines


def render_reference() -> str:
    modules = {
        name: parse_module(name) for _group, names in MODULE_GROUPS for name in names
    }
    lines = [
        "The most common entry point is `caliber_sdk.CaliberClient`; the rest of the package fans out into typed resource modules, dataclass models, shared transport and error helpers, and an async client.",
        "",
        "The reference tables below are generated directly from the current SDK source. Behavior notes and examples come from the SDK docstrings and the executable example files the test suite runs.",
        "",
    ]
    index_rows = [
        "| Group | Modules |",
        "| --- | --- |",
    ]
    for group_title, module_names in MODULE_GROUPS:
        links = ", ".join(
            f"[`{module_name}`]({heading_link(f'Module `{module_name}`')})"
            for module_name in module_names
        )
        index_rows.append(f"| {group_title} | {links} |")
    lines.append("## Module index")
    lines.append("")
    lines.extend(index_rows)
    lines.append("")
    lines.extend(render_symbol_index(modules))
    for group_title, module_names in MODULE_GROUPS:
        lines.append(f"## {group_title}")
        lines.append("")
        for module_name in module_names:
            lines.extend(render_module(modules[module_name]))
    return "\n".join(lines).strip() + "\n"


def render_cookbooks() -> str:
    lines = [
        "Each script on this page finishes its scenario entirely through typed SDK calls. Recipes 01–16 extend versioned entries in the built-in cookbook catalog; standalone SDK automations such as recipe 17 create their own project and assets directly. `client.raw` is the SDK's permanent escape hatch for anything not yet wrapped, but none of these recipes currently need it.",
        "",
        "The code blocks are full files, not snippets. You can run them directly once `CALIBER_BASE_URL` and `CALIBER_TOKEN` are set.",
        "",
        "```bash",
        "export CALIBER_BASE_URL=https://caliber.example.com",
        "export CALIBER_TOKEN=calpat_...",
        "python sdk/caliber-sdk/examples/cookbooks/cookbook_01_trustworthy_intake_classifier.py",
        "```",
        "",
    ]
    for item in COOKBOOKS:
        lines.append(f"## Cookbook {item['id']} — {item['title']}")
        lines.append("")
        lines.append(item["summary"])
        lines.append("")
        lines.append(
            f"**SDK surfaces:** {', '.join(f'`{name}`' for name in item['surfaces'])}"
        )
        lines.append("")
        for index, step in enumerate(item["steps"], start=1):
            lines.append(f"{index}. {step}")
        lines.append("")
        lines.append("```python-file")
        lines.append(item["file"])
        lines.append("```")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"reference", "cookbooks"}:
        print("usage: generate_sdk_docs.py [reference|cookbooks]", file=sys.stderr)
        return 1
    if argv[1] == "reference":
        sys.stdout.write(render_reference())
    else:
        sys.stdout.write(render_cookbooks())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
