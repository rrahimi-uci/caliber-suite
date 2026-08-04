"""add independent assistant reviewer decisions

Revision ID: 0084
Revises: 0083
Create Date: 2026-08-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0084"
down_revision: str | Sequence[str] | None = "0083"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("caliber_assistant_drafts") as batch:
        batch.add_column(sa.Column("review_report", sa.JSON(), nullable=True))

    op.create_table(
        "caliber_assistant_reviews",
        sa.Column("review_id", sa.String(64), primary_key=True),
        sa.Column(
            "draft_id",
            sa.String(64),
            sa.ForeignKey("caliber_assistant_drafts.draft_id"),
            nullable=False,
        ),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("candidate_version", sa.Integer(), nullable=False),
        sa.Column("candidate_hash", sa.String(64), nullable=False),
        sa.Column("candidate_snapshot", sa.JSON(), nullable=False),
        sa.Column("author_user", sa.String(256), nullable=False),
        sa.Column("reviewer_user", sa.String(256), nullable=False),
        sa.Column("reviewer_kind", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(256), nullable=False),
        sa.Column("model", sa.String(256), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("evidence", sa.JSON(), nullable=True),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_asst_review_draft_created",
        "caliber_assistant_reviews",
        ["draft_id", "created_at"],
    )
    op.create_index(
        "ix_asst_review_decision_created",
        "caliber_assistant_reviews",
        ["decision", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_asst_review_decision_created", table_name="caliber_assistant_reviews")
    op.drop_index("ix_asst_review_draft_created", table_name="caliber_assistant_reviews")
    op.drop_table("caliber_assistant_reviews")
    with op.batch_alter_table("caliber_assistant_drafts") as batch:
        batch.drop_column("review_report")
