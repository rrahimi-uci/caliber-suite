"""Acceptance predicates: pure judgements over observations a caller already read (B09.1, #779).

Acceptance of a run is an authoritative domain predicate, not HTTP success and not model text (gap analysis V11: the model says
"complete", the downstream answers 200, the target state is wrong). A :class:`Predicate` is one such check. It is a function of
:class:`Observations` and nothing else: the caller reads the downstream (the case record, the receipts) and hands the result
in, so a predicate does no I/O, reads no clock and draws no random number, the same observations always give the same
:class:`CriterionResult`, and a recorded observation can be re-judged later without touching the downstream (historical
replay, B09.4, #805). The module imports only the standard library; a test asserts it.

Observations are a frozen mapping: JSON-shaped values (``dict`` and ``list`` become read-only mappings and tuples; strings,
integers, finite floats, booleans and ``None`` stay), string keys only, bounded depth. A value that cannot be recorded as
JSON is refused at the door (``invalid_observations``), so nothing a predicate sees can change underneath it.

The Harborlight set :data:`HARBORLIGHT_V1` reads three keys of the observations:

* ``proposal``: ``document_type`` (a non-empty string) and ``expected_version`` (the case version the effect was proposed
  against, a non-negative integer).
* ``case_record``: ``document_requests``, the list the downstream case record returns, each entry a mapping with
  ``request_id``, ``document_type`` and ``case_version``.
* ``receipt`` (optional): ``downstream_request_id``, the request the dispatch's receipt names. When present it is binding:
  only that request is judged, and a receipt without a usable id fails both criteria. Without it every request of the
  proposal's type is a candidate, which can accept a refused proposal on another proposal's request; a caller that holds a
  receipt (every governed dispatch does) should always supply it.

Its two predicates, both **fail closed**: a missing, malformed or ambiguous value is a failed criterion (never an exception, and
never a pass), and every :class:`CriterionResult` names, in ``evidence_ref``, the RFC 6901 JSON pointer into the observations
of the value that decided it.

* ``document_request_exists``: the case record lists a request whose ``document_type`` is exactly the proposal's.
* ``attached_to_expected_version``: a request of the proposal's ``document_type`` carries the case version the effect
  *produces*, ``expected_version + 1``. The issue text says "equals the proposal's expected version", but the downstream (and
  the connector that dispatches to it, :func:`caliber.effects.connectors.document_request.facts_from_lookup`) stamps a stored
  request with the version **after** its mutation, one more than the ``If-Match`` it was sent; a literal equality would reject
  every real success. Equality with ``expected_version`` itself therefore means the downstream did not advance the case.

Every failure of the caller's own contract (a bad predicate, a mixed criteria version, a predicate that raises) is a
:class:`PredicateError` with a stable ``code``; it is raised, not turned into a verdict, so a bug cannot become an accepted run
or a silent rejection.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Final

#: The criteria version of :data:`HARBORLIGHT_V1`.
CRITERIA_VERSION: Final = "harborlight-v1"
#: The versions a successful effect adds to the case: the downstream stamps a request with the version after its mutation.
CASE_VERSION_STEP: Final = 1
#: Observations nest at most this deep; a deeper (or cyclic) structure is refused instead of recursing without bound.
MAX_DEPTH: Final = 32

INVALID_PREDICATE: Final = "invalid_predicate"
INVALID_OBSERVATIONS: Final = "invalid_observations"
INVALID_RESULT: Final = "invalid_result"
EMPTY_PREDICATE_SET: Final = "empty_predicate_set"
DUPLICATE_PREDICATE_ID: Final = "duplicate_predicate_id"
MIXED_CRITERIA_VERSIONS: Final = "mixed_criteria_versions"
PREDICATE_FAILED: Final = "predicate_failed"

#: Every code a :class:`PredicateError` carries.
CODES: Final = frozenset(
    {
        INVALID_PREDICATE,
        INVALID_OBSERVATIONS,
        INVALID_RESULT,
        EMPTY_PREDICATE_SET,
        DUPLICATE_PREDICATE_ID,
        MIXED_CRITERIA_VERSIONS,
        PREDICATE_FAILED,
    }
)

# The same shape as a rule id in a rule bundle (caliber.context.rule_bundle), so a predicate cites a rule by its real id.
_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_VERSION = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
_POINTER_ESCAPES: Final = str.maketrans({"~": "~0", "/": "~1"})


class PredicateError(ValueError):
    """The caller broke the predicate contract. ``code`` is one of :data:`CODES`; the message names a JSON pointer, never an observation value."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


def _freeze(value: object, path: str, depth: int) -> Any:
    if depth > MAX_DEPTH:
        raise PredicateError(
            INVALID_OBSERVATIONS, f"{path or '/'} is nested deeper than {MAX_DEPTH} levels"
        )
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PredicateError(INVALID_OBSERVATIONS, f"{path} is not a finite number")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise PredicateError(
                    INVALID_OBSERVATIONS, f"{path or '/'} has a key that is not a string"
                )
            frozen[key] = _freeze(item, f"{path}/{key.translate(_POINTER_ESCAPES)}", depth + 1)
        return MappingProxyType(frozen)
    if isinstance(value, list | tuple):
        return tuple(
            _freeze(item, f"{path}/{index}", depth + 1) for index, item in enumerate(value)
        )
    raise PredicateError(
        INVALID_OBSERVATIONS, f"{path or '/'} is a {type(value).__name__}, which is not JSON-shaped"
    )


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


class Observations(Mapping[str, Any]):
    """A frozen, JSON-shaped mapping of downstream reads and receipts, supplied by the caller (no I/O inside).

    Built from any mapping; the source is copied, so changing it afterwards changes nothing here, and nothing here can be
    changed. Nested mappings are read-only and nested lists are tuples.
    """

    __slots__ = ("_data",)
    _data: Mapping[str, Any]

    def __init__(self, data: Mapping[str, Any]) -> None:
        if not isinstance(data, Mapping):
            raise PredicateError(
                INVALID_OBSERVATIONS, f"observations must be a mapping, not a {type(data).__name__}"
            )
        object.__setattr__(self, "_data", _freeze(data, "", 0))

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("Observations is frozen")

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def as_dict(self) -> dict[str, Any]:
        """A plain, mutable ``dict``/``list`` copy (for a digest or a stored record); changing it changes nothing here."""
        thawed: dict[str, Any] = _thaw(self._data)
        return thawed


@dataclass(frozen=True)
class CriterionResult:
    """One predicate's verdict. ``evidence_ref`` is the JSON pointer into the observations of the value that decided it."""

    predicate_id: str
    passed: bool
    evidence_ref: str

    def __post_init__(self) -> None:
        if not isinstance(self.predicate_id, str) or not _ID.fullmatch(self.predicate_id):
            raise PredicateError(INVALID_RESULT, "predicate_id is not a valid predicate id")
        if type(self.passed) is not bool:
            raise PredicateError(INVALID_RESULT, "passed must be a bool")
        if not isinstance(self.evidence_ref, str) or not self.evidence_ref.startswith("/"):
            raise PredicateError(
                INVALID_RESULT, "evidence_ref must be a JSON pointer starting with '/'"
            )


@dataclass(frozen=True)
class Predicate:
    """One acceptance criterion: an id, the criteria version it belongs to, the rules it enforces and a pure ``evaluate``.

    ``rule_ids`` (kept as a tuple) cite the rules of the run's pinned rule bundle that the predicate checks. ``evaluate`` must
    be a function of the observations alone; it is called through :meth:`check`, which refuses a result that is not a
    :class:`CriterionResult` for this predicate.
    """

    id: str
    criteria_version: str
    rule_ids: Sequence[str]
    evaluate: Callable[[Observations], CriterionResult]

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _ID.fullmatch(self.id):
            raise PredicateError(INVALID_PREDICATE, "id must match [a-z0-9][a-z0-9._-]{0,127}")
        if not isinstance(self.criteria_version, str) or not _VERSION.fullmatch(
            self.criteria_version
        ):
            raise PredicateError(
                INVALID_PREDICATE,
                f"{self.id}: criteria_version must match [a-z0-9][a-z0-9._-]{{0,63}}",
            )
        if (
            isinstance(self.rule_ids, str)
            or not isinstance(self.rule_ids, list | tuple)
            or not self.rule_ids
        ):
            raise PredicateError(
                INVALID_PREDICATE, f"{self.id}: rule_ids must be a non-empty list of rule ids"
            )
        if not all(isinstance(rule, str) and _ID.fullmatch(rule) for rule in self.rule_ids):
            raise PredicateError(
                INVALID_PREDICATE,
                f"{self.id}: every rule id must match [a-z0-9][a-z0-9._-]{{0,127}}",
            )
        if len(set(self.rule_ids)) != len(self.rule_ids):
            raise PredicateError(INVALID_PREDICATE, f"{self.id}: rule_ids repeats a rule id")
        if not callable(self.evaluate):
            raise PredicateError(INVALID_PREDICATE, f"{self.id}: evaluate must be callable")
        object.__setattr__(self, "rule_ids", tuple(self.rule_ids))

    def check(self, observations: Observations) -> CriterionResult:
        """Run ``evaluate``; raise :class:`PredicateError` if it raises or returns anything but this predicate's result."""
        try:
            result = self.evaluate(observations)
        except Exception as exc:
            raise PredicateError(
                PREDICATE_FAILED, f"{self.id} raised {type(exc).__name__}"
            ) from exc
        if not isinstance(result, CriterionResult) or result.predicate_id != self.id:
            raise PredicateError(
                INVALID_RESULT, f"{self.id} did not return its own CriterionResult"
            )
        return result


def evaluate_all(
    predicates: Sequence[Predicate], observations: Observations
) -> list[CriterionResult]:
    """Every predicate's result over ``observations``, in the order given (no predicate is skipped after a failure).

    Refused with :class:`PredicateError`: observations that are not an :class:`Observations`, no predicate at all (a decision
    over nothing would accept everything), anything that is not a :class:`Predicate`, a repeated predicate id, and predicates of
    more than one criteria version (one decision is made under one version).
    """
    if not isinstance(observations, Observations):
        raise PredicateError(
            INVALID_OBSERVATIONS, "observations must be an Observations (freeze them first)"
        )
    try:
        items = tuple(predicates)
    except TypeError as exc:
        raise PredicateError(
            INVALID_PREDICATE, "predicates must be a sequence of Predicate"
        ) from exc
    if not items:
        raise PredicateError(EMPTY_PREDICATE_SET, "there is no predicate to evaluate")
    if not all(isinstance(item, Predicate) for item in items):
        raise PredicateError(INVALID_PREDICATE, "every predicate must be a Predicate")
    if len({item.id for item in items}) != len(items):
        raise PredicateError(DUPLICATE_PREDICATE_ID, "a predicate id appears more than once")
    if len({item.criteria_version for item in items}) != 1:
        raise PredicateError(
            MIXED_CRITERIA_VERSIONS, "predicates belong to more than one criteria version"
        )
    return [item.check(observations) for item in items]


_REQUESTS_REF: Final = "/case_record/document_requests"


def _proposal_field(observations: Observations, name: str) -> object:
    proposal = observations.get("proposal")
    return proposal.get(name) if isinstance(proposal, Mapping) else None


def _requests(observations: Observations) -> tuple[Any, ...] | None:
    record = observations.get("case_record")
    requests = record.get("document_requests") if isinstance(record, Mapping) else None
    return requests if isinstance(requests, tuple) else None


def _candidates(observations: Observations) -> list[tuple[int, Mapping[str, Any]]] | str:
    """The listed requests that could be the proposal's effect, or the pointer of the value that makes judging impossible.

    A candidate is a mapping whose ``document_type`` is exactly the proposal's. When the observations carry a ``receipt``, it
    must name a ``downstream_request_id`` and only the request with that ``request_id`` is a candidate: without that, a second
    proposal of the same type that the downstream refused would be "accepted" on the strength of the first one's request.
    """
    document_type = _proposal_field(observations, "document_type")
    if not isinstance(document_type, str) or not document_type:
        return "/proposal/document_type"
    requests = _requests(observations)
    if requests is None:
        return _REQUESTS_REF
    request_id: object = None
    if "receipt" in observations:
        receipt = observations["receipt"]
        request_id = receipt.get("downstream_request_id") if isinstance(receipt, Mapping) else None
        if not isinstance(request_id, str) or not request_id:
            return "/receipt/downstream_request_id"
    return [
        (index, request)
        for index, request in enumerate(requests)
        if isinstance(request, Mapping)
        and request.get("document_type") == document_type
        and (request_id is None or request.get("request_id") == request_id)
    ]


def _document_request_exists(observations: Observations) -> CriterionResult:
    pid = "document_request_exists"
    candidates = _candidates(observations)
    if isinstance(candidates, str):
        return CriterionResult(pid, False, candidates)
    if candidates:
        return CriterionResult(pid, True, f"{_REQUESTS_REF}/{candidates[0][0]}")
    return CriterionResult(pid, False, _REQUESTS_REF)


def _attached_to_expected_version(observations: Observations) -> CriterionResult:
    pid = "attached_to_expected_version"
    expected = _proposal_field(observations, "expected_version")
    if type(expected) is not int or expected < 0:
        return CriterionResult(pid, False, "/proposal/expected_version")
    candidates = _candidates(observations)
    if isinstance(candidates, str):
        return CriterionResult(pid, False, candidates)
    for index, request in candidates:
        version = request.get("case_version")
        if type(version) is int and version == expected + CASE_VERSION_STEP:
            return CriterionResult(pid, True, f"{_REQUESTS_REF}/{index}/case_version")
    if not candidates:
        return CriterionResult(pid, False, _REQUESTS_REF)
    return CriterionResult(pid, False, f"{_REQUESTS_REF}/{candidates[0][0]}/case_version")


#: The Harborlight document-request acceptance criteria. Every predicate is critical: a failed one rejects the candidate.
HARBORLIGHT_V1: Final = (
    Predicate(
        id="document_request_exists",
        criteria_version=CRITERIA_VERSION,
        rule_ids=["harborlight.document-request-recorded"],
        evaluate=_document_request_exists,
    ),
    Predicate(
        id="attached_to_expected_version",
        criteria_version=CRITERIA_VERSION,
        rule_ids=["harborlight.request-attached-to-expected-version"],
        evaluate=_attached_to_expected_version,
    ),
)

__all__ = [
    "CODES",
    "CRITERIA_VERSION",
    "HARBORLIGHT_V1",
    "CriterionResult",
    "Observations",
    "Predicate",
    "PredicateError",
    "evaluate_all",
]
