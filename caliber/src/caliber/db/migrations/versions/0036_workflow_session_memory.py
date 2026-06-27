"""workflow session memory

Revision ID: 0036
Revises: 0035
Create Date: 2026-06-13

Adds persistent workflow session memory rows so agent nodes can maintain
conversation state across runs when a workflow enables persistent session
memory.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | Sequence[str] | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_workflow_session_memory",
        sa.Column(
            "workflow_id",
            sa.String(length=128),
            sa.ForeignKey("caliber_workflows.workflow_id"),
            nullable=False,
        ),
        sa.Column("node_id", sa.String(length=128), nullable=False),
        sa.Column("session_id", sa.String(length=128), nullable=False),
        sa.Column("message_history", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("workflow_id", "node_id", "session_id"),
    )
    op.create_index(
        "ix_wf_session_memory_lookup",
        "caliber_workflow_session_memory",
        ["workflow_id", "session_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_wf_session_memory_lookup", table_name="caliber_workflow_session_memory")
    op.drop_table("caliber_workflow_session_memory")
