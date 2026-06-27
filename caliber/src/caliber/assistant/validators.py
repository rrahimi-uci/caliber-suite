"""Draft validators — one per artifact type."""

from __future__ import annotations

import ast
import re
from typing import Any

from caliber.assistant.models import ValidationReport

# Patterns that look like secrets (env-var refs are OK, literal values are not).
_SECRET_PATTERNS = re.compile(
    r"""(?x)
    (password|secret|api[_-]?key|token|auth)
    \s*[:=]\s*
    ["'][^"']{8,}["']
    """,
    re.IGNORECASE,
)


def validate_tool_draft(
    artifact: dict[str, Any],
    *,
    max_source_bytes: int = 200_000,
) -> ValidationReport:
    errors: list[str] = []
    warnings: list[str] = []

    source = artifact.get("source", "")
    if not source:
        errors.append("Tool source code is required.")
    elif len(source.encode()) > max_source_bytes:
        errors.append(f"Source exceeds {max_source_bytes} byte limit.")
    else:
        try:
            ast.parse(source)
        except SyntaxError as exc:
            errors.append(f"Source has syntax error: {exc.msg} (line {exc.lineno})")

    if not artifact.get("name"):
        errors.append("Tool name is required.")

    if not artifact.get("input_schema"):
        warnings.append("No input_schema provided — callers won't know the signature.")

    if _SECRET_PATTERNS.search(source):
        errors.append(
            "Source appears to contain inline secrets. Use environment variable references."
        )

    return ValidationReport(valid=not errors, errors=errors, warnings=warnings)


def validate_skill_draft(artifact: dict[str, Any]) -> ValidationReport:
    errors: list[str] = []
    warnings: list[str] = []

    if not artifact.get("name"):
        errors.append("Skill name is required.")
    if not artifact.get("prompt") and not artifact.get("description"):
        errors.append("Skill must have a prompt or description.")

    meta = artifact.get("metadata")
    if meta is not None and not isinstance(meta, dict):
        errors.append("metadata must be a JSON object.")

    return ValidationReport(valid=not errors, errors=errors, warnings=warnings)


def validate_prompt_draft(artifact: dict[str, Any]) -> ValidationReport:
    errors: list[str] = []
    warnings: list[str] = []

    template = artifact.get("template", "")
    if not template:
        errors.append("Prompt template is required.")

    declared_vars = set(artifact.get("variables", []))
    placeholders = set(re.findall(r"\{\{(\w+)\}\}", template))
    missing = placeholders - declared_vars
    extra = declared_vars - placeholders
    if missing:
        errors.append(f"Template uses undeclared variables: {sorted(missing)}")
    if extra:
        warnings.append(f"Declared variables not in template: {sorted(extra)}")

    if _SECRET_PATTERNS.search(template):
        errors.append("Template appears to contain inline secrets.")

    return ValidationReport(valid=not errors, errors=errors, warnings=warnings)


def validate_workflow_draft(artifact: dict[str, Any]) -> ValidationReport:
    errors: list[str] = []
    warnings: list[str] = []

    manifest = artifact.get("manifest")
    if not manifest:
        errors.append("Workflow manifest is required.")
    elif not isinstance(manifest, dict):
        errors.append("Workflow manifest must be a JSON object.")
    else:
        if "steps" not in manifest and "nodes" not in manifest:
            errors.append("Manifest must define steps or nodes.")
        if not manifest.get("version"):
            warnings.append("Manifest missing 'version' field.")

    if _SECRET_PATTERNS.search(str(artifact)):
        errors.append("Manifest appears to contain inline secrets.")

    return ValidationReport(valid=not errors, errors=errors, warnings=warnings)


def validate_mcp_server_draft(artifact: dict[str, Any]) -> ValidationReport:
    errors: list[str] = []
    warnings: list[str] = []

    if not artifact.get("name"):
        errors.append("MCP server name is required.")

    transport = artifact.get("transport", "")
    if transport not in ("stdio", "sse", "streamable-http"):
        errors.append(f"Invalid transport '{transport}'. Must be stdio, sse, or streamable-http.")

    if transport == "stdio" and not artifact.get("command"):
        errors.append("stdio transport requires a 'command' field.")

    if transport in ("sse", "streamable-http") and not artifact.get("uri"):
        errors.append(f"{transport} transport requires a 'uri' field.")

    return ValidationReport(valid=not errors, errors=errors, warnings=warnings)


# Dispatcher.
_VALIDATORS = {
    "tool": validate_tool_draft,
    "skill": validate_skill_draft,
    "prompt": validate_prompt_draft,
    "workflow": validate_workflow_draft,
    "mcp_server": validate_mcp_server_draft,
}


def validate_draft(
    artifact_type: str,
    artifact: dict[str, Any],
    **kwargs: Any,
) -> ValidationReport:
    """Validate a draft artifact. Returns a ``ValidationReport``."""
    validator = _VALIDATORS.get(artifact_type)
    if validator is None:
        return ValidationReport(valid=False, errors=[f"Unknown artifact type: {artifact_type}"])
    if artifact_type == "tool":
        return validator(artifact, **kwargs)
    return validator(artifact)
