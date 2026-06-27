"""Aria mid-run interactions (agentic orchestration, Phase 2)

Revision ID: 0056
Revises: 0055
Create Date: 2026-06-21

Adds ``caliber_aria_interactions`` — the mid-run pause/resume record. When the
executor reaches a step that the autonomy dial says needs a human (any gated step;
mutate/safe under ``ask_each``), it writes one of these and pauses the plan;
answering it resumes execution. See docs/12-assistant/aria-agentic-orchestration.md
Component 4.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0056"
down_revision: str | Sequence[str] | None = "0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_aria_interactions",
        sa.Column("interaction_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(length=64),
            sa.ForeignKey("caliber_aria_plans.plan_id"),
            nullable=False,
        ),
        sa.Column("step_id", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False, server_default="permission"),
        sa.Column("prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("options", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("required_scope", sa.String(length=32), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("response", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("responded_by", sa.String(length=256), nullable=True),
        sa.Column("responded_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_aria_interaction_plan", "caliber_aria_interactions", ["plan_id"])


def downgrade() -> None:
    op.drop_index("ix_aria_interaction_plan", table_name="caliber_aria_interactions")
    op.drop_table("caliber_aria_interactions")
