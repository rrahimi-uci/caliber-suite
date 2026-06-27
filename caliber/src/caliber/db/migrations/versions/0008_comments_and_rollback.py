"""approval comments + rollback checkpoints

Revision ID: 0008
Revises: 0007
Create Date: 2026-05-15

Two new tables that unblock Phase 3 backend completion:

1. ``caliber_approval_comments`` — free-form review threads attached to
   each approval row. Reviewers can leave context for each other
   ("@bob, can you double-check the Spanish examples?") without
   conflating prose with structured approve/reject votes.
2. ``caliber_rollback_checkpoints`` — pre-promotion artifact snapshots.
   Each successful promotion writes one row; the rollback endpoint
   reads it to restore the prior state without re-deriving from
   MLflow tags or audit-log scanning.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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

    op.create_table(
        "caliber_rollback_checkpoints",
        sa.Column("checkpoint_id", sa.String(64), primary_key=True),
        sa.Column(
            "approval_id",
            sa.String(64),
            sa.ForeignKey("caliber_approval_requests.approval_id"),
            nullable=False,
        ),
        sa.Column(
            "agent_id",
            sa.String(64),
            sa.ForeignKey("caliber_agent_config.agent_id"),
            nullable=False,
        ),
        sa.Column("artifact_type", sa.String(32), nullable=False),
        sa.Column("artifact_name", sa.String(256), nullable=False),
        sa.Column("artifact_ref_before", sa.String(512), nullable=True),
        sa.Column("artifact_ref_after", sa.String(512), nullable=False),
        sa.Column("version_before", sa.Integer, nullable=True),
        sa.Column("version_after", sa.Integer, nullable=True),
        sa.Column("snapshot_payload", sa.JSON, nullable=True),
        sa.Column("rolled_back_at", sa.DateTime, nullable=True),
        sa.Column("rolled_back_by", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_rollback_agent_created",
        "caliber_rollback_checkpoints",
        ["agent_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_rollback_agent_created", table_name="caliber_rollback_checkpoints")
    op.drop_table("caliber_rollback_checkpoints")
    op.drop_index("ix_approval_comments_approval", table_name="caliber_approval_comments")
    op.drop_table("caliber_approval_comments")
