"""Shapes shared across the API, independent of any one resource."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

T = TypeVar("T")

#: Stability tiers advertised by /capabilities and the OpenAPI document.
STABILITY_GA = "ga"
STABILITY_BETA = "beta"
STABILITY_INTERNAL = "internal"


@dataclass(frozen=True)
class CursorPage(Generic[T]):
    """One page of a cursor-paginated list.

    Unlike :class:`Page`'s ``limit``/``offset``, the cursor is an opaque,
    server-issued token: a caller resumes by passing ``next_cursor`` straight
    back as the next request's ``cursor`` parameter, never by computing an
    offset itself. Workspace's import/revision/Change-Request list endpoints
    use this scheme because their rows can be deleted or reordered between
    pages in a way a numeric offset would silently skip or repeat.
    """

    items: list[T] = field(default_factory=list)
    next_cursor: str | None = None

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


@dataclass(frozen=True)
class Page:
    """One page of an offset-paginated list.

    Kept even though :meth:`Transport.paginate` hides pagination from most
    callers: a caller who needs to checkpoint and resume needs the offset, and
    reconstructing it from a flat iterator is not possible.
    """

    items: list[Any] = field(default_factory=list)
    limit: int = 0
    offset: int = 0

    @property
    def is_last(self) -> bool:
        """Whether this looks like the final page.

        A short page ends the sequence. A full page might too -- the only way to
        know is to ask for the next one and get nothing.
        """
        return len(self.items) < self.limit

    @property
    def next_offset(self) -> int:
        return self.offset + len(self.items)


@dataclass(frozen=True)
class Stability:
    """Which API tags fall in which tier."""

    ga: tuple[str, ...] = ()
    beta: tuple[str, ...] = ()
    internal: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, payload: Any) -> Stability:
        if not isinstance(payload, dict):
            return cls()
        return cls(
            ga=tuple(payload.get(STABILITY_GA) or ()),
            beta=tuple(payload.get(STABILITY_BETA) or ()),
            internal=tuple(payload.get(STABILITY_INTERNAL) or ()),
        )

    def tier_of(self, tag: str) -> str | None:
        for tier, tags in (
            (STABILITY_GA, self.ga),
            (STABILITY_BETA, self.beta),
            (STABILITY_INTERNAL, self.internal),
        ):
            if tag in tags:
                return tier
        return None


__all__ = [
    "STABILITY_BETA",
    "STABILITY_GA",
    "STABILITY_INTERNAL",
    "CursorPage",
    "Page",
    "Stability",
]
