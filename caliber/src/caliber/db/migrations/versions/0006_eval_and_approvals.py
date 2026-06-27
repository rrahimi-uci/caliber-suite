"""eval results column + approval requests table

Revision ID: 0006
Revises: 0005
Create Date: 2025-05-15

Adds:

* ``caliber_refinement_jobs.eval_results`` — JSON column holding the eval
  comparison the Eval stage produces (candidate vs. baseline scores,
  deltas, gate decision).
* ``caliber_approval_requests`` — one row per job that passes the eval
  gate. The approve/reject endpoints (next milestone) mutate it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("caliber_refinement_jobs") as batch_op:
        batch_op.add_column(sa.Column("eval_results", sa.JSON, nullable=True))

    op.create_table(
        "caliber_approval_requests",
        sa.Column("approval_id", sa.String(64), primary_key=True),
        sa.Column(
            "job_id",
            sa.String(64),
            sa.ForeignKey("caliber_refinement_jobs.job_id"),
            unique=True,
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            sa.String(64),
            sa.ForeignKey("caliber_agent_config.agent_id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("eval_results", sa.JSON, nullable=True),
        sa.Column("candidate_snapshot", sa.JSON, nullable=True),
        sa.Column("diagnosis_snapshot", sa.JSON, nullable=True),
        sa.Column("approved_by", sa.String(256), nullable=True),
        sa.Column("approved_at", sa.DateTime, nullable=True),
        sa.Column("rejection_reason", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_approval_requests_status_created",
        "caliber_approval_requests",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_approval_requests_status_created", table_name="caliber_approval_requests"
    )
    op.drop_table("caliber_approval_requests")
    with op.batch_alter_table("caliber_refinement_jobs") as batch_op:
        batch_op.drop_column("eval_results")
