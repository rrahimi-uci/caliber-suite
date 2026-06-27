"""Aria plan task-context persistence

Revision ID: 0058
Revises: 0057
Create Date: 2026-06-23

Adds durable task-context columns to ``caliber_aria_plans`` so a plan can keep
the user-specified completion criteria, constraints, and referenced resources
across execution, resume, and UI reloads.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0058"
down_revision: str | Sequence[str] | None = "0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_aria_plans",
        sa.Column(
            "constraints",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "caliber_aria_plans",
        sa.Column(
            "done_when",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )
    op.add_column(
        "caliber_aria_plans",
        sa.Column(
            "context_refs",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    op.drop_column("caliber_aria_plans", "context_refs")
    op.drop_column("caliber_aria_plans", "done_when")
    op.drop_column("caliber_aria_plans", "constraints")
