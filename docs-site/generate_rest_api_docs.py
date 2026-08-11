#!/usr/bin/env python3
"""Generate a build-time REST API inventory for the CALIBER docs site.

The inventory is derived from the same live route table that backs the served
management OpenAPI document. That keeps the published HTML reference aligned
with the repository's actual route surface instead of relying on hand-maintained
tables that silently drift.
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CALIBER_ROOT = REPO_ROOT / "caliber"
sys.path.insert(0, str(CALIBER_ROOT / "src"))

from caliber.routes.openapi import build_openapi_document  # noqa: E402
from caliber.server import create_app  # noqa: E402

TIER_ORDER = ("ga", "beta", "internal")
TIER_LABELS = {
    "ga": "GA routes",
    "beta": "Beta routes",
    "internal": "Internal routes",
}
TIER_NOTES = {
    "ga": "Supported management routes that belong to the stable public automation surface.",
    "beta": "Supported but still moving route groups. Expect capability growth and narrower compatibility guarantees.",
    "internal": "Published for route-table completeness, but not part of the supported SDK contract.",
}
SDK_SURFACE_MAP = {
    "openapi.json": {
        "entry": "`CaliberClient.openapi()`",
        "notes": "Fetch the live management OpenAPI document directly from the root client.",
    },
    "auth": {
        "entry": "`client.auth.tokens`, `client.auth.accounts`, `client.auth.session()`",
        "notes": "Token issuance, rotation, revocation, account management, and session inspection.",
    },
    "me": {
        "entry": "`CaliberClient.whoami()`, `client.me.get()`",
        "notes": "Identity and effective scopes for the current credential.",
    },
    "capabilities": {
        "entry": "`CaliberClient.capabilities()`, `client.capabilities_api.get()`",
        "notes": "Feature flags and SDK stability tiers for the current deployment.",
    },
    "settings": {
        "entry": "`client.settings.runtime()`, `client.settings.llm()`",
        "notes": "Runtime configuration summary and LLM credential status.",
    },
    "projects": {
        "entry": "`client.projects`, `client.projects.files`",
        "notes": "Project records, project storage visibility, uploads, folders, and downloads.",
    },
    "prompts": {
        "entry": "`client.prompts`",
        "notes": "Prompt registry, versions, and alias promotion.",
    },
    "skills": {
        "entry": "`client.skills`",
        "notes": "Skill registry, render checks, selection tests, and versions.",
    },
    "tools": {
        "entry": "`client.tools`",
        "notes": "Tool registry plus calibration job submission and polling.",
    },
    "workflows": {
        "entry": "`client.workflows`",
        "notes": "Workflow CRUD plus the umbrella entry point for versions, runs, and services.",
    },
    "workflow-versions": {
        "entry": "`client.workflows.versions`",
        "notes": "Create, validate, compile, and publish immutable workflow versions.",
    },
    "workflow-runs": {
        "entry": "`client.workflows.runs`",
        "notes": "Submit, inspect, cancel, and wait on workflow executions.",
    },
    "services": {
        "entry": "`client.workflows.services`",
        "notes": "Publish a workflow as a service, inspect service OpenAPI, and invoke it.",
    },
    "eval-datasets": {
        "entry": "`client.datasets`",
        "notes": "Evaluation datasets, examples, and trace-to-dataset capture.",
    },
    "evaluations": {
        "entry": "`client.evaluations`",
        "notes": "Create, inspect, and wait on evaluation runs.",
    },
    "judges": {
        "entry": "`client.judges`",
        "notes": "Judge creation, test execution, and human-alignment scoring.",
    },
    "mcp-servers": {
        "entry": "`client.mcp_servers`",
        "notes": "Managed MCP server definitions, discovery, governance, and invocation.",
    },
    "gateway": {
        "entry": "`client.gateway`",
        "notes": "Gateway endpoint discovery, guardrails, and trace-derived usage.",
    },
    "knowledge-bases": {
        "entry": "`client.knowledge_bases`",
        "notes": "Knowledge-base lifecycle, versions, runs, retrieval, baseline, and rollback.",
    },
    "knowledge-base-versions": {
        "entry": "`client.knowledge_bases`",
        "notes": "Version-specific reads are exposed through the knowledge-bases resource methods.",
    },
    "knowledge-runs": {
        "entry": "`client.knowledge_bases.run_events(...)`",
        "notes": "Knowledge-base build run event inspection.",
    },
    "knowledge": {
        "entry": "`client.knowledge_bases.query(...)`, `client.knowledge_bases.test_run(...)`",
        "notes": "Retrieval/query and knowledge test-run reads.",
    },
    "object-store": {
        "entry": "`client.object_store`",
        "notes": "Bucket/object console operations, distinct from project file management.",
    },
    "jobs": {
        "entry": "`client.jobs`",
        "notes": "Durable background jobs, targets, apply, and wait semantics.",
    },
    "review-queues": {
        "entry": "`client.review_queues`",
        "notes": "Queue creation, enqueue/submit flows, and alignment examples.",
    },
    "aria": {
        "entry": "`client.aria`",
        "notes": "Goal-plan creation, approval, execution, polling, and interactions.",
    },
    "releases": {
        "entry": "`client.releases`",
        "notes": "Release candidates, evaluation, waivers, reports, and signoff.",
    },
    "observability": {
        "entry": "`client.observability`",
        "notes": "Trace listing/detail, experiments, and metrics reads.",
    },
    "audit-log": {
        "entry": "`client.audit`",
        "notes": "Audit-log listing and export.",
    },
    "events": {
        "entry": "`client.events`",
        "notes": "Server-sent events stream access.",
    },
    "cookbooks": {
        "entry": "`client.cookbooks`",
        "notes": "Cookbook catalog and related operational metadata.",
    },
    "secrets": {
        "entry": "`client.secrets`",
        "notes": "Secret inventory and mutation surfaces.",
    },
}
PREFERRED_TAG_ORDER = {
    "openapi.json": 0,
    "auth": 1,
    "csrf": 2,
    "me": 3,
    "capabilities": 4,
    "settings": 5,
    "projects": 6,
    "prompts": 7,
    "skills": 8,
    "tools": 9,
    "agents": 10,
    "workflows": 11,
    "workflow-versions": 12,
    "workflow-runs": 13,
    "workflow-components": 14,
    "workflow-templates": 15,
    "workflow-files": 16,
    "services": 17,
    "eval-datasets": 18,
    "evaluations": 19,
    "judges": 20,
    "knowledge-bases": 21,
    "knowledge": 22,
    "mcp-servers": 23,
    "releases": 24,
    "review-queues": 25,
    "jobs": 26,
    "observability": 27,
    "events": 28,
    "gateway": 29,
    "cookbooks": 30,
}


def _humanize_tag(tag: str) -> str:
    acronyms = {
        "api": "API",
        "mcp": "MCP",
        "qa": "QA",
        "llm": "LLM",
        "csrf": "CSRF",
        "openapi": "OpenAPI",
        "json": "JSON",
    }
    words = []
    for piece in tag.replace(".", " ").replace("-", " ").split():
        words.append(acronyms.get(piece.lower(), piece.capitalize()))
    return " ".join(words)


def _slugify(text: str) -> str:
    import re

    return re.sub(
        r"-+",
        "-",
        re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", text.lower().replace("`", "")).strip()),
    )


def _tag_heading(tag: str) -> str:
    return f"{_humanize_tag(tag)} (`{tag}`)"


def _tag_anchor(tag: str) -> str:
    return f"#{_slugify(_tag_heading(tag))}"


def _escape_cell(value: str) -> str:
    return value.replace("|", r"\|").replace("\n", " ").strip()


def _sdk_surface_row(tag: str, tier: str) -> tuple[str, str, str]:
    surface = SDK_SURFACE_MAP.get(tag)
    if surface:
        return ("Typed SDK", surface["entry"], surface["notes"])
    if tier == "internal":
        return (
            "No typed SDK",
            "—",
            "Internal route family. Use the served OpenAPI or raw HTTP only when you are intentionally working below the supported SDK contract.",
        )
    return (
        "Raw only",
        "`client.raw`",
        "No typed wrapper on the current SDK surface. Use raw HTTP or generate a client against the served OpenAPI if you need this family today.",
    )


def _security_scheme_summary(name: str, payload: dict[str, object]) -> tuple[str, str]:
    scheme_type = str(payload.get("type", ""))
    if scheme_type == "http":
        return ("HTTP bearer", "Automation token in the `Authorization` header.")
    if scheme_type == "apiKey" and payload.get("in") == "cookie":
        return ("Session cookie", "Browser session auth for same-origin product use.")
    if scheme_type == "apiKey" and payload.get("in") == "header":
        return (
            f"Header `{payload.get('name', '')}`",
            str(payload.get("description", "")).strip() or "Header-based request modifier.",
        )
    return (scheme_type or "unknown", str(payload))


def _schema_summary(name: str, payload: dict[str, object]) -> tuple[str, str]:
    props = payload.get("properties", {})
    required = payload.get("required", [])
    property_names = list(props.keys()) if isinstance(props, dict) else []
    required_names = list(required) if isinstance(required, list) else []
    fields = ", ".join(f"`{field}`" for field in property_names[:4]) or "—"
    note = str(payload.get("description", "")).strip()
    if required_names:
        note = (note + " " if note else "") + f"Required: {', '.join(required_names)}."
    return (fields, note or "Shared OpenAPI component schema.")


def _format_params(operation: dict[str, object]) -> str:
    params = [
        f"`{parameter['name']}`"
        for parameter in operation.get("parameters", [])
        if isinstance(parameter, dict) and parameter.get("name")
    ]
    return ", ".join(params) if params else "—"


def _format_responses(operation: dict[str, object]) -> str:
    responses = operation.get("responses", {})
    if not isinstance(responses, dict) or not responses:
        return "—"
    return ", ".join(f"`{code}`" for code in responses)


def _format_details(operation: dict[str, object]) -> str:
    details: list[str] = []
    operation_id = operation.get("operationId")
    if isinstance(operation_id, str) and operation_id:
        details.append(f"`operationId`: `{operation_id}`")
    request_body = operation.get("requestBody")
    if isinstance(request_body, dict):
        details.append("request body documented in OpenAPI")
    summary = operation.get("summary")
    if isinstance(summary, str) and summary.strip():
        details.append(summary.strip())
    return "; ".join(details) if details else "—"


def _tag_sort_key(tag: str) -> tuple[int, str]:
    return (PREFERRED_TAG_ORDER.get(tag, 999), tag)


def render_inventory() -> str:
    app = create_app()
    document = build_openapi_document(app)
    coverage = document.get("x-caliber-schema-coverage", {})
    components = document.get("components", {})
    schemas = components.get("schemas", {}) if isinstance(components, dict) else {}
    responses = components.get("responses", {}) if isinstance(components, dict) else {}
    security_schemes = (
        components.get("securitySchemes", {}) if isinstance(components, dict) else {}
    )

    grouped: dict[str, list[tuple[str, str, dict[str, object]]]] = defaultdict(list)
    for path, operations in document["paths"].items():
        for method, operation in operations.items():
            tag = operation.get("tags", ["root"])[0]
            grouped[tag].append((path, method.upper(), operation))

    tiered_tags: dict[str, list[str]] = defaultdict(list)
    for tag, entries in grouped.items():
        tier = entries[0][2].get("x-caliber-stability", "internal")
        tiered_tags[str(tier)].append(tag)
        entries.sort(key=lambda item: (item[0], item[1]))

    path_count = len(document["paths"])
    operation_count = sum(len(operations) for operations in document["paths"].values())

    lines = [
        "## Shared contract building blocks",
        "",
        "These pieces come directly from the served management OpenAPI document and apply across the whole management API, independent of any one route family.",
        "",
        "### Security and scoping schemes",
        "",
        "| Scheme | Type | Purpose |",
        "| --- | --- | --- |",
    ]

    for name in ["bearerAuth", "sessionCookie", "csrfToken", "projectScope"]:
        payload = security_schemes.get(name)
        if not isinstance(payload, dict):
            continue
        scheme_type, note = _security_scheme_summary(name, payload)
        lines.append(f"| `{name}` | {_escape_cell(scheme_type)} | {_escape_cell(note)} |")

    lines.extend(
        [
            "",
            "### Shared JSON shapes",
            "",
            "| Schema | Key fields | Notes |",
            "| --- | --- | --- |",
        ]
    )

    for name in ["Envelope", "Error", "ValidationError"]:
        payload = schemas.get(name)
        if not isinstance(payload, dict):
            continue
        fields, note = _schema_summary(name, payload)
        lines.append(f"| `{name}` | {fields} | {_escape_cell(note)} |")

    lines.extend(
        [
            "",
            "### Shared response components",
            "",
            "| Response | Meaning |",
            "| --- | --- |",
        ]
    )

    for name in ["Success", "Unauthenticated", "Forbidden", "NotFound", "ValidationFailed"]:
        payload = responses.get(name)
        if not isinstance(payload, dict):
            continue
        description = str(payload.get("description", "")).strip() or "Shared response component."
        lines.append(f"| `{name}` | {_escape_cell(description)} |")

    lines.extend(
        [
            "",
            "## SDK coverage by route family",
            "",
            "Use the typed SDK where it exists. When a family is marked `Raw only`, the current supported fallback is `client.raw` or an HTTP client generated from the served OpenAPI document.",
            "",
            "| Family | Stability | Ops | SDK coverage | SDK entry point | Notes |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )

    for tier in TIER_ORDER:
        for tag in sorted(tiered_tags.get(tier, []), key=_tag_sort_key):
            entries = grouped[tag]
            coverage_label, sdk_entry, sdk_note = _sdk_surface_row(tag, tier)
            lines.append(
                "| "
                + " | ".join(
                    [
                        _tag_heading(tag),
                        f"`{tier}`",
                        f"`{len(entries)}`",
                        _escape_cell(coverage_label),
                        _escape_cell(sdk_entry),
                        _escape_cell(sdk_note),
                    ]
                )
                + " |"
            )

    lines.extend(
        [
        "",
        "## Current route inventory",
        "",
        "This inventory is generated at build time from the live CALIBER route table and the same OpenAPI builder that serves `GET /ajax-api/2.0/mlflow/caliber/openapi.json`.",
        "The served contract is route-table grounded and body-complete: paths and methods come from the live router, while request and success-response bodies are inferred from the handlers and the Pydantic models they already use.",
        "",
        "### Coverage summary",
        "",
        "| Field | Value |",
        "| --- | --- |",
        f"| Route paths | `{path_count}` |",
        f"| Operations | `{operation_count}` |",
        f"| Path coverage | `{coverage.get('paths', 'unknown')}` |",
        f"| Request bodies | `{coverage.get('request_bodies', 'unknown')}` |",
        f"| GA families | `{len(tiered_tags.get('ga', []))}` |",
        f"| Beta families | `{len(tiered_tags.get('beta', []))}` |",
        f"| Internal families | `{len(tiered_tags.get('internal', []))}` |",
        "",
        "### Auth and scoping contract",
        "",
        "- Every route below requires an authenticated CALIBER caller.",
        "- Browser-style writes additionally require `X-CALIBER-CSRF` when CSRF enforcement is enabled.",
        "- Project-scoped automation can supply `X-CALIBER-Project` to select the active workspace.",
        "- Internal routes are listed for completeness, not as a supported public SDK contract.",
        "",
        "### Jump by resource family",
        "",
        "Use these quick jumps when you already know the CALIBER subsystem and want the detailed route table directly.",
        "",
        ]
    )

    for tier in TIER_ORDER:
        tags = sorted(tiered_tags.get(tier, []), key=_tag_sort_key)
        if not tags:
            continue
        lines.extend(
            [
                f"#### {TIER_LABELS[tier]} index",
                "",
                "| Family | Ops | Paths |",
                "| --- | --- | --- |",
            ]
        )
        for tag in tags:
            entries = grouped[tag]
            unique_paths = len({path for path, _, _ in entries})
            lines.append(
                f"| [{_tag_heading(tag)}]({_tag_anchor(tag)}) | `{len(entries)}` | `{unique_paths}` |"
            )
        lines.append("")

    for tier in TIER_ORDER:
        tags = sorted(tiered_tags.get(tier, []), key=_tag_sort_key)
        if not tags:
            continue
        lines.extend(
            [
                f"### {TIER_LABELS[tier]}",
                "",
                TIER_NOTES[tier],
                "",
            ]
        )
        for tag in tags:
            entries = grouped[tag]
            unique_paths = len({path for path, _, _ in entries})
            lines.extend(
                [
                    f"#### {_tag_heading(tag)}",
                    "",
                    f"{len(entries)} operation(s) across {unique_paths} route path(s).",
                    "",
                    "| Method | Path | Parameters | Responses | Details |",
                    "| --- | --- | --- | --- | --- |",
                ]
            )
            for path, method, operation in entries:
                lines.append(
                    "| "
                    + " | ".join(
                        [
                            f"`{method}`",
                            f"`{path}`",
                            _format_params(operation),
                            _format_responses(operation),
                            _escape_cell(_format_details(operation)),
                        ]
                    )
                    + " |"
                )
            lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main() -> None:
    sys.stdout.write(render_inventory())


if __name__ == "__main__":
    main()
