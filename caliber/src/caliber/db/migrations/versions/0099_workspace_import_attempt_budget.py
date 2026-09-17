"""add bounded Workspace import attempt budgets

P4-B: make known import failures deliberately retryable within a durable total
attempt budget while leaving ambiguous expired work in ``reconcile_required``.

Revision ID: 0099
Revises: 0098
Create Date: 2026-09-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0099"
down_revision: str | Sequence[str] | None = "0098"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_workspace_import_jobs",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "caliber_workspace_import_jobs",
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
    )
    with op.batch_alter_table("caliber_workspace_import_jobs") as batch:
        batch.create_check_constraint(
            "ck_workspace_import_attempt_count",
            "attempt_count >= 0",
        )
        batch.create_check_constraint(
            "ck_workspace_import_max_attempts",
            "max_attempts >= 1",
        )
        batch.create_check_constraint(
            "ck_workspace_import_attempt_budget",
            "attempt_count <= max_attempts",
        )
        batch.create_check_constraint(
            "ck_workspace_import_queued_attempt_budget",
            "status != 'queued' OR attempt_count < max_attempts",
        )


def downgrade() -> None:
    with op.batch_alter_table("caliber_workspace_import_jobs") as batch:
        batch.drop_constraint("ck_workspace_import_queued_attempt_budget", type_="check")
        batch.drop_constraint("ck_workspace_import_attempt_budget", type_="check")
        batch.drop_constraint("ck_workspace_import_max_attempts", type_="check")
        batch.drop_constraint("ck_workspace_import_attempt_count", type_="check")
    op.drop_column("caliber_workspace_import_jobs", "max_attempts")
    op.drop_column("caliber_workspace_import_jobs", "attempt_count")
