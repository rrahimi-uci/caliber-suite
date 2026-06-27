"""project scoping columns on user-facing resources

Revision ID: 0026
Revises: 0025
Create Date: 2026-06-05

Adds ``project_id`` (nullable) and ``visibility`` (NOT NULL, default 'project')
to the six user-facing resource tables, plus a composite ``(owner, project_id,
visibility)`` index for the scoped list query.

Backward compatibility: existing rows are backfilled to ``visibility = 'public'``
so everything that was visible before the upgrade stays visible. (See design
gap G — switch the backfill to
'user' if pre-existing data should instead be scoped to its owner.)

``owner`` already exists on every one of these tables, so this migration only
adds the two new columns. Additive + reversible.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | Sequence[str] | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Tables that gain project/visibility scoping. caliber_mcp_servers is
# intentionally excluded (see design gap F); caliber_projects IS the scope.
_SCOPED_TABLES: tuple[str, ...] = (
    "caliber_agent_config",
    "caliber_skills",
    "caliber_tool_registry",
    "caliber_workflows",
    "caliber_eval_datasets",
    "caliber_conversation_policies",
)


def upgrade() -> None:
    for table in _SCOPED_TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(
                sa.Column("project_id", sa.String(64), nullable=True)
            )
            batch_op.add_column(
                sa.Column(
                    "visibility",
                    sa.String(16),
                    nullable=False,
                    server_default="project",
                )
            )
            batch_op.create_index(
                f"ix_{table}_scoping",
                ["owner", "project_id", "visibility"],
            )
        # Keep pre-existing rows visible after rollout.
        op.execute(sa.text(f"UPDATE {table} SET visibility = 'public'"))  # noqa: S608


def downgrade() -> None:
    for table in _SCOPED_TABLES:
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_index(f"ix_{table}_scoping")
            batch_op.drop_column("visibility")
            batch_op.drop_column("project_id")
