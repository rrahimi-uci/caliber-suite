"""add quality reviews

Revision ID: 0092
Revises: 0091
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0092"
down_revision: str | Sequence[str] | None = "0091"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_quality_reviews",
        sa.Column("review_id", sa.String(64), primary_key=True),
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
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.String(256), nullable=False),
        sa.Column("candidate_snapshot", sa.JSON(), nullable=True),
        sa.Column("eval_results_snapshot", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index(
        "ix_quality_reviews_job_created",
        "caliber_quality_reviews",
        ["job_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_quality_reviews_job_created", table_name="caliber_quality_reviews")
    op.drop_table("caliber_quality_reviews")
