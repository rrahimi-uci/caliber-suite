"""initial: caliber_agent_config and caliber_verification_queue

Revision ID: 0001
Revises:
Create Date: 2025-05-15

Establishes the two foundational CALIBER tables. Both are read by the
``GET /caliber/agents`` and ``GET /caliber/verification-queue`` endpoints
shipped in Phase 1.5; writes against these tables (and the rest of the
schema) follow in subsequent migrations.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_agent_config",
        sa.Column("agent_id", sa.String(64), primary_key=True),
        sa.Column("experiment_id", sa.String(64), nullable=False, unique=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("owner", sa.String(256), nullable=False),
        sa.Column("artifact_types", sa.JSON, nullable=False),
        sa.Column("eval_thresholds", sa.JSON, nullable=False),
        sa.Column("optimizer_config", sa.JSON, nullable=False),
        sa.Column("approval_policy", sa.JSON, nullable=False),
        sa.Column("optimize_for", sa.String(16), nullable=False, server_default="quality"),
        sa.Column("collaboration_mode", sa.String(32), nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("required_approvals", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )

    op.create_table(
        "caliber_verification_queue",
        sa.Column("item_id", sa.String(64), primary_key=True),
        sa.Column(
            "agent_id",
            sa.String(64),
            sa.ForeignKey("caliber_agent_config.agent_id"),
            nullable=False,
        ),
        sa.Column("assessment_id", sa.String(64), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("experiment_id", sa.String(64), nullable=True),
        sa.Column("session_id", sa.String(128), nullable=True),
        sa.Column("workflow_id", sa.String(128), nullable=True),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("free_text", sa.Text, nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("artifact_type_hint", sa.String(32), nullable=True),
        sa.Column("artifact_ref", sa.String(512), nullable=True),
        sa.Column("submitted_context", sa.JSON, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("priority", sa.Integer, nullable=False, server_default="0"),
        sa.Column("assigned_to", sa.String(256), nullable=True),
        sa.Column("verified_by", sa.String(256), nullable=True),
        sa.Column("verified_at", sa.DateTime, nullable=True),
        sa.Column("verification_notes", sa.Text, nullable=True),
        sa.Column("refinement_target", sa.String(32), nullable=True),
        sa.Column(
            "duplicate_of_id",
            sa.String(64),
            sa.ForeignKey("caliber_verification_queue.item_id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("assessment_id", name="uq_verification_assessment_id"),
    )
    op.create_index(
        "ix_verification_queue_status_priority",
        "caliber_verification_queue",
        ["status", "priority"],
    )


def downgrade() -> None:
    op.drop_index("ix_verification_queue_status_priority", table_name="caliber_verification_queue")
    op.drop_table("caliber_verification_queue")
    op.drop_table("caliber_agent_config")
