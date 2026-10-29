"""persist Workspace release evaluation claim history and provider provenance

Revision ID: 0129
Revises: 0128
Create Date: 2026-10-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0129"
down_revision: str | Sequence[str] | None = "0128"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_workspace_release_evaluation_attempts",
        sa.Column("attempt_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "evaluation_id",
            sa.String(length=64),
            sa.ForeignKey("caliber_workspace_release_evaluations.evaluation_id"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="running"),
        sa.Column("claimed_at", sa.DateTime(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("provider_run_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("authoritative", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_summary", sa.String(length=4000), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "evaluation_id", "attempt_number", name="uq_workspace_release_eval_attempt_number"
        ),
        sa.CheckConstraint(
            "status IN ('running', 'requeued', 'orphaned', 'succeeded', 'failed')",
            name="ck_workspace_release_eval_attempt_status",
        ),
        sa.CheckConstraint("attempt_number >= 1", name="ck_workspace_release_eval_attempt_number"),
    )
    op.create_index(
        "ix_workspace_release_eval_attempts_evaluation",
        "caliber_workspace_release_evaluation_attempts",
        ["evaluation_id", "attempt_number"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workspace_release_eval_attempts_evaluation",
        table_name="caliber_workspace_release_evaluation_attempts",
    )
    op.drop_table("caliber_workspace_release_evaluation_attempts")
