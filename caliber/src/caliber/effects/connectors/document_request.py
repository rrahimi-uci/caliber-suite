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

import hashlib
import re
from typing import Any, Final

from caliber.effects.connectors import ConnectorCapability
from caliber.effects.receipts import ReceiptFacts
from caliber.effects.records import Intent, ProposalError, ProposalView
from caliber.run_manifest import canonical_bytes

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


def facts_from_lookup(
    view: ProposalView, *, lookup_status: int, lookup_body: Any
) -> ReceiptFacts | None:
    """What the lookup by key says about ``view``'s dispatch, as receipt facts, or ``None`` if it does not confirm it.

    ``None`` is *not* a failure: it is a lookup that answered something other than this proposal's effect (a 404, a 5xx, a
    body that is not a stored request, a request under a different case or document type, a stored version that is not the
    one this effect should have produced). The proposal stays ``unknown``.

    The stored request must carry the proposal's logical key as its idempotency key; rebuilt as an intent (this connector's
    identity and version, the proposal's effect class and expected version, the case's resource and the stored document type
    as the arguments) it must hash to the proposal's ``intent_hash``; and when the proposal expected a case version, the
    stored ``case_version`` must be exactly one more (the version this effect produces).
    """
    if lookup_status != 200 or not isinstance(lookup_body, dict) or view.attempt is None:  # noqa: PLR2004 - HTTP status
        return None
    request_id, key = lookup_body.get("request_id"), lookup_body.get("idempotency_key")
    case_id, document_type = lookup_body.get("case_id"), lookup_body.get("document_type")
    version = lookup_body.get("case_version")
    if (
        key != view.logical_key
        or not isinstance(request_id, str)
        or not request_id.strip()
        or not isinstance(case_id, str)
        or not isinstance(document_type, str)
        or type(version) is not int
    ):
        return None
    if view.expected_resource_version is not None:
        try:
            if version != int(view.expected_resource_version) + 1:
                return None
        except ValueError:
            return None
    try:
        stored = Intent(
            connector=CONNECTOR,
            connector_version=VERSION,
            effect_class=view.effect_class,
            resource=f"cases/{case_id}/document-requests",
            args={"document_type": document_type},
            expected_resource_version=view.expected_resource_version,
        )
        facts = ReceiptFacts(
            attempt=view.attempt,
            idempotency_key=str(key),
            connector=CONNECTOR,
            connector_version=VERSION,
            resource=stored.resource,
            intent_hash=stored.digest,
            downstream_request_id=request_id,
            response_digest="sha256:" + hashlib.sha256(canonical_bytes(lookup_body)).hexdigest(),
            resource_version=str(version),
        )
    except ProposalError:
        return None  # a stored value this connector cannot represent is not this proposal's effect
    return facts if stored.digest == view.intent_hash else None


__all__ = [
    "CAPABILITY",
    "CONNECTOR",
    "KEY_HEADER",
    "LOOKUP_PATH",
    "PRECONDITION_HEADER",
    "SUBMIT_PATH",
    "VERSION",
    "facts_from_lookup",
    "lookup_config",
    "lookup_input",
    "submit_config",
    "submit_input",
]
