"""Test helpers for the effect ledger: the execution-owner lease an owner captures before it dispatches or settles (B05.5a)."""

from __future__ import annotations

from sqlalchemy.orm import Session

from caliber.ownership import Lease, acquire, current_lease, database_now, release

TTL_SECONDS = 600.0


def leased(session: Session, owner_id: str) -> Lease:
    """The lease ``owner_id`` holds now: the one already held, or a fresh acquisition (a new token) after taking it over.

    Another owner's unexpired lease is released first, as if it had lapsed, so asking for a different owner models a
    takeover and leaves every lease captured earlier stale.
    """
    stored = current_lease(session)
    if (
        stored is not None
        and stored.owner_id == owner_id
        and stored.expires_at > database_now(session)
    ):
        return stored
    if stored is not None and stored.expires_at > database_now(session):
        release(session, stored)
    lease = acquire(session, owner_id, ttl_seconds=TTL_SECONDS)
    assert lease is not None
    return lease
