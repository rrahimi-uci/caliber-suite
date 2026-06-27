"""knowledge graph artifacts and queued knowledge-base builds

Revision ID: 0034
Revises: 0033
Create Date: 2026-06-12

Extends the knowledge-base build system with:

* durable queue / lease columns on build runs so work can resume safely after
  restarts;
* version artifact URIs for entity, relationship, and graph outputs; and
* first-class entity / relationship tables that preserve document-to-chunk
  lineage for future GraphRAG and retrieval strategies.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str | Sequence[str] | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "caliber_knowledge_base_versions",
        sa.Column("entities_uri", sa.String(2048), nullable=True),
    )
    op.add_column(
        "caliber_knowledge_base_versions",
        sa.Column("relationships_uri", sa.String(2048), nullable=True),
    )
    op.add_column(
        "caliber_knowledge_base_versions",
        sa.Column("graph_uri", sa.String(2048), nullable=True),
    )

    op.add_column(
        "caliber_knowledge_base_runs",
        sa.Column("queued_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "caliber_knowledge_base_runs",
        sa.Column("claimed_by", sa.String(128), nullable=True),
    )
    op.add_column(
        "caliber_knowledge_base_runs",
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "caliber_knowledge_base_runs",
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "caliber_knowledge_base_runs",
        sa.Column("last_heartbeat_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_knowledge_base_runs_status_queued",
        "caliber_knowledge_base_runs",
        ["status", "queued_at"],
    )

    op.create_table(
        "caliber_knowledge_base_entities",
        sa.Column("knowledge_base_entity_id", sa.String(64), primary_key=True),
        sa.Column(
            "knowledge_base_version_id",
            sa.String(64),
            sa.ForeignKey("caliber_knowledge_base_versions.knowledge_base_version_id"),
            nullable=False,
        ),
        sa.Column("entity_key", sa.String(256), nullable=False),
        sa.Column("label", sa.String(512), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False, server_default="term"),
        sa.Column("aliases", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("mention_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("source_documents", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("source_keys", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("source_chunks", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "knowledge_base_version_id",
            "entity_key",
            name="uq_knowledge_base_entity_key",
        ),
    )
    op.create_index(
        "ix_knowledge_base_entities_version_mentions",
        "caliber_knowledge_base_entities",
        ["knowledge_base_version_id", "mention_count"],
    )

    op.create_table(
        "caliber_knowledge_base_relationships",
        sa.Column("knowledge_base_relationship_id", sa.String(64), primary_key=True),
        sa.Column(
            "knowledge_base_version_id",
            sa.String(64),
            sa.ForeignKey("caliber_knowledge_base_versions.knowledge_base_version_id"),
            nullable=False,
        ),
        sa.Column(
            "source_entity_id",
            sa.String(64),
            sa.ForeignKey("caliber_knowledge_base_entities.knowledge_base_entity_id"),
            nullable=False,
        ),
        sa.Column(
            "target_entity_id",
            sa.String(64),
            sa.ForeignKey("caliber_knowledge_base_entities.knowledge_base_entity_id"),
            nullable=False,
        ),
        sa.Column("relationship_type", sa.String(64), nullable=False, server_default="co_occurs"),
        sa.Column("weight", sa.Float(), nullable=False, server_default="0"),
        sa.Column("evidence_chunk_ids", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("source_documents", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("metadata", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "knowledge_base_version_id",
            "source_entity_id",
            "target_entity_id",
            "relationship_type",
            name="uq_knowledge_base_relationship_edge",
        ),
    )
    op.create_index(
        "ix_knowledge_base_relationships_version_weight",
        "caliber_knowledge_base_relationships",
        ["knowledge_base_version_id", "weight"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_knowledge_base_relationships_version_weight",
        table_name="caliber_knowledge_base_relationships",
    )
    op.drop_table("caliber_knowledge_base_relationships")

    op.drop_index(
        "ix_knowledge_base_entities_version_mentions",
        table_name="caliber_knowledge_base_entities",
    )
    op.drop_table("caliber_knowledge_base_entities")

    op.drop_index(
        "ix_knowledge_base_runs_status_queued",
        table_name="caliber_knowledge_base_runs",
    )
    op.drop_column("caliber_knowledge_base_runs", "last_heartbeat_at")
    op.drop_column("caliber_knowledge_base_runs", "lease_expires_at")
    op.drop_column("caliber_knowledge_base_runs", "claimed_at")
    op.drop_column("caliber_knowledge_base_runs", "claimed_by")
    op.drop_column("caliber_knowledge_base_runs", "queued_at")

    op.drop_column("caliber_knowledge_base_versions", "graph_uri")
    op.drop_column("caliber_knowledge_base_versions", "relationships_uri")
    op.drop_column("caliber_knowledge_base_versions", "entities_uri")
