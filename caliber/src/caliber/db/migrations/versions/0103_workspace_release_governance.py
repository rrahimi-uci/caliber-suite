"""add Workspace release governance bindings and audit severity

P5-B adds the persisted fields needed to enforce digest-bound decisions,
explicitly disabled recovery policy, and high-severity break-glass auditing.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0103"
down_revision: str | Sequence[str] | None = "0102"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_audit_log",
        sa.Column("severity", sa.String(16), nullable=False, server_default="standard"),
    )
    # SQLite cannot ALTER a table constraint in place.  Batch mode keeps the
    # migration valid for both the local SQLite test database and PostgreSQL.
    with op.batch_alter_table("caliber_audit_log") as batch:
        batch.create_check_constraint(
            "ck_audit_log_severity",
            "severity IN ('standard', 'high', 'critical')",
        )
    op.add_column(
        "caliber_workspace_environments",
        sa.Column(
            "recovery_policy_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    with op.batch_alter_table("caliber_workspace_release_decisions") as batch:
        batch.add_column(
            sa.Column(
                "change_request_head_id",
                sa.String(64),
                sa.ForeignKey(
                    "caliber_workspace_change_request_heads.head_id",
                    name="fk_workspace_release_decision_head",
                ),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("caliber_workspace_release_decisions") as batch:
        batch.drop_column("change_request_head_id")
    op.drop_column("caliber_workspace_environments", "recovery_policy_enabled")
    with op.batch_alter_table("caliber_audit_log") as batch:
        batch.drop_constraint("ck_audit_log_severity", type_="check")
    op.drop_column("caliber_audit_log", "severity")
