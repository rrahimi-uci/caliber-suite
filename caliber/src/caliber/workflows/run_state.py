"""Workflow-run lifecycle vocabulary and transition helpers.

Queue-based execution introduces additional run statuses (queued, waiting_*
states, cancellation/expiry). This module centralizes legal transitions and
runtime-status mapping so routes and workers share one contract.
"""

from __future__ import annotations

from dataclasses import dataclass

RUN_STATUS_QUEUED = "queued"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_WAITING_APPROVAL = "waiting_approval"
RUN_STATUS_WAITING_EVENT = "waiting_event"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"
RUN_STATUS_EXPIRED = "expired"

TERMINAL_RUN_STATUSES = frozenset(
    {
        RUN_STATUS_COMPLETED,
        RUN_STATUS_FAILED,
        RUN_STATUS_CANCELLED,
        RUN_STATUS_EXPIRED,
    }
)

ALLOWED_RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    RUN_STATUS_QUEUED: frozenset({RUN_STATUS_RUNNING, RUN_STATUS_CANCELLED}),
    RUN_STATUS_RUNNING: frozenset(
        {
            RUN_STATUS_WAITING_APPROVAL,
            RUN_STATUS_WAITING_EVENT,
            RUN_STATUS_COMPLETED,
            RUN_STATUS_FAILED,
            RUN_STATUS_CANCELLED,
        }
    ),
    RUN_STATUS_WAITING_APPROVAL: frozenset(
        {
            RUN_STATUS_QUEUED,
            RUN_STATUS_RUNNING,
            RUN_STATUS_FAILED,
            RUN_STATUS_CANCELLED,
            RUN_STATUS_EXPIRED,
        }
    ),
    RUN_STATUS_WAITING_EVENT: frozenset(
        {
            RUN_STATUS_QUEUED,
            RUN_STATUS_RUNNING,
            RUN_STATUS_FAILED,
            RUN_STATUS_CANCELLED,
            RUN_STATUS_EXPIRED,
        }
    ),
    RUN_STATUS_COMPLETED: frozenset(),
    RUN_STATUS_FAILED: frozenset(),
    RUN_STATUS_CANCELLED: frozenset(),
    RUN_STATUS_EXPIRED: frozenset(),
}

RUNTIME_RESULT_TO_RUN_STATUS: dict[str, str] = {
    "completed": RUN_STATUS_COMPLETED,
    "blocked": RUN_STATUS_FAILED,
    "error": RUN_STATUS_FAILED,
}


@dataclass(frozen=True)
class InvalidRunStateTransition(Exception):  # noqa: N818 - public API name
    """Raised when a run lifecycle transition violates the allowed matrix."""

    from_status: str
    to_status: str

    def __str__(self) -> str:
        return f"invalid workflow-run transition: {self.from_status!r} -> {self.to_status!r}"


def normalize_runtime_result_status(runtime_status: str) -> str:
    """Map runtime interpreter statuses into persisted workflow-run statuses."""
    return RUNTIME_RESULT_TO_RUN_STATUS.get(runtime_status, RUN_STATUS_FAILED)


def can_transition_run_status(from_status: str, to_status: str) -> bool:
    """Return whether ``from_status -> to_status`` is legal."""
    if from_status == to_status:
        return True
    allowed = ALLOWED_RUN_TRANSITIONS.get(from_status)
    if allowed is None:
        return False
    return to_status in allowed


def assert_run_transition(from_status: str, to_status: str) -> None:
    """Validate a lifecycle transition or raise :class:`InvalidRunStateTransition`."""
    if not can_transition_run_status(from_status, to_status):
        raise InvalidRunStateTransition(from_status=from_status, to_status=to_status)


__all__ = [
    "ALLOWED_RUN_TRANSITIONS",
    "RUN_STATUS_CANCELLED",
    "RUN_STATUS_COMPLETED",
    "RUN_STATUS_EXPIRED",
    "RUN_STATUS_FAILED",
    "RUN_STATUS_QUEUED",
    "RUN_STATUS_RUNNING",
    "RUN_STATUS_WAITING_APPROVAL",
    "RUN_STATUS_WAITING_EVENT",
    "TERMINAL_RUN_STATUSES",
    "InvalidRunStateTransition",
    "assert_run_transition",
    "can_transition_run_status",
    "normalize_runtime_result_status",
]
