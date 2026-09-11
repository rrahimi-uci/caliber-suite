"""add workspace schema foundation: project slug/source-mode/archive/accepted-revision
fields, project-member deactivation fields, audit-log environment correlation, and
the caliber_workspace_environments table

`P1-A` (docs/workspace-plan.md Phase 1 items 1 and 4): schema additions plus a
deterministic, additive backfill -- every existing project gets a derived,
unique-within-tenant slug, a default source mode, and its four fixed
environment rows (dev active; qa/staging/prod disabled). No data loss, no
change to any row that doesn't gain new columns/rows.

Revision ID: 0093
Revises: 0092
Create Date: 2026-09-10
"""

from __future__ import annotations

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0093"
down_revision: str | Sequence[str] | None = "0092"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: name, environment_class, promotion_order, default status -- matches
#: caliber.deployment_environments.WORKSPACE_ENVIRONMENT_CLASSES /
#: WORKSPACE_ENVIRONMENT_PROMOTION_ORDER / WORKSPACE_ENVIRONMENT_DEFAULT_STATUS,
#: duplicated here as plain literals since a migration must not import
#: application code (a later refactor of that module must not silently
#: change what an already-applied migration did).
_ENVIRONMENTS: tuple[tuple[str, str, int, str], ...] = (
    ("dev", "development", 10, "active"),
    ("qa", "qa", 20, "disabled"),
    ("staging", "staging", 30, "disabled"),
    ("prod", "production", 40, "disabled"),
)


def _slugify(name: str) -> str:
    """Lowercase, non-alphanumeric runs collapsed to a single ``-``, trimmed.
    Falls back to ``"workspace"`` for a name with no alphanumeric content at
    all (e.g. a project named only punctuation) so a slug is never empty.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "workspace"


def upgrade() -> None:
    with op.batch_alter_table("caliber_projects") as batch:
        batch.add_column(sa.Column("slug", sa.String(128), nullable=False, server_default=""))
        batch.add_column(
            sa.Column(
                "source_mode", sa.String(24), nullable=False, server_default="caliber_managed"
            )
        )
        batch.add_column(sa.Column("archived_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("archived_by", sa.String(256), nullable=True))
        batch.add_column(sa.Column("accepted_revision_id", sa.String(64), nullable=True))

    # Partial, not a plain unique constraint: `slug` defaults to `""`, and
    # every pre-existing row still has that blank default at this exact
    # point (the backfill below hasn't run yet). Restricting uniqueness to
    # non-blank slugs means this index is safe to create *before* the
    # backfill assigns real ones -- multiple blank-slug rows never collide,
    # matching `db/models.py::CaliberProject`'s own table args exactly (see
    # that comment for why this needs to be partial at all, not just why
    # this migration orders it this way).
    op.create_index(
        "uq_project_tenant_slug",
        "caliber_projects",
        ["tenant_id", "slug"],
        unique=True,
        sqlite_where=sa.text("slug <> ''"),
        postgresql_where=sa.text("slug <> ''"),
    )

    op.add_column("caliber_project_members", sa.Column("deactivated_at", sa.DateTime(), nullable=True))
    op.add_column(
        "caliber_project_members", sa.Column("deactivated_by", sa.String(256), nullable=True)
    )

    op.add_column("caliber_audit_log", sa.Column("environment_id", sa.String(64), nullable=True))
    op.create_index("ix_audit_log_environment", "caliber_audit_log", ["environment_id"])

    op.create_table(
        "caliber_workspace_environments",
        sa.Column("environment_id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("caliber_projects.project_id"),
            nullable=False,
        ),
        sa.Column("name", sa.String(16), nullable=False),
        sa.Column("environment_class", sa.String(16), nullable=False),
        sa.Column("promotion_order", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="disabled"),
        sa.Column("created_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint("project_id", "name", name="uq_workspace_environment_project_name"),
    )

    # Additive backfill. Slug derivation needs per-row collision handling
    # (two projects named e.g. "Demo" and "demo!" both slugify to "demo"),
    # which raw SQL can't express -- so this walks rows in Python via the
    # migration's own bound connection, the same escape hatch the manifest
    # canonicalization work uses for logic SQL alone can't carry.
    bind = op.get_bind()
    projects = bind.execute(
        sa.text("SELECT project_id, tenant_id, name FROM caliber_projects ORDER BY project_id")
    ).fetchall()

    used_slugs: dict[str, set[str]] = {}
    for project_id, tenant_id, name in projects:
        base_slug = _slugify(name or project_id)
        taken = used_slugs.setdefault(tenant_id, set())
        slug = base_slug
        suffix = 2
        while slug in taken:
            slug = f"{base_slug}-{suffix}"
            suffix += 1
        taken.add(slug)

        bind.execute(
            sa.text("UPDATE caliber_projects SET slug = :slug WHERE project_id = :project_id"),
            {"slug": slug, "project_id": project_id},
        )
        for env_name, env_class, order, status in _ENVIRONMENTS:
            bind.execute(
                sa.text(
                    "INSERT INTO caliber_workspace_environments "
                    "(environment_id, project_id, name, environment_class, "
                    "promotion_order, status, created_by) "
                    "VALUES (:environment_id, :project_id, :name, :environment_class, "
                    ":promotion_order, :status, '')"
                ),
                {
                    "environment_id": f"WSE-{project_id}-{env_name}",
                    "project_id": project_id,
                    "name": env_name,
                    "environment_class": env_class,
                    "promotion_order": order,
                    "status": status,
                },
            )


def downgrade() -> None:
    op.drop_table("caliber_workspace_environments")
    op.drop_index("ix_audit_log_environment", table_name="caliber_audit_log")
    op.drop_column("caliber_audit_log", "environment_id")
    op.drop_column("caliber_project_members", "deactivated_by")
    op.drop_column("caliber_project_members", "deactivated_at")
    op.drop_index("uq_project_tenant_slug", table_name="caliber_projects")
    with op.batch_alter_table("caliber_projects") as batch:
        batch.drop_column("accepted_revision_id")
        batch.drop_column("archived_by")
        batch.drop_column("archived_at")
        batch.drop_column("source_mode")
        batch.drop_column("slug")
