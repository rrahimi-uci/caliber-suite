"""Row-level scoping for multi-user project isolation.

Implements the 3-tier visibility model:

* ``project`` — the owner's resources inside the active project
* ``user``    — the owner's cross-project ("My Library") resources
* ``public``  — visible to everyone

The helper operates on SQLAlchemy 2.0 ``Select`` statements (the codebase never
uses the legacy ``Query`` API) and returns a new ``Select`` with the visibility
predicate applied. Wire it into a list endpoint like::

    stmt = select(CaliberSkill)
    stmt = apply_visibility_filter(stmt, CaliberSkill, identity, identity.active_project_id)
    rows = session.execute(stmt).scalars().all()
"""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import and_, false, or_, select
from sqlalchemy.sql import Select

from caliber.auth import SCOPE_ADMIN, CaliberIdentity

VisibilityTier = Literal["project", "user", "public"]


def get_visible(
    session: Any,
    model: Any,
    pk_column: Any,
    pk_value: Any,
    identity: CaliberIdentity,
) -> Any | None:
    """Fetch a row by primary key **only if it's visible** to ``identity``.

    Detail-GET endpoints historically used a bare ``session.get(Model, id)``,
    which bypasses the 3-tier visibility model their *list* sibling enforces —
    letting a user read another project's resource by guessing its id. This
    re-queries by id *through* :func:`apply_visibility_filter`, so an
    out-of-scope row simply returns ``None`` (the caller 404s — it doesn't leak
    "exists but forbidden").
    """
    stmt = apply_visibility_filter(
        select(model).where(pk_column == pk_value),
        model,
        identity,
        identity.active_project_id,
    )
    return session.execute(stmt).scalars().first()


def apply_visibility_filter(  # noqa: PLR0911 (one return per visibility tier reads clearer than nesting)
    stmt: Select[Any],
    model: Any,
    identity: CaliberIdentity,
    project_id: str | None,
    *,
    only: VisibilityTier | None = None,
) -> Select[Any]:
    """Filter a SELECT by the 3-tier visibility model.

    ``model`` is the ORM class being queried (must expose ``visibility``,
    ``owner``, and ``project_id`` columns).

    ``only`` restricts the result to a single tier — used by the My Library
    (``only="user"``) and Public Library (``only="public"``) views.

    Admins bypass owner scoping entirely so they can inspect/manage every
    resource. When ``only`` is set, admins still receive the requested tier but
    across all owners.
    """
    if identity.has_scope(SCOPE_ADMIN):
        if only is None:
            return stmt
        if only == "public":
            return stmt.where(model.visibility == "public")
        if only == "user":
            return stmt.where(model.visibility == "user")
        if only == "project":
            if not project_id:
                return stmt.where(false())
            return stmt.where(
                and_(
                    model.visibility == "project",
                    model.project_id == project_id,
                )
            )

    conditions = []
    if only in (None, "public"):
        conditions.append(model.visibility == "public")
    if only in (None, "user"):
        conditions.append(and_(model.visibility == "user", model.owner == identity.user_id))
    if only in (None, "project") and project_id:
        conditions.append(
            and_(
                model.visibility == "project",
                model.project_id == project_id,
                model.owner == identity.user_id,
            )
        )

    if not conditions:
        # e.g. only="project" with no active project → match nothing rather
        # than silently returning every row.
        return stmt.where(false())
    return stmt.where(or_(*conditions))
