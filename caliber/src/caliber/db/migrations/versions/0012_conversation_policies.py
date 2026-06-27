"""conversation policies

Revision ID: 0012
Revises: 0011
Create Date: 2026-05-15

Adds ``caliber_conversation_policies`` — Phase 4 multi-turn behavior
policies (turn budgets, escalation triggers, clarification rules)
that refinement can evolve the same way it evolves a prompt.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_conversation_policies",
        sa.Column("policy_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("rules", sa.JSON, nullable=False),
        sa.Column("owner", sa.String(256), nullable=False),
        sa.Column("tags", sa.JSON, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime, nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.UniqueConstraint("name", name="uq_conversation_policy_name"),
    )


def downgrade() -> None:
    op.drop_table("caliber_conversation_policies")
