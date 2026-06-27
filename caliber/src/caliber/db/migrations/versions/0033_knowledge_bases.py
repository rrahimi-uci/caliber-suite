"""knowledge bases for chunking, embeddings, and RAG

Revision ID: 0033
Revises: 0032
Create Date: 2026-06-12

Adds first-class knowledge-base tables for versioned chunking runs, document
lineage, stored embeddings, pipeline logs, and retrieval-ready chunk records.
The design keeps every build immutable: a logical knowledge base points at an
active version, while each build stores its own source manifest, outputs, and
run telemetry.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0033"
down_revision: str | Sequence[str] | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_knowledge_bases",
        sa.Column("project_id", sa.String(64), nullable=True),
        sa.Column("visibility", sa.String(16), nullable=False, server_default="project"),
        sa.Column("knowledge_base_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("owner", sa.String(256), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("source_bucket", sa.String(256), nullable=False),
        sa.Column("source_manifest", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("source_fingerprint", sa.String(64), nullable=False, server_default=""),
        sa.Column("active_version_id", sa.String(64), nullable=True),
        sa.Column("last_run_id", sa.String(64), nullable=True),
        sa.Column("last_run_status", sa.String(32), nullable=True),
        sa.Column("last_run_completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_knowledge_bases_owner_status",
        "caliber_knowledge_bases",
        ["owner", "status"],
    )
    op.create_index(
        "ix_knowledge_bases_project_status",
        "caliber_knowledge_bases",
        ["project_id", "status"],
    )

    op.create_table(
        "caliber_knowledge_base_versions",
        sa.Column("knowledge_base_version_id", sa.String(64), primary_key=True),
        sa.Column(
            "knowledge_base_id",
            sa.String(64),
            sa.ForeignKey("caliber_knowledge_bases.knowledge_base_id"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="processing"),
        sa.Column("chunking_strategy", sa.String(64), nullable=False),
        sa.Column("chunking_config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("embedding_provider", sa.String(32), nullable=False, server_default="huggingface"),
        sa.Column("embedding_model", sa.String(256), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=True),
        sa.Column("source_manifest", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("source_fingerprint", sa.String(64), nullable=False, server_default=""),
        sa.Column("output_bucket", sa.String(256), nullable=False),
        sa.Column("output_prefix", sa.String(1024), nullable=False),
        sa.Column("chunks_uri", sa.String(2048), nullable=True),
        sa.Column("manifest_uri", sa.String(2048), nullable=True),
        sa.Column("logs_uri", sa.String(2048), nullable=True),
        sa.Column("stats_uri", sa.String(2048), nullable=True),
        sa.Column("summary", sa.JSON(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "knowledge_base_id",
            "version_number",
            name="uq_knowledge_base_version_number",
        ),
    )
    op.create_index(
        "ix_knowledge_base_versions_lookup",
        "caliber_knowledge_base_versions",
        ["knowledge_base_id", "version_number"],
    )
    op.create_index(
        "ix_knowledge_base_versions_status_created",
        "caliber_knowledge_base_versions",
        ["status", "created_at"],
    )

    op.create_table(
        "caliber_knowledge_base_sources",
        sa.Column("knowledge_base_source_id", sa.String(64), primary_key=True),
        sa.Column(
            "knowledge_base_version_id",
            sa.String(64),
            sa.ForeignKey("caliber_knowledge_base_versions.knowledge_base_version_id"),
            nullable=False,
        ),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("selection_kind", sa.String(16), nullable=False),
        sa.Column("bucket", sa.String(256), nullable=False),
        sa.Column("object_key", sa.String(2048), nullable=False),
        sa.Column("object_name", sa.String(512), nullable=False),
        sa.Column("object_store_path", sa.String(2048), nullable=False),
        sa.Column("content_type", sa.String(255), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("etag", sa.String(256), nullable=True),
        sa.Column("last_modified", sa.DateTime(), nullable=True),
        sa.Column("extracted_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("extracted_format", sa.String(32), nullable=True),
        sa.Column("ocr_used", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.String(24), nullable=False, server_default="processed"),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_knowledge_base_sources_version",
        "caliber_knowledge_base_sources",
        ["knowledge_base_version_id", "document_id"],
    )
    op.create_index(
        "ix_knowledge_base_sources_bucket_key",
        "caliber_knowledge_base_sources",
        ["bucket", "object_key"],
    )

    op.create_table(
        "caliber_knowledge_base_chunks",
        sa.Column("knowledge_base_chunk_id", sa.String(64), primary_key=True),
        sa.Column(
            "knowledge_base_version_id",
            sa.String(64),
            sa.ForeignKey("caliber_knowledge_base_versions.knowledge_base_version_id"),
            nullable=False,
        ),
        sa.Column("document_id", sa.String(64), nullable=False),
        sa.Column("source_bucket", sa.String(256), nullable=False),
        sa.Column("source_key", sa.String(2048), nullable=False),
        sa.Column("source_name", sa.String(512), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("token_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("start_index", sa.Integer(), nullable=True),
        sa.Column("end_index", sa.Integer(), nullable=True),
        sa.Column("embedding", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index(
        "ix_knowledge_base_chunks_version_ordinal",
        "caliber_knowledge_base_chunks",
        ["knowledge_base_version_id", "ordinal"],
    )
    op.create_index(
        "ix_knowledge_base_chunks_version_document",
        "caliber_knowledge_base_chunks",
        ["knowledge_base_version_id", "document_id", "chunk_index"],
    )

    op.create_table(
        "caliber_knowledge_base_runs",
        sa.Column("knowledge_base_run_id", sa.String(64), primary_key=True),
        sa.Column(
            "knowledge_base_id",
            sa.String(64),
            sa.ForeignKey("caliber_knowledge_bases.knowledge_base_id"),
            nullable=False,
        ),
        sa.Column(
            "knowledge_base_version_id",
            sa.String(64),
            sa.ForeignKey("caliber_knowledge_base_versions.knowledge_base_version_id"),
            nullable=False,
        ),
        sa.Column("status", sa.String(24), nullable=False, server_default="running"),
        sa.Column("source_manifest", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("log_line_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_knowledge_base_runs_base_created",
        "caliber_knowledge_base_runs",
        ["knowledge_base_id", "created_at"],
    )
    op.create_index(
        "ix_knowledge_base_runs_status_created",
        "caliber_knowledge_base_runs",
        ["status", "created_at"],
    )

    op.create_table(
        "caliber_knowledge_base_run_events",
        sa.Column("event_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "knowledge_base_run_id",
            sa.String(64),
            sa.ForeignKey("caliber_knowledge_base_runs.knowledge_base_run_id"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "knowledge_base_run_id",
            "sequence",
            name="uq_knowledge_base_run_event_sequence",
        ),
    )
    op.create_index(
        "ix_knowledge_base_run_events_run_sequence",
        "caliber_knowledge_base_run_events",
        ["knowledge_base_run_id", "sequence"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_base_run_events_run_sequence", table_name="caliber_knowledge_base_run_events")
    op.drop_table("caliber_knowledge_base_run_events")

    op.drop_index("ix_knowledge_base_runs_status_created", table_name="caliber_knowledge_base_runs")
    op.drop_index("ix_knowledge_base_runs_base_created", table_name="caliber_knowledge_base_runs")
    op.drop_table("caliber_knowledge_base_runs")

    op.drop_index("ix_knowledge_base_chunks_version_document", table_name="caliber_knowledge_base_chunks")
    op.drop_index("ix_knowledge_base_chunks_version_ordinal", table_name="caliber_knowledge_base_chunks")
    op.drop_table("caliber_knowledge_base_chunks")

    op.drop_index("ix_knowledge_base_sources_bucket_key", table_name="caliber_knowledge_base_sources")
    op.drop_index("ix_knowledge_base_sources_version", table_name="caliber_knowledge_base_sources")
    op.drop_table("caliber_knowledge_base_sources")

    op.drop_index("ix_knowledge_base_versions_status_created", table_name="caliber_knowledge_base_versions")
    op.drop_index("ix_knowledge_base_versions_lookup", table_name="caliber_knowledge_base_versions")
    op.drop_table("caliber_knowledge_base_versions")

    op.drop_index("ix_knowledge_bases_project_status", table_name="caliber_knowledge_bases")
    op.drop_index("ix_knowledge_bases_owner_status", table_name="caliber_knowledge_bases")
    op.drop_table("caliber_knowledge_bases")
