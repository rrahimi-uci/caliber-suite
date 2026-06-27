"""workflow compiled bundle

Revision ID: 0018
Revises: 0017
Create Date: 2026-05-31

Adds ``caliber_workflow_versions.compiled_bundle`` (plan §13.3): the immutable
compiled artifact stored with each version — generated SDK code, compiler
report, and dependency metadata — so a published version's compiled output is
durable and exportable without recompilation.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("caliber_workflow_versions") as batch_op:
        batch_op.add_column(sa.Column("compiled_bundle", sa.JSON, nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("caliber_workflow_versions") as batch_op:
        batch_op.drop_column("compiled_bundle")
