"""Acceptance: whether a run's downstream effect is what it was meant to be (B09, #527).

A run is accepted on an authoritative domain predicate over what the system of record says, never on an HTTP status or on
what a model wrote. The first step, :mod:`caliber.acceptance.predicates`, is pure: it judges observations a caller already
read and does no I/O of its own. Persistence of a decision (B09.2a, #784) and the runtime's use of it (B09.2b, #791) build
on these names.
"""

from caliber.acceptance.predicates import (
    CRITERIA_VERSION,
    HARBORLIGHT_V1,
    CriterionResult,
    Observations,
    Predicate,
    PredicateError,
    evaluate_all,
)

__all__ = [
    "CRITERIA_VERSION",
    "HARBORLIGHT_V1",
    "CriterionResult",
    "Observations",
    "Predicate",
    "PredicateError",
    "evaluate_all",
]
