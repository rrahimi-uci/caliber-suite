"""add the nullable rework-task Workspace-release FK and exactly-one-source check

`P3-A` (docs/workspace-plan.md section 16) shipped `caliber_rework_tasks`
scoped to a refinement job only, deferring the Workspace-release source and
its `release_no_go` failure kind until the aggregate release machinery
existed. Phase 5 delivered that machinery, so this migration follows section
15.2's anticipated sequencing exactly: "only then add the nullable
rework-task release FK and exactly-one-source check."

`job_id` and `agent_id` become nullable (a release-sourced task has neither);
`workspace_release_id` and `project_id` are added, both nullable
(`project_id` is populated only for a release-sourced task -- see
`db/models.py::CaliberReworkTask`'s docstring for why a job-sourced task's
project boundary is deliberately left to the existing agent join instead of
being backfilled here). No data backfill is needed: every existing row is
job-sourced, so it already satisfies the new check constraint and simply
gains two more NULL columns.

Revision ID: 0106
Revises: 0105
Create Date: 2026-09-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0106"
down_revision: str | Sequence[str] | None = "0105"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("caliber_rework_tasks") as batch_op:
        batch_op.alter_column(
            "job_id",
            existing_type=sa.String(length=64),
            nullable=True,
        )
        batch_op.alter_column(
            "agent_id",
            existing_type=sa.String(length=64),
            nullable=True,
        )
        batch_op.add_column(
            sa.Column(
                "workspace_release_id",
                sa.String(64),
                sa.ForeignKey(
                    "caliber_workspace_releases.release_id",
                    name="fk_rework_tasks_workspace_release",
                ),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "project_id",
                sa.String(64),
                sa.ForeignKey("caliber_projects.project_id", name="fk_rework_tasks_project"),
                nullable=True,
            )
        )
        batch_op.create_unique_constraint(
            "uq_rework_task_workspace_release", ["workspace_release_id"]
        )
        batch_op.create_index("ix_rework_tasks_project_id", ["project_id"])
        batch_op.create_check_constraint(
            "ck_rework_task_exactly_one_source",
            "(job_id IS NOT NULL) != (workspace_release_id IS NOT NULL)",
        )


def downgrade() -> None:
    # Symmetric with 15.2's downgrade policy: additive/unused columns can be
    # dropped automatically. This assumes no release-sourced row exists yet
    # (job_id/agent_id NOT NULL would otherwise reject real data) -- once a
    # release has been rejected and owns a task, downgrading past this
    # revision requires an operational decision, not an automated one, the
    # same as dropping import/release history is refused elsewhere in
    # section 15.2.
    with op.batch_alter_table("caliber_rework_tasks") as batch_op:
        batch_op.drop_constraint("ck_rework_task_exactly_one_source", type_="check")
        batch_op.drop_index("ix_rework_tasks_project_id")
        batch_op.drop_constraint("uq_rework_task_workspace_release", type_="unique")
        batch_op.drop_constraint("fk_rework_tasks_project", type_="foreignkey")
        batch_op.drop_column("project_id")
        batch_op.drop_constraint("fk_rework_tasks_workspace_release", type_="foreignkey")
        batch_op.drop_column("workspace_release_id")
        batch_op.alter_column(
            "agent_id",
            existing_type=sa.String(length=64),
            nullable=False,
        )
        batch_op.alter_column(
            "job_id",
            existing_type=sa.String(length=64),
            nullable=False,
        )
