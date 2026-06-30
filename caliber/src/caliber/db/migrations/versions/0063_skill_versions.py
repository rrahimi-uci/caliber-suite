"""immutable per-version skill content snapshots

Revision ID: 0063
Revises: 0062
Create Date: 2026-06-30

Adds ``caliber_skill_versions`` — an immutable snapshot of a skill's content
(and summary) at each ``version_number``. Skills are otherwise a single mutable
row, so this table gives them real version history: a per-version list/diff and
an exact rollback (restore a prior snapshot as a new version). One row per
``(skill_id, version_number)``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0063"
down_revision: str | Sequence[str] | None = "0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_skill_versions",
        sa.Column("skill_version_id", sa.String(length=64), primary_key=True),
        sa.Column(
            "skill_id",
            sa.String(length=64),
            sa.ForeignKey("caliber_skills.skill_id"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("skill_id", "version_number", name="uq_skill_version_number"),
    )
    op.create_index(
        "ix_skill_versions_skill",
        "caliber_skill_versions",
        ["skill_id", "version_number"],
    )


def downgrade() -> None:
    op.drop_index("ix_skill_versions_skill", table_name="caliber_skill_versions")
    op.drop_table("caliber_skill_versions")
