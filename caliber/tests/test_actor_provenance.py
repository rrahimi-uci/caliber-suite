"""Unit tests for the reusable actor-provenance/distinct-actor primitives
(`P1-F`, ``caliber.actor_provenance``).

Nothing in this codebase wires these onto a real route yet -- Phase 5's job,
once the release/Change-Request records these checks apply to actually
exist (see the module's own docstring) -- so these are pure unit tests of
the primitive itself, the same standalone-testable pattern
``workflows/approval_policy.py``'s own distinct-actor rule already follows.
"""

from __future__ import annotations

import pytest

from caliber.actor_provenance import (
    ActorProvenance,
    DistinctActorError,
    require_distinct_actors,
)
from caliber.auth import CaliberIdentity


def test_same_user_id_is_the_same_actor_regardless_of_credential() -> None:
    """The whole point: a platform identity cannot defeat a distinct-actor
    check by switching credentials between the two recorded actions."""
    via_session = ActorProvenance(user_id="@alice", credential_kind="session", credential_id="S-1")
    via_pat = ActorProvenance(user_id="@alice", credential_kind="pat", credential_id="PAT-1")
    assert via_session.is_same_actor(via_pat)
    assert via_pat.is_same_actor(via_session)


def test_different_user_id_is_a_different_actor() -> None:
    alice = ActorProvenance(user_id="@alice")
    bob = ActorProvenance(user_id="@bob")
    assert not alice.is_same_actor(bob)


def test_require_distinct_actors_raises_when_the_same_actor_appears_twice() -> None:
    alice = ActorProvenance(user_id="@alice", credential_kind="session")
    also_alice = ActorProvenance(user_id="@alice", credential_kind="pat")
    with pytest.raises(DistinctActorError, match="technical review") as excinfo:
        require_distinct_actors(alice, also_alice, context="technical review")
    assert "@alice" in str(excinfo.value)


def test_require_distinct_actors_passes_for_two_different_actors() -> None:
    alice = ActorProvenance(user_id="@alice")
    bob = ActorProvenance(user_id="@bob")
    # No exception -- the whole assertion is that this does not raise.
    require_distinct_actors(alice, bob, context="QA sign-off")


def test_from_identity_copies_the_credential_context() -> None:
    identity = CaliberIdentity(
        user_id="@alice",
        scopes=frozenset(),
        credential_kind="pat",
        credential_id="PAT-1",
        credential_project_id="PRJ-1",
    )
    provenance = ActorProvenance.from_identity(identity)
    assert provenance.user_id == "@alice"
    assert provenance.credential_kind == "pat"
    assert provenance.credential_id == "PAT-1"


def test_from_identity_handles_a_session_identity_with_no_credential_context() -> None:
    """`CaliberIdentity`'s credential fields default to `None` (e.g. built
    directly in a test, or before `resolve_identity` populates them)."""
    identity = CaliberIdentity(user_id="@alice", scopes=frozenset())
    provenance = ActorProvenance.from_identity(identity)
    assert provenance.credential_kind is None
    assert provenance.credential_id is None
