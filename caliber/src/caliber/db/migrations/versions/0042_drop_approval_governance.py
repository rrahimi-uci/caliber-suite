"""drop approval governance tables

Revision ID: 0042
Revises: 0041
Create Date: 2026-06-17

Human-feedback approval governance (votes/comments/quorum) was removed.
Refinement jobs that pass the eval gate now land at the terminal
``candidate_ready`` status; an operator promotes the candidate via the Apply
endpoint. The ``caliber_approval_votes`` and ``caliber_approval_comments``
tables are dropped. ``caliber_approval_requests`` is KEPT (it remains the
born-``approved`` provenance/rollback anchor) and ``caliber_regression_runs``
is KEPT (eval-replay provenance).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0042"
down_revision: str | Sequence[str] | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("ix_approval_comments_approval", table_name="caliber_approval_comments")
    op.drop_table("caliber_approval_comments")
    op.drop_table("caliber_approval_votes")


def downgrade() -> None:
    op.create_table(
        "caliber_approval_votes",
        sa.Column("vote_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "approval_id",
            sa.String(64),
            sa.ForeignKey("caliber_approval_requests.approval_id"),
            nullable=False,
        ),
        sa.Column("voter", sa.String(256), nullable=False),
        sa.Column("vote", sa.String(16), nullable=False),
        sa.Column("comment", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("approval_id", "voter", name="uq_approval_vote_per_voter"),
    )
    op.create_table(
        "caliber_approval_comments",
        sa.Column("comment_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "approval_id",
            sa.String(64),
            sa.ForeignKey("caliber_approval_requests.approval_id"),
            nullable=False,
        ),
        sa.Column("author", sa.String(256), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_approval_comments_approval",
        "caliber_approval_comments",
        ["approval_id", "created_at"],
    )
