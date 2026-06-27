"""Unit tests for workflow-run lifecycle transition helpers."""

from __future__ import annotations

import pytest

from caliber.workflows.run_state import (
    RUN_STATUS_CANCELLED,
    RUN_STATUS_COMPLETED,
    RUN_STATUS_EXPIRED,
    RUN_STATUS_FAILED,
    RUN_STATUS_QUEUED,
    RUN_STATUS_RUNNING,
    RUN_STATUS_WAITING_APPROVAL,
    RUN_STATUS_WAITING_EVENT,
    InvalidRunStateTransition,
    assert_run_transition,
    can_transition_run_status,
    normalize_runtime_result_status,
)


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        (RUN_STATUS_QUEUED, RUN_STATUS_RUNNING),
        (RUN_STATUS_QUEUED, RUN_STATUS_CANCELLED),
        (RUN_STATUS_RUNNING, RUN_STATUS_WAITING_APPROVAL),
        (RUN_STATUS_RUNNING, RUN_STATUS_WAITING_EVENT),
        (RUN_STATUS_RUNNING, RUN_STATUS_COMPLETED),
        (RUN_STATUS_RUNNING, RUN_STATUS_FAILED),
        (RUN_STATUS_RUNNING, RUN_STATUS_CANCELLED),
        (RUN_STATUS_WAITING_APPROVAL, RUN_STATUS_QUEUED),
        (RUN_STATUS_WAITING_APPROVAL, RUN_STATUS_RUNNING),
        (RUN_STATUS_WAITING_APPROVAL, RUN_STATUS_FAILED),
        (RUN_STATUS_WAITING_APPROVAL, RUN_STATUS_CANCELLED),
        (RUN_STATUS_WAITING_APPROVAL, RUN_STATUS_EXPIRED),
        (RUN_STATUS_WAITING_EVENT, RUN_STATUS_QUEUED),
        (RUN_STATUS_WAITING_EVENT, RUN_STATUS_RUNNING),
        (RUN_STATUS_WAITING_EVENT, RUN_STATUS_CANCELLED),
        (RUN_STATUS_WAITING_EVENT, RUN_STATUS_EXPIRED),
    ],
)
def test_allowed_transitions(from_status: str, to_status: str) -> None:
    assert can_transition_run_status(from_status, to_status) is True
    assert_run_transition(from_status, to_status)


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        (RUN_STATUS_QUEUED, RUN_STATUS_COMPLETED),
        (RUN_STATUS_COMPLETED, RUN_STATUS_RUNNING),
        (RUN_STATUS_FAILED, RUN_STATUS_RUNNING),
        (RUN_STATUS_CANCELLED, RUN_STATUS_RUNNING),
        (RUN_STATUS_EXPIRED, RUN_STATUS_RUNNING),
        (RUN_STATUS_WAITING_APPROVAL, RUN_STATUS_WAITING_EVENT),
        (RUN_STATUS_WAITING_EVENT, RUN_STATUS_COMPLETED),
        (RUN_STATUS_WAITING_EVENT, RUN_STATUS_WAITING_APPROVAL),
    ],
)
def test_disallowed_transitions_raise(from_status: str, to_status: str) -> None:
    assert can_transition_run_status(from_status, to_status) is False
    with pytest.raises(InvalidRunStateTransition):
        assert_run_transition(from_status, to_status)


@pytest.mark.parametrize(
    ("runtime_status", "run_status"),
    [
        ("completed", RUN_STATUS_COMPLETED),
        ("blocked", RUN_STATUS_FAILED),
        ("error", RUN_STATUS_FAILED),
        ("unexpected", RUN_STATUS_FAILED),
    ],
)
def test_runtime_status_mapping(runtime_status: str, run_status: str) -> None:
    assert normalize_runtime_result_status(runtime_status) == run_status
