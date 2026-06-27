"""assistant authoring

Revision ID: 0019
Revises: 0018
Create Date: 2026-06-01

Adds the five tables for Caliber Assistant agentic authoring:
sessions, messages, drafts, runs, and publish events.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_assistant_sessions",
        sa.Column("session_id", sa.String(64), primary_key=True),
        sa.Column("title", sa.String(256), nullable=False, server_default=""),
        sa.Column("owner", sa.String(256), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("goal", sa.Text, nullable=False, server_default=""),
        sa.Column("active_draft_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    )

    op.create_table(
        "caliber_assistant_messages",
        sa.Column("message_id", sa.String(64), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("caliber_assistant_sessions.session_id"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text, nullable=False, server_default=""),
        sa.Column("metadata", sa.JSON, nullable=True),
        sa.Column("sequence_number", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.UniqueConstraint("session_id", "sequence_number", name="uq_asst_msg_seq"),
    )
    op.create_index("ix_asst_msg_session", "caliber_assistant_messages", ["session_id"])

    op.create_table(
        "caliber_assistant_drafts",
        sa.Column("draft_id", sa.String(64), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("caliber_assistant_sessions.session_id"),
            nullable=False,
        ),
        sa.Column("artifact_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("title", sa.String(256), nullable=False, server_default=""),
        sa.Column("summary", sa.Text, nullable=False, server_default=""),
        sa.Column("spec", sa.JSON, nullable=True),
        sa.Column("artifact", sa.JSON, nullable=True),
        sa.Column("validation_report", sa.JSON, nullable=True),
        sa.Column("test_report", sa.JSON, nullable=True),
        sa.Column("target_registry_id", sa.String(64), nullable=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("updated_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index("ix_asst_draft_session", "caliber_assistant_drafts", ["session_id"])

    op.create_table(
        "caliber_assistant_runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("caliber_assistant_sessions.session_id"),
            nullable=False,
        ),
        sa.Column("draft_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="running"),
        sa.Column("engine", sa.String(32), nullable=False, server_default="fake"),
        sa.Column("model", sa.String(128), nullable=False, server_default=""),
        sa.Column("input_summary", sa.Text, nullable=False, server_default=""),
        sa.Column("output_summary", sa.Text, nullable=False, server_default=""),
        sa.Column("trace_id", sa.String(128), nullable=True),
        sa.Column("mlflow_run_id", sa.String(128), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("started_at", sa.DateTime, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime, nullable=True),
    )
    op.create_index("ix_asst_run_session", "caliber_assistant_runs", ["session_id"])

    op.create_table(
        "caliber_assistant_publish_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column(
            "draft_id",
            sa.String(64),
            sa.ForeignKey("caliber_assistant_drafts.draft_id"),
            nullable=False,
        ),
        sa.Column("artifact_type", sa.String(32), nullable=False),
        sa.Column("target_registry_id", sa.String(64), nullable=False),
        sa.Column("target_version", sa.String(64), nullable=True),
        sa.Column("approved_by", sa.String(256), nullable=False),
        sa.Column("published_by", sa.String(256), nullable=False),
        sa.Column("publish_report", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_asst_pub_draft", "caliber_assistant_publish_events", ["draft_id"],
    )


def downgrade() -> None:
    op.drop_table("caliber_assistant_publish_events")
    op.drop_table("caliber_assistant_runs")
    op.drop_table("caliber_assistant_drafts")
    op.drop_table("caliber_assistant_messages")
    op.drop_table("caliber_assistant_sessions")
