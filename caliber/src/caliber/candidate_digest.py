"""The digest that binds an Apply to the exact candidate that cleared the eval gate (B06.4, #753).

When the eval gate passes, the stage that moves a refinement job to ``candidate_ready`` stores the digest of the
candidate it evaluated on the job (``candidate_digest``). The Apply claim recomputes the digest of the candidate it is
about to promote, inside the claim transaction, and refuses when the two differ: a candidate edited after the gate
cleared is never promoted on the strength of an evaluation of different content.

The digest is the SHA-256 of the candidate's canonical JSON (sorted keys, no insignificant whitespace, ASCII-only), so it
does not depend on key order or on how the database stores the document. It is a hash: it is safe to write to an audit
row and reveals nothing of the candidate's text.

This module imports only the standard library so the orchestrator stages and the Apply route can share it without
importing each other.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

#: Prefix of every candidate digest. ``sha256:`` plus 64 lowercase hex digits is 71 characters, the column width.
DIGEST_PREFIX = "sha256:"
CANDIDATE_DIGEST_LENGTH = len(DIGEST_PREFIX) + 64


def candidate_digest(candidate: Mapping[str, Any] | None) -> str:
    """``sha256:<hex>`` of ``candidate``'s canonical JSON.

    Raises ``ValueError`` when ``candidate`` is not a mapping or holds a value that has no JSON form (``NaN``,
    ``Infinity``, a non-string key that is not a scalar, an arbitrary object), so a caller can treat "cannot be
    digested" as "cannot be verified" and fail closed.
    """
    if not isinstance(candidate, Mapping):
        raise ValueError("a candidate to digest must be a mapping")
    try:
        canonical = json.dumps(
            candidate, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("candidate has no canonical JSON form") from exc
    return DIGEST_PREFIX + hashlib.sha256(canonical.encode("ascii")).hexdigest()
