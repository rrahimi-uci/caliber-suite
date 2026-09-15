"""Legacy-null and duplicate-name migration report.

`P2` (docs/workspace-plan.md Phase 2 item 9): "Produce the legacy-null,
duplicate-name, and orphan-project-id migration report without enforcing
destructive constraints yet." Read-only evidence, not a schema or behavior
change -- it feeds two later, separate decisions this same phase names:

* item 2 ("require project IDs for new project-owned root records"): a
  model with a nonzero legacy-null count cannot get a `project_id NOT NULL`
  constraint without either a backfill or an explicit "personal/global stays
  legitimately nullable" exemption first.
* item 3 ("add missing indexes and FKs where migration evidence permits"):
  narrowing a model's uniqueness from global to per-project (the "two
  workspaces can use the same logical manifest names" acceptance criterion)
  is only safe once any existing same-name-different-project collisions are
  known and reconciled. Adding a foreign key to a project-scoped column is
  only safe after every non-null value resolves to a real project.

Built on :func:`caliber.db.resource_inventory.resource_inventory`'s own
classification rather than a second, hand-maintained model list -- the two
tiers it names `visibility`/`project_only` are exactly the ones where
`project_id` genuinely varies row to row (an `owned_catalog`/`unscoped`
model has no `project_id` column to report on at all).

Unlike `resource_inventory()` itself (pure model introspection, no DB
needed), this module runs real queries against a live session -- there is
no way to know how much legacy-null or duplicate-name data exists without
looking at the data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from caliber.db.models import CaliberProject
from caliber.db.resource_inventory import (
    SCOPING_PROJECT_ONLY,
    SCOPING_VISIBILITY,
    resource_inventory,
)


@dataclass(frozen=True)
class LegacyNullFinding:
    """How many of a project-scoped model's rows have no `project_id` yet."""

    model: str
    table: str
    total_rows: int
    null_project_id_rows: int


@dataclass(frozen=True)
class DuplicateNameFinding:
    """A `name` value shared across more than one distinct `project_id`
    bucket for one model -- i.e., would collide if that model's uniqueness
    were narrowed to per-project without first reconciling these rows.
    `None` (no project) counts as its own bucket, same as every other
    project-scoping check in this codebase.
    """

    model: str
    table: str
    name: str
    project_ids: tuple[str | None, ...]


@dataclass(frozen=True)
class OrphanProjectIdFinding:
    """A project-scoped row whose non-null project id has no project row."""

    model: str
    table: str
    project_id: str
    row_count: int


def _project_scoped_models() -> list[Any]:
    """Every model class `resource_inventory()` classifies as project-scoped
    (`visibility` or `project_only`) -- the two tiers where `project_id`
    genuinely varies row to row, per that inventory's own docstring."""
    from caliber.db.base import Base  # noqa: PLC0415

    by_name = {mapper.class_.__name__: mapper.class_ for mapper in Base.registry.mappers}
    return [
        by_name[descriptor.name]
        for descriptor in resource_inventory()
        if descriptor.scoping in (SCOPING_VISIBILITY, SCOPING_PROJECT_ONLY)
    ]


def legacy_null_report(session: Session) -> list[LegacyNullFinding]:
    """Count legacy-null `project_id` rows for every project-scoped model
    that has at least one row. Zero behavior change: read-only counts.
    """
    findings: list[LegacyNullFinding] = []
    for model in _project_scoped_models():
        total = session.execute(select(func.count()).select_from(model)).scalar_one()
        if total == 0:
            continue
        nulls = session.execute(
            select(func.count()).select_from(model).where(model.project_id.is_(None))
        ).scalar_one()
        findings.append(
            LegacyNullFinding(
                model=model.__name__,
                table=str(model.__tablename__),
                total_rows=int(total),
                null_project_id_rows=int(nulls),
            )
        )
    return findings


def duplicate_name_report(session: Session) -> list[DuplicateNameFinding]:
    """Find `name` values shared across more than one `project_id` bucket,
    for every project-scoped model that has a `name` column.

    A model whose current uniqueness is already global (e.g.
    `CaliberSkill`'s `uq_skill_name`) cannot have real duplicates today --
    the constraint itself prevents it -- so a nonzero result here only ever
    comes from a model with no such constraint, and names exactly the
    collisions a future per-project namespace strategy (item 4) would need
    to reconcile before narrowing any global uniqueness to per-project.
    """
    findings: list[DuplicateNameFinding] = []
    for model in _project_scoped_models():
        if "name" not in model.__mapper__.columns:
            continue
        name_column = model.name
        rows = session.execute(select(name_column, model.project_id)).all()
        by_name: dict[str, set[str | None]] = {}
        for name, project_id in rows:
            by_name.setdefault(name, set()).add(project_id)
        for name, project_ids in sorted(by_name.items()):
            if len(project_ids) > 1:
                findings.append(
                    DuplicateNameFinding(
                        model=model.__name__,
                        table=str(model.__tablename__),
                        name=name,
                        project_ids=tuple(
                            sorted(project_ids, key=lambda value: (value is None, value))
                        ),
                    )
                )
    return findings


def orphan_project_id_report(session: Session) -> list[OrphanProjectIdFinding]:
    """Find non-null project ids that do not resolve to ``CaliberProject``.

    The report covers the same inventory-derived project-scoped models as the
    null and duplicate reports. Deleted rows are intentionally included: a
    foreign-key migration must account for every stored reference, not only
    rows currently returned by a visibility-aware list endpoint. Findings are
    grouped by model and orphan id, and returned in stable order for audit
    diffs and migration review.
    """
    known_projects = select(CaliberProject.project_id)
    findings: list[OrphanProjectIdFinding] = []
    for model in _project_scoped_models():
        rows = session.execute(
            select(model.project_id, func.count())
            .where(model.project_id.is_not(None))
            .where(~model.project_id.in_(known_projects))
            .group_by(model.project_id)
        ).all()
        findings.extend(
            OrphanProjectIdFinding(
                model=model.__name__,
                table=str(model.__tablename__),
                project_id=str(project_id),
                row_count=int(row_count),
            )
            for project_id, row_count in rows
            if project_id is not None
        )
    return sorted(findings, key=lambda finding: (finding.model, finding.project_id))


__all__ = [
    "DuplicateNameFinding",
    "LegacyNullFinding",
    "OrphanProjectIdFinding",
    "duplicate_name_report",
    "legacy_null_report",
    "orphan_project_id_report",
]
