"""add nullable project_id to caliber_verification_queue and backfill it

`P2-Q` (docs/workspace-plan.md's own row, closed here): `CaliberVerificationItem`
(`routes/verification.py`'s Stage ① Verify queue, `P3-B`) had no `project_id`
at all, so its entire list/get/verify/dismiss/duplicate/batch surface was
globally unscoped -- any operator could see, verify, dismiss, or link every
project's manually-flagged feedback item. `P2-Q`'s own sweep row explicitly
named this a real, disclosed gap rather than sweeping it under that ticket,
since it needed its own scoping-shape decision.

Adds a bare, nullable `project_id` -- no `visibility`/owner column, the same
`project_only` shape `CaliberReleaseOperation` (`P2-R`, migration `0112`) and
`CaliberWorkspaceReleaseOperation` already use (this is a derived triage-queue
record, not a first-class owned resource). Existing rows are backfilled by
resolving each item's `agent_id` (`NOT NULL` and FK-constrained -- every row
has one) through `caliber_agent_config.agent_id`: a resolvable agent's
`project_id` is copied over, and a row whose agent has no project (a
personal/global agent) is left `NULL`, matching the same "no target =
personal/global" carve-out `prompt_targets.py` already applies. `agent_id` is
that table's primary key, so the correlated subquery below matches at most
one row.

Follows `CaliberReleaseOperation`'s migration `0112` precedent (itself
following `CaliberReworkTask`'s migration `0106`) for adding a nullable FK/
scoping column to an existing table via `batch_alter_table` (dialect-safe:
SQLite recreates the table, other dialects issue a plain `ALTER TABLE`).

Revision ID: 0113
Revises: 0112
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0113"
down_revision: str | Sequence[str] | None = "0112"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("caliber_verification_queue") as batch_op:
        batch_op.add_column(
            sa.Column(
                "project_id",
                sa.String(64),
                sa.ForeignKey("caliber_projects.project_id", name="fk_verification_queue_project"),
                nullable=True,
            )
        )
        batch_op.create_index("ix_verification_queue_project_id", ["project_id"])

    # Additive backfill: a correlated subquery keyed on `caliber_agent_config`'s
    # own primary key (`agent_id`), so it resolves to at most one row and
    # leaves `project_id` NULL wherever the agent itself has no project --
    # exactly the "no target = personal/global" carve-out this column's
    # docstring names.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE caliber_verification_queue "
            "SET project_id = ("
            "SELECT project_id FROM caliber_agent_config "
            "WHERE caliber_agent_config.agent_id = caliber_verification_queue.agent_id"
            ")"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("caliber_verification_queue") as batch_op:
        batch_op.drop_index("ix_verification_queue_project_id")
        batch_op.drop_constraint("fk_verification_queue_project", type_="foreignkey")
        batch_op.drop_column("project_id")
