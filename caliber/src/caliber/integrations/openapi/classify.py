"""Deterministic classification of normalized OpenAPI operations.

Classification answers questions curation needs and the spec only implies: is this
a read or a destructive write, does it page, is it the start of an async job, what
resource does it act on. Every rule here is a pure function of the normalized
operation, so re-importing the same document yields the same classification —
which is what makes import diffs and dependency detection reviewable.

The ``side_effect_level`` this produces deliberately stays inside CALIBER's tool
vocabulary (``read`` / ``write`` / ``external_action``, see
``TOOL_SIDE_EFFECT_PATTERN``) so a published draft is a valid registry row. The
richer judgement — admin surface, pagination style, async shape — rides alongside
in ``classification`` rather than being crushed into that one field.
"""

from __future__ import annotations

# Rule-based classification keeps the parameter names explicit for auditability.
# ruff: noqa: PLR2004, PLC0206
import re
from typing import Any

SIDE_EFFECT_READ = "read"
SIDE_EFFECT_WRITE = "write"
SIDE_EFFECT_EXTERNAL_ACTION = "external_action"

_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_DESTRUCTIVE_METHODS = frozenset({"DELETE"})

#: Path or operation-id fragments that mark an administrative surface. An admin
#: operation is classified ``external_action`` rather than ``write`` because its
#: blast radius is the tenant, not one record.
_ADMIN_TOKENS = (
    "admin",
    "permission",
    "role",
    "policy",
    "setting",
    "config",
    "billing",
    "license",
    "member",
    "invite",
    "apikey",
    "api-key",
    "api_key",
    "token",
    "credential",
    "secret",
    "sso",
    "audit",
)

#: Verb fragments that signal an action with effects outside the record itself:
#: something is sent, charged, deployed, or published to a third party.
_EXTERNAL_ACTION_TOKENS = (
    "send",
    "email",
    "sms",
    "notify",
    "publish",
    "deploy",
    "charge",
    "refund",
    "payment",
    "payout",
    "transfer",
    "execute",
    "trigger",
    "dispatch",
    "invoke",
    "purge",
    "rotate",
    "revoke",
)

#: Query parameter names that indicate a paged collection, grouped by style so the
#: executor and the graph can tell a cursor API from an offset API.
_CURSOR_PARAMS = frozenset(
    {
        "cursor",
        "next",
        "next_token",
        "nexttoken",
        "page_token",
        "pagetoken",
        "after",
        "start_after",
        "continuation_token",
    }
)
_PAGE_PARAMS = frozenset({"page", "page_number", "pagenumber", "page_index"})
_OFFSET_PARAMS = frozenset({"offset", "skip", "start", "start_index", "from"})
_LIMIT_PARAMS = frozenset(
    {
        "limit",
        "per_page",
        "perpage",
        "page_size",
        "pagesize",
        "count",
        "max_results",
        "maxresults",
        "top",
        "size",
    }
)

#: Response fields that mark an async job handle rather than the finished result.
_JOB_FIELDS = frozenset(
    {
        "job_id",
        "jobid",
        "task_id",
        "taskid",
        "operation_id",
        "operationid",
        "execution_id",
        "executionid",
        "request_id",
        "requestid",
    }
)

#: Path/operation fragments for the "check on the job I started" half of an async pair.
_POLL_TOKENS = ("status", "state", "progress", "result", "poll", "wait")

_PATH_PARAM = re.compile(r"\{([^{}]+)\}")
_WORD = re.compile(r"[^a-z0-9]+")


def classify_operation(operation: dict[str, Any]) -> dict[str, Any]:
    """Return the classification block for one normalized operation.

    ``operation`` is the dict produced by
    :func:`caliber.integrations.openapi.normalize._normalize_operation`, before it
    is persisted.
    """

    method = str(operation.get("method") or "GET").upper()
    path = str(operation.get("path") or "")
    tokens = _tokens(path, operation.get("spec_operation_id"), operation.get("summary"))
    normalized = operation.get("normalized_operation")
    normalized = normalized if isinstance(normalized, dict) else {}
    parameters = normalized.get("parameters")
    parameters = parameters if isinstance(parameters, list) else []
    responses = normalized.get("responses")
    responses = responses if isinstance(responses, dict) else {}

    is_admin = any(token in tokens for token in _ADMIN_TOKENS)
    is_external = any(token in tokens for token in _EXTERNAL_ACTION_TOKENS)
    is_collection = _is_collection_path(path)
    pagination = _pagination(method, parameters)
    async_job = _async_job(method, responses)
    is_poll = method in _READ_METHODS and any(token in tokens for token in _POLL_TOKENS)

    return {
        "operation_kind": _operation_kind(method, is_collection),
        "side_effect_level": _side_effect_level(method, is_admin=is_admin, is_external=is_external),
        "is_admin": is_admin,
        "is_destructive": method in _DESTRUCTIVE_METHODS,
        "is_collection": is_collection,
        "resource_type": _resource_type(path),
        "path_parameters": _PATH_PARAM.findall(path),
        "pagination": pagination,
        "is_paginated": pagination["style"] != "none",
        "async_job": async_job,
        "is_async": async_job["is_async"],
        "is_status_poll": is_poll,
        "requires_auth": bool(operation.get("auth_schemes")),
        "deprecated": bool(operation.get("deprecated")),
    }


def _operation_kind(method: str, is_collection: bool) -> str:
    """A CRUD-shaped label for the operation.

    ``list`` vs ``get`` is decided by whether the path ends in a parameter, which
    is the one structural signal OpenAPI reliably carries.
    """

    if method == "GET":
        return "list" if is_collection else "get"
    if method == "POST":
        return "create"
    if method in ("PUT", "PATCH"):
        return "update"
    if method == "DELETE":
        return "delete"
    return "other"


def _side_effect_level(method: str, *, is_admin: bool, is_external: bool) -> str:
    """Map an operation onto CALIBER's three-level tool vocabulary.

    A read stays a read even on an admin path: listing roles does not change
    anything. Everything else escalates — an admin or third-party-effect write is
    ``external_action`` so the approval defaults that key off it apply.
    """

    if method in _READ_METHODS:
        return SIDE_EFFECT_READ
    if is_admin or is_external:
        return SIDE_EFFECT_EXTERNAL_ACTION
    return SIDE_EFFECT_WRITE


def _is_collection_path(path: str) -> bool:
    """True when the path addresses a collection rather than one member."""

    trimmed = path.rstrip("/")
    if not trimmed:
        return True
    last = trimmed.rsplit("/", 1)[-1]
    return not (last.startswith("{") and last.endswith("}"))


def _resource_type(path: str) -> str:
    """The last non-parameter path segment, singularized loosely.

    Used to group operations that act on the same kind of thing, which is what
    makes ``create ticket`` → ``get ticket`` discoverable as a dependency.
    """

    segments = [
        segment
        for segment in path.strip("/").split("/")
        if segment and not (segment.startswith("{") and segment.endswith("}"))
    ]
    if not segments:
        return ""
    last = segments[-1].lower()
    # Skip version-ish and verb-ish tails so /v1/tickets/search resolves to "ticket".
    for candidate in reversed(segments):
        lowered = candidate.lower()
        if re.fullmatch(r"v\d+(\.\d+)*", lowered) or lowered in _POLL_TOKENS:
            continue
        last = lowered
        break
    return _singular(last)


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ses") or word.endswith("xes") or word.endswith("ches"):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _pagination(method: str, parameters: list[Any]) -> dict[str, Any]:
    """Detect the pagination style from query parameter names.

    Only ``GET`` is considered: a paginated ``POST`` search exists, but inferring
    it from parameter names alone produces false positives on request filters.
    """

    if method != "GET":
        return {"style": "none", "cursor_param": None, "limit_param": None}
    names = {
        str(item.get("name") or "").strip().lower(): str(item.get("name") or "").strip()
        for item in parameters
        if isinstance(item, dict) and str(item.get("in") or "") == "query"
    }
    limit_param = next((names[key] for key in names if key in _LIMIT_PARAMS), None)
    for key in names:
        if key in _CURSOR_PARAMS:
            return {"style": "cursor", "cursor_param": names[key], "limit_param": limit_param}
    for key in names:
        if key in _PAGE_PARAMS:
            return {"style": "page", "cursor_param": names[key], "limit_param": limit_param}
    for key in names:
        if key in _OFFSET_PARAMS:
            return {"style": "offset", "cursor_param": names[key], "limit_param": limit_param}
    return {"style": "none", "cursor_param": None, "limit_param": limit_param}


def _async_job(method: str, responses: dict[str, Any]) -> dict[str, Any]:
    """Detect the "accepted, poll for the result" shape.

    Two independent signals: a ``202`` response, or a success response whose body
    is a bare job handle. Either alone is enough to warrant operator review, and
    both are recorded so the reviewer can see which fired.
    """

    has_202 = any(str(code).strip() == "202" for code in responses)
    job_field: str | None = None
    for code, response in responses.items():
        if not str(code).startswith("2") or not isinstance(response, dict):
            continue
        for field in _response_schema_fields(response):
            if field.lower().replace("-", "_") in _JOB_FIELDS:
                job_field = field
                break
        if job_field:
            break
    return {
        "is_async": bool(has_202 or (job_field and method != "GET")),
        "accepted_202": has_202,
        "job_handle_field": job_field,
    }


def _response_schema_fields(response: dict[str, Any]) -> list[str]:
    content = response.get("content")
    if not isinstance(content, dict):
        return []
    for payload in content.values():
        if not isinstance(payload, dict):
            continue
        schema = payload.get("schema")
        if isinstance(schema, dict):
            properties = schema.get("properties")
            if isinstance(properties, dict):
                return [str(key) for key in properties]
    return []


def _tokens(path: str, operation_id: object, summary: object) -> set[str]:
    """Word set drawn from the path, operationId, and summary, for token matching."""

    parts: list[str] = []
    for raw in (path, operation_id, summary):
        text = str(raw or "")
        # Split camelCase before lowercasing so createApiKey yields "api", "key".
        spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", text)
        parts.extend(item for item in _WORD.split(spaced.lower()) if item)
    tokens = set(parts)
    # Keep multi-word markers reachable: "api-key" arrives as {"api", "key"}.
    joined = "".join(parts)
    tokens.update(token for token in (*_ADMIN_TOKENS, *_EXTERNAL_ACTION_TOKENS) if token in joined)
    return tokens
