"""The generic ``http`` connector's read-by-key contract: how an unknown webhook or API-request effect is settled (#1055; ADR 0003).

A governed webhook or API-request node (B04.2) sends one request with the proposal's stable logical key as its
``Idempotency-Key``. A 2xx is only a claim, and a timeout after the send, a 5xx or a settle that failed leaves the proposal
``unknown``. The connector used to promise no lookup at all, so every such proposal reported ``not_bound`` forever and parked its
run with no exit. This module is the connector's side of the one exit ADR 0003 allows, a **lookup by key that the platform reads
itself**:

* **Which endpoints can be verified is the operator's decision, never a request's** (:mod:`caliber.effects.http_verifiers`).
  ``CALIBER_EFFECT_HTTP_VERIFIERS`` is a JSON array of ``{"project_id", "resource", "lookup_url"}``: for effects in that project
  whose recorded ``resource`` (the URL the node sends to) is exactly ``resource``, the downstream is asked at ``lookup_url``. A manifest, a tool call or a model chooses
  only *which listed resource* an effect is for, by naming its URL; it can never add one, point a lookup somewhere else or make a
  URL verifiable. An effect whose URL is not listed is refused **before** it is recorded (``no_authoritative_verifier``), so no
  approval is asked for, or spent on, an effect the platform could never confirm.
* **The contract** the downstream implements at ``lookup_url`` (no credentials are sent; put it where the platform's egress
  policy allows it): ``GET <lookup_url>?idempotency_key=<key>`` answers ``200`` with a JSON object that holds the request it stored
  under that key, and anything else (a ``404``, a ``5xx``, an unreadable or different body) leaves the proposal ``unknown``:

  ``idempotency_key``
      the key it stored the request under: the proposal's logical key.
  ``request_id``
      the id the downstream assigned to the request it stored (1 to 256 printable characters). One id belongs to one proposal.
  ``method`` and ``url``
      the method and the URL of the request it stored.
  ``body``
      the JSON value it stored as the request body (``null`` or absent when there was none).

  It must read its own writes: a ``2xx`` the sender saw is visible to the very next lookup (the capability record's
  ``read_your_writes``). A ``404`` is **never** "it did not happen" for this connector (``absence_proves_noncommit`` is false): the
  request may still be in flight at the downstream, so the proposal stays ``unknown`` (the attested operator resolution of
  #1054 is the separate, not yet built, way out of a permanent unknown).
* **What the platform believes.** :func:`facts_from_lookup` builds a receipt only if the stored request is *this proposal's
  effect*: the key is the logical key, the method, the URL and the body are exactly the ones the proposal recorded (compared as
  canonical JSON, never trusting the downstream's say-so about "the same"), and the proposal expects no resource version (a
  version this connector cannot read is refused, not guessed). The proposal's own ``intent_hash``, attempt and logical key are
  what the receipt carries, so a downstream cannot choose them. A lookup that finds anything else is not a receipt and not a
  failure.

Nothing here sends a request, reads a database or decides a state.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import urlsplit

from caliber.effects.connectors import ConnectorCapability
from caliber.effects.http_verifiers import CONNECTOR
from caliber.effects.receipts import ReceiptFacts
from caliber.effects.records import ProposalError, ProposalView
from caliber.run_manifest import canonical_bytes

VERSION: Final = "1"
#: A header name, not a credential.
KEY_HEADER: Final = "Idempotency-Key"  # gitleaks:allow
QUERY_PARAM: Final = "idempotency_key"
#: A key is printable ASCII with no surrounding whitespace (the same rule the document-request connector applies).
_KEY = re.compile(r"[\x21-\x7e](?:[\x20-\x7e]{0,253}[\x21-\x7e])?")
_HTTP_OK: Final = 200

#: The connector's documented behaviour. The key is the proposal's logical key and nothing more is promised about its scope or
#: retention (``key_scope`` unknown, retention unconfirmed), so the connector still carries a write at-least-once as far as the
#: platform can prove; what it *does* have, for every endpoint an operator lists, is a lookup by key that reads its own writes.
#: A lookup that finds nothing proves nothing (``absence_proves_noncommit`` false).
CAPABILITY: Final = ConnectorCapability(
    connector=CONNECTOR,
    version=VERSION,
    idempotency_key_header=KEY_HEADER,
    key_scope="unknown",
    key_retention="unconfirmed",
    key_retention_seconds=None,
    key_conflict_status=None,
    precondition_header=None,
    stale_precondition_status=None,
    definitive_rejection_statuses=frozenset(),
    lookup_by_key=True,
    lookup_consistency="read_your_writes",
    absence_proves_noncommit=False,
)


def lookup_config(lookup_url: str) -> dict[str, Any]:
    """The execution config of the lookup-by-key operation at ``lookup_url`` (a read; one attempt, the caller decides about retrying)."""
    parts = urlsplit(lookup_url)
    return {
        "kind": "openapi_http",
        "method": "GET",
        "path": parts.path or "/",
        "server_url": f"{parts.scheme}://{parts.netloc}",
        "auth_binding": None,
        "max_attempts": 1,
    }


def lookup_input(idempotency_key: str) -> dict[str, Any]:
    """The input of a lookup-by-key call."""
    if not isinstance(idempotency_key, str) or not _KEY.fullmatch(idempotency_key):
        raise ValueError(
            "an idempotency key is 1 to 255 printable ASCII characters with no surrounding whitespace"
        )
    return {"query_params": {QUERY_PARAM: idempotency_key}}


def _same_json(left: object, right: object) -> bool:
    """Whether two JSON values are the same canonical document (``False`` for anything that is not plain JSON)."""
    try:
        return canonical_bytes(left) == canonical_bytes(right)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _method(value: object) -> str | None:
    return value.strip().upper() if isinstance(value, str) and value.strip() else None


def facts_from_lookup(
    view: ProposalView, *, lookup_status: int, lookup_body: Any
) -> ReceiptFacts | None:
    """What the lookup by key says about ``view``'s dispatch, as receipt facts, or ``None`` if it does not confirm it.

    ``None`` is *not* a failure: it is a lookup that answered something other than this proposal's effect (a 404, a 5xx, a body
    that is not a stored request, a request stored under another key, URL, method or body). The proposal stays ``unknown``.

    The stored request must carry the proposal's logical key, the downstream's own request id, and the method, URL and body the
    proposal recorded (the method defaults to ``POST`` as the sender's does). A proposal that expects a resource version is never
    verified here: this connector cannot read one back.
    """
    args = view.args
    if (
        lookup_status != _HTTP_OK
        or not isinstance(lookup_body, dict)
        or view.attempt is None
        or view.connector != CONNECTOR
        or view.connector_version != VERSION
        or view.expected_resource_version is not None
        or not isinstance(args, Mapping)
    ):
        return None
    request_id = lookup_body.get("request_id")
    expected_method = _method(args.get("method") or "POST")
    if (
        lookup_body.get("idempotency_key") != view.logical_key
        or not isinstance(request_id, str)
        or not request_id.strip()
        or lookup_body.get("url") != view.resource
        or args.get("url") != view.resource
        or expected_method is None
        or _method(lookup_body.get("method")) != expected_method
        or not _same_json(lookup_body.get("body"), args.get("body"))
    ):
        return None
    try:
        return ReceiptFacts(
            attempt=view.attempt,
            idempotency_key=view.logical_key,
            connector=CONNECTOR,
            connector_version=VERSION,
            resource=view.resource,
            intent_hash=view.intent_hash,
            downstream_request_id=request_id,
            response_digest="sha256:" + hashlib.sha256(canonical_bytes(lookup_body)).hexdigest(),
            resource_version=None,
        )
    except (ProposalError, ValueError, TypeError):
        # an id this connector cannot represent, or a body that cannot be digested (a NaN is not JSON): not this effect
        return None


__all__ = [
    "CAPABILITY",
    "CONNECTOR",
    "KEY_HEADER",
    "QUERY_PARAM",
    "VERSION",
    "facts_from_lookup",
    "lookup_config",
    "lookup_input",
]
