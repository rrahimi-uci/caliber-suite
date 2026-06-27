"""jobs and audit log

Revision ID: 0002
Revises: 0001
Create Date: 2025-05-15

Adds ``caliber_refinement_jobs`` and ``caliber_audit_log``. The verify
endpoint and the orchestrator's triage stage both write to these tables;
the latter is the source of truth for "who did what to which artifact when."
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_refinement_jobs",
        sa.Column("job_id", sa.String(64), primary_key=True),
        sa.Column(
            "agent_id",
            sa.String(64),
            sa.ForeignKey("caliber_agent_config.agent_id"),
            nullable=False,
        ),
        sa.Column("workflow_id", sa.String(128), nullable=True),
        sa.Column(
            "primary_item_id",
            sa.String(64),
            sa.ForeignKey("caliber_verification_queue.item_id"),
            nullable=False,
        ),
        sa.Column("mlflow_run_id", sa.String(64), nullable=True),
        sa.Column("artifact_type", sa.String(32), nullable=False),
        sa.Column("optimizer_type", sa.String(32), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("current_stage", sa.String(32), nullable=False, server_default="triage"),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("total_tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("cost_usd", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("bundle_targets", sa.JSON, nullable=False),
        sa.Column(
            "bundle_expansion_count",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime,
            nullable=False,
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_refinement_jobs_status_created",
        "caliber_refinement_jobs",
        ["status", "created_at"],
    )

    op.create_table(
        "caliber_audit_log",
        sa.Column("log_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("actor", sa.String(256), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(128), nullable=False),
        sa.Column("details", sa.JSON, nullable=True),
    )
    op.create_index(
        "ix_audit_log_entity",
        "caliber_audit_log",
        ["entity_type", "entity_id"],
    )
    op.create_index(
        "ix_audit_log_actor_timestamp",
        "caliber_audit_log",
        ["actor", "timestamp"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_log_actor_timestamp", table_name="caliber_audit_log")
    op.drop_index("ix_audit_log_entity", table_name="caliber_audit_log")
    op.drop_table("caliber_audit_log")
    op.drop_index("ix_refinement_jobs_status_created", table_name="caliber_refinement_jobs")
    op.drop_table("caliber_refinement_jobs")
