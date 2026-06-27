"""eval datasets

Revision ID: 0011
Revises: 0010
Create Date: 2026-05-15

Adds the eval-dataset tables — ``caliber_eval_datasets`` and
``caliber_eval_dataset_examples``. A dataset is a versioned set of
input/expected pairs the refinement pipeline scores candidates
against; examples are append-only so historical job runs stay
auditable.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_eval_datasets",
        sa.Column("dataset_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("owner", sa.String(256), nullable=False),
        sa.Column("tags", sa.JSON, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
        sa.UniqueConstraint("name", name="uq_eval_dataset_name"),
    )
    op.create_table(
        "caliber_eval_dataset_examples",
        sa.Column("example_id", sa.String(64), primary_key=True),
        sa.Column(
            "dataset_id",
            sa.String(64),
            sa.ForeignKey("caliber_eval_datasets.dataset_id"),
            nullable=False,
        ),
        sa.Column("dataset_version", sa.Integer, nullable=False),
        sa.Column("input", sa.JSON, nullable=False),
        sa.Column("expected", sa.JSON, nullable=False),
        sa.Column("weight", sa.Float, nullable=False, server_default="1.0"),
        sa.Column("tags", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("superseded_at", sa.DateTime, nullable=True),
    )
    op.create_index(
        "ix_eval_example_dataset",
        "caliber_eval_dataset_examples",
        ["dataset_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_eval_example_dataset", table_name="caliber_eval_dataset_examples")
    op.drop_table("caliber_eval_dataset_examples")
    op.drop_table("caliber_eval_datasets")
