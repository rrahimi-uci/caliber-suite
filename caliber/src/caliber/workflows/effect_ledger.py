"""At-most-once external effects across a run restart.

The review's finding, in its own words: "Crash recovery is at-least-once for side
effects. An expired running lease is reset to queued, and a run without a
wait/approval checkpoint restarts from the beginning. There is no platform effect
ledger or per-node idempotency key, so a mutation completed just before process
failure can execute again."

That is a correctness defect, not a scale one: a payment webhook or an outbound
API mutation performed twice is not recoverable by retrying differently.

## The contract

Before an effectful node performs its effect, the runtime **claims** a ledger row
keyed by an attempt-invariant idempotency key. Three outcomes:

``fresh``
    No row existed. The claim is recorded as ``in_progress`` and the node performs
    the effect, then records the result.
``replay``
    A ``completed`` row exists — this effect already happened on an earlier
    attempt. The recorded result is returned and the effect is **not** repeated.
``indeterminate``
    An ``in_progress`` row exists from an attempt that never finished. The process
    died between claiming and completing, so whether the effect reached the remote
    system is genuinely unknown. This is surfaced, not guessed: the run fails with
    an explicit message rather than silently duplicating (retry) or silently
    skipping (drop). A human decides, which is the only correct answer when the
    truth is unknowable from this side.

## The key, and why it carries an occurrence number

The key is derived from ``(workflow_run_id, node_id, canonical inputs, occurrence)``.
It is deliberately **not** salted with the attempt number: a restarted run keeps its
run id and replays the same inputs, which is exactly the case that must collide.

``occurrence`` closes the opposite error. Without it the key was
``(run, node, inputs)``, so a loop that legitimately performs the *same* request
twice — notifying the same endpoint once per retry-worthy item, re-posting an
identical status update — produced one key, and the second effect was suppressed as
if it were a replay. Dropping a real effect is as wrong as duplicating one.

The counter is scoped to ``(node, canonical inputs)`` and to the current attempt,
which is what makes it both correct and restart-stable:

* it counts only claims whose inputs are **identical**, so two loop iterations with
  different payloads keep distinct keys through the payload itself and each is
  occurrence 0 — no dependence on iteration order;
* among genuinely identical payloads the effects are interchangeable, so which one is
  occurrence 0 and which is 1 does not matter, only *how many* there are; and
* it resets per attempt, and a restart replays the same iterations in the same
  multiset, so attempt 2's occurrence *n* of a payload matches attempt 1's occurrence
  *n* of that payload and replays instead of repeating.

That last property is why the counter is not persisted: persisting it across attempts
would make a restart allocate fresh keys and re-fire every effect, which is the
original defect.

## What this is not

Not distributed two-phase commit, and not a guarantee about the remote system: if a
receiver is not itself idempotent and the effect landed twice *before* CALIBER ever
recorded anything, no ledger can undo that. What this removes is the failure mode
the review identified — CALIBER re-performing an effect it had already completed.

The occurrence counter assumes a restart replays the same *multiset* of effects for a
node. A workflow whose loop body depends on wall-clock time or a mutating external
read can produce a different multiset on replay; those claims then look fresh and the
effect repeats. That is a property of the workflow, not of the ledger, and is stated
rather than papered over.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

#: Ledger row states.
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
FAILED = "failed"


class IndeterminateEffectError(RuntimeError):
    """An earlier attempt claimed this effect but never recorded an outcome.

    Raised rather than resolved: whether the effect reached the remote system is
    unknowable from here, and both silent choices (repeat it / skip it) can be
    wrong in a way the operator would want to know about.
    """


@dataclass(frozen=True)
class EffectClaim:
    """The result of claiming an effect before performing it."""

    key: str
    #: ``True`` when this attempt owns the effect and must perform it.
    fresh: bool
    #: The recorded result of a previous successful attempt, when replaying.
    replayed_result: Any | None = None


def effect_fingerprint(*, workflow_run_id: str, node_id: str, payload: Any) -> str:
    """Identity of an effect *ignoring* how many times it occurs.

    Split out from :func:`effect_key` so the occurrence counter can be keyed on
    exactly the thing it counts, with one canonicalization used for both.
    """
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return f"{workflow_run_id}\x1f{node_id}\x1f{canonical}"


def effect_key(*, workflow_run_id: str, node_id: str, payload: Any, occurrence: int = 0) -> str:
    """Attempt-invariant idempotency key for one occurrence of one node's effect.

    ``payload`` is canonicalized (sorted keys, stable separators) so an equivalent
    request produces an equal key regardless of dict ordering.

    ``occurrence`` distinguishes repeated *identical* effects from the same node in
    the same run — a loop posting the same body twice is two effects, not a replay.
    It defaults to 0 so the single-effect case, and any caller that predates the
    parameter, keeps the key it already had.
    """
    fingerprint = effect_fingerprint(
        workflow_run_id=workflow_run_id, node_id=node_id, payload=payload
    )
    digest = hashlib.sha256(f"{fingerprint}\x1f{int(occurrence)}".encode()).hexdigest()
    return f"eff-{digest[:56]}"


class SqlEffectLedger:
    """Durable effect ledger backed by ``caliber_effect_ledger``.

    One instance per run. Methods take a session factory rather than a live session
    so a claim commits **independently of the run's own transaction**: a claim that
    rolled back with the run would be no claim at all, which is precisely the
    window this class exists to close.
    """

    def __init__(self, session_factory: Any, *, workflow_run_id: str) -> None:
        self._session_factory = session_factory
        self._run_id = workflow_run_id
        # Per-attempt count of claims already made for each identical effect
        # fingerprint. Not persisted — see the module docstring: carrying it across
        # attempts would allocate fresh keys on restart and re-fire every effect.
        self._occurrences: dict[str, int] = {}
        # Parallel branches and a concurrent ForEach claim from several threads, and
        # two threads reading the same count would allocate the same key — turning
        # one of two real effects into a bogus replay.
        self._lock = threading.Lock()

    @property
    def workflow_run_id(self) -> str:
        return self._run_id

    def _next_occurrence(self, fingerprint: str) -> int:
        with self._lock:
            occurrence = self._occurrences.get(fingerprint, 0)
            self._occurrences[fingerprint] = occurrence + 1
            return occurrence

    def claim(self, *, node_id: str, payload: Any) -> EffectClaim:
        """Claim this occurrence of ``node_id``'s effect, or report it already happened."""
        from caliber.db.models import CaliberEffectLedger  # noqa: PLC0415

        fingerprint = effect_fingerprint(
            workflow_run_id=self._run_id, node_id=node_id, payload=payload
        )
        occurrence = self._next_occurrence(fingerprint)
        key = effect_key(
            workflow_run_id=self._run_id,
            node_id=node_id,
            payload=payload,
            occurrence=occurrence,
        )
        now = datetime.now(timezone.utc)
        with self._session_factory() as session:
            existing = session.execute(
                select(CaliberEffectLedger).where(CaliberEffectLedger.effect_key == key)
            ).scalar_one_or_none()
            if existing is not None:
                return self._claim_from_existing(session, existing, key, now)

            session.add(
                CaliberEffectLedger(
                    effect_key=key,
                    workflow_run_id=self._run_id,
                    node_id=node_id,
                    status=IN_PROGRESS,
                    claimed_at=now,
                )
            )
            try:
                session.commit()
            except IntegrityError:
                # Another worker claimed it between the SELECT and the INSERT. The
                # unique key is the arbiter, so re-read and follow the same rules.
                session.rollback()
                with self._session_factory() as retry:
                    winner = retry.execute(
                        select(CaliberEffectLedger).where(CaliberEffectLedger.effect_key == key)
                    ).scalar_one_or_none()
                    if winner is None:  # pragma: no cover - the row must exist
                        raise
                    return self._claim_from_existing(retry, winner, key, now)
        return EffectClaim(key=key, fresh=True)

    def _claim_from_existing(
        self, session: Any, existing: Any, key: str, now: datetime
    ) -> EffectClaim:
        if existing.status == COMPLETED:
            return EffectClaim(key=key, fresh=False, replayed_result=existing.result)
        if existing.status == IN_PROGRESS:
            raise IndeterminateEffectError(
                f"effect {key} for node {existing.node_id!r} was claimed at "
                f"{existing.claimed_at} but never recorded an outcome; whether it reached "
                "the remote system is unknown. Resolve it with "
                f"POST /caliber/system/effects/{key}/resolve — "
                f"{RESOLUTION_SKIP!r} if the effect did happen (do not repeat it), "
                f"{RESOLUTION_RETRY!r} if it did not."
            )
        # A recorded failure is safe to retry: the effect did not take.
        existing.status = IN_PROGRESS
        existing.claimed_at = now
        existing.completed_at = None
        existing.error = None
        session.commit()
        return EffectClaim(key=key, fresh=True)

    def record_success(self, key: str, result: Any) -> None:
        """Mark the effect completed and store the result for future replays."""
        self._finish(key, status=COMPLETED, result=result, error=None)

    def record_failure(self, key: str, error: str) -> None:
        """Mark the effect failed so a retry is allowed.

        Only call this when the effect definitively did **not** happen — a
        connection refused, a request never sent. For an ambiguous failure (a
        timeout after the request went out) leave the row ``in_progress`` so the
        next attempt reports it as indeterminate instead of silently repeating it.
        """
        self._finish(key, status=FAILED, result=None, error=error[:2000])

    def _finish(self, key: str, *, status: str, result: Any, error: str | None) -> None:
        from caliber.db.models import CaliberEffectLedger  # noqa: PLC0415

        with self._session_factory() as session:
            row = session.execute(
                select(CaliberEffectLedger).where(CaliberEffectLedger.effect_key == key)
            ).scalar_one_or_none()
            if row is None:  # pragma: no cover - claim always precedes finish
                return
            row.status = status
            row.result = result
            row.error = error
            row.completed_at = datetime.now(timezone.utc)
            session.commit()


#: How an operator may resolve an indeterminate claim, and what each means.
#: ``skip`` asserts the effect *did* reach the remote system, so the run must not
#: repeat it. ``retry`` asserts it did not, releasing the row for another attempt.
#: There is deliberately no "figure it out" option: the whole point of surfacing an
#: indeterminate claim is that only a human can know which of these is true.
RESOLUTION_SKIP = "skip"
RESOLUTION_RETRY = "retry"
RESOLUTIONS: tuple[str, ...] = (RESOLUTION_SKIP, RESOLUTION_RETRY)


class EffectResolutionError(ValueError):
    """An indeterminate claim could not be resolved as requested."""


def list_effects(
    session: Any,
    *,
    status: str | None = None,
    workflow_run_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Ledger rows for operator inspection, newest claim first.

    Exists because ``IndeterminateEffectError`` used to instruct the operator to
    "resolve it manually" with no surface on which to do so — the row was only
    reachable with direct database access, which is not an operational procedure.
    """
    from caliber.db.models import CaliberEffectLedger  # noqa: PLC0415

    query = select(CaliberEffectLedger).order_by(CaliberEffectLedger.claimed_at.desc())
    if status:
        query = query.where(CaliberEffectLedger.status == status)
    if workflow_run_id:
        query = query.where(CaliberEffectLedger.workflow_run_id == workflow_run_id)
    rows = session.execute(query.limit(max(1, min(int(limit), 1000)))).scalars().all()
    return [
        {
            "effect_key": row.effect_key,
            "workflow_run_id": row.workflow_run_id,
            "node_id": row.node_id,
            "status": row.status,
            "error": row.error,
            "claimed_at": row.claimed_at.isoformat() if row.claimed_at else None,
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            # The recorded result can contain a response body, so it is summarized
            # rather than returned: this is an operations list, not a data export.
            "has_result": row.result is not None,
        }
        for row in rows
    ]


def resolve_effect(
    session: Any,
    *,
    effect_key_value: str,
    resolution: str,
    actor: str,
    reason: str = "",
) -> dict[str, Any]:
    """Resolve one indeterminate claim, recording who decided and why.

    Only ``in_progress`` rows are resolvable. A ``completed`` row would let an
    operator erase a genuine effect record and a ``failed`` row is already retryable,
    so allowing either would turn a recovery tool into a way to corrupt the evidence
    the ledger exists to hold.
    """
    from caliber.db.models import CaliberEffectLedger  # noqa: PLC0415

    if resolution not in RESOLUTIONS:
        raise EffectResolutionError(
            f"resolution must be one of {list(RESOLUTIONS)}, not {resolution!r}"
        )
    row = session.execute(
        select(CaliberEffectLedger).where(CaliberEffectLedger.effect_key == effect_key_value)
    ).scalar_one_or_none()
    if row is None:
        raise EffectResolutionError(f"effect {effect_key_value!r} not found")
    if row.status != IN_PROGRESS:
        raise EffectResolutionError(
            f"effect {effect_key_value!r} is {row.status!r}, not {IN_PROGRESS!r}; "
            "only an indeterminate claim can be resolved"
        )
    note = f"resolved {resolution!r} by {actor}" + (f": {reason}" if reason else "")
    if resolution == RESOLUTION_SKIP:
        # Completed with an empty result: the next attempt replays this and moves on.
        row.status = COMPLETED
        row.result = {"resolved": resolution, "by": actor, "reason": reason}
    else:
        row.status = FAILED
        row.result = None
    row.error = note[:2000]
    row.completed_at = datetime.now(timezone.utc)
    session.flush()
    return {
        "effect_key": row.effect_key,
        "workflow_run_id": row.workflow_run_id,
        "node_id": row.node_id,
        "status": row.status,
        "error": row.error,
    }


__all__ = [
    "COMPLETED",
    "FAILED",
    "IN_PROGRESS",
    "RESOLUTIONS",
    "RESOLUTION_RETRY",
    "RESOLUTION_SKIP",
    "EffectClaim",
    "EffectResolutionError",
    "IndeterminateEffectError",
    "SqlEffectLedger",
    "effect_fingerprint",
    "effect_key",
    "list_effects",
    "resolve_effect",
]
