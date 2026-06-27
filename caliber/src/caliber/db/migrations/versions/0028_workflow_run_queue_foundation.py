"""workflow run queue foundation tables and lifecycle columns

Revision ID: 0028
Revises: 0027
Create Date: 2026-06-06

Adds queue/runtime lifecycle fields to ``caliber_workflow_runs`` and introduces
durable run-events, checkpoints, and runtime-approval tables used by async
workflow-run APIs.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0028"
down_revision: str | Sequence[str] | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("caliber_workflow_runs") as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=sa.String(length=16),
            type_=sa.String(length=32),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "started_at",
            existing_type=sa.DateTime(),
            nullable=True,
        )
        batch_op.add_column(sa.Column("project_id", sa.String(length=64), nullable=True))
        batch_op.add_column(
            sa.Column("tenant_id", sa.String(length=64), nullable=False, server_default="local")
        )
        batch_op.add_column(
            sa.Column("source", sa.String(length=32), nullable=False, server_default="manual")
        )
        batch_op.add_column(
            sa.Column("priority", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.add_column(
            sa.Column("queued_at", sa.DateTime(), nullable=False, server_default=sa.func.now())
        )
        batch_op.add_column(sa.Column("claimed_by", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("claimed_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("lease_expires_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column("attempt_number", sa.Integer(), nullable=False, server_default="1")
        )
        batch_op.add_column(sa.Column("parent_run_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("cancel_requested_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("cancel_requested_by", sa.String(length=256), nullable=True))
        batch_op.add_column(sa.Column("cancel_reason", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("current_node_id", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("idempotency_key", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("input_file_ref", sa.String(length=1024), nullable=True))
        batch_op.add_column(sa.Column("error_code", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("error_summary", sa.Text(), nullable=True))
        batch_op.create_index(
            "ix_workflow_runs_queue_claim",
            ["status", "priority", "queued_at"],
        )
        batch_op.create_index(
            "ix_workflow_runs_lease",
            ["status", "lease_expires_at"],
        )

    op.execute(
        sa.text(
            "UPDATE caliber_workflow_runs "
            "SET queued_at = COALESCE(started_at, queued_at, CURRENT_TIMESTAMP)"
        )
    )

    op.create_index(
        "ix_workflow_runs_idempotency",
        "caliber_workflow_runs",
        ["workflow_id", "source", "idempotency_key"],
        unique=True,
        sqlite_where=sa.text("idempotency_key IS NOT NULL"),
        postgresql_where=sa.text("idempotency_key IS NOT NULL"),
    )

    op.create_table(
        "caliber_workflow_run_events",
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column(
            "workflow_run_id",
            sa.String(length=64),
            sa.ForeignKey("caliber_workflow_runs.workflow_run_id"),
            nullable=False,
        ),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("node_id", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint("workflow_run_id", "sequence", name="uq_workflow_run_event_sequence"),
    )
    op.create_index(
        "ix_workflow_run_events_run_sequence",
        "caliber_workflow_run_events",
        ["workflow_run_id", "sequence"],
    )
    op.create_index(
        "ix_workflow_run_events_run_created",
        "caliber_workflow_run_events",
        ["workflow_run_id", "created_at"],
    )
    op.create_index(
        "ix_workflow_run_events_type_created",
        "caliber_workflow_run_events",
        ["event_type", "created_at"],
    )

    op.create_table(
        "caliber_workflow_run_checkpoints",
        sa.Column("checkpoint_id", sa.String(length=64), nullable=False),
        sa.Column(
            "workflow_run_id",
            sa.String(length=64),
            sa.ForeignKey("caliber_workflow_runs.workflow_run_id"),
            nullable=False,
        ),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("node_id", sa.String(length=128), nullable=False),
        sa.Column("state_blob", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("checkpoint_id"),
        sa.UniqueConstraint(
            "workflow_run_id",
            "sequence",
            name="uq_workflow_run_checkpoint_sequence",
        ),
    )
    op.create_index(
        "ix_workflow_run_checkpoints_run_sequence",
        "caliber_workflow_run_checkpoints",
        ["workflow_run_id", "sequence"],
    )
    op.create_index(
        "ix_workflow_run_checkpoints_run_created",
        "caliber_workflow_run_checkpoints",
        ["workflow_run_id", "created_at"],
    )

    op.create_table(
        "caliber_runtime_approval_requests",
        sa.Column("runtime_approval_id", sa.String(length=64), nullable=False),
        sa.Column(
            "workflow_run_id",
            sa.String(length=64),
            sa.ForeignKey("caliber_workflow_runs.workflow_run_id"),
            nullable=False,
        ),
        sa.Column("project_id", sa.String(length=64), nullable=True),
        sa.Column("node_id", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("requested_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.Column("decided_by", sa.String(length=256), nullable=True),
        sa.Column("decision_reason", sa.Text(), nullable=True),
        sa.Column("policy_snapshot", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("runtime_approval_id"),
    )
    op.create_index(
        "ix_runtime_approvals_run_status",
        "caliber_runtime_approval_requests",
        ["workflow_run_id", "status"],
    )
    op.create_index(
        "ix_runtime_approvals_project_status",
        "caliber_runtime_approval_requests",
        ["project_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_runtime_approvals_project_status", table_name="caliber_runtime_approval_requests"
    )
    op.drop_index("ix_runtime_approvals_run_status", table_name="caliber_runtime_approval_requests")
    op.drop_table("caliber_runtime_approval_requests")

    op.drop_index(
        "ix_workflow_run_checkpoints_run_created", table_name="caliber_workflow_run_checkpoints"
    )
    op.drop_index(
        "ix_workflow_run_checkpoints_run_sequence", table_name="caliber_workflow_run_checkpoints"
    )
    op.drop_table("caliber_workflow_run_checkpoints")

    op.drop_index("ix_workflow_run_events_type_created", table_name="caliber_workflow_run_events")
    op.drop_index("ix_workflow_run_events_run_created", table_name="caliber_workflow_run_events")
    op.drop_index("ix_workflow_run_events_run_sequence", table_name="caliber_workflow_run_events")
    op.drop_table("caliber_workflow_run_events")

    op.drop_index("ix_workflow_runs_idempotency", table_name="caliber_workflow_runs")

    op.execute(
        sa.text(
            "UPDATE caliber_workflow_runs "
            "SET started_at = COALESCE(started_at, queued_at, CURRENT_TIMESTAMP)"
        )
    )

    with op.batch_alter_table("caliber_workflow_runs") as batch_op:
        batch_op.drop_index("ix_workflow_runs_lease")
        batch_op.drop_index("ix_workflow_runs_queue_claim")
        batch_op.drop_column("error_summary")
        batch_op.drop_column("error_code")
        batch_op.drop_column("input_file_ref")
        batch_op.drop_column("idempotency_key")
        batch_op.drop_column("current_node_id")
        batch_op.drop_column("cancel_reason")
        batch_op.drop_column("cancel_requested_by")
        batch_op.drop_column("cancel_requested_at")
        batch_op.drop_column("parent_run_id")
        batch_op.drop_column("attempt_number")
        batch_op.drop_column("last_heartbeat_at")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("claimed_at")
        batch_op.drop_column("claimed_by")
        batch_op.drop_column("queued_at")
        batch_op.drop_column("priority")
        batch_op.drop_column("source")
        batch_op.drop_column("tenant_id")
        batch_op.drop_column("project_id")
        batch_op.alter_column(
            "started_at",
            existing_type=sa.DateTime(),
            nullable=False,
        )
        batch_op.alter_column(
            "status",
            existing_type=sa.String(length=32),
            type_=sa.String(length=16),
            existing_nullable=False,
        )
