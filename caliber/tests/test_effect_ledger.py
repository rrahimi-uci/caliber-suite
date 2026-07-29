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
    FAILED,
    IN_PROGRESS,
    RESOLUTION_RETRY,
    RESOLUTION_SKIP,
    EffectResolutionError,
    IndeterminateEffectError,
    SqlEffectLedger,
    effect_key,
    list_effects,
    resolve_effect,
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


@pytest.fixture
def restart(session_factory):  # type: ignore[no-untyped-def]
    """Build a *fresh* ledger for the same run — what a lease expiry produces.

    One ``SqlEffectLedger`` instance is one attempt: it holds the per-attempt
    occurrence counter that distinguishes a loop's repeated identical effects from a
    replay. Reusing a single instance to model a restart would test the loop case
    while claiming to test the restart case, so restarts get their own object here.
    """

    def _restart() -> SqlEffectLedger:
        return SqlEffectLedger(session_factory, workflow_run_id="WFR-1")

    return _restart


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
    ledger: SqlEffectLedger, db_session: Session, restart
) -> None:
    payload = {"amount": 10}
    first = ledger.claim(node_id="charge", payload=payload)
    ledger.record_success(first.key, {"status_code": 200, "text": "ok"})

    second = restart().claim(node_id="charge", payload=payload)
    assert second.fresh is False
    assert second.replayed_result == {"status_code": 200, "text": "ok"}
    db_session.expire_all()
    assert db_session.get(CaliberEffectLedger, first.key).status == COMPLETED


def test_an_abandoned_claim_is_indeterminate_not_silently_repeated(
    ledger: SqlEffectLedger, restart
) -> None:
    """The process died between claiming and completing, so whether the effect
    reached the remote system is unknowable from here. Both silent choices —
    repeat it, skip it — can be the wrong one, so a human decides."""
    payload = {"amount": 10}
    ledger.claim(node_id="charge", payload=payload)  # never completed

    with pytest.raises(IndeterminateEffectError) as excinfo:
        restart().claim(node_id="charge", payload=payload)
    message = str(excinfo.value)
    assert "unknown" in message
    # ...and it names the endpoint that resolves it, plus both decisions. Telling an
    # operator to "resolve it manually" without saying where was the L4 gap.
    assert "/system/effects/" in message
    assert RESOLUTION_SKIP in message
    assert RESOLUTION_RETRY in message


def test_a_definitive_failure_releases_the_claim_for_retry(
    ledger: SqlEffectLedger, restart
) -> None:
    payload = {"amount": 10}
    first = ledger.claim(node_id="charge", payload=payload)
    ledger.record_failure(first.key, "ConnectionRefusedError: nothing listening")

    retried = restart().claim(node_id="charge", payload=payload)
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

    assert _perform_guarded_effect(
        plan, node_id="n", request={"url": "https://x.example/hook"}, perform=_perform
    ) == {"text": "done"}
    assert len(calls) == 1


def test_a_restarted_run_replays_rather_than_re_performing_the_effect(
    ledger: SqlEffectLedger, restart
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
    # A restart rebuilds the plan and the ledger, which is what makes the second
    # claim collide with the first rather than counting as a second occurrence.
    resumed = RuntimePlan(ir=None, resolver=None, effect_ledger=restart())  # type: ignore[arg-type]
    second = _perform_guarded_effect(resumed, node_id="charge", request=request, perform=_perform)

    assert len(calls) == 1  # performed once...
    assert first == second  # ...and the second attempt saw the same result


def test_the_guard_surfaces_an_indeterminate_effect_as_a_runtime_error(
    ledger: SqlEffectLedger, restart
) -> None:
    from caliber.workflows.runtime import ToolExecutionError

    request = {"url": "https://x.example/pay"}
    ledger.claim(node_id="charge", payload=request)  # abandoned by a dead attempt
    plan = RuntimePlan(ir=None, resolver=None, effect_ledger=restart())  # type: ignore[arg-type]

    with pytest.raises(ToolExecutionError, match="unknown"):
        _perform_guarded_effect(
            plan, node_id="charge", request=request, perform=lambda: {"text": "x"}
        )


def test_a_connection_failure_is_retryable_but_a_timeout_is_not(
    ledger: SqlEffectLedger, restart
) -> None:
    """A refused connection proves the request never went out. A timeout does not:
    the remote system may already have processed it, so the claim is kept and the
    next attempt reports it as indeterminate rather than repeating the effect."""
    plan = RuntimePlan(ir=None, resolver=None, effect_ledger=ledger)  # type: ignore[arg-type]

    def _refused() -> dict[str, str]:
        raise ConnectionRefusedError("nothing listening")

    with pytest.raises(ConnectionRefusedError):
        _perform_guarded_effect(plan, node_id="refused", request={"u": 1}, perform=_refused)
    assert restart().claim(node_id="refused", payload={"u": 1}).fresh is True

    def _timeout() -> dict[str, str]:
        raise TimeoutError("read timed out")

    with pytest.raises(TimeoutError):
        _perform_guarded_effect(plan, node_id="timeout", request={"u": 2}, perform=_timeout)
    with pytest.raises(IndeterminateEffectError):
        restart().claim(node_id="timeout", payload={"u": 2})


# ---------------------------------------------------------------------------
# L4 — occurrence identity: a repeated identical effect is not a replay
# ---------------------------------------------------------------------------


def test_the_key_separates_occurrences_of_an_identical_effect() -> None:
    base = {"workflow_run_id": "WFR-1", "node_id": "notify", "payload": {"body": "same"}}
    assert effect_key(**base, occurrence=0) != effect_key(**base, occurrence=1)
    # Default 0 keeps the single-effect key the ledger already produced.
    assert effect_key(**base) == effect_key(**base, occurrence=0)


def test_a_loop_repeating_the_same_request_performs_it_every_time(
    ledger: SqlEffectLedger,
) -> None:
    """L4: the key was ``(run, node, inputs)`` with no occurrence identity, so a loop
    posting an identical body twice produced one key and the second effect was
    suppressed as a replay. Dropping a real effect is as wrong as duplicating one.
    """
    calls: list[dict[str, object]] = []
    plan = RuntimePlan(ir=None, resolver=None, effect_ledger=ledger)  # type: ignore[arg-type]
    request = {"url": "https://x.example/notify", "method": "POST", "body": {"msg": "same"}}

    def _perform() -> dict[str, object]:
        calls.append(request)
        return {"status_code": 200, "n": len(calls)}

    first = _perform_guarded_effect(plan, node_id="notify", request=request, perform=_perform)
    second = _perform_guarded_effect(plan, node_id="notify", request=request, perform=_perform)

    assert len(calls) == 2, "both iterations are real effects, not a replay"
    assert first != second


def test_occurrences_line_up_after_a_restart(ledger: SqlEffectLedger, restart) -> None:
    """The counter has to be restart-*stable*, not merely present.

    Attempt 1 performs three identical effects and dies. Attempt 2 must replay the
    first two and perform only the third — if the counter were persisted, or salted
    with the attempt, attempt 2 would allocate fresh keys and re-fire all three.
    """
    request = {"url": "https://x.example/n", "body": {"same": True}}
    keys_attempt_1 = []
    for _ in range(3):
        claim = ledger.claim(node_id="notify", payload=request)
        keys_attempt_1.append(claim.key)
    # The first two completed before the crash; the third never recorded an outcome.
    ledger.record_success(keys_attempt_1[0], {"n": 1})
    ledger.record_success(keys_attempt_1[1], {"n": 2})
    ledger.record_failure(keys_attempt_1[2], "ConnectionRefusedError: nothing listening")

    resumed = restart()
    replayed_1 = resumed.claim(node_id="notify", payload=request)
    replayed_2 = resumed.claim(node_id="notify", payload=request)
    third = resumed.claim(node_id="notify", payload=request)

    assert replayed_1.fresh is False and replayed_1.replayed_result == {"n": 1}
    assert replayed_2.fresh is False and replayed_2.replayed_result == {"n": 2}
    assert third.fresh is True, "the effect that never completed must be retried"
    assert [replayed_1.key, replayed_2.key, third.key] == keys_attempt_1


def test_distinct_payloads_do_not_depend_on_iteration_order(
    ledger: SqlEffectLedger, restart
) -> None:
    """A concurrent ForEach has no guaranteed ordering, so keys must come from the
    payload rather than from position. Attempt 2 visits the items in reverse and must
    still replay both."""
    a = {"url": "https://x.example/a", "body": {"i": 1}}
    b = {"url": "https://x.example/b", "body": {"i": 2}}
    key_a = ledger.claim(node_id="fan", payload=a).key
    key_b = ledger.claim(node_id="fan", payload=b).key
    ledger.record_success(key_a, {"i": 1})
    ledger.record_success(key_b, {"i": 2})

    resumed = restart()
    assert resumed.claim(node_id="fan", payload=b).replayed_result == {"i": 2}
    assert resumed.claim(node_id="fan", payload=a).replayed_result == {"i": 1}


def test_concurrent_claims_of_an_identical_effect_get_distinct_keys(
    ledger: SqlEffectLedger,
) -> None:
    """Parallel branches claim from several threads. Two threads reading the same
    counter would allocate one key and turn a real effect into a bogus replay, so the
    increment is locked."""
    import threading

    payload = {"url": "https://x.example/p", "body": {"same": True}}
    keys: list[str] = []
    lock = threading.Lock()

    def _claim() -> None:
        claim = ledger.claim(node_id="par", payload=payload)
        with lock:
            keys.append(claim.key)

    threads = [threading.Thread(target=_claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(set(keys)) == 8, "every concurrent claim is its own effect"


# ---------------------------------------------------------------------------
# L4 — the resolution path the error message promises
# ---------------------------------------------------------------------------


def test_an_indeterminate_claim_is_listable(ledger: SqlEffectLedger, db_session: Session) -> None:
    """ "Resolve it manually" needs something to resolve *from*. Before this the row
    was only reachable with a database client, which is not a procedure."""
    ledger.claim(node_id="charge", payload={"amount": 10})
    db_session.expire_all()

    rows = list_effects(db_session, status=IN_PROGRESS)

    assert len(rows) == 1
    assert rows[0]["node_id"] == "charge"
    assert rows[0]["workflow_run_id"] == "WFR-1"
    # The stored result can hold a response body; an operations list must not leak it.
    assert "result" not in rows[0]
    assert rows[0]["has_result"] is False


def test_resolving_skip_stops_the_effect_from_being_repeated(
    ledger: SqlEffectLedger, db_session: Session, restart
) -> None:
    """``skip`` asserts the effect did reach the remote system, so the next attempt
    must replay rather than re-fire."""
    claim = ledger.claim(node_id="charge", payload={"amount": 10})
    db_session.expire_all()

    resolve_effect(
        db_session,
        effect_key_value=claim.key,
        resolution=RESOLUTION_SKIP,
        actor="@ops",
        reason="confirmed in the provider dashboard",
    )
    db_session.commit()

    resumed = restart().claim(node_id="charge", payload={"amount": 10})
    assert resumed.fresh is False, "a skipped effect must not be performed again"
    row = db_session.get(CaliberEffectLedger, claim.key)
    assert row.status == COMPLETED
    # Who decided and why is part of the record: this asserts something about the
    # outside world that CALIBER could not verify.
    assert "@ops" in row.error
    assert "provider dashboard" in row.error


def test_resolving_retry_releases_the_claim(
    ledger: SqlEffectLedger, db_session: Session, restart
) -> None:
    claim = ledger.claim(node_id="charge", payload={"amount": 10})
    db_session.expire_all()

    resolve_effect(
        db_session, effect_key_value=claim.key, resolution=RESOLUTION_RETRY, actor="@ops"
    )
    db_session.commit()

    assert db_session.get(CaliberEffectLedger, claim.key).status == FAILED
    assert restart().claim(node_id="charge", payload={"amount": 10}).fresh is True


def test_only_an_indeterminate_claim_is_resolvable(
    ledger: SqlEffectLedger, db_session: Session
) -> None:
    """Resolving a completed row would let an operator erase a genuine effect record,
    turning a recovery tool into a way to corrupt the ledger's own evidence."""
    claim = ledger.claim(node_id="charge", payload={"amount": 10})
    ledger.record_success(claim.key, {"status_code": 200})
    db_session.expire_all()

    with pytest.raises(EffectResolutionError, match="only an indeterminate claim"):
        resolve_effect(
            db_session, effect_key_value=claim.key, resolution=RESOLUTION_SKIP, actor="@ops"
        )


def test_an_unknown_resolution_or_key_is_rejected(db_session: Session) -> None:
    with pytest.raises(EffectResolutionError, match="resolution must be one of"):
        resolve_effect(db_session, effect_key_value="eff-nope", resolution="whatever", actor="@ops")
    with pytest.raises(EffectResolutionError, match="not found"):
        resolve_effect(
            db_session, effect_key_value="eff-nope", resolution=RESOLUTION_SKIP, actor="@ops"
        )
