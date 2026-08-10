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
from typing import Any

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


def format_annotation(node: ast.AST | None) -> str:
    if node is None:
        return "Any"
    return ast.unparse(node)


def format_default(node: ast.AST | None) -> str:
    if node is None:
        return "—"
    return ast.unparse(node)


def linkify_types(text: str) -> str:
    anchors = {
        "CaliberClient": heading_link("`CaliberClient`"),
        "AsyncCaliberClient": heading_link("`AsyncCaliberClient`"),
        "WorkflowRunFailed": heading_link("`WorkflowRunFailed`"),
        "WaitTimeout": heading_link("`WaitTimeout`"),
        "WaitFailed": heading_link("`WaitFailed`"),
        "CaliberError": heading_link("`CaliberError`"),
        "CaliberConfigError": heading_link("`CaliberConfigError`"),
        "CaliberTransportError": heading_link("`CaliberTransportError`"),
        "CaliberAPIError": heading_link("`CaliberAPIError`"),
        "CaliberAuthenticationError": heading_link("`CaliberAuthenticationError`"),
        "CaliberPermissionError": heading_link("`CaliberPermissionError`"),
        "CaliberNotFoundError": heading_link("`CaliberNotFoundError`"),
        "CaliberConflictError": heading_link("`CaliberConflictError`"),
        "CaliberValidationError": heading_link("`CaliberValidationError`"),
        "CaliberRateLimitError": heading_link("`CaliberRateLimitError`"),
        "CaliberServerError": heading_link("`CaliberServerError`"),
    }
    for name in (
        "Account",
        "AriaInteraction",
        "AriaPlan",
        "AriaPlanDetail",
        "AriaPlanStep",
        "AuditEntry",
        "Bucket",
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
        "Workflow",
        "WorkflowRun",
        "WorkflowRunCapabilities",
        "WorkflowService",
        "WorkflowVersion",
    ):
        anchors.setdefault(name, heading_link(f"`{name}`"))

    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        anchor = anchors.get(token)
        if not anchor:
            return token
        return f"[{token}]({anchor})"

    return re.sub(r"\b[A-Z][A-Za-z0-9_]+\b", replace, text)


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
    "capabilities_api": "Runtime stability tiers and deployment capabilities.",
    "settings": "Runtime and LLM configuration inventory.",
    "projects": "Projects plus the managed file registry.",
    "prompts": "Prompt registry authoring and promotion.",
    "skills": "Skill registry, render tests, selection tests, and versions.",
    "tools": "Tool registry, schemas, and deterministic calibration.",
    "workflows": "Workflow registry plus versions, runs, and services.",
    "datasets": "Evaluation datasets and examples.",
    "judges": "Model-backed graders and alignment scoring.",
    "evaluations": "Scored dataset runs.",
    "mcp_servers": "Managed MCP server registry and governed tool invocation.",
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
    "caliber_sdk.resources.auth": "sdk/caliber-sdk/examples/tokens.py#issue_scoped_token",
    "caliber_sdk.resources.assets": "sdk/caliber-sdk/examples/prompt_lifecycle.py#prompt_lifecycle",
    "caliber_sdk.resources.quality": "sdk/caliber-sdk/examples/evaluation.py#build_and_score",
    "caliber_sdk.resources.workflows": "sdk/caliber-sdk/examples/workflow_run.py#run_and_wait",
    "caliber_sdk.resources.operations": "sdk/caliber-sdk/examples/agentic.py#plan_from_intent",
    "caliber_sdk.aio.client": "sdk/caliber-sdk/examples/workflow_run.py#run_and_wait",
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
        "summary": "Install the versioned recipe, create a regression dataset, register the compliance judge, and launch a scoreable evaluation run.",
        "surfaces": ["cookbooks", "datasets", "judges", "evaluations"],
        "steps": [
            "Inspect cookbook readiness and acknowledge prerequisites before installation.",
            "Install the built-in recipe as a paused workflow and editable draft.",
            "Create the dataset, add labeled intake rows, register the JSON-compliance judge, and start an evaluation run.",
        ],
    },
    {
        "id": "02",
        "title": "Precision Skills",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_02_precision_skills.py",
        "summary": "Install the recipe, register the reusable skill, run render and selection tests, and trigger skill calibration through the live route.",
        "surfaces": ["cookbooks", "skills", "raw"],
        "steps": [
            "Materialize the cookbook draft through the built-in installer.",
            "Create the skill and immediately prove its variable rendering and trigger-selection behavior.",
            "Start the server-side calibration job through `client.raw` so the example uses the current backend route without re-implementing it.",
        ],
    },
    {
        "id": "03",
        "title": "Policy-Safe Decision Tool",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_03_policy_safe_decision_tool.py",
        "summary": "Install the recipe, register the deterministic tool, persist hardening cases, and run a calibration pass against those fixtures.",
        "surfaces": ["cookbooks", "tools", "raw"],
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
        "summary": "Install the recipe, upload a source document into a project, and validate the generated workflow draft against the uploaded managed file.",
        "surfaces": ["cookbooks", "projects", "workflows"],
        "steps": [
            "Create a project-scoped home for the source documents and upload a managed file through the SDK.",
            "Install the cookbook draft so the platform materializes the maintained workflow manifest for you.",
            "Validate the installed workflow version and return the file/workflow identities needed for the next execution step.",
        ],
    },
    {
        "id": "05",
        "title": "Governed Tool Connectivity",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_05_governed_tool_connectivity.py",
        "summary": "Install the recipe, connect an MCP server, test reachability, discover tools, enforce a policy overlay, and calibrate the allowed tool.",
        "surfaces": ["cookbooks", "mcp_servers"],
        "steps": [
            "Install the official recipe and connect the target MCP server through the registry surface.",
            "Probe the server, refresh the discovered inventory, and apply a policy block to the write tool.",
            "Save calibration cases for the read tool and start the governed calibration run.",
        ],
    },
    {
        "id": "06",
        "title": "Grounded Knowledge Assistant",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_06_grounded_knowledge_assistant.py",
        "summary": "Install the recipe, create the knowledge base, build a version, query it, and launch a retrieval calibration run.",
        "surfaces": ["cookbooks", "knowledge_bases"],
        "steps": [
            "Install the recipe as the versioned workflow scaffold for the scenario.",
            "Create the knowledge base and register a first version from SDK-supplied source metadata.",
            "Query the active corpus and launch an inline calibration run to capture retrieval evidence.",
        ],
    },
    {
        "id": "07",
        "title": "Support Triage Copilot",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_07_support_triage_copilot.py",
        "summary": "Install the recipe, create the support review queue, build the evaluation dataset, and score grounded replies with a custom judge.",
        "surfaces": ["cookbooks", "review_queues", "datasets", "judges", "evaluations"],
        "steps": [
            "Install the maintained recipe instead of copying a workflow manifest into the script.",
            "Create the human-review queue that governs escalations and issue filing.",
            "Create the support dataset, register the grounding judge, and launch the evaluation run.",
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
        "summary": "Install the recipe, publish the workflow version, submit a run, wait for the failure state, and capture the failed run for triage.",
        "surfaces": ["cookbooks", "workflows"],
        "steps": [
            "Install the cookbook draft and promote the version from draft to runnable.",
            "Submit the workflow run against the installed version with an idempotency key.",
            "Wait for the run to stop and return the failure state without hiding it behind a generic exception.",
        ],
    },
    {
        "id": "10",
        "title": "Trustworthy Evaluation",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_10_trustworthy_evaluation.py",
        "summary": "Install the recipe, build the dataset, register the judge, create the evaluation, and provision the review queue used for disagreement analysis.",
        "surfaces": ["cookbooks", "datasets", "judges", "evaluations", "review_queues"],
        "steps": [
            "Install the versioned recipe through the SDK so the platform owns the scaffold.",
            "Create the evaluation dataset and the custom judge that scores grounded correctness.",
            "Launch the evaluation run and create the queue that will collect disagreement items for human review.",
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
        "summary": "Install the recipe, ask Aria for the plan, approve it, execute it, and return the resulting plan state.",
        "surfaces": ["cookbooks", "aria"],
        "steps": [
            "Install the recipe so the workflow scaffold stays aligned with the product catalog.",
            "Create the Aria plan from a typed intent, then wait until it pauses or completes.",
            "Approve and execute the plan explicitly rather than inferring approval from continued script execution.",
        ],
    },
    {
        "id": "13",
        "title": "Aria: Human-Review Queue from Intent",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_13_aria_review_queue.py",
        "summary": "Install the recipe, create the Aria plan, approve it, and inspect the resulting review queue inventory.",
        "surfaces": ["cookbooks", "aria", "review_queues"],
        "steps": [
            "Install the catalog-managed recipe for the review-governance scenario.",
            "Drive the plan lifecycle through the Aria surface until the queue-creation steps settle.",
            "Read back the queue inventory through the typed review-queue API so the result is visible without opening the UI.",
        ],
    },
    {
        "id": "14",
        "title": "Aria: Governance Starter Kit from Intent",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_14_aria_governance_starter_kit.py",
        "summary": "Install the recipe, run the multi-artifact Aria plan, and return the resulting judge, dataset, and review-queue inventory.",
        "surfaces": ["cookbooks", "aria", "judges", "datasets", "review_queues"],
        "steps": [
            "Install the recipe that binds the governance starter-kit scenario to the live platform catalog.",
            "Drive Aria through plan creation, approval, and execution with explicit operator acknowledgement.",
            "Read the resulting governance inventory through the typed resource APIs.",
        ],
    },
    {
        "id": "15",
        "title": "Aria: Triage & Recalibrate Loop",
        "file": "sdk/caliber-sdk/examples/cookbooks/cookbook_15_aria_triage_recalibrate_loop.py",
        "summary": "Install the recipe, execute the Aria plan, list the queue inventory, and poll the background jobs that the recalibration loop spawns.",
        "surfaces": ["cookbooks", "aria", "review_queues", "jobs"],
        "steps": [
            "Install the recipe through the supported cookbook installer.",
            "Create, approve, and execute the Aria plan for triage and recalibration.",
            "Read back the review queues and any spawned background jobs so the operator can follow the loop without the browser.",
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
]


def parse_exports(tree: ast.Module) -> list[str]:
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "__all__":
                    if isinstance(node.value, (ast.List, ast.Tuple)):
                        values: list[str] = []
                        for element in node.value.elts:
                            if isinstance(element, ast.Constant) and isinstance(element.value, str):
                                values.append(element.value)
                        return values
    return []


def signature_from_function(node: ast.FunctionDef | ast.AsyncFunctionDef, *, bound: bool) -> tuple[str, list[ParamInfo], str]:
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


def parse_attributes(init_node: ast.FunctionDef | ast.AsyncFunctionDef | None) -> list[AttributeInfo]:
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


def infer_raises(
    member: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    class_name: str | None,
) -> list[str]:
    names: set[str] = set()
    if class_name in RESOURCE_CLASS_NAMES or class_name in {"AsyncRawAPI", "AsyncMeAPI", "AsyncCapabilitiesAPI", "AsyncWorkflowRunsAPI", "AsyncJobsAPI", "AsyncEventsAPI"}:
        if member.name not in {"__init__", "__enter__", "__exit__", "__aenter__", "__aexit__", "__repr__"}:
            names.update({"CaliberAPIError", "CaliberTransportError"})
    for node in ast.walk(member):
        if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            func = node.exc.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
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
        bases=[ast.unparse(base) for base in node.bases],
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
            if not exports and (name.startswith("_") or not name.isupper()) and name != "__version__":
                continue
            if exports and name not in exports:
                continue
            if isinstance(node.value, (ast.Constant, ast.List, ast.Tuple, ast.Dict, ast.Set)):
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


def render_params(params: list[ParamInfo]) -> list[str]:
    if not params:
        return ["This callable takes no public parameters.", ""]
    lines = [
        "| Parameter | Kind | Type | Default |",
        "| --- | --- | --- | --- |",
    ]
    for param in params:
        lines.append(
            f"| `{param.name}` | {param.kind} | `{linkify_types(param.annotation)}` | `{linkify_types(param.default)}` |"
        )
    lines.append("")
    return lines


def render_member(member: MemberInfo, *, label_prefix: str = "") -> list[str]:
    title = f"`{label_prefix}{member.name}{member.signature}`"
    lines = [f"###### {title}", ""]
    if member.doc:
        lines.append(member.doc)
        lines.append("")
    lines.extend(render_params(member.params))
    lines.append(f"**Returns:** `{linkify_types(member.returns)}`")
    lines.append("")
    if member.raises:
        lines.append("**Raises:**")
        lines.append("")
        for name in member.raises:
            lines.append(f"- [`{name}`]({heading_link(f'`{name}`')})")
        lines.append("")
    return lines


def render_class(info: ClassInfo) -> list[str]:
    lines = [f"##### `{info.name}`", "", f"`{info.signature}`", ""]
    if info.bases:
        lines.append(f"**Bases:** `{', '.join(info.bases)}`")
        lines.append("")
    if info.doc:
        lines.append(info.doc)
        lines.append("")
    if info.constructor:
        lines.append("**Constructor**")
        lines.append("")
        lines.extend(render_member(info.constructor, label_prefix=""))
    if info.attributes:
        lines.append("**Attributes**")
        lines.append("")
        lines.append("| Attribute | Type | Notes |")
        lines.append("| --- | --- | --- |")
        for attribute in info.attributes:
            note = attribute.note or "—"
            lines.append(
                f"| `{attribute.name}` | `{linkify_types(attribute.inferred_type)}` | {note} |"
            )
        lines.append("")
    if info.fields:
        lines.append("**Dataclass fields**")
        lines.append("")
        lines.append("| Field | Type | Default |")
        lines.append("| --- | --- | --- |")
        for field_info in info.fields:
            lines.append(
                f"| `{field_info.name}` | `{linkify_types(field_info.annotation)}` | `{linkify_types(field_info.default)}` |"
            )
        lines.append("")
    if info.properties:
        lines.append("**Properties**")
        lines.append("")
        for member in info.properties:
            lines.extend(render_member(member, label_prefix=""))
    if info.methods:
        lines.append("**Methods**")
        lines.append("")
        for member in info.methods:
            lines.extend(render_member(member, label_prefix=""))
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
            lines.append(f"| `{constant.name}` | `{linkify_types(constant.value)}` |")
        lines.append("")
    if info.functions:
        lines.append("#### Functions")
        lines.append("")
        for function in info.functions:
            lines.extend(render_member(function))
    if info.classes:
        lines.append("#### Classes")
        lines.append("")
        for class_info in info.classes:
            lines.extend(render_class(class_info))
    return lines


def render_reference() -> str:
    modules = {name: parse_module(name) for _group, names in MODULE_GROUPS for name in names}
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
    for group_title, module_names in MODULE_GROUPS:
        lines.append(f"## {group_title}")
        lines.append("")
        for module_name in module_names:
            lines.extend(render_module(modules[module_name]))
    return "\n".join(lines).strip() + "\n"


def render_cookbooks() -> str:
    lines = [
        "Each script on this page follows the same contract: inspect readiness, install the versioned recipe through the cookbook catalog, and then finish the scenario through typed SDK calls or `client.raw` where the typed layer deliberately has not wrapped a route yet.",
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
        lines.append(f"**SDK surfaces:** {', '.join(f'`{name}`' for name in item['surfaces'])}")
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
