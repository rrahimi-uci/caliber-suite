"""add DB-level name-uniqueness constraints for 4 root resources with no constraint at all

`P2-A` slice 10's nullability/uniqueness audit (docs/workspace-plan.md section 16)
found that ``caliber_workflows``, ``caliber_knowledge_bases``,
``caliber_workflow_benchmark_reports``, and ``caliber_openapi_integrations`` have
no database-level unique constraint on ``name`` at all -- a pre-existing
TOCTOU-race gap between each route's app-level "select, then insert if absent"
check and the actual insert. This is that row's own named follow-up, not a new
finding.

Each table's uniqueness *scope* mirrors whatever its existing app-level check
already enforced, rather than inventing new semantics (docs/workspace-plan.md
section 15.3: "migrate unique constraints ... only after all ... paths stop
resolving by a global bare name" -- these four never resolved by a *scoped*
bare name to begin with, so narrowing scope here is not in play; this is
purely "give the existing scope a DB backstop"):

- ``caliber_workflows``: ``routes/workflows.py::create_workflow`` already
  checks ``name`` globally (no ``project_id`` filter) -- the same "shared
  fleet-wide handle" convention already ratified for ``uq_skill_name`` /
  ``uq_judge_name`` / ``uq_eval_dataset_name`` / ``uq_review_queue_name`` /
  ``uq_mcp_server_name`` (`P2-A` slice 10). A plain global unique constraint
  preserves that.
- ``caliber_openapi_integrations``: no app-level check existed. Structurally
  this table matches the global-handle family above (a plain
  ``(project_id, visibility)`` composite index, no owner-scoped index), not
  ``caliber_knowledge_bases``'s per-owner family, so it follows the same
  global convention rather than inventing a per-project one.
- ``caliber_knowledge_bases``: ``knowledge/service.py::_assert_unique_name``
  already checks ``name`` scoped to (``project_id`` -- or ``IS NULL`` for the
  no-active-project/personal-library case -- AND ``owner``). Because every SQL
  dialect treats each NULL as distinct from every other NULL, a plain
  ``UniqueConstraint(project_id, owner, name)`` would silently *not*
  reproduce the ``IS NULL`` branch (two personal-library rows for the same
  owner/name would never collide). Two partial indexes split the app check's
  two branches explicitly instead.
- ``caliber_workflow_benchmark_reports``: no app-level check existed, but this
  table's existing ``owner_status``/``project_status`` index pair was clearly
  modeled directly on ``caliber_knowledge_bases``'s identical pair, so it
  follows that sibling's per-(project, owner) convention and the same
  NULL-bucketing partial-index treatment.

Defensive handling of pre-existing duplicate data follows this repo's one
prior precedent for retrofitting a uniqueness constraint onto an
already-populated table, ``0079_unique_open_incident.py``: a bounded
``GROUP BY ... HAVING COUNT(*) > 1`` preflight against the live connection,
which aborts the migration with a readable preview of the offending values
rather than attempting to auto-deduplicate or silently pick a winner. There
is no real production deployment of this repo yet (pre-launch), so in
practice these preflights are expected to find nothing; they exist so a
concurrent-write race or a stray fixture seed cannot silently corrupt data
into an unresolvable migration failure with no diagnostic.

Revision ID: 0110
Revises: 0109
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0110"
down_revision: str | Sequence[str] | None = "0109"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_KB_PROJECT_INDEX = "uq_knowledge_base_project_owner_name"
_KB_NO_PROJECT_INDEX = "uq_knowledge_base_owner_name_no_project"
_WFBR_PROJECT_INDEX = "uq_wf_benchmark_report_project_owner_name"
_WFBR_NO_PROJECT_INDEX = "uq_wf_benchmark_report_owner_name_no_project"


def _reject_if_duplicates(
    *, table: str, group_by: str, select_cols: str, order_by: str, where: str = ""
) -> None:
    """Abort with a readable preview if `group_by` has any repeated value.

    Mirrors ``0079_unique_open_incident.py``'s preflight exactly: a bounded,
    read-only ``GROUP BY ... HAVING COUNT(*) > 1`` against the live
    connection, refusing to guess which row is authoritative.
    """

    clause = f"WHERE {where}\n            " if where else ""
    # All interpolated values are fixed, hardcoded call-site literals from
    # this file's own `upgrade()` (table/column names), never external input
    # -- same shape as `0026_project_scoping.py`'s identical dynamic-table
    # preflight.
    query = f"""
            SELECT {select_cols}, COUNT(*) AS row_count
            FROM {table}
            {clause}GROUP BY {group_by}
            HAVING COUNT(*) > 1
            ORDER BY {order_by}
            LIMIT 10
            """  # noqa: S608
    rows = op.get_bind().execute(sa.text(query)).all()
    if rows:
        preview = ", ".join(f"{tuple(row)[:-1]!r} ({row.row_count})" for row in rows)
        raise RuntimeError(
            f"cannot enforce name uniqueness on {table}; resolve duplicate "
            f"({select_cols}) rows first: {preview}"
        )


def upgrade() -> None:
    # caliber_workflows: global uniqueness on `name`.
    _reject_if_duplicates(
        table="caliber_workflows",
        select_cols="name",
        group_by="name",
        order_by="name",
    )
    with op.batch_alter_table("caliber_workflows") as batch_op:
        batch_op.create_unique_constraint("uq_workflow_name", ["name"])

    # caliber_openapi_integrations: global uniqueness on `name`. The old plain
    # index on `name` is superseded by the unique constraint's own implicit
    # index, so it is dropped to keep the schema in sync with `db/models.py`.
    _reject_if_duplicates(
        table="caliber_openapi_integrations",
        select_cols="name",
        group_by="name",
        order_by="name",
    )
    with op.batch_alter_table("caliber_openapi_integrations") as batch_op:
        batch_op.drop_index("ix_openapi_integrations_name")
        batch_op.create_unique_constraint("uq_openapi_integration_name", ["name"])

    # caliber_knowledge_bases: per-(project, owner) uniqueness, NULL-bucketed.
    _reject_if_duplicates(
        table="caliber_knowledge_bases",
        select_cols="project_id, owner, name",
        group_by="project_id, owner, name",
        order_by="project_id, owner, name",
        where="project_id IS NOT NULL",
    )
    _reject_if_duplicates(
        table="caliber_knowledge_bases",
        select_cols="owner, name",
        group_by="owner, name",
        order_by="owner, name",
        where="project_id IS NULL",
    )
    op.create_index(
        _KB_PROJECT_INDEX,
        "caliber_knowledge_bases",
        ["project_id", "owner", "name"],
        unique=True,
        sqlite_where=sa.text("project_id IS NOT NULL"),
        postgresql_where=sa.text("project_id IS NOT NULL"),
    )
    op.create_index(
        _KB_NO_PROJECT_INDEX,
        "caliber_knowledge_bases",
        ["owner", "name"],
        unique=True,
        sqlite_where=sa.text("project_id IS NULL"),
        postgresql_where=sa.text("project_id IS NULL"),
    )

    # caliber_workflow_benchmark_reports: same shape as caliber_knowledge_bases.
    _reject_if_duplicates(
        table="caliber_workflow_benchmark_reports",
        select_cols="project_id, owner, name",
        group_by="project_id, owner, name",
        order_by="project_id, owner, name",
        where="project_id IS NOT NULL",
    )
    _reject_if_duplicates(
        table="caliber_workflow_benchmark_reports",
        select_cols="owner, name",
        group_by="owner, name",
        order_by="owner, name",
        where="project_id IS NULL",
    )
    op.create_index(
        _WFBR_PROJECT_INDEX,
        "caliber_workflow_benchmark_reports",
        ["project_id", "owner", "name"],
        unique=True,
        sqlite_where=sa.text("project_id IS NOT NULL"),
        postgresql_where=sa.text("project_id IS NOT NULL"),
    )
    op.create_index(
        _WFBR_NO_PROJECT_INDEX,
        "caliber_workflow_benchmark_reports",
        ["owner", "name"],
        unique=True,
        sqlite_where=sa.text("project_id IS NULL"),
        postgresql_where=sa.text("project_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(_WFBR_NO_PROJECT_INDEX, table_name="caliber_workflow_benchmark_reports")
    op.drop_index(_WFBR_PROJECT_INDEX, table_name="caliber_workflow_benchmark_reports")

    op.drop_index(_KB_NO_PROJECT_INDEX, table_name="caliber_knowledge_bases")
    op.drop_index(_KB_PROJECT_INDEX, table_name="caliber_knowledge_bases")

    with op.batch_alter_table("caliber_openapi_integrations") as batch_op:
        batch_op.drop_constraint("uq_openapi_integration_name", type_="unique")
        batch_op.create_index("ix_openapi_integrations_name", ["name"])

    with op.batch_alter_table("caliber_workflows") as batch_op:
        batch_op.drop_constraint("uq_workflow_name", type_="unique")
