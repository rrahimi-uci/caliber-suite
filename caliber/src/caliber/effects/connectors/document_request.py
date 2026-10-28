"""The document-request connector's capability record and request builders (B05.3, #711).

What the downstream fixture (the document-request downstream app under ``caliber/tests``) shows, through the real OpenAPI executor with no
mocked transport (tests in the connector outcome scenario tests and the connector capability scenario tests; the
decision record is ``docs/reports/egi/D1-connector-capability.md``):

* **Key emission.** ``POST /cases/{case_id}/document-requests`` needs an ``Idempotency-Key`` header; the same key again
  replays the stored answer (201 the first time, 200 after) and commits once.
* **Replay comes before the precondition.** The stored answer for a key is replayed even if the retry carries a stale
  ``If-Match``, so a retry of a request that committed is a 200, not a 412 (measured).
* **Intent conflict.** The same key for a different request is 422 and commits nothing.
* **Precondition.** ``If-Match`` carries the case's integer version; a stale one is 412 and commits nothing.
* **Lookup.** ``GET /document-requests?idempotency_key=...`` reads the stored request (200) or says there is none (404); it
  reads its own writes (measured).

Two claims are **argued from the fixture's code, not measured**, and so are the ones to confirm against the real system: the
key's scope is one namespace for the whole downstream (the fixture holds one case), and a 404 does **not** prove the request
will never be processed (a request in flight at the downstream may still commit; the fixture keeps no tombstone and offers no
cancel), so absence is not proof of non-commit and an unknown effect stays unknown. Key **retention is unconfirmed** (the
fixture never expires a key; the real system's is unknown), so a dispatcher must not rely on a key older than a retention
someone has confirmed.
"""

from __future__ import annotations

import re
from typing import Any, Final

from caliber.effects.connectors import ConnectorCapability

CONNECTOR: Final = "document-request"
VERSION: Final = "1"
#: A header name, not a credential.
KEY_HEADER: Final = "Idempotency-Key"  # gitleaks:allow
PRECONDITION_HEADER: Final = "If-Match"
SUBMIT_PATH: Final = "/cases/{case_id}/document-requests"
LOOKUP_PATH: Final = "/document-requests"

CAPABILITY: Final = ConnectorCapability(
    connector=CONNECTOR,
    version=VERSION,
    idempotency_key_header=KEY_HEADER,
    key_scope="downstream",
    key_retention="unconfirmed",
    key_retention_seconds=None,
    key_conflict_status=422,
    precondition_header=PRECONDITION_HEADER,
    stale_precondition_status=412,
    definitive_rejection_statuses=frozenset({400, 404, 412, 422}),
    lookup_by_key=True,
    lookup_consistency="read_your_writes",
    absence_proves_noncommit=False,
)


def submit_config(
    server_url: str, *, max_attempts: int = 1, timeout_seconds: float | None = None
) -> dict[str, Any]:
    """The execution config of the submit operation: the key header is bound, so a retry is safe only with a key."""
    config: dict[str, Any] = {
        "kind": "openapi_http",
        "method": "POST",
        "path": SUBMIT_PATH,
        "server_url": server_url,
        "auth_binding": None,
        "request_content_types": ["application/json"],
        "idempotency_key_header": KEY_HEADER,
        "max_attempts": max_attempts,
    }
    if timeout_seconds is not None:
        config["timeout_seconds"] = timeout_seconds
    return config


def lookup_config(server_url: str) -> dict[str, Any]:
    """The execution config of the lookup-by-key operation (a read; one attempt, the caller decides about retrying)."""
    return {
        "kind": "openapi_http",
        "method": "GET",
        "path": LOOKUP_PATH,
        "server_url": server_url,
        "auth_binding": None,
        "max_attempts": 1,
    }


#: A case id the path can carry as one segment (no ``.``/``..``, no separators); the executor also refuses dot segments.
_CASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
#: A key is printable ASCII with no surrounding whitespace: anything else is refused here, before a request is built,
#: rather than failing inside the HTTP client as an error that looks like "may have been sent".
_KEY = re.compile(r"[\x21-\x7e](?:[\x20-\x7e]{0,253}[\x21-\x7e])?")


def _key(value: object) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise ValueError(
            "an idempotency key is 1 to 255 printable ASCII characters with no surrounding whitespace"
        )
    return value


def submit_input(
    *,
    case_id: str,
    idempotency_key: str,
    expected_resource_version: int | None,
    body: dict[str, Any],
) -> dict[str, Any]:
    """The input of a submit call: the key and the expected version travel as the headers the downstream reads.

    This is the one place they are put on the request, so a caller cannot send a precondition the proposal did not record
    or a request without its key. ``case_id`` must be a single safe path segment.
    """
    if not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id):
        raise ValueError(
            "case_id must be 1 to 128 letters, digits, dots, underscores or hyphens, starting with a letter or digit"
        )
    headers = {KEY_HEADER: _key(idempotency_key)}
    if expected_resource_version is not None:
        if type(expected_resource_version) is not int or expected_resource_version < 0:
            raise ValueError("expected_resource_version must be a non-negative integer or None")
        headers[PRECONDITION_HEADER] = str(expected_resource_version)
    return {"path_params": {"case_id": case_id}, "header_params": headers, "body": body}


def lookup_input(idempotency_key: str) -> dict[str, Any]:
    """The input of a lookup-by-key call."""
    return {"query_params": {"idempotency_key": _key(idempotency_key)}}


__all__ = [
    "CAPABILITY",
    "CONNECTOR",
    "KEY_HEADER",
    "LOOKUP_PATH",
    "PRECONDITION_HEADER",
    "SUBMIT_PATH",
    "VERSION",
    "lookup_config",
    "lookup_input",
    "submit_config",
    "submit_input",
]
