"""Signed receipt callbacks from a connector: who may call, and what proves it was them (B05.4b, #725).

A connector tells the platform "the effect for this idempotency key is done" by calling one fixed endpoint. The call is not a
CALIBER session; its only admission control is a signature over everything that matters, made with a secret that belongs to
*one connector in one project*:

``X-Caliber-Signature: v1=<hex>`` is ``HMAC-SHA256(secret, "v1\\n<connector>\\n<project>\\n<timestamp>\\n<delivery>\\n<sha256 of the
body, hex>")``, with the connector, project, timestamp and delivery id carried in ``X-Caliber-Connector``, ``X-Caliber-Project``,
``X-Caliber-Timestamp`` (whole seconds since the epoch, no leading zeros) and ``X-Caliber-Delivery`` (a unique id of 1 to 128 printable
characters). Because the connector and the project are *inside* the signed string, a signature made for one connector or
project is useless for another, and a captured message cannot be replayed outside the timestamp window
(:data:`TOLERANCE_SECONDS`); within it a replay is harmless, because the callback only ever triggers the platform's own query
of the downstream and a receipt is recorded once.

Bindings come from ``CALIBER_EFFECT_CALLBACK_CONNECTORS`` (a JSON array): each names a ``connector``, the ``project_id`` it may
act in, the ``secret_source`` its signing secret is read from (never the secret itself) and the ``base_url`` of its
downstream, used for the authoritative lookup. An empty list (the default) means no callback is accepted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

TOLERANCE_SECONDS: Final = 300
_MAX_TIMESTAMP_DIGITS: Final = 12
#: A signing secret shorter than this is refused (fail closed) rather than trusted.
MIN_SECRET_CHARS: Final = 16
SIGNATURE_VERSION: Final = "v1"
_PRINTABLE = re.compile(r"[\x21-\x7e]{1,128}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SIGNATURE = re.compile(r"v1=([0-9a-f]{64})")
_KEYS: Final = frozenset({"connector", "project_id", "secret_source", "base_url"})


@dataclass(frozen=True)
class CallbackBinding:
    """One connector's permission to call back for one project."""

    connector: str
    project_id: str
    secret_source: str
    base_url: str


def parse_bindings(raw: str) -> tuple[CallbackBinding, ...]:
    """The bindings in ``raw`` (a JSON array), or ``ValueError`` saying what is wrong.

    A binding has exactly the four keys, a connector and project made of letters, digits, dots, underscores and hyphens, a
    non-blank secret source and an ``http`` or ``https`` base URL with a host and no credentials; a connector may be bound to a
    project once.
    """
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("effect_callback_connectors must be a JSON array") from exc
    if not isinstance(parsed, list):
        raise ValueError("effect_callback_connectors must be a JSON array")
    bindings: list[CallbackBinding] = []
    seen: set[tuple[str, str]] = set()
    for item in parsed:
        if not isinstance(item, dict) or set(item) != _KEYS:
            raise ValueError(f"each binding has exactly the keys {sorted(_KEYS)}")
        for key in ("connector", "project_id"):
            value = item[key]
            if not isinstance(value, str) or not _NAME.fullmatch(value):
                raise ValueError(f"{key} must be letters, digits, dots, underscores and hyphens")
        source, url = item["secret_source"], item["base_url"]
        if not isinstance(source, str) or not source.strip():
            raise ValueError("secret_source must name where the signing secret is read from")
        if not isinstance(url, str) or not _is_base_url(url):
            raise ValueError("base_url must be an http or https URL with a host and no credentials")
        identity = (item["connector"], item["project_id"])
        if identity in seen:
            raise ValueError(f"connector {identity[0]!r} is bound to project {identity[1]!r} twice")
        seen.add(identity)
        bindings.append(
            CallbackBinding(item["connector"], item["project_id"], source.strip(), url.rstrip("/"))
        )
    return tuple(bindings)


def _is_base_url(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return (
            parts.scheme in ("http", "https")
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and not parts.query
            and not parts.fragment
        )
    except ValueError:
        return False


def signed_string(
    connector: str, project_id: str, timestamp: str, delivery: str, body: bytes
) -> bytes:
    """The exact bytes that are signed (every part that matters, and the body only through its digest)."""
    digest = hashlib.sha256(body).hexdigest()
    return f"{SIGNATURE_VERSION}\n{connector}\n{project_id}\n{timestamp}\n{delivery}\n{digest}".encode()


def sign(
    secret: str, connector: str, project_id: str, timestamp: int, delivery: str, body: bytes
) -> str:
    """The ``X-Caliber-Signature`` header value for these parts: what a connector sends."""
    mac = hmac.new(
        secret.encode("utf-8"),
        signed_string(connector, project_id, str(timestamp), delivery, body),
        hashlib.sha256,
    )
    return f"{SIGNATURE_VERSION}={mac.hexdigest()}"


def check_signature(
    secret: str,
    *,
    connector: str,
    project_id: str,
    timestamp: str,
    delivery: str,
    signature: str,
    body: bytes,
    now: float,
) -> bool:
    """Whether ``signature`` is valid for these parts, the timestamp is within the window and the delivery id is well formed.

    Every failure is the same ``False`` (the caller answers one generic refusal): a probe cannot tell an unknown connector
    from a bad signature or a stale timestamp.
    """
    if not _SIGNATURE.fullmatch(signature) or not _PRINTABLE.fullmatch(delivery):
        return False
    if not (
        timestamp.isascii() and timestamp.isdigit() and len(timestamp) <= _MAX_TIMESTAMP_DIGITS
    ):
        return False
    if timestamp != str(int(timestamp)):  # the canonical spelling only: no leading zeros
        return False
    if abs(now - int(timestamp)) > TOLERANCE_SECONDS:
        return False
    expected = sign(secret, connector, project_id, int(timestamp), delivery, body)
    return hmac.compare_digest(expected, signature)


def describe(bindings: tuple[CallbackBinding, ...]) -> list[dict[str, Any]]:
    """The bindings without their secret sources, for a settings page."""
    return [{"connector": b.connector, "project_id": b.project_id} for b in bindings]


__all__ = [
    "MIN_SECRET_CHARS",
    "SIGNATURE_VERSION",
    "TOLERANCE_SECONDS",
    "CallbackBinding",
    "check_signature",
    "describe",
    "parse_bindings",
    "sign",
    "signed_string",
]
