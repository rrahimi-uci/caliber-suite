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

The key is derived from ``(workflow_run_id, node_id, canonical inputs)``. It is
deliberately **not** salted with the attempt number: a restarted run keeps its run
id and replays the same inputs, which is exactly the case that must collide.

## What this is not

Not distributed two-phase commit, and not a guarantee about the remote system: if a
receiver is not itself idempotent and the effect landed twice *before* CALIBER ever
recorded anything, no ledger can undo that. What this removes is the failure mode
the review identified — CALIBER re-performing an effect it had already completed.
"""

from __future__ import annotations

import hashlib
import json
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


def effect_key(*, workflow_run_id: str, node_id: str, payload: Any) -> str:
    """Attempt-invariant idempotency key for one node's effect in one run.

    ``payload`` is canonicalized (sorted keys, stable separators) so an equivalent
    request produces an equal key regardless of dict ordering.
    """
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    digest = hashlib.sha256(f"{workflow_run_id}\x1f{node_id}\x1f{canonical}".encode()).hexdigest()
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

    @property
    def workflow_run_id(self) -> str:
        return self._run_id

    def claim(self, *, node_id: str, payload: Any) -> EffectClaim:
        """Claim ``node_id``'s effect, or report that it already happened."""
        from caliber.db.models import CaliberEffectLedger  # noqa: PLC0415

        key = effect_key(workflow_run_id=self._run_id, node_id=node_id, payload=payload)
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
                "the remote system is unknown. Resolve it manually — mark the ledger row "
                "completed to skip the effect, or failed to allow a retry."
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


__all__ = [
    "COMPLETED",
    "FAILED",
    "IN_PROGRESS",
    "EffectClaim",
    "IndeterminateEffectError",
    "SqlEffectLedger",
    "effect_key",
]
