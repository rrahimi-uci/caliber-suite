"""Aria goal-plans (agentic orchestration, Phase 1)

Revision ID: 0055
Revises: 0054
Create Date: 2026-06-21

Adds ``caliber_aria_plans`` + ``caliber_aria_plan_steps`` — Aria's durable,
first-class decomposition of a user goal into capability-invoking steps (see
docs/12-assistant/aria-agentic-orchestration.md, Component 2). A plan is the
assistant's persisted "todo list"; each step invokes a registered capability and
links to the artifacts it produced (draft / job / approval / checkpoint) for
lineage. Later phases add the resumable executor + the interaction protocol on
top of these tables.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0055"
down_revision: str | Sequence[str] | None = "0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_aria_plans",
        sa.Column("plan_id", sa.String(length=64), primary_key=True),
        sa.Column("session_id", sa.String(length=64), nullable=True),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="draft"),
        sa.Column("autonomy", sa.String(length=16), nullable=False, server_default="approve_plan"),
        sa.Column("owner", sa.String(length=256), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("visibility", sa.String(length=16), nullable=False, server_default="user"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_table(
        "caliber_aria_plan_steps",
        sa.Column("step_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(length=64),
            sa.ForeignKey("caliber_aria_plans.plan_id"),
            nullable=False,
        ),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("capability_key", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("inputs", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("depends_on", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("result", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("evidence", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("draft_id", sa.String(length=64), nullable=True),
        sa.Column("job_id", sa.String(length=64), nullable=True),
        sa.Column("approval_id", sa.String(length=64), nullable=True),
        sa.Column("checkpoint_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_aria_plan_step_plan", "caliber_aria_plan_steps", ["plan_id"])


def downgrade() -> None:
    op.drop_index("ix_aria_plan_step_plan", table_name="caliber_aria_plan_steps")
    op.drop_table("caliber_aria_plan_steps")
    op.drop_table("caliber_aria_plans")
