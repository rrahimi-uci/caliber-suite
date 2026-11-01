"""The operator's list of generic ``http`` endpoints the platform can read back by key (#1055; ADR 0003).

``CALIBER_EFFECT_HTTP_VERIFIERS`` is a JSON array of ``{"project_id", "resource", "lookup_url"}``. This module only parses and
validates it (like :mod:`caliber.effects.callbacks` does for the callback bindings, and for the same reason: the configuration
imports it, so it imports nothing from the database layer). What a listed endpoint promises, and how its answer is read, is
:mod:`caliber.effects.connectors.http_lookup`.

An endpoint is the **operator's** decision: for effects in ``project_id`` whose recorded ``resource`` (the URL a node sends to) is
exactly ``resource``, the downstream is asked at ``lookup_url``. A manifest, a tool call or a model chooses only *which listed
resource* an effect is for, by naming its URL; it can never add one or point a lookup somewhere else.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Final
from urllib.parse import urlsplit

CONNECTOR: Final = "http"
#: The longest ``resource`` a proposal can record (its column), and a lookup URL's own bound.
MAX_RESOURCE_CHARS: Final = 512
MAX_LOOKUP_URL_CHARS: Final = 2048
_KEYS: Final = frozenset({"project_id", "resource", "lookup_url"})
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


@dataclass(frozen=True)
class VerifierEndpoint:
    """One operator-listed endpoint: where the downstream of ``resource`` is asked, for effects of ``project_id``."""

    project_id: str
    resource: str
    lookup_url: str


def _is_http_url(value: str, *, query: bool) -> bool:
    try:
        parts = urlsplit(value)
        return (
            parts.scheme in ("http", "https")
            and bool(parts.hostname)
            and parts.port
            != 0  # (``port`` raises ValueError for a value that is not a port number)
            and parts.username is None
            and parts.password is None
            and not parts.fragment
            and (query or not parts.query)
            and value.isprintable()
            and value.isascii()
            and " " not in value
        )
    except ValueError:
        return False


def parse_endpoints(raw: str) -> tuple[VerifierEndpoint, ...]:
    """The endpoints in ``raw`` (a JSON array), or ``ValueError`` saying what is wrong.

    Each has exactly the keys ``project_id``, ``resource`` and ``lookup_url``: a project made of letters, numbers, dots,
    underscores and hyphens; a ``resource`` that is an ``http`` or ``https`` URL with a host, no credentials and no fragment, at
    most :data:`MAX_RESOURCE_CHARS` characters (compared exactly with the URL a node sends to, so write it the way the workflow
    does); and a ``lookup_url`` that is an ``http`` or ``https`` URL with a host, no credentials, no query, no fragment and no
    braces (the platform appends the key as the one query parameter). A resource is listed once per project.
    """
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("effect_http_verifiers must be a JSON array") from exc
    if not isinstance(parsed, list):
        raise ValueError("effect_http_verifiers must be a JSON array")
    endpoints: list[VerifierEndpoint] = []
    seen: set[tuple[str, str]] = set()
    for item in parsed:
        if not isinstance(item, dict) or set(item) != _KEYS:
            raise ValueError(f"each endpoint has exactly the keys {sorted(_KEYS)}")
        project, resource, lookup = item["project_id"], item["resource"], item["lookup_url"]
        if not isinstance(project, str) or not _NAME.fullmatch(project):
            raise ValueError("project_id must be letters, numbers, dots, underscores and hyphens")
        if (
            not isinstance(resource, str)
            or len(resource) > MAX_RESOURCE_CHARS
            or not _is_http_url(resource, query=True)
        ):
            raise ValueError(
                f"resource must be an http or https URL with a host and no credentials, at most {MAX_RESOURCE_CHARS} characters"
            )
        if (
            not isinstance(lookup, str)
            or len(lookup) > MAX_LOOKUP_URL_CHARS
            or not _is_http_url(lookup, query=False)
            or "{" in lookup
            or "}" in lookup
        ):
            raise ValueError(
                "lookup_url must be an http or https URL with a host, no credentials, no query and no braces"
            )
        identity = (project, resource)
        if identity in seen:
            raise ValueError(f"resource {resource!r} is listed for project {project!r} twice")
        seen.add(identity)
        endpoints.append(VerifierEndpoint(project, resource, lookup))
    return tuple(endpoints)


def endpoints_for_project(
    endpoints: tuple[VerifierEndpoint, ...], project_id: str | None
) -> dict[str, VerifierEndpoint]:
    """``resource -> endpoint`` for ``project_id`` (none for a run with no project: nothing is verifiable for it)."""
    if project_id is None:
        return {}
    return {e.resource: e for e in endpoints if e.project_id == project_id}


def target_key(resource: str) -> str:
    """The reconciler's target name for ``resource``: ``http:<resource>``. A callback binding's connector name cannot contain a
    colon, so no binding can collide with it."""
    return f"{CONNECTOR}:{resource}"


def targets_from_endpoints(endpoints: tuple[VerifierEndpoint, ...]) -> dict[tuple[str, str], str]:
    """``(target name, project) -> lookup URL``, the shape the reconciler resolves a target from."""
    return {(target_key(e.resource), e.project_id): e.lookup_url for e in endpoints}


__all__ = [
    "CONNECTOR",
    "MAX_LOOKUP_URL_CHARS",
    "MAX_RESOURCE_CHARS",
    "VerifierEndpoint",
    "endpoints_for_project",
    "parse_endpoints",
    "target_key",
    "targets_from_endpoints",
]
