"""add nullable project_id to caliber_release_operations and backfill it

`P2-R` (docs/workspace-plan.md Phase 2 item 6): `CaliberReleaseOperation`
(legacy prompt-alias release intents, distinct from the newer
`CaliberWorkspaceReleaseOperation`) had no `project_id` at all, so its
list/detail reads were globally unscoped -- any operator could see every
project's prompt release-operation history. This was previously blocked on
"prompts have no project binding to scope by"; `P2-G` removed that blocker
by giving every prompt a hidden, project-scoped `CaliberAgentConfig` runtime
target (`agent_id == prompt_name`).

Adds a bare, nullable `project_id` -- no `visibility`/owner column, the same
`project_only` shape `CaliberWorkspaceReleaseOperation` already uses (this is
a derived/historical record, not a first-class owned resource). Existing
rows are backfilled by resolving each operation's `resource_name` (the
prompt name; every row today has `resource_type = 'prompt'`) through
`caliber_agent_config.agent_id`: a resolvable hidden target's `project_id`
is copied over, and a row with no such target (a bare provider-only/legacy
prompt, or a personal "My Library" one) is left `NULL`, matching the same
"no target = personal/global" carve-out `prompt_targets.py` already applies.
`agent_id` is that table's primary key, so the correlated subquery below
matches at most one row.

Follows `CaliberReworkTask`'s migration `0106` precedent for adding a
nullable FK/scoping column to an existing table via `batch_alter_table`
(dialect-safe: SQLite recreates the table, other dialects issue a plain
`ALTER TABLE`).

Revision ID: 0112
Revises: 0111
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0112"
down_revision: str | Sequence[str] | None = "0111"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("caliber_release_operations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "project_id",
                sa.String(64),
                sa.ForeignKey("caliber_projects.project_id", name="fk_release_operations_project"),
                nullable=True,
            )
        )
        batch_op.create_index("ix_release_operations_project_id", ["project_id"])

    # Additive backfill: a correlated subquery keyed on `caliber_agent_config`'s
    # own primary key (`agent_id`), so it resolves to at most one row and
    # leaves `project_id` NULL wherever no hidden target exists -- exactly the
    # "no target = personal/global" carve-out this column's docstring names.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE caliber_release_operations "
            "SET project_id = ("
            "SELECT project_id FROM caliber_agent_config "
            "WHERE caliber_agent_config.agent_id = caliber_release_operations.resource_name"
            ") "
            "WHERE resource_type = 'prompt'"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("caliber_release_operations") as batch_op:
        batch_op.drop_index("ix_release_operations_project_id")
        batch_op.drop_constraint("fk_release_operations_project", type_="foreignkey")
        batch_op.drop_column("project_id")
