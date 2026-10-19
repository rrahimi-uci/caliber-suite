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
    "AsyncProjectFilesAPI",
    "AsyncProjectsAPI",
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
    "PlatformAdminInventory",
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
    "WorkspaceEnvironment",
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
    "rework_tasks": "Owned, recoverable work auto-created from a rejected refinement job.",
    "quality_reviews": (
        "A human go/no-go on a job's candidate, distinct from the machine eval gate."
    ),
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
    "caliber_sdk.aio.projects": "sdk/caliber-sdk/examples/workflow_run.py#run_and_wait",
    "caliber_sdk.aio.transport": "sdk/caliber-sdk/examples/workflow_run.py#run_and_wait",
    "caliber_sdk.aio.waiters": "sdk/caliber-sdk/examples/workflow_run.py#run_and_wait",
}

CLASS_EXAMPLES = {
    "CaliberClient": MODULE_EXAMPLES["caliber_sdk.client"],
    "AsyncCaliberClient": MODULE_EXAMPLES["caliber_sdk.aio.client"],
    "AsyncProjectFilesAPI": MODULE_EXAMPLES["caliber_sdk.aio.projects"],
    "AsyncProjectsAPI": MODULE_EXAMPLES["caliber_sdk.aio.projects"],
    "WorkflowRunsAPI": MODULE_EXAMPLES["caliber_sdk.resources.workflows"],
    "WorkflowsAPI": MODULE_EXAMPLES["caliber_sdk.resources.workflows"],
    "PromptsAPI": MODULE_EXAMPLES["caliber_sdk.resources.assets"],
    "EvalDatasetsAPI": MODULE_EXAMPLES["caliber_sdk.resources.quality"],
    "JudgesAPI": MODULE_EXAMPLES["caliber_sdk.resources.quality"],
    "EvaluationsAPI": MODULE_EXAMPLES["caliber_sdk.resources.quality"],
    "CookbooksAPI": "sdk/caliber-sdk/examples/agentic.py#install_ready_cookbook",
    "AriaAPI": MODULE_EXAMPLES["caliber_sdk.resources.operations"],
    "ProjectChangeRequestsAPI": (
        "sdk/caliber-sdk/examples/workspace_release.py#promote_a_reviewed_revision"
    ),
    "ProjectReleasesAPI": (
        "sdk/caliber-sdk/examples/workspace_release.py#promote_a_reviewed_revision"
    ),
    "ProjectReleaseOperationsAPI": (
        "sdk/caliber-sdk/examples/workspace_release.py#promote_a_reviewed_revision"
    ),
    "ProjectSourceConnectionAPI": (
        "sdk/caliber-sdk/examples/workspace_github_source.py"
        "#connect_github_source_and_import_on_push"
    ),
    "ProjectImportsAPI": (
        "sdk/caliber-sdk/examples/workspace_github_source.py"
        "#connect_github_source_and_import_on_push"
    ),
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
            "caliber_sdk.models.workspace",
            "caliber_sdk.models.errors",
        ],
    ),
    (
        "Async client",
        [
            "caliber_sdk.aio",
            "caliber_sdk.aio.client",
            "caliber_sdk.aio.projects",
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
        "summary": "Author and promote a strict-JSON intake prompt, import the 44-case labelled test set, register a weaker v2, then calibrate the live prompt, apply the candidate and roll it back, verifying the registry alias, live board, release timeline and audit log at each step; it exits 0 only if all 26 checks hold. It needs `CALIBER_FEATURE_COOKBOOKS=true`, an operator or admin token and `MLFLOW_TRACKING_URI`; on the default simulated stack evaluation is refused with 400 (the script asserts that refusal) and calibration evidence is canned and marked `simulated`, while a real model gives a scorecard that catches the v2 regression (`--skip-release` stops after that). It uses `client.raw` for `/readiness` and for the two `/evaluations` failure probes, and a plain `httpx` login to mint the viewer token.",
        "surfaces": ["cookbooks", "prompts", "datasets", "evaluations", "jobs", "releases", "audit", "auth"],
        "steps": [
            "Check readiness and that Cookbook 01 is listed with guided steps, then create the strict-JSON prompt v1 and promote it, verifying the `prod` alias in the MLflow registry.",
            "Import the 44-case labelled test set from the JSONL file and register a weaker v2 that is not made live.",
            "Evaluate v1 and v2 and catch the regression against the baseline (44/44 strict JSON against 0/44); on a simulated model verify the 400 refusal instead, and verify that evaluating a missing test set is a 404.",
            "Calibrate the live prompt through the typed prompts and jobs resources, wait for `candidate_ready`, and read its evidence (44 examples, dataset version, `simulated: true`) and diff.",
            "Exercise the failure paths: a viewer's apply is 403, a candidate below the gate is `rejected` and its apply is 409; then apply the candidate and verify the alias, live board, release timeline and audit log all name you.",
            "Roll back and verify the alias returned to v1 in the registry, on the live board and in the audit log.",
        ],
    },
    {
        "id": "02",
        "title": "Precision Skills",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_02_precision_skills.py",
        "summary": "Author the support-tone skill from the lab's asset files, preview its rendering, run two rounds of trigger-selection cases (30/40 with the starter phrases, 40/40 after tightening) and pin round 2 as the baseline, then calibrate, apply, roll back, export and re-import the skill package, and prove the failure paths and archive; it exits 0 only when every documented outcome holds. It needs an admin token plus a plain `lab02-viewer` account for the 403 checks and does not install the cookbook; skill calibration has no test set, so under the default simulated providers the candidate and scores are canned and the script asserts wiring only, while trigger selection is deterministic and needs no model. It uses `client.raw` only to post the invalid skill payloads, and a plain `httpx` login to mint the viewer token.",
        "surfaces": ["skills", "jobs", "auth"],
        "steps": [
            "Create the skill from the asset file (version 1, six starter exclusion phrases) and preview its rendering with all variables, then only one, checking the unresolved variables.",
            "Run trigger round 1 (30/40 right, below the 0.95 gate), tighten the trigger phrases without bumping the version, then run round 2 (40/40) and pin it as the baseline test run.",
            "Calibrate the skill, wait for `candidate_ready` (`simulated: true`, `n_examples: 0`), apply it as version 2, and roll back to the original text as version 3 (history 3, 2, 1).",
            "Export the package ZIP, check re-import under the same name is 409, import it under a new name, and show it keeps the instructions but not the tags, category or trigger phrases.",
            "Prove the failure paths: a viewer's apply, calibrate, rollback and create are each 403, invalid skill content is 400, a duplicate name is 409, and calibrating with no baseline pinned still works.",
            "Archive every skill the run created and confirm each leaves the default list.",
        ],
    },
    {
        "id": "03",
        "title": "Policy-Safe Decision Tool",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_03_policy_safe_decision_tool.py",
        "summary": "Register a deterministic read tool (`decide_refund`) and a write tool (`initiate_refund`) in a project, sandbox-test them, run and pin the 49-fixture calibration as the baseline, provoke a one-case regression, then import a refund workflow whose write step needs a second person's approval and drive approve, reject, deny and manual-review runs, checking the audit trail and where the sandbox boundary stops. It needs an admin token, the `cookbooks` flag (for the last step, installing the gallery starter) and `CALIBER_APPROVER_USERS` including `approver1`; it creates `approver1` and `viewer1` accounts, does not use a model, and the simulated providers play no part. It uses `client.raw` once, to read `/system/effects`, and plain `httpx` to sign the two accounts in.",
        "surfaces": ["projects", "tools", "workflows", "audit", "auth", "cookbooks"],
        "steps": [
            "Create a project, add `approver1` as reviewer and `viewer1` as viewer, and register the two tools; a write tool without `requires_approval` is refused with 400.",
            "Sandbox-test the tools (the read tool runs live in a subprocess, the write tool is mocked) and check viewer and approver scopes get 403.",
            "Save the 49 fixtures and calibrate to 49/49, record the suite run and pin it as the baseline, then run a regression drill whose single wrong fixture shows as exactly one regression against that baseline.",
            "Import the refund-decision workflow and run it through the approval gate: the run waits at the `refund` node, the initiator's own approval is 403, `approver1` approves, Resume is needed, then exactly one refund fires; a rejected run fails with `approval_rejected`, and a deny decision completes with no approval.",
            "Read the audit trail and the effect ledger, then probe the sandbox boundary: a stalled tool is killed, a large file write is refused, the environment is empty, and the network is still reachable.",
            "Install the Cookbook 03 gallery starter and verify its draft holds a decision table and a Human Approval node.",
        ],
    },
    {
        "id": "04",
        "title": "Document-to-JSON Pipeline",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_04_document_to_json_pipeline.py",
        "summary": "Upload 30 sample documents to an object-store bucket, install the built-in Document-to-JSON Pipeline in a project, run it once per document, and check every outcome against the lab's expectations: 16 valid invoices land as JSON in the output bucket, 13 invalid ones are held at the approval gate with exact schema errors, a duplicate is refused, and one held document is approved and one rejected, leaving exactly 17 output objects. It needs an admin token, the `cookbooks` flag, an S3-compatible object store behind the server and a `reviewer` account holding the operator and approver scopes; nothing calls a model, so simulated providers change nothing. It does not use `client.raw`; plain `httpx` signs the reviewer in.",
        "surfaces": ["cookbooks", "projects", "object_store", "workflows", "auth"],
        "steps": [
            "Check the recipe's configuration checks and that the object store answers, then create a project with a second person (`reviewer`) in it.",
            "Create the `doc-intake` and `doc-extracted` buckets and upload the 30 documents under `incoming/`.",
            "Install the pipeline (it installs paused), validate the draft, resume it, and run it once per document with the object key as input; compare each extracted JSON field by field and each held document's schema errors by path.",
            "Verify a duplicate invoice and a key that matches no object both fail with a clear message.",
            "Exercise the approval gate: the account that started the run is refused with 403, the reviewer approves one held document (its JSON lands under `reviewed/`) and rejects another (`approval_rejected`, nothing written).",
            "Confirm the output bucket holds exactly the 17 expected objects.",
        ],
    },
    {
        "id": "05",
        "title": "Governed Tool Connectivity",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_05_governed_tool_connectivity.py",
        "summary": "Register the lab's own stdio MCP server in a project, discover its six tools (all start unclassified and denied), classify them, then invoke a read tool, check refusals (blocked, unknown, rate limited) and prove from the server's ledger that a refused call never reached it, calibrate three read tools (16/16, 8/8, 4/4) and catch a deliberately wrong expectation, and run the write tool behind a workflow approval gate. It needs an admin token, the `integrations` and `cookbooks` flags, operator and approver accounts, and the MCP server script on the stdio allowlist; it runs a real MCP server and no model, so nothing is simulated. It does not use `client.raw`; plain `httpx` signs the lab accounts in.",
        "surfaces": ["projects", "mcp_servers", "workflows", "observability", "audit", "auth", "cookbooks"],
        "steps": [
            "Create a project with operator, approver and viewer accounts, register the stdio MCP server, run `test_connection` and `discover_tools`, and check the server-side refusals (an unclassified tool cannot be called, a write tool needs `requires_approval`).",
            "Classify every tool through `update_tool_policy`, then invoke the allowed read tool and check the ledger shows exactly one execution.",
            "Save 28 test cases and calibrate the three read tools, catch one wrong expectation (15/16, pass rate 0.9375), and check calibrating an approval-class, case-less or missing tool is refused (409, 400, 404).",
            "Check the refusals (blocked `create_release`, unknown `drop_database`, over the rate limit) and the tool's own errors, and that the ledger shows none of the refused calls ran.",
            "Bind the write tool in an imported workflow: it stops at `waiting_approval`, the initiator's approval is 403, and it runs once only after a second person approves and the run is resumed; a blocked tool in a workflow is refused at submit.",
            "Check that `/bin/sh`, `python -c` and loopback HTTP servers are registered but refused, then read the history, audit log and `mcp.<server>.<tool>` traces (a refusal leaves none) and install the gallery starter.",
        ],
    },
    {
        "id": "06",
        "title": "Grounded Knowledge Assistant",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_06_grounded_knowledge_assistant.py",
        "summary": "Upload 11 policy documents to an object-store bucket, build knowledge base version 1 and check that the answer cites the chunk holding the clause, compare dense, hybrid and graph_hybrid retrieval over 41 answerable questions, calibrate Recall@k and nDCG@k against a pinned baseline, build a tuned version 2, test activation, rollback and failing builds, then install the answer workflow and route its low-confidence run to a review queue. It needs an admin token, the `cookbooks` flag, an S3-compatible object store and the local `all-MiniLM-L6-v2` embedding model; retrieval and its metrics are real, while the answer text, model-judged metrics (Faithfulness, Answer-correctness) and prompt scoring (refused with 400) are simulated on the default stack. It uses `client.raw` once, for `/readiness`.",
        "surfaces": ["cookbooks", "knowledge_bases", "object_store", "datasets", "workflows", "review_queues", "prompts", "judges", "evaluations"],
        "steps": [
            "Read readiness, the object store status and the knowledge-base options (chunking strategies and embedding models), then upload the 11 documents to `policy-corpus/policies/`.",
            "Create the knowledge base and build version 1 through the queue and worker (11 sources, 27 chunks of 384-dimension vectors), and query it to check the answer cites the `data-retention-policy.md` chunk containing `365 days`.",
            "Compare dense, hybrid and graph_hybrid hit rates at top 3, check what a 0.42 score gate refuses (every out-of-corpus question, not the two trap questions), then calibrate and pin the baseline run.",
            "Build the tuned version 2 (69 chunks), and verify activation, rollback, a build that skips an unreadable file (it completes with `failed_sources: 1`), and a build with nothing readable (it fails and cannot be activated).",
            "Install the answer workflow, run three questions through it, and enqueue only the low-confidence run in a review queue, where the reviewer's answer is written back to the trace.",
            "Store the answer prompt and the faithfulness judge, and check scoring the prompt is refused with 400 on the simulated model.",
        ],
    },
    {
        "id": "07",
        "title": "Support Triage Copilot",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_07_support_triage_copilot.py",
        "summary": "Create a project with two reviewers, install the model-free Cookbooks starter and prove the approval rules on it, import the lab's support-triage workflow, promote it to a `staging` alias, and run all 44 tickets of `ticket-cases.jsonl` through the run queue, asserting decision, severity, SLA, approval flag, flags and node path (44/44) and that exactly the 15 approval-required tickets pause at the gate. It then answers the gate (one approved, one rejected, the rest cancelled), ships a deliberately bad version that the dataset catches, rolls back, and reads the run audit trail. It needs an admin-scoped SDK token, a server started with `CALIBER_FEATURE_COOKBOOKS=true`, `CALIBER_OPERATOR_USERS=admin,lena,sam`, `CALIBER_APPROVER_USERS=admin,lena` and a `staging` approval policy, and no model: the classifier is deterministic rules in one Python Code node and the `draft_reply` agent runs on the simulated provider. The script creates the `lena` and `sam` accounts, signs them in with plain `httpx` calls to `/auth/login`, and does not use `client.raw`.",
        "surfaces": ["cookbooks", "projects", "auth", "workflows", "datasets", "audit"],
        "steps": [
            "Create a project, add `lena` (approver) and `sam` (operator without `caliber.approver`) as reviewers, and check their scopes.",
            "Install the starter recipe (it arrives paused), activate and publish it, and prove the approval rules: the person who started the run cannot approve it (403), an operator without the scope cannot (403), a second person with the scope approves and the run completes, and a rejection ends it `failed` with `approval_rejected`.",
            "Import the triage workflow, validate it, confirm Preview refuses its `python_code` node, publish it, and promote the `staging` alias (approved by the second person when the server asks).",
            "Load the 44 tickets as a test set, read them back, run every ticket through the queue against the alias, and check each run's decision, severity, SLA, approval flag, flags and path; answer the gate on live runs (approve one and release the action, reject one and release nothing, cancel the rest).",
            "Publish a version with a bad rule, move the alias onto it, watch the dataset catch the regression (3 of 11 bug tickets wrong), then roll back and see the same tickets pass again.",
            "Read the audit log to find who enqueued, approved and resumed the approved run; `--cleanup` archives what earlier runs created.",
        ],
    },
    {
        "id": "08",
        "title": "Incident Response Copilot",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_08_incident_response_copilot.py",
        "summary": "Install the incident-commander starter, import the lab workflow (refused until its 41-incident test set exists), Preview it, promote it through a gated `prod` alias whose deploy gate replays the dataset, then run all 41 incidents through the queue and assert severity, action and path (41/41) with exactly the 5 rollback incidents paused at the approval gate. It also checks that the gate refuses a bad version, exercises four failure drills (Error Boundary, unregistered tool, blocked API Request host, Wait For Event), rolls back, and asks for a workflow calibration. It needs an admin-scoped SDK token and a server started with `CALIBER_FEATURE_COOKBOOKS=true`, `CALIBER_OPERATOR_USERS=admin,lena,sam`, `CALIBER_APPROVER_USERS=admin,lena`, a `production` human-approval policy and the graded-executor requirement emptied; no model is needed. The deploy gate replays with the simulated executor and says so, `draft_update` runs on the simulated provider, and evidence is supplied inside each incident record. It does not use `client.raw` (it signs in as `lena` with plain `httpx` calls).",
        "surfaces": ["cookbooks", "projects", "auth", "workflows", "datasets", "evaluations", "jobs"],
        "steps": [
            "Install the starter recipe and run it through the queue; Preview refuses it because its API Request nodes cannot be isolated.",
            "Import the 41-incident test set (the workflow import is refused until it exists), then import and validate the commander workflow and Preview it, where every node is declarative and the dataset's verdicts hold.",
            "Publish it and move the aliases: `staging` rotates at once, and `prod` replays all 41 incidents through the deploy gate (pass rate 1.0, executor reported as simulated) and waits for a second person to approve; a bad rollback rule is refused by the gate and `prod` stays on version 1.",
            "Run every incident through the queue against `prod`, check severity, action and path (41/41), and answer the rollback gate: self-approval 403, approve releases `ROLLBACK_APPROVED`, reject releases nothing, cancel the rest.",
            "Run the failure drills: an Error Boundary degrades a failing lookup to a fallback, an unregistered tool is refused at import, a blocked API Request host fails the run with the egress reason, and a Wait For Event run pauses with a readable checkpoint and completes when its event arrives.",
            "Promote a good version and roll `prod` back to version 1, create a workflow calibration run (it reports no candidate), and confirm the Evaluations route refuses workflow scoring without a real model (400).",
        ],
    },
    {
        "id": "09",
        "title": "Self-Healing Workflows",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_09_self_healing_workflows.py",
        "summary": "Import the lab's own `refund-intake` workflow (it does not install the Cookbooks starter), run 35 tickets through version 1 and assert that exactly the documented 24 fail (17 at `validate`, 3 at `check_reference`, 4 at `decide`), read the debugger evidence and a same-way-failing retry, show what `propose-patch` can and cannot do for a code node, then write the patch as version 2, rerun all 35 (none fails, the 11 that worked are byte-identical), and exercise the `staging` alias, a rollback, and the `prod` refusal. It needs an admin-scoped SDK token, the lab's stand-in account service (`assets/upstream/account_service.py`, port 8780, not a real system) and a server started with `CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1`; it needs no model and no feature flag. It uses only typed `client.workflows` calls (no `client.raw`); `--cleanup` deletes the workflow it created.",
        "surfaces": ["workflows"],
        "steps": [
            "Import `ticket-intake.v1.workflow.yaml` as `refund-intake` and publish version 1.",
            "Run the 35 tickets of `failing-inputs.jsonl` through version 1 and check that exactly the documented tickets fail, at the documented node, with the documented error.",
            "Read the debugger evidence for one failing run of each kind (the `ERROR` span in the trace, the `workflow.run.failed` event, no checkpoint), then retry one and confirm it fails at the same node as attempt 2 of 2 with a two-attempt lineage.",
            "Ask `propose-patch` for a fix: it returns a generic prompt suggestion with no graph change for the timed-out node, and a guardrail patch aimed at a code node is refused with a 400.",
            "Write the patch (three node bodies) as version 2 of the same workflow, validate and publish it, and rerun all 35 tickets: none fails and the 11 that already worked produce identical output.",
            "Promote version 1 then version 2 to `staging`, run through the alias, roll the alias back and see the same ticket fail again, and confirm `prod` refuses a version with no deploy gate (400).",
        ],
    },
    {
        "id": "10",
        "title": "Trustworthy Evaluation",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_10_trustworthy_evaluation.py",
        "summary": "Load a 50-row test set, create a `FaithfulnessJudge` and a deliberately weak `LenientJudge` (a template with an unsupported variable is refused with a 400), seed 50 traces, have them reviewed in a queue with the human labels from the dataset, import those labels, and compute the judge's agreement, Cohen's kappa and confusion counts, recomputing each independently and comparing it with the server's numbers before certifying or rejecting the judge on the lab's thresholds (agreement >= 0.80, kappa >= 0.60, at least 30 rows). It runs in two modes, chosen by asking the server: with a model behind it (a real provider or the lab's stand-in model) every step runs, and on the default simulated stack (`CALIBER_LLM_PROVIDER=fake`) the Evaluations and the judge are refused (400, 'real LLM provider') and the script verifies those refusals plus the model-free steps and a calibration job whose canned score is marked simulated. It needs an admin-scoped SDK token, `CALIBER_FEATURE_COOKBOOKS=true`, and `mlflow` plus `MLFLOW_TRACKING_URI`, because the lab's `seed_traces.py` writes the traces with MLflow directly, not through the SDK. It does not use `client.raw`; `--expect-stand-in` pins the stand-in model's exact numbers and `--cleanup` archives what it created.",
        "surfaces": ["cookbooks", "datasets", "judges", "evaluations", "review_queues", "prompts", "jobs"],
        "steps": [
            "Read the recipe through `client.cookbooks`, then create the 50-row test set (dataset version 51) and read it back.",
            "Create `FaithfulnessJudge` and `LenientJudge`, show a template using `{{ evidence }}` refused with a 400 that names it, and show an evaluation of an empty test set refused.",
            "Score a baseline and a candidate with the deterministic scorers and the judge and read the per-row results (with a model); on the simulated stack, verify the refusal the product gives instead.",
            "Seed 50 traces, create the review queue, enqueue every trace, submit the human answers, and import the labels (50 imported, none skipped).",
            "Compute judge/human alignment: recompute agreement, Cohen's kappa and confusion counts independently and compare, certify or reject each judge on the lab's rule, and show that five rows alone are not certifiable (with a model); with no model every row errors and a calibration job reports a simulated score.",
            "Feed the nine disagreements back into the test set as hard cases (dataset version 60, with a model).",
        ],
    },
    {
        "id": "11",
        "title": "Release Signoff Factory",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_11_release_signoff_factory.py",
        "summary": "Create a real prompt with two versions, create the 20 release candidates of `release-candidates.jsonl` as an operator and assert the server's weighted score, status and blockers against the dataset's independently worked figures (14 ready, 6 blocked), then exercise refusals, waivers, the Allure-compatible report job, go and no-go signoffs, final-once-signed behaviour, and the audit trail. It asserts that signing off deploys nothing (no alias moves; a separate prompt promote does), and that a candidate for a non-existent artifact is accepted because CALIBER records the reference without looking it up. It needs an admin-scoped SDK token, `CALIBER_FEATURE_RELEASE_CANDIDATES=true`, and `CALIBER_OPERATOR_USERS` listing `lab11-operator` next to `admin`; no model is involved and nothing is simulated. It creates the `lab11-operator` and `lab11-viewer` accounts and signs them in with plain `httpx` calls to mint their tokens (no `client.raw`); it does not call the cookbooks resource.",
        "surfaces": ["releases", "prompts", "auth", "audit"],
        "steps": [
            "Create a `payments-assistant` prompt with two versions and check that no alias points at either.",
            "Create the 20 candidates as the operator and check each weighted score, status and blocker list against the dataset (14 ready, 6 blocked).",
            "Show what a candidate is refused for (zero weight, score above 1, a key with a space, no criteria, an unknown field, a viewer creating one) and that a candidate for a missing artifact is accepted.",
            "Record waivers (admin only; reason, expiry and criterion key validated) and show that a waiver clears a blocker but cannot make a low score ready (GO refused with 409), with a stale report snapshot hash before and after.",
            "Generate the report job, sign RC20 GO, RC19 NO-GO and RC18 GO as admin (an operator cannot), and show a signed candidate is final: re-evaluate, waiver and a second signoff all return 409.",
            "Show the signed GO deployed nothing (aliases, live board and timeline unchanged until a separate prompt promote), then read each action and its actor back from the audit log.",
        ],
    },
    {
        "id": "12",
        "title": "Aria: Evaluation Harness from Intent",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_12_aria_evaluation_harness.py",
        "summary": "Check Aria's nine capabilities and draw a plan for each of 28 goals (28/28 match the dataset, every draft cancelled, nothing run), then state the canonical intent, approve the two-step plan (`judge.create`, `eval_dataset.create`), answer its four interactions with typed values, and assert the active judge and the empty version-1 test set exist under the typed names. It then loads the 30 rows of `faithfulness-cases.jsonl`, checks them back (version 31), and provokes the refusals (a goal with no steps, a bad form answer, a denied step, a judge with no evaluation variable, a taken name, no operator scope, a non-owner). It needs an admin-scoped SDK token, `CALIBER_FEATURE_ARIA=true` and `CALIBER_OPERATOR_USERS=admin,olga`, and creates the `olga` and `vera` accounts (signed in with plain `httpx` calls, no `client.raw`). Aria's planner is deterministic rules and no model is needed: running the judge in Evaluations is refused with a 400 on the simulated stack and the script verifies that refusal instead of a score (with a real provider it asserts all 30 rows were scored). It does not call the cookbooks resource.",
        "surfaces": ["aria", "judges", "datasets", "evaluations", "auth"],
        "steps": [
            "Check that Aria lists its nine capabilities with their tiers, and draw a plan for each of the 28 goals in `goals.jsonl`, comparing it with the dataset (28/28) and cancelling every draft.",
            "Create the plan from the typed intent (it draws `judge.create` then `eval_dataset.create`), approve it and execute it; it pauses on the judge step waiting for input.",
            "Answer the four interactions (judge fields, approve, test set fields, approve) until the plan reads `completed`, then check the judge is active as `bool` and the test set has its own name, version 1 and no rows.",
            "Add the 30 cases to the test set, read them back (30 rows, version 31, each stored row equal to the file's row), and run the test set in Evaluations, which is refused (400) without a real model.",
            "Provoke the refusals: a goal with no steps cannot be approved (409), a form rejects an unknown field, a wrong type, an empty name and a missing field, a denied step is skipped while the other runs, a judge with no evaluation variable fails its step, and a taken judge name fails its step.",
            "Show the role rules: an account without the operator scope is refused (403) and creates nothing, and an operator who does not own the plan cannot answer its question (403) or see it (404).",
        ],
    },
    {
        "id": "13",
        "title": "Aria: Human-Review Queue from Intent",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_13_aria_review_queue.py",
        "summary": "Seed 30 flagged agent replies as MLflow traces, state one sentence of intent to Aria, answer its four questions so it creates a review queue and enqueues the 30 traces, review 12 of them as the queue's owner, and check that each answer landed on its trace as an assessment and as a labelled alignment example; then provoke the refusals (bad answers, a missing trace id, skipped, denied and failing steps, a non-operator account, an operator who does not own the queue) and assert the exact status codes. It needs `CALIBER_FEATURE_ARIA=true`, an admin-scoped token, `CALIBER_OPERATOR_USERS=admin,olga` (the script also creates `vera`, a non-operator), `mlflow` with `MLFLOW_TRACKING_URI` for the seed traces; no model is needed because Aria's planner is deterministic. It does not install a cookbook, and it reads the queue back with `client.raw` (`GET /review-queues/{id}`) in several places.",
        "surfaces": ["aria", "review_queues", "observability", "auth"],
        "steps": [
            "Write the 30 flagged replies to MLflow as traces and draw a plan for each of the 26 goals in the dataset, checking every plan against the expected steps (every draft is cancelled, nothing runs).",
            "State the canonical intent and answer four questions: the queue's fields, an Approve, the 30 trace ids, an Approve; check the plan completed, the queue is active with its five questions, and it holds the 30 traces, all pending.",
            "Review 12 of the 30 as the queue's owner through `review_queues.submit`, then check each answer is on its trace as an assessment and that the safety answers are 12 labelled examples for judge alignment.",
            "Provoke the review refusals: a missing, mistyped, unknown-option or unknown-question answer (400), a second review (409), a non-operator (403), an operator who does not own the queue (404), and a trace id that does not exist (queued, then 502 on review).",
            "Provoke the plan refusals: a skipped queue skips the enqueue step, a denied enqueue keeps the empty queue, malformed forms stay open (409), a categorical question without options and a taken name fail their step, a goal with no steps is refused at approval, and a plan owned by a non-operator fails at execute.",
        ],
    },
    {
        "id": "14",
        "title": "Aria: Governance Starter Kit from Intent",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_14_aria_governance_starter_kit.py",
        "summary": "State one sentence of intent to Aria and answer the seven questions its plan asks, so it creates a judge, a review queue and a test set (and skips the enqueue step, since no traces exist yet); then fill the test set with 30 cases, rerun the sentence under a lower autonomy level to show only the forms are asked, and provoke the refusals (a denied step that takes its dependent with it, a failure half way, an invalid judge, a goal with no steps, non-operator and non-owner accounts). It needs `CALIBER_FEATURE_ARIA=true`, an admin-scoped token and `CALIBER_OPERATOR_USERS=admin,olga` (the script also creates `vera`, a non-operator); no model is needed, and running the judge through Evaluations is expected to be refused (400) on a simulated provider, which the script verifies. It does not install a cookbook and does not use `client.raw`.",
        "surfaces": ["aria", "judges", "datasets", "review_queues", "evaluations", "auth"],
        "steps": [
            "Draw a plan for each of the 28 goals in the dataset and compare it with the expected steps (every draft is cancelled, nothing runs).",
            "State the canonical intent under 'Stop before each change' and answer seven questions (three forms with an Approve each, then the enqueue form, skipped because there are no traces); check the judge, the review queue and the test set exist under three different names.",
            "Add the 30 cases of the dataset to the test set through `eval_datasets.add_example`, check each stored row, and confirm that Evaluations are refused (400) without a real model.",
            "Run the same sentence under 'Run everything it safely can' after changing the draft's autonomy, and check that only the four forms are asked and the same kit is built; a plan can no longer be edited after approval (409).",
            "Provoke the refusals: a denied queue skips the enqueue step with it, a taken queue name fails the plan after the judge was made, a judge with no evaluation variable fails before anything is made, a goal with no steps is refused at approval, and a non-operator or non-owner cannot run or answer the plan.",
        ],
    },
    {
        "id": "15",
        "title": "Aria: Triage & Recalibrate Loop",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_15_aria_triage_recalibrate_loop.py",
        "summary": "Register the existing subject Aria remediates (two tools, a published support workflow with its 30-case test set as deploy gate, an agent), seed 12 weak runs as MLflow traces, then state one sentence of intent and answer six questions so Aria creates a triage queue, enqueues the 12 traces and starts a real workflow calibration; the plan parks in `waiting_job`, is resumed by the plan worker (falling back to `aria.poll_plan`) and must end where the job ended, after which a reviewer triages three runs and the refusals are provoked. It needs `CALIBER_FEATURE_ARIA=true`, an admin-scoped token, `CALIBER_OPERATOR_USERS=admin,olga` (the script also creates `vera`, a non-operator), `mlflow` with `MLFLOW_TRACKING_URI`, and the server's plan and refinement workers running; no model is needed, and on a simulated deployment the calibration job ends `rejected` (gain 0.000, below the 0.02 gate) so the plan ends `failed` with that reason (a real model may reach `candidate_ready`, and the script accepts either). It does not install a cookbook; it reads the queue back with `client.raw` (`GET /review-queues/{id}`) twice.",
        "surfaces": ["aria", "review_queues", "observability", "workflows", "datasets", "tools", "jobs", "auth"],
        "steps": [
            "Register the two tools, import and publish the support workflow with its test set as the deploy gate, and register an agent (the existing subject Aria remediates); write the 12 weak runs to MLflow as traces and check the planner's drawn plan for each of the 28 goals.",
            "State the canonical intent and answer six questions (the queue's fields, an Approve, the 12 trace ids, an Approve, the workflow and agent ids, an Approve); the last Approve enqueues a calibration job and parks the plan as `running` with the step `waiting_job`, and a parked plan cannot be edited (409).",
            "Wait for the plan worker to resume the plan (or advance it with `aria.poll_plan`), then check it ends where the job ended, polling a finished plan changes nothing, and the job replayed 20 of the gate's 30 cases and reports its baseline.",
            "Check the triage queue holds the 12 flagged traces pending, then have a reviewer triage three of them and verify the verdict, failure mode and expected answer land on their traces.",
            "Provoke the refusals: an unknown workflow, an unregistered agent and a workflow with no deploy-gate test set fail at once and queue no job, malformed forms stay open (409), a denied calibration queues nothing, and a non-operator or non-owner cannot run, answer or poll a plan.",
        ],
    },
    {
        "id": "16",
        "title": "Production Observability & Triage",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_16_observability_triage.py",
        "summary": "Import and publish the cookbook 09 workflow and submit 40 production tickets as real runs, read the resulting traces back through Observability (12 ERROR, 28 OK, 4 of the OK ones silently wrong), capture the 16 traces that matter into a test set with their gold outcome, and stand up the triage queue, enqueue those traces and answer two, checking the answers become assessments on the traces. It needs no Aria flag and no model (the workflow has no agent node, so the simulated provider changes nothing), but it does need cookbook 09's stand-in account service (not a real system) on port 8780, `CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1` on the server, and the 40 sample tickets; it does not install a cookbook through `client.cookbooks` and does not use `client.raw`.",
        "surfaces": ["workflows", "observability", "datasets", "review_queues"],
        "steps": [
            "Seed production traffic: import and publish the workflow and submit the 40 tickets as runs, so the traces are the ones the runtime records (none is written by the script).",
            "Read the traces back through Observability: the status counts (12 ERROR, 28 OK), the Error filter, and the request and node tree of one failing trace per failure kind; find the 4 OK traces whose answer differs from the gold outcome.",
            "Check the failure paths: an unknown trace id is a 404, a window in the future is an empty list, and a trace's run is found from its trace id.",
            "Capture the 16 failing or silently wrong traces into a test set with `eval_datasets.add_from_trace`, each with its gold outcome and tags.",
            "Create the triage queue, enqueue the same 16 traces, answer two of them and check the answers are written back onto the traces as assessments.",
        ],
    },
    {
        "id": "17",
        "title": "Monthly Financial Analysis",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_17_financial_analysis.py",
        "summary": "Create a project and object-store bucket, generate and persist realistic monthly financial CSV data, register and promote a dashboard-ready statistics prompt, execute it through a typed assistant session, then validate the returned mean/median/percentile/extrema JSON, persist it and download-verify it -- entirely through typed SDK resources. This standalone example (it has no lab folder and does not install a cookbook) needs an admin-scoped SDK token for object-store mutations, a connected object store, a configured model provider for live prompt execution, and `CALIBER_FEATURE_ARIA=true`, because the assistant-session surface (`/assistant/*`) is served only when that flag is on; it does not use `client.raw`.",
        "surfaces": ["projects", "object_store", "prompts", "aria"],
        "steps": [
            "Create the financial-analysis project and select it as the temporary SDK project scope.",
            "Create a dedicated blob bucket, generate the CSV in memory, upload and download-verify it, then import the object into the project's governed file registry for lineage.",
            "Create and re-read the registered prompt, promote its final version to the prod alias, and verify it is visible through the Prompt dashboard inventory.",
            "Execute the prompt through the typed assistant-session surface (`aria.sessions.create` and `send_message`), surfacing any provider failure, and validate the mean/median/percentile/extrema JSON against statistics computed from the CSV.",
            "Upload the result document to blob storage and download-verify it.",
        ],
    },
    {
        "id": "18",
        "title": "Projects, Roles, Tokens & Audit",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_18_projects_roles_tokens_audit.py",
        "summary": "Create a project for a nine-person team, add the members with owner, editor, reviewer and viewer roles, prove the role matrix from a data file with real requests, issue a project-bound access token, show it refused outside its project and after revocation or expiry, release one prompt, and read every one of those actions back from the audit log, the release timeline and readiness. This recipe needs an admin-scoped SDK token and accounts whose platform scopes match the roster.",
        "surfaces": ["projects", "auth", "tokens", "audit", "releases", "cookbooks"],
        "steps": [
            "Check `/health` and `/readiness`, and record which providers are simulated.",
            "Create the team's accounts, sign each person in, create the project as its primary owner, and add the members with their roles.",
            "Send every roster person every action in the role matrix and assert the status code the data file expects (403 for a role that may not, 404 for a non-member).",
            "Issue a token bound to the project, use it from the SDK and `caliberctl` to create and release a prompt, and read the release back from the timeline and the live board.",
            "Provoke the refusals: the primary owner cannot be removed, roles cannot be escalated, a bound token is refused in another project, and a revoked or expired token gets 401.",
            "Read the audit log by actor, action and entity to find each of those actions, then offboard a member and end the sessions the script opened.",
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
    if class_name == "CaliberClient" and member.name == "project_scope":
        # Delegates to ``workspace_scope`` via a ``with`` statement, so the
        # ``raise CaliberConfigError(...)`` it can propagate lives in a
        # different function body and a single-function AST walk never sees it.
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


#: Base classes that carry no public surface. ``Resource``, ``_AsyncResource``,
#: and the project module's ``_AsyncProjectResource``
#: exist so every resource class shares one envelope-unwrapping request path --
#: their entire membership is ``_get``/``_post``/..., so a section for them would
#: document nothing a caller can call. Naming them in a **Bases:** line was worse
#: than omitting them: 38 classes pointed at a type the reference never defines,
#: which is a dead end for the reader rather than a hint.
INTERNAL_BASES = frozenset({"Resource", "_AsyncResource", "_AsyncProjectResource"})


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
        "Each script on this page finishes its scenario entirely through typed SDK calls. Recipes 03 to 08, 10 and 18 install their versioned entry from the built-in cookbook catalog, recipe 01 checks that its entry is listed, the others drive the lab's own prompts, skills, workflows or assets directly, and recipe 17 is a standalone SDK automation that creates its own project and assets. `client.raw` is the SDK's permanent escape hatch for anything not yet wrapped; a few scripts use it for a readiness probe, an invalid payload or a route the typed layer does not cover, and each summary below says so. Some scripts sign test accounts in with plain `httpx` calls, since the SDK acts as one token.",
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
