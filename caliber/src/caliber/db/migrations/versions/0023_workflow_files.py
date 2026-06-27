"""workflow file/workspace storage tables

Revision ID: 0023
Revises: 0022
Create Date: 2026-06-04

Adds caliber_workflow_files (file metadata, source of truth) and
caliber_workflow_file_events (operational telemetry) for the file/workspace
storage subsystem.
Additive — no existing rows change.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_workflow_files",
        sa.Column("file_id", sa.String(64), primary_key=True),
        sa.Column("tenant_id", sa.String(64), nullable=False, server_default="local"),
        sa.Column("project_id", sa.String(64), nullable=False, server_default="default"),
        sa.Column("workflow_id", sa.String(128), nullable=True),
        sa.Column("workflow_version_id", sa.String(64), nullable=True),
        sa.Column("workflow_run_id", sa.String(64), nullable=True),
        sa.Column("playground_run_id", sa.String(64), nullable=True),
        sa.Column("session_id", sa.String(128), nullable=True),
        sa.Column("dataset_id", sa.String(64), nullable=True),
        sa.Column("example_id", sa.String(64), nullable=True),
        sa.Column("parent_file_id", sa.String(64), nullable=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(512), nullable=False),
        sa.Column("relative_path", sa.String(1024), nullable=False),
        sa.Column("file_ref", sa.String(1024), nullable=False),
        sa.Column("storage_backend", sa.String(16), nullable=False),
        sa.Column("storage_uri", sa.String(2048), nullable=False),
        sa.Column("bucket", sa.String(256), nullable=True),
        sa.Column("object_key", sa.String(2048), nullable=False),
        sa.Column("object_version_id", sa.String(256), nullable=True),
        sa.Column("media_type", sa.String(255), nullable=True),
        sa.Column("size_bytes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("etag", sa.String(256), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending_upload"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("producer_node_id", sa.String(128), nullable=True),
        sa.Column("producer_tool_name", sa.String(128), nullable=True),
        sa.Column("created_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("finalized_at", sa.DateTime, nullable=True),
        sa.Column("deleted_at", sa.DateTime, nullable=True),
        sa.Column("metadata", sa.JSON, nullable=True),
        sa.UniqueConstraint("file_ref", name="uq_workflow_files_file_ref"),
    )
    op.create_index(
        "ix_workflow_files_run",
        "caliber_workflow_files",
        ["workflow_run_id", "kind", "relative_path"],
    )
    op.create_index(
        "ix_workflow_files_dataset",
        "caliber_workflow_files",
        ["dataset_id", "example_id"],
    )
    op.create_index(
        "ix_workflow_files_playground",
        "caliber_workflow_files",
        ["playground_run_id"],
    )

    op.create_table(
        "caliber_workflow_file_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("file_id", sa.String(64), nullable=True),
        sa.Column("workflow_run_id", sa.String(64), nullable=True),
        sa.Column("actor", sa.String(256), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("relative_path", sa.String(1024), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("metadata", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_workflow_file_events_file", "caliber_workflow_file_events", ["file_id"]
    )
    op.create_index(
        "ix_workflow_file_events_run",
        "caliber_workflow_file_events",
        ["workflow_run_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workflow_file_events_run", table_name="caliber_workflow_file_events"
    )
    op.drop_index(
        "ix_workflow_file_events_file", table_name="caliber_workflow_file_events"
    )
    op.drop_table("caliber_workflow_file_events")
    op.drop_index("ix_workflow_files_playground", table_name="caliber_workflow_files")
    op.drop_index("ix_workflow_files_dataset", table_name="caliber_workflow_files")
    op.drop_index("ix_workflow_files_run", table_name="caliber_workflow_files")
    op.drop_table("caliber_workflow_files")
