"""What a connector can promise about an effect, and what its answer to one request proves (B05.3, #711; ADR 0003).

The dispatcher (B05.5) sends an effect through a connector and has to decide what to record. Two small, pure things let it
decide without guessing:

* a :class:`ConnectorCapability`: the connector's *documented, tested* behaviour for exactly-once dispatch: how it takes a
  stable key (header, scope, retention), what it answers when the key is reused for a different request, which
  precondition it honours and what it answers when it fails, whether it can be asked "did my key commit?" and how
  consistent that answer is, and whether its "no such request" proves the effect can never happen. A connector without a
  proven capability is not trusted to carry a write (connector admission, B12, binds this record's digest).
* a :class:`TransportOutcome` for one request, one of four, and never more optimistic than the evidence:

  ``proven_not_sent``
      The request never reached the downstream (refused before it was built, or every attempt failed to connect).
      Safe to record as not happened.
  ``definitive_rejection``
      The downstream answered with a 4xx that refuses this request (a stale precondition, a reused key, a bad body,
      no such resource): it committed nothing. Recorded as ``failed``.
  ``committed_receipt``
      The downstream answered 2xx: it says it committed (or replayed a commit for this key). This is a *claim*: it
      becomes ``committed`` only when the receipt is verified against the intent (B05.4).
  ``ambiguous``
      Anything else: a dropped connection or a timeout after the request may have left, a 5xx, a 408, a redirect, an
      error nobody classified. The effect may or may not have happened: the proposal becomes ``unknown`` and only a
      lookup by key can say more.

A 4xx that follows an earlier attempt (``attempts > 1``) is ``ambiguous``, not a rejection: the first attempt may have
committed, and a retry with a stale precondition then answers 412 *because* it committed. Likewise a lookup that finds
nothing (:func:`interpret_lookup`) is ``unknown`` unless the connector proves absence is final: a 404 never certifies
"it did not happen" on its own (D1 finding; ADR 0003).

Nothing here sends a request, reads a database or decides a state; it names what happened.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Any, Final


class TransportOutcome(str, Enum):
    """What one dispatch attempt proves about whether the effect happened."""

    PROVEN_NOT_SENT = "proven_not_sent"
    DEFINITIVE_REJECTION = "definitive_rejection"
    COMMITTED_RECEIPT = "committed_receipt"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class Classified:
    """An outcome with a short stable ``code`` (a status or a cause, never a payload) and the attempts it took."""

    outcome: TransportOutcome
    code: str
    status_code: int | None = None
    attempts: int = 0


KEY_SCOPES: Final = ("downstream", "resource", "unknown")
LOOKUP_CONSISTENCY: Final = ("read_your_writes", "eventual", "unknown")
_REQUEST_TIMEOUT: Final = 408
_FIRST_CLIENT_ERROR: Final = 400
_FIRST_SERVER_ERROR: Final = 500
_FIRST_SUCCESS: Final = 200
_AFTER_SUCCESS: Final = 300


@dataclass(frozen=True)
class ConnectorCapability:
    """A connector's documented behaviour for exactly-once dispatch, as measured against its fixture.

    ``key_retention_seconds`` is how long the downstream remembers a key (``None``: it never expires one in the fixture,
    and the real system's retention is unconfirmed, so a dispatcher must not rely on a key older than the retention it
    has confirmed). ``absence_proves_noncommit`` is ``True`` only if the connector can prove a request that is not
    found will never be processed (a tombstone or a cancel the downstream honours); without that, a lookup that finds
    nothing leaves the proposal ``unknown``.
    """

    connector: str
    version: str
    idempotency_key_header: str | None
    key_scope: str
    key_retention_seconds: int | None
    key_conflict_status: int | None
    precondition_header: str | None
    stale_precondition_status: int | None
    lookup_by_key: bool
    lookup_consistency: str
    absence_proves_noncommit: bool

    def __post_init__(self) -> None:
        for name in ("connector", "version"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be a non-blank string")
        if self.key_scope not in KEY_SCOPES:
            raise ValueError(f"key_scope must be one of {KEY_SCOPES}")
        if self.lookup_consistency not in LOOKUP_CONSISTENCY:
            raise ValueError(f"lookup_consistency must be one of {LOOKUP_CONSISTENCY}")
        if self.idempotency_key_header is None:
            if (
                self.key_conflict_status is not None
                or self.lookup_by_key
                or self.absence_proves_noncommit
            ):
                raise ValueError(
                    "a connector with no stable key has no key conflicts and no lookup by key"
                )
        elif not self.idempotency_key_header.strip():
            raise ValueError("idempotency_key_header must not be blank")
        if self.precondition_header is None and self.stale_precondition_status is not None:
            raise ValueError("a connector with no precondition has no stale-precondition status")
        for name in ("key_conflict_status", "stale_precondition_status"):
            status = getattr(self, name)
            if status is not None and not (
                type(status) is int and _FIRST_CLIENT_ERROR <= status < _FIRST_SERVER_ERROR
            ):
                raise ValueError(f"{name} must be a 4xx status")
        if self.key_retention_seconds is not None and (
            type(self.key_retention_seconds) is not int or self.key_retention_seconds < 1
        ):
            raise ValueError("key_retention_seconds must be a positive integer or None")
        if self.absence_proves_noncommit and not self.lookup_by_key:
            raise ValueError("absence can only be proven through a lookup by key")

    @property
    def carries_a_write_exactly_once(self) -> bool:
        """Whether it can carry a write: a stable key, a way to ask whether the key committed, and a precondition."""
        return (
            self.idempotency_key_header is not None
            and self.lookup_by_key
            and self.precondition_header is not None
        )


def classify_status(status_code: int, *, attempts: int = 1) -> Classified:
    """What an HTTP answer proves. ``attempts`` above 1 makes a 4xx ambiguous (an earlier attempt may have committed)."""
    if type(status_code) is not int:
        raise TypeError("status_code must be an integer")
    code = f"http_{status_code}"
    if _FIRST_SUCCESS <= status_code < _AFTER_SUCCESS:
        return Classified(TransportOutcome.COMMITTED_RECEIPT, code, status_code, attempts)
    if _FIRST_CLIENT_ERROR <= status_code < _FIRST_SERVER_ERROR and status_code != _REQUEST_TIMEOUT:
        outcome = (
            TransportOutcome.DEFINITIVE_REJECTION if attempts <= 1 else TransportOutcome.AMBIGUOUS
        )
        return Classified(outcome, code, status_code, attempts)
    return Classified(TransportOutcome.AMBIGUOUS, code, status_code, attempts)


def classify_result(result: Mapping[str, Any]) -> Classified:
    """Classify the dict ``execute_openapi_http_tool`` returns (its ``status_code`` and ``attempts``)."""
    attempts = result.get("attempts", 1)
    return classify_status(
        result["status_code"], attempts=attempts if type(attempts) is int and attempts >= 1 else 1
    )


def classify_error(error: BaseException) -> Classified:
    """Classify an exception from a dispatch attempt: ``proven_not_sent`` only if the executor says nothing was sent.

    An ``OpenApiExecutionError`` carries ``never_sent``; any other exception (a bug, a cancelled task, an unknown
    transport class) proves nothing about whether the request left, so it is ``ambiguous``.
    """
    attempts = getattr(error, "attempts", 0)
    attempts = attempts if type(attempts) is int and attempts >= 0 else 0
    if getattr(error, "never_sent", None) is True:
        return Classified(TransportOutcome.PROVEN_NOT_SENT, "not_sent", None, attempts)
    if hasattr(error, "never_sent"):
        return Classified(TransportOutcome.AMBIGUOUS, "may_have_been_sent", None, attempts)
    return Classified(TransportOutcome.AMBIGUOUS, "unclassified_error", None, attempts)


def interpret_lookup(capability: ConnectorCapability, status_code: int) -> str:
    """What a lookup by key says: ``found``, ``not_committed`` or ``unknown``.

    A 2xx means the downstream holds a stored request under the key (its content is verified against the intent by the
    receipt check, B05.4). A 404 means ``not_committed`` only if the connector proves absence is final; otherwise, as for
    every other answer (a 5xx, a 401, a malformed reply), the outcome stays ``unknown``.
    """
    if not capability.lookup_by_key:
        return "unknown"
    if _FIRST_SUCCESS <= status_code < _AFTER_SUCCESS:
        return "found"
    if status_code == 404 and capability.absence_proves_noncommit:  # noqa: PLR2004 - the HTTP status
        return "not_committed"
    return "unknown"


__all__ = [
    "KEY_SCOPES",
    "LOOKUP_CONSISTENCY",
    "Classified",
    "ConnectorCapability",
    "TransportOutcome",
    "classify_error",
    "classify_result",
    "classify_status",
    "interpret_lookup",
]
