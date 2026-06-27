"""Aria plan-step quality gate (self-correction)

Revision ID: 0057
Revises: 0056
Create Date: 2026-06-22

Adds a nullable ``gate`` JSON column to ``caliber_aria_plan_steps``. When set
(e.g. ``{"metric": "score", "min": 0.9}``), a completed step whose evidence /
result falls below the gate escalates a confirm interaction for human review
instead of silently passing — the self-correction guarantee that quality is
never asserted below the bar. Pure additive column migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0057"
down_revision: str | Sequence[str] | None = "0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("caliber_aria_plan_steps", sa.Column("gate", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("caliber_aria_plan_steps", "gate")
