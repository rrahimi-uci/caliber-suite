"""Tolerant payload -> dataclass decoding.

Two properties matter more than convenience here.

**Unknown fields must not break an older client.** CALIBER adds fields to
responses; a client that raised on the first one would break every existing
script the moment the server was upgraded. Unknown keys are kept in ``extra``
rather than dropped, so a caller can still reach a field this SDK predates.

**Missing fields must not raise either.** A deployment running an older server
will omit newer fields, and an SDK that insisted on them would be unusable
against exactly the deployments most likely to be behind.

The result is a client that degrades in both directions instead of failing in
either.
"""

from __future__ import annotations

import dataclasses
from typing import Any, TypeVar

T = TypeVar("T")


def decode(cls: type[T], payload: Any) -> T:
    """Build ``cls`` from ``payload``, tolerating unknown and missing keys."""
    if not isinstance(payload, dict):
        payload = {}
    fields = {field.name for field in dataclasses.fields(cls)}  # type: ignore[arg-type]
    known = {key: value for key, value in payload.items() if key in fields}
    if "extra" in fields:
        known["extra"] = {key: value for key, value in payload.items() if key not in fields}
    return cls(**known)


def decode_list(cls: type[T], payload: Any) -> list[T]:
    """Decode a list payload, tolerating a non-list by returning nothing."""
    if not isinstance(payload, list):
        return []
    return [decode(cls, item) for item in payload]


__all__ = ["decode", "decode_list"]
