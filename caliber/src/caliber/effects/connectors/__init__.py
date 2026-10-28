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
      The downstream answered with a 4xx *that the connector's capability documents as a refusal* (a stale
      precondition, a reused key, a bad body, no such resource): it committed nothing. Recorded as ``failed``. A 4xx
      the capability does not name (a gateway's 401 or 413, a 409 "in progress") is ``ambiguous``.
  ``committed_receipt``
      The downstream answered 200, 201 or 204: it says it committed (or replayed a commit for this key). This is a
      *claim*: it becomes ``committed`` only when the receipt is verified against the intent (B05.4).
  ``ambiguous``
      Anything else: a dropped connection or a timeout after the request may have left, a 5xx, a 408, a redirect, an
      error nobody classified. The effect may or may not have happened: the proposal becomes ``unknown`` and only a
      lookup by key can say more.

A 4xx that follows an earlier attempt that may have reached the downstream, or an earlier dispatch of the same effect the
caller cannot see (a recovery after a crash), is ``ambiguous``, not a rejection: that attempt may have committed, and a retry
with a stale precondition then answers 412 *because* it committed. Likewise a lookup that finds
nothing (:func:`interpret_lookup`) is ``unknown`` unless the connector proves absence is final: a 404 never certifies
"it did not happen" on its own (D1 finding; ADR 0003).

Nothing here sends a request, reads a database or decides a state; it names what happened.
"""

from __future__ import annotations

import hashlib
import json
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
KEY_RETENTIONS: Final = ("unbounded", "bounded", "unconfirmed")
LOOKUP_CONSISTENCY: Final = ("read_your_writes", "eventual", "unknown")
_REQUEST_TIMEOUT: Final = 408
_FIRST_CLIENT_ERROR: Final = 400
_FIRST_SERVER_ERROR: Final = 500
#: The only success answers that are a commit claim: 202 is "accepted for later processing", and 203, 205 and 206 are
#: not what a write answers with.
_COMMIT_CLAIMS: Final = frozenset({200, 201, 204})
_FIRST_SUCCESS: Final = 200
_AFTER_SUCCESS: Final = 300


@dataclass(frozen=True)
class ConnectorCapability:
    """A connector's documented behaviour for exactly-once dispatch, as measured against its fixture.

    ``key_retention`` is ``unbounded`` (the downstream never forgets a key), ``bounded`` (it forgets after
    ``key_retention_seconds``) or ``unconfirmed`` (nobody has confirmed either: a dispatcher must not rely on an old key).
    ``definitive_rejection_statuses`` are the 4xx answers the connector documents as "refused, nothing committed": a 4xx
    outside this set (a gateway's 401 or 413, a 409 "in progress") proves nothing about the effect. ``absence_proves_noncommit``
    is ``True`` only if the connector proves a request that is not found will never be processed (a tombstone or a cancel
    the downstream honours) and its lookup reads its own writes; without that, a lookup that finds nothing leaves the
    proposal ``unknown``.
    """

    connector: str
    version: str
    idempotency_key_header: str | None
    key_scope: str
    key_retention: str
    key_retention_seconds: int | None
    key_conflict_status: int | None
    precondition_header: str | None
    stale_precondition_status: int | None
    definitive_rejection_statuses: frozenset[int]
    lookup_by_key: bool
    lookup_consistency: str
    absence_proves_noncommit: bool

    def __post_init__(self) -> None:
        self._check_identity()
        self._check_key()
        self._check_statuses()
        self._check_lookup()

    def _check_identity(self) -> None:
        for name in ("connector", "version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-blank string")
        if self.key_scope not in KEY_SCOPES:
            raise ValueError(f"key_scope must be one of {KEY_SCOPES}")
        if self.key_retention not in KEY_RETENTIONS:
            raise ValueError(f"key_retention must be one of {KEY_RETENTIONS}")
        if self.lookup_consistency not in LOOKUP_CONSISTENCY:
            raise ValueError(f"lookup_consistency must be one of {LOOKUP_CONSISTENCY}")

    def _check_key(self) -> None:
        header = self.idempotency_key_header
        if header is not None and (not isinstance(header, str) or not header.strip()):
            raise ValueError("idempotency_key_header must be a non-blank string or None")
        if header is None and (
            self.key_conflict_status is not None
            or self.lookup_by_key
            or self.key_scope != "unknown"
            or self.key_retention != "unconfirmed"
            or self.key_retention_seconds is not None
        ):
            raise ValueError(
                "a connector with no stable key has an unknown key scope and retention, no key conflicts and no lookup by key"
            )
        if (self.key_retention == "bounded") != (self.key_retention_seconds is not None):
            raise ValueError("key_retention_seconds is set exactly when key_retention is 'bounded'")
        if self.key_retention_seconds is not None and (
            type(self.key_retention_seconds) is not int or self.key_retention_seconds < 1
        ):
            raise ValueError("key_retention_seconds must be a positive integer")

    def _check_statuses(self) -> None:
        if self.precondition_header is None and self.stale_precondition_status is not None:
            raise ValueError("a connector with no precondition has no stale-precondition status")
        refusals = self.definitive_rejection_statuses
        if not isinstance(refusals, frozenset) or any(
            type(status) is not int
            or not _FIRST_CLIENT_ERROR <= status < _FIRST_SERVER_ERROR
            or status == _REQUEST_TIMEOUT
            for status in refusals
        ):
            raise ValueError(
                "definitive_rejection_statuses must be a frozenset of 4xx statuses other than 408"
            )
        for name in ("key_conflict_status", "stale_precondition_status"):
            status = getattr(self, name)
            if status is not None and status not in refusals:
                raise ValueError(f"{name} must be one of definitive_rejection_statuses")

    def _check_lookup(self) -> None:
        for name in ("lookup_by_key", "absence_proves_noncommit"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        if self.absence_proves_noncommit and not self.lookup_by_key:
            raise ValueError("absence can only be proven through a lookup by key")
        if self.absence_proves_noncommit and self.lookup_consistency != "read_your_writes":
            raise ValueError("an eventually consistent lookup cannot prove absence")

    @property
    def retention_confirmed(self) -> bool:
        """Whether how long the downstream remembers a key is known (``unbounded`` or ``bounded``)."""
        return self.key_retention != "unconfirmed"

    @property
    def carries_a_write_exactly_once(self) -> bool:
        """Whether it has everything a write needs: a key with a known scope that conflicts are refused for, a lookup by
        key with a known consistency, and a precondition. Whether its retention is confirmed is separate
        (:attr:`retention_confirmed`): connector admission must require it or record the accepted risk."""
        return (
            self.idempotency_key_header is not None
            and self.key_scope != "unknown"
            and self.key_conflict_status is not None
            and self.lookup_by_key
            and self.lookup_consistency != "unknown"
            and self.precondition_header is not None
        )

    def digest(self) -> str:
        """``sha256:`` of the record's canonical JSON: what connector admission binds, so a changed promise is a new digest."""
        record = {
            "connector": self.connector,
            "version": self.version,
            "idempotency_key_header": self.idempotency_key_header,
            "key_scope": self.key_scope,
            "key_retention": self.key_retention,
            "key_retention_seconds": self.key_retention_seconds,
            "key_conflict_status": self.key_conflict_status,
            "precondition_header": self.precondition_header,
            "stale_precondition_status": self.stale_precondition_status,
            "definitive_rejection_statuses": sorted(self.definitive_rejection_statuses),
            "lookup_by_key": self.lookup_by_key,
            "lookup_consistency": self.lookup_consistency,
            "absence_proves_noncommit": self.absence_proves_noncommit,
        }
        data = json.dumps(record, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "sha256:" + hashlib.sha256(data).hexdigest()


def classify_status(
    status_code: int,
    *,
    capability: ConnectorCapability,
    attempts: int = 1,
    earlier_attempt_may_have_been_sent: bool | None = None,
    earlier_dispatch_possible: bool = False,
) -> Classified:
    """What an HTTP answer proves for this connector.

    A 200, 201 or 204 is a commit *claim* (``committed_receipt``); every other 2xx (a 202 is "accepted for later
    processing"), a 408, a 3xx, a 5xx and anything unknown is ``ambiguous``. A 4xx is a ``definitive_rejection`` only if the
    capability documents that status as "refused, nothing committed" **and** nothing earlier could have committed;
    otherwise it is ``ambiguous`` (a gateway's 401, a 409 "in progress", a 413, or a 412 that an earlier commit caused).
    "Earlier" is an earlier attempt by the executor's own retry loop (``earlier_attempt_may_have_been_sent``, which defaults
    to ``attempts > 1`` when the caller does not know) or an earlier dispatch of the same logical effect that this call
    cannot see (``earlier_dispatch_possible``: a recovery after a crash, where the first dispatch may have committed).
    """
    if type(status_code) is not int:
        raise TypeError("status_code must be an integer")
    code = f"http_{status_code}"
    if status_code in _COMMIT_CLAIMS:
        return Classified(TransportOutcome.COMMITTED_RECEIPT, code, status_code, attempts)
    if _FIRST_CLIENT_ERROR <= status_code < _FIRST_SERVER_ERROR and status_code != _REQUEST_TIMEOUT:
        earlier = (
            attempts > 1
            if earlier_attempt_may_have_been_sent is None
            else earlier_attempt_may_have_been_sent
        ) or earlier_dispatch_possible
        if status_code in capability.definitive_rejection_statuses and not earlier:
            return Classified(TransportOutcome.DEFINITIVE_REJECTION, code, status_code, attempts)
        reason = (
            code if status_code in capability.definitive_rejection_statuses else "unrecognised_4xx"
        )
        return Classified(TransportOutcome.AMBIGUOUS, reason, status_code, attempts)
    return Classified(TransportOutcome.AMBIGUOUS, code, status_code, attempts)


def classify_result(
    result: Mapping[str, Any],
    *,
    capability: ConnectorCapability,
    earlier_dispatch_possible: bool = False,
) -> Classified:
    """Classify the dict ``execute_openapi_http_tool`` returns (its ``status_code``, ``attempts`` and
    ``earlier_attempts_may_have_been_sent``)."""
    attempts = result.get("attempts", 1)
    earlier = result.get("earlier_attempts_may_have_been_sent")
    return classify_status(
        result["status_code"],
        capability=capability,
        attempts=attempts if type(attempts) is int and attempts >= 1 else 1,
        earlier_attempt_may_have_been_sent=earlier if type(earlier) is bool else None,
        earlier_dispatch_possible=earlier_dispatch_possible,
    )


def classify_error(error: BaseException) -> Classified:
    """Classify an exception from a dispatch attempt: ``proven_not_sent`` only if the executor says nothing was sent.

    The executor marks every exception raised before a request is built, and every error whose attempts all failed to
    connect, with ``never_sent=True``. An error that says ``never_sent=False``, and any exception without the marker
    (a bug, a cancelled task, an unknown transport class), proves nothing about whether the request left: ``ambiguous``.
    """
    attempts = getattr(error, "attempts", 0)
    attempts = attempts if type(attempts) is int and attempts >= 0 else 0
    if getattr(error, "never_sent", None) is True:
        return Classified(TransportOutcome.PROVEN_NOT_SENT, "not_sent", None, attempts)
    if hasattr(error, "never_sent"):
        return Classified(TransportOutcome.AMBIGUOUS, "may_have_been_sent", None, attempts)
    return Classified(TransportOutcome.AMBIGUOUS, "unclassified_error", None, attempts)


def interpret_lookup(capability: ConnectorCapability, status_code: int) -> str:
    """What a lookup by key says: ``answered``, ``not_committed`` or ``unknown``.

    A 2xx means the downstream *answered*; it does not say a request is stored under the key (a filtering lookup answers
    200 with an empty list) or that it is the intended one: that is verified against the intent by the receipt check
    (B05.4). A 404 means ``not_committed`` only if the connector proves absence is final (and reads its own writes);
    otherwise, as for every other answer (a 5xx, a 401, a malformed reply), the outcome stays ``unknown``.
    """
    if not capability.lookup_by_key:
        return "unknown"
    if _FIRST_SUCCESS <= status_code < _AFTER_SUCCESS:
        return "answered"
    if status_code == 404 and capability.absence_proves_noncommit:  # noqa: PLR2004 - the HTTP status
        return "not_committed"
    return "unknown"


__all__ = [
    "KEY_RETENTIONS",
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
