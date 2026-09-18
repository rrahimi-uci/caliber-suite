"""add durable Workspace environment operation pointers

P5-C makes the environment pointer and operation lock explicit.  The pointer
columns deliberately do not add reverse foreign keys: release and operation
rows already reference the environment, and a bidirectional cycle is not
portable across SQLite metadata creation and PostgreSQL migrations.  The
operation service enforces the same coordinate checks and CAS invariant.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0104"
down_revision: str | Sequence[str] | None = "0103"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("caliber_workspace_environments") as batch:
        batch.add_column(sa.Column("current_release_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("pending_operation_id", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column("operation_state", sa.String(24), nullable=False, server_default="idle")
        )
        batch.add_column(
            sa.Column(
                "policy",
                sa.JSON(),
                nullable=False,
                server_default=sa.text("'{}'"),
            )
        )
        batch.add_column(
            sa.Column("policy_sha256", sa.String(64), nullable=False, server_default="")
        )
        batch.add_column(
            sa.Column("lock_version", sa.Integer(), nullable=False, server_default="1")
        )
        batch.create_check_constraint(
            "ck_workspace_environment_operation_state",
            "operation_state IN ('idle', 'applying', 'reconcile_required')",
        )
        batch.create_check_constraint(
            "ck_workspace_environment_lock_version",
            "lock_version >= 1",
        )
        batch.create_index(
            "ix_workspace_environments_pending_operation",
            ["pending_operation_id"],
        )
        batch.create_index(
            "ix_workspace_environments_current_release",
            ["current_release_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("caliber_workspace_environments") as batch:
        batch.drop_index("ix_workspace_environments_current_release")
        batch.drop_index("ix_workspace_environments_pending_operation")
        batch.drop_constraint("ck_workspace_environment_lock_version", type_="check")
        batch.drop_constraint("ck_workspace_environment_operation_state", type_="check")
        batch.drop_column("lock_version")
        batch.drop_column("policy_sha256")
        batch.drop_column("policy")
        batch.drop_column("operation_state")
        batch.drop_column("pending_operation_id")
        batch.drop_column("current_release_id")
