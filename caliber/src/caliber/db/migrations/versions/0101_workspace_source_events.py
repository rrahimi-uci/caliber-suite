"""add Workspace source webhook inbox and actor links

P4-E persistence slice: retain the normalized, signature-verified delivery
identity and the provider-principal links needed by later review attestation.
Raw provider payloads and credentials remain outside these tables.

Revision ID: 0101
Revises: 0100
Create Date: 2026-09-17
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0101"
down_revision: str | Sequence[str] | None = "0100"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_workspace_source_events",
        sa.Column("source_event_id", sa.String(64), primary_key=True),
        sa.Column(
            "source_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_sources.source_id"),
            nullable=False,
        ),
        sa.Column("provider_delivery_id", sa.String(256), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("repository_id", sa.String(256), nullable=False),
        sa.Column("payload_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="received"),
        sa.Column("received_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "source_id", "provider_delivery_id", name="uq_workspace_source_event_delivery"
        ),
        sa.CheckConstraint(
            "status IN ('received', 'processed', 'failed')",
            name="ck_workspace_source_event_status",
        ),
    )
    op.create_index(
        "ix_workspace_source_events_source_status",
        "caliber_workspace_source_events",
        ["source_id", "status"],
    )

    op.create_table(
        "caliber_workspace_source_actor_links",
        sa.Column("source_actor_link_id", sa.String(64), primary_key=True),
        sa.Column(
            "project_id",
            sa.String(64),
            sa.ForeignKey("caliber_projects.project_id"),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            sa.String(64),
            sa.ForeignKey("caliber_workspace_sources.source_id"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_host", sa.String(256), nullable=False),
        sa.Column("provider_subject_id", sa.String(256), nullable=False),
        sa.Column("caliber_user_id", sa.String(256), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("verified_method", sa.String(64), nullable=False),
        sa.Column("verified_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.String(256), nullable=True),
        sa.Column("revocation_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('active', 'revoked')",
            name="ck_workspace_source_actor_link_status",
        ),
    )
    op.create_index(
        "uq_workspace_source_actor_link_active",
        "caliber_workspace_source_actor_links",
        ["source_id", "provider_subject_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_workspace_source_actor_links_project_status",
        "caliber_workspace_source_actor_links",
        ["project_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_workspace_source_actor_links_project_status",
        table_name="caliber_workspace_source_actor_links",
    )
    op.drop_index(
        "uq_workspace_source_actor_link_active",
        table_name="caliber_workspace_source_actor_links",
    )
    op.drop_table("caliber_workspace_source_actor_links")
    op.drop_index(
        "ix_workspace_source_events_source_status",
        table_name="caliber_workspace_source_events",
    )
    op.drop_table("caliber_workspace_source_events")
