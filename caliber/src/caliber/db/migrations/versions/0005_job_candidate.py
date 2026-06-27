"""job candidate column

Revision ID: 0005
Revises: 0004
Create Date: 2025-05-15

Adds the ``candidate`` JSON column to ``caliber_refinement_jobs``. The
candidate-generation stage (``caliber.orchestrator.candidate.run_candidate``)
writes its output here for the Eval and Approval stages to consume.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("caliber_refinement_jobs") as batch_op:
        batch_op.add_column(sa.Column("candidate", sa.JSON, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("caliber_refinement_jobs") as batch_op:
        batch_op.drop_column("candidate")
