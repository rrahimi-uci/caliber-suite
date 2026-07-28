"""Regression tests for at-most-once external effects across a run restart.

The review's finding: "Crash recovery is at-least-once for side effects. An expired
running lease is reset to queued, and a run without a wait/approval checkpoint
restarts from the beginning. There is no platform effect ledger or per-node
idempotency key, so a mutation completed just before process failure can execute
again."

The four properties that make the ledger a real fix:

1. the key is **attempt-invariant** — a restarted run's replay collides with its
   earlier attempt, which is the whole point;
2. a completed effect is **replayed, not repeated**;
3. an attempt that claimed an effect and then died leaves an *indeterminate* row,
   which is surfaced rather than silently repeated or silently skipped; and
4. a definitive failure (the request never went out) releases the claim so a retry
   is allowed.
"""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from caliber.db.models import CaliberEffectLedger
from caliber.workflows.effect_ledger import (
    COMPLETED,
    IN_PROGRESS,
    IndeterminateEffectError,
    SqlEffectLedger,
    effect_key,
)
from caliber.workflows.runtime import RuntimePlan, _perform_guarded_effect

# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


def test_the_key_is_stable_across_attempts_of_the_same_run() -> None:
    """Deliberately *not* salted with the attempt number: a restarted run keeps its
    id and replays the same inputs, and that collision is what stops the duplicate."""
    payload = {"url": "https://x.example/pay", "method": "POST", "body": {"amount": 10}}
    first = effect_key(workflow_run_id="WFR-1", node_id="charge", payload=payload)
    second = effect_key(workflow_run_id="WFR-1", node_id="charge", payload=payload)
    assert first == second


def test_the_key_is_insensitive_to_dict_ordering() -> None:
    """An equivalent request must not look like a different effect just because the
    runtime happened to build the dict in another order."""
    left = effect_key(workflow_run_id="WFR-1", node_id="n", payload={"a": 1, "b": {"c": 2, "d": 3}})
    right = effect_key(
        workflow_run_id="WFR-1", node_id="n", payload={"b": {"d": 3, "c": 2}, "a": 1}
    )
    assert left == right


def test_the_key_separates_runs_nodes_and_payloads() -> None:
    base = {"workflow_run_id": "WFR-1", "node_id": "n", "payload": {"x": 1}}
    assert effect_key(**base) != effect_key(**{**base, "workflow_run_id": "WFR-2"})
    assert effect_key(**base) != effect_key(**{**base, "node_id": "m"})
    assert effect_key(**base) != effect_key(**{**base, "payload": {"x": 2}})


# ---------------------------------------------------------------------------
# Claim / replay / indeterminate
# ---------------------------------------------------------------------------


@pytest.fixture
def ledger(db_session: Session, session_factory) -> SqlEffectLedger:  # type: ignore[no-untyped-def]
    del db_session
    return SqlEffectLedger(session_factory, workflow_run_id="WFR-1")


def test_a_first_claim_is_fresh_and_recorded_in_progress(
    ledger: SqlEffectLedger, db_session: Session
) -> None:
    claim = ledger.claim(node_id="charge", payload={"amount": 10})
    assert claim.fresh is True
    row = db_session.get(CaliberEffectLedger, claim.key)
    assert row is not None
    assert row.status == IN_PROGRESS
    assert row.node_id == "charge"


def test_a_completed_effect_replays_instead_of_repeating(
    ledger: SqlEffectLedger, db_session: Session
) -> None:
    payload = {"amount": 10}
    first = ledger.claim(node_id="charge", payload=payload)
    ledger.record_success(first.key, {"status_code": 200, "text": "ok"})

    second = ledger.claim(node_id="charge", payload=payload)
    assert second.fresh is False
    assert second.replayed_result == {"status_code": 200, "text": "ok"}
    db_session.expire_all()
    assert db_session.get(CaliberEffectLedger, first.key).status == COMPLETED


def test_an_abandoned_claim_is_indeterminate_not_silently_repeated(
    ledger: SqlEffectLedger,
) -> None:
    """The process died between claiming and completing, so whether the effect
    reached the remote system is unknowable from here. Both silent choices —
    repeat it, skip it — can be the wrong one, so a human decides."""
    payload = {"amount": 10}
    ledger.claim(node_id="charge", payload=payload)  # never completed

    with pytest.raises(IndeterminateEffectError) as excinfo:
        ledger.claim(node_id="charge", payload=payload)
    message = str(excinfo.value)
    assert "unknown" in message
    # ...and it tells the operator exactly how to resolve it.
    assert "completed" in message
    assert "failed" in message


def test_a_definitive_failure_releases_the_claim_for_retry(
    ledger: SqlEffectLedger,
) -> None:
    payload = {"amount": 10}
    first = ledger.claim(node_id="charge", payload=payload)
    ledger.record_failure(first.key, "ConnectionRefusedError: nothing listening")

    retried = ledger.claim(node_id="charge", payload=payload)
    assert retried.fresh is True
    assert retried.key == first.key


def test_different_nodes_in_one_run_do_not_share_a_claim(ledger: SqlEffectLedger) -> None:
    assert ledger.claim(node_id="a", payload={}).fresh is True
    assert ledger.claim(node_id="b", payload={}).fresh is True


# ---------------------------------------------------------------------------
# The runtime guard
# ---------------------------------------------------------------------------


def test_without_a_ledger_the_effect_is_performed_plainly() -> None:
    """Preview, evaluation, and deploy-gate paths refuse unisolated effect nodes
    outright, so there is nothing to deduplicate and no ledger is attached."""
    calls: list[int] = []
    plan = RuntimePlan(ir=None, resolver=None)  # type: ignore[arg-type]

    def _perform() -> dict[str, str]:
        calls.append(1)
        return {"text": "done"}

    assert _perform_guarded_effect(plan, node_id="n", request={"url": "x"}, perform=_perform) == {
        "text": "done"
    }
    assert len(calls) == 1


def test_a_restarted_run_replays_rather_than_re_performing_the_effect(
    ledger: SqlEffectLedger,
) -> None:
    """The concrete regression: the same node, same run, same inputs — as after a
    lease expiry — must not fire the effect twice."""
    calls: list[int] = []
    plan = RuntimePlan(ir=None, resolver=None, effect_ledger=ledger)  # type: ignore[arg-type]
    request = {"url": "https://x.example/pay", "method": "POST", "body": {"amount": 10}}

    def _perform() -> dict[str, object]:
        calls.append(1)
        return {"status_code": 200, "text": "charged"}

    first = _perform_guarded_effect(plan, node_id="charge", request=request, perform=_perform)
    second = _perform_guarded_effect(plan, node_id="charge", request=request, perform=_perform)

    assert len(calls) == 1  # performed once...
    assert first == second  # ...and the second attempt saw the same result


def test_the_guard_surfaces_an_indeterminate_effect_as_a_runtime_error(
    ledger: SqlEffectLedger,
) -> None:
    from caliber.workflows.runtime import ToolExecutionError

    plan = RuntimePlan(ir=None, resolver=None, effect_ledger=ledger)  # type: ignore[arg-type]
    request = {"url": "https://x.example/pay"}
    ledger.claim(node_id="charge", payload=request)  # abandoned by a dead attempt

    with pytest.raises(ToolExecutionError, match="unknown"):
        _perform_guarded_effect(
            plan, node_id="charge", request=request, perform=lambda: {"text": "x"}
        )


def test_a_connection_failure_is_retryable_but_a_timeout_is_not(
    ledger: SqlEffectLedger,
) -> None:
    """A refused connection proves the request never went out. A timeout does not:
    the remote system may already have processed it, so the claim is kept and the
    next attempt reports it as indeterminate rather than repeating the effect."""
    plan = RuntimePlan(ir=None, resolver=None, effect_ledger=ledger)  # type: ignore[arg-type]

    def _refused() -> dict[str, str]:
        raise ConnectionRefusedError("nothing listening")

    with pytest.raises(ConnectionRefusedError):
        _perform_guarded_effect(plan, node_id="refused", request={"u": 1}, perform=_refused)
    assert ledger.claim(node_id="refused", payload={"u": 1}).fresh is True

    def _timeout() -> dict[str, str]:
        raise TimeoutError("read timed out")

    with pytest.raises(TimeoutError):
        _perform_guarded_effect(plan, node_id="timeout", request={"u": 2}, perform=_timeout)
    with pytest.raises(IndeterminateEffectError):
        ledger.claim(node_id="timeout", payload={"u": 2})
