"""job diagnosis column

Revision ID: 0004
Revises: 0003
Create Date: 2025-05-15

Adds the ``diagnosis`` JSON column to ``caliber_refinement_jobs``. The
diagnosis stage (``caliber.orchestrator.diagnosis.run_diagnosis``) writes
its output here for the candidate-generation stage to consume.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("caliber_refinement_jobs") as batch_op:
        batch_op.add_column(sa.Column("diagnosis", sa.JSON, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("caliber_refinement_jobs") as batch_op:
        batch_op.drop_column("diagnosis")
