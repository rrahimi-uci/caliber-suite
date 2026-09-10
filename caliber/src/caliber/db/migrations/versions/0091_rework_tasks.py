"""add rework tasks

Revision ID: 0091
Revises: 0090
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0091"
down_revision: str | Sequence[str] | None = "0090"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_rework_tasks",
        sa.Column("task_id", sa.String(64), primary_key=True),
        sa.Column(
            "job_id",
            sa.String(64),
            sa.ForeignKey("caliber_refinement_jobs.job_id"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            sa.String(64),
            sa.ForeignKey("caliber_agent_config.agent_id"),
            nullable=False,
        ),
        sa.Column("failure_kind", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("gate_evidence", sa.JSON(), nullable=True),
        sa.Column("assigned_to", sa.String(256), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column(
            "resolution_job_id",
            sa.String(64),
            sa.ForeignKey("caliber_refinement_jobs.job_id"),
            nullable=True,
        ),
        sa.Column("resolution_notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(256), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column("resolved_by", sa.String(256), nullable=True),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("job_id", name="uq_rework_task_job"),
    )
    op.create_index(
        "ix_rework_tasks_status_created",
        "caliber_rework_tasks",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_rework_tasks_status_created", table_name="caliber_rework_tasks")
    op.drop_table("caliber_rework_tasks")
