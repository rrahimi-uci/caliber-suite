"""eval dataset file join table

Revision ID: 0024
Revises: 0023
Create Date: 2026-06-04

Adds caliber_eval_dataset_files: the join/role table linking dataset examples to
physical files in caliber_workflow_files, with the artifact-comparison match spec.
Additive.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | Sequence[str] | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_eval_dataset_files",
        sa.Column("dataset_file_id", sa.String(64), primary_key=True),
        sa.Column("dataset_id", sa.String(64), nullable=False),
        sa.Column("example_id", sa.String(64), nullable=True),
        sa.Column("role", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column(
            "file_id",
            sa.String(64),
            sa.ForeignKey("caliber_workflow_files.file_id"),
            nullable=False,
        ),
        sa.Column("match_spec", sa.JSON, nullable=True),
        sa.Column("metadata", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_eval_dataset_files_dataset",
        "caliber_eval_dataset_files",
        ["dataset_id", "example_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_eval_dataset_files_dataset", table_name="caliber_eval_dataset_files"
    )
    op.drop_table("caliber_eval_dataset_files")
