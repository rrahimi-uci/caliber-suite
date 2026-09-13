"""Reusable actor-provenance and distinct-actor policy primitives.

`P1-F` (docs/workspace-plan.md Phase 1 item 14): "Add reusable
actor-provenance and distinct-actor policy primitives; release enforcement
lands with the release records in Phase 5." Section 5.3's target release
flow needs a "originator versus decision-maker" axis in several places --
technical review, QA sign-off, and Admin final approval must each be a
*different* platform identity from whoever authored the change being
decided on -- and today only one such check exists anywhere in this
codebase, hand-written and workflow-run-specific
(:func:`caliber.workflows.approval_policy.record_approval`'s bare
``actor == initiated_by`` comparison). This module extracts the general
shape of that check into a primitive Phase 5's Change-Request/QA/release
records can share, without wiring it onto anything yet -- none of those
records exist in this codebase today, so there is nothing to enforce this
against until Phase 5 builds them (`P5-B`).

This module deliberately does **not** touch
:mod:`caliber.workflows.approval_policy`: that module's own self-approval
rule is already shipped, tested, and configurable
(``CALIBER_APPROVAL_ALLOW_SELF_APPROVAL``) for a real, live enforcement
path -- refactoring it to share this primitive is a behavior-preserving
cleanup with no test coverage benefit today, not something this slice
needs to do to satisfy item 14's own ask.
"""

from __future__ import annotations

from dataclasses import dataclass

from caliber.auth import CaliberIdentity


class DistinctActorError(ValueError):
    """Raised by :func:`require_distinct_actors` when the same platform
    identity appears on both sides of an originator-versus-decision-maker
    check."""


@dataclass(frozen=True)
class ActorProvenance:
    """Who performed one recorded action, and how.

    Captures enough to answer "was this the same actor as that other
    recorded action?" (:meth:`is_same_actor`) without conflating *how*
    someone authenticated with *who* they are: two records naming the
    same ``user_id`` are the same actor for distinct-actor purposes even
    if one went through a browser session and the other through a
    personal access token -- a platform identity cannot satisfy a
    separation-of-duties requirement against itself by switching
    credentials. ``credential_kind``/``credential_id`` (from `P1-E`'s
    ``CaliberIdentity`` fields) are carried for audit/display only; they
    are never part of the distinctness comparison itself.
    """

    user_id: str
    credential_kind: str | None = None
    credential_id: str | None = None

    @classmethod
    def from_identity(cls, identity: CaliberIdentity) -> ActorProvenance:
        """Build provenance from a resolved `CaliberIdentity` (`P1-E`)."""
        return cls(
            user_id=identity.user_id,
            credential_kind=identity.credential_kind,
            credential_id=identity.credential_id,
        )

    def is_same_actor(self, other: ActorProvenance) -> bool:
        """Whether ``other`` names the same platform identity as this one.

        Compares ``user_id`` only -- see the class docstring for why
        credential kind/id are irrelevant to this comparison.
        """
        return self.user_id == other.user_id


def require_distinct_actors(
    originator: ActorProvenance, decision_maker: ActorProvenance, *, context: str
) -> None:
    """Raise unless ``originator`` and ``decision_maker`` are different
    platform identities.

    ``context`` names the specific rule in the error message (e.g.
    ``"technical review"``, ``"QA sign-off"``, ``"release approval"``) --
    the same "a refusal without a reason is unusable" discipline
    :func:`caliber.workflows.approval_policy.record_approval` already
    follows for its own, separate self-approval check.
    """
    if originator.is_same_actor(decision_maker):
        raise DistinctActorError(
            f"{context}: {originator.user_id!r} originated this and cannot also be "
            f"the decision-maker; a different person must decide"
        )


__all__ = [
    "ActorProvenance",
    "DistinctActorError",
    "require_distinct_actors",
]
