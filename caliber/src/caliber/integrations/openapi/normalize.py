"""Parse and normalize imported OpenAPI documents.

This is intentionally a control-plane normalizer, not a code generator. The
output is stable JSON-shaped metadata CALIBER can audit, diff, and curate
before any runtime publication exists.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from typing import Any

import yaml

_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "options", "head")
_READ_METHODS = frozenset({"get", "head", "options"})


class OpenApiImportError(ValueError):
    """The supplied document is not a supported OpenAPI import source."""


def parse_openapi_text(spec_text: str) -> dict[str, Any]:
    """Load a pasted JSON/YAML OpenAPI document."""

    text = (spec_text or "").strip()
    if not text:
        raise OpenApiImportError("spec_text is required")
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise OpenApiImportError(f"could not parse OpenAPI document: {exc}") from exc
    if not isinstance(payload, dict):
        raise OpenApiImportError("OpenAPI document must be a JSON/YAML object at the top level")
    version = str(payload.get("openapi") or "").strip()
    if not version.startswith("3."):
        raise OpenApiImportError(
            f"unsupported OpenAPI version {version!r}; only OpenAPI 3.x is supported"
        )
    paths = payload.get("paths")
    if not isinstance(paths, dict) or not paths:
        raise OpenApiImportError("OpenAPI document must declare at least one path")
    return payload


def spec_sha256(document: dict[str, Any]) -> str:
    """Stable digest of the parsed document."""

    canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_openapi_document(
    document: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Return normalized summary, normalized operations, and import warnings."""

    warnings: list[str] = []
    info = document.get("info")
    if not isinstance(info, dict):
        info = {}
        warnings.append("document has no info object; title/version defaulted")

    servers = _server_urls(document.get("servers"))
    if not servers:
        warnings.append("document declared no explicit servers; callers must bind one later")

    auth_schemes = _component_auth_schemes(document)
    operations: list[dict[str, Any]] = []
    top_level_security = _security_names(document.get("security"))
    paths = document.get("paths")
    assert isinstance(paths, dict)  # guarded by parse_openapi_text()
    for path, path_item in sorted(paths.items()):
        if not isinstance(path, str) or not isinstance(path_item, dict):
            warnings.append(f"ignored non-object path entry for {path!r}")
            continue
        for method in _HTTP_METHODS:
            raw_operation = path_item.get(method)
            if raw_operation is None:
                continue
            if not isinstance(raw_operation, dict):
                warnings.append(f"ignored {method.upper()} {path} because the operation is not an object")
                continue
            operations.append(
                _normalize_operation(
                    path=path,
                    method=method,
                    path_item=path_item,
                    operation=raw_operation,
                    top_level_security=top_level_security,
                )
            )

    if not operations:
        raise OpenApiImportError("OpenAPI document declared no supported HTTP operations")

    summary = {
        "openapi": str(document.get("openapi") or ""),
        "title": str(info.get("title") or ""),
        "version": str(info.get("version") or ""),
        "description": str(info.get("description") or ""),
        "server_urls": servers,
        "auth_schemes": auth_schemes,
        "operation_count": len(operations),
    }
    return summary, operations, warnings


def _normalize_operation(
    *,
    path: str,
    method: str,
    path_item: dict[str, Any],
    operation: dict[str, Any],
    top_level_security: list[str],
) -> dict[str, Any]:
    responses = operation.get("responses")
    response_statuses = sorted(
        str(code)
        for code in (responses.keys() if isinstance(responses, dict) else [])
        if str(code).strip()
    )
    request_body = operation.get("requestBody")
    request_content_types: list[str] = []
    request_body_required = False
    if isinstance(request_body, dict):
        request_body_required = bool(request_body.get("required", False))
        content = request_body.get("content")
        if isinstance(content, dict):
            request_content_types = sorted(str(kind) for kind in content if str(kind).strip())

    security = _security_names(operation.get("security")) or top_level_security
    tags = [str(tag) for tag in _iter_strings(operation.get("tags"))]
    parameters = _merged_parameters(path_item.get("parameters"), operation.get("parameters"))
    return {
        "method": method.upper(),
        "path": path,
        "operation_key": f"{method.upper()} {path}",
        "spec_operation_id": str(operation.get("operationId") or "") or None,
        "summary": str(operation.get("summary") or ""),
        "description": str(operation.get("description") or ""),
        "tags": tags,
        "deprecated": bool(operation.get("deprecated", False)),
        "side_effect_level": "read" if method in _READ_METHODS else "write",
        "auth_schemes": security,
        "request_body_required": request_body_required,
        "request_content_types": request_content_types,
        "response_statuses": response_statuses,
        "normalized_operation": {
            "parameters": parameters,
            "requestBody": request_body if isinstance(request_body, dict) else None,
            "responses": responses if isinstance(responses, dict) else {},
            "security": operation.get("security"),
            "tags": tags,
        },
    }


def _server_urls(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    urls: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if url:
            urls.append(url)
    return sorted(set(urls))


def _component_auth_schemes(document: dict[str, Any]) -> list[str]:
    components = document.get("components")
    if not isinstance(components, dict):
        return []
    schemes = components.get("securitySchemes")
    if not isinstance(schemes, dict):
        return []
    return sorted(str(name) for name in schemes if str(name).strip())


def _merged_parameters(path_level: object, operation_level: object) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for raw in (path_level, operation_level):
        if not isinstance(raw, list):
            continue
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            location = str(item.get("in") or "").strip()
            if not name or not location:
                continue
            key = (location, name)
            if key in seen:
                merged = [row for row in merged if (str(row.get("in") or ""), str(row.get("name") or "")) != key]
            merged.append(dict(item))
            seen.add(key)
    return merged


def _security_names(value: object) -> list[str]:
    names: list[str] = []
    if isinstance(value, list):
        for item in value:
            if not isinstance(item, dict):
                continue
            for name in item:
                text = str(name).strip()
                if text:
                    names.append(text)
    return sorted(set(names))


def _iter_strings(value: object) -> Iterable[str]:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                yield item.strip()
