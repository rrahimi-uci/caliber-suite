"""workflow calibration spec on refinement jobs

Revision ID: 0022
Revises: 0021
Create Date: 2026-06-04

Adds a nullable JSON calibration_spec column. Existing refinement jobs keep
their current behavior because NULL means "not a workflow calibration run".
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("caliber_refinement_jobs") as batch_op:
        batch_op.add_column(sa.Column("calibration_spec", sa.JSON, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("caliber_refinement_jobs") as batch_op:
        batch_op.drop_column("calibration_spec")
