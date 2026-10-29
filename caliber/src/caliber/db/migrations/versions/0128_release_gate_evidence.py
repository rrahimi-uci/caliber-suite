"""Persist release gate evidence rows with evaluation attempts.

Revision ID: 0128
Revises: 0127
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0128"
down_revision = "0127"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "caliber_workspace_release_evaluations",
        sa.Column("gate_evidence", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("caliber_workspace_release_evaluations", "gate_evidence")
