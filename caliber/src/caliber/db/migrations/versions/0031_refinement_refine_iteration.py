"""add refine_iteration to refinement jobs

Revision ID: 0031
Revises: 0030
Create Date: 2026-06-11

Tracks how many automatic candidate→eval→re-candidate self-correction
iterations a refinement job has spent (see ``config.refinement_max_iterations``).
Defaults to 0 so existing rows and the off-by-default behaviour are unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | Sequence[str] | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_refinement_jobs",
        sa.Column("refine_iteration", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    with op.batch_alter_table("caliber_refinement_jobs") as batch_op:
        batch_op.drop_column("refine_iteration")
