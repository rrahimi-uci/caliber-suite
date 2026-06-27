"""skill standard fields

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-17

Adds columns to ``caliber_skills`` aligned with the Anthropic skill
standard:

* ``summary``      — progressive-disclosure level-1 text.
* ``category``     — use-case bucket (document_creation, workflow_automation,
                     mcp_enhancement, custom).
* ``metadata``     — open JSON bag for extra key-value pairs.
* ``allowed_tools``— tool restriction string.
* ``depends_on``   — composability: list of skill names this skill requires.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("caliber_skills") as batch_op:
        batch_op.add_column(sa.Column("summary", sa.Text, nullable=False, server_default=""))
        batch_op.add_column(
            sa.Column("category", sa.String(32), nullable=False, server_default="custom")
        )
        batch_op.add_column(
            sa.Column("skill_metadata", sa.JSON, nullable=False, server_default="{}")
        )
        batch_op.add_column(sa.Column("allowed_tools", sa.Text, nullable=True))
        batch_op.add_column(
            sa.Column("depends_on", sa.JSON, nullable=False, server_default="[]")
        )


def downgrade() -> None:
    with op.batch_alter_table("caliber_skills") as batch_op:
        batch_op.drop_column("depends_on")
        batch_op.drop_column("allowed_tools")
        batch_op.drop_column("skill_metadata")
        batch_op.drop_column("category")
        batch_op.drop_column("summary")
