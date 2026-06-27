"""skills table

Revision ID: 0010
Revises: 0009
Create Date: 2026-05-15

Adds ``caliber_skills`` — the first Phase 4 resource type. A skill is
a reusable prompt fragment that agents compose into their prompts;
refining a skill once cascades to every agent that references it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_skills",
        sa.Column("skill_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("owner", sa.String(256), nullable=False),
        sa.Column("tags", sa.JSON, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column(
            "created_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.UniqueConstraint("name", name="uq_skill_name"),
    )


def downgrade() -> None:
    op.drop_table("caliber_skills")
