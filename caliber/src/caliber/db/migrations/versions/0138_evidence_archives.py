"""Persist scope-bound evidence archive locators.

The archive bytes remain in ``StorageBackend``.  This migration adds the
database source of truth for the object key, canonical view digest and
authorization scope used to select/cache an exported package.  Expiry and
tombstone columns are additive preparation for B14.4; this migration does not
run retention or delete objects.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0138"
down_revision: str | Sequence[str] | None = "0137"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_evidence_archives",
        sa.Column("archive_id", sa.String(length=64), nullable=False),
        sa.Column("workflow_run_id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("project_id", sa.String(length=64), nullable=False),
        sa.Column("package_id", sa.String(length=128), nullable=False),
        sa.Column("package_sha256", sa.String(length=64), nullable=False),
        sa.Column("authorization_scope_digest", sa.String(length=64), nullable=False),
        sa.Column("member_ids_json", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("storage_backend", sa.String(length=16), nullable=False),
        sa.Column("storage_uri", sa.String(length=2048), nullable=False),
        sa.Column("bucket", sa.String(length=256), nullable=True),
        sa.Column("object_key", sa.String(length=2048), nullable=False),
        sa.Column("object_version_id", sa.String(length=256), nullable=True),
        sa.Column("archive_sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("exported_by", sa.String(length=256), nullable=False),
        sa.Column("exported_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=True),
        sa.Column("tombstone_json", sa.JSON(), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"], ["caliber_projects.project_id"], name="fk_evidence_archives_project"
        ),
        sa.ForeignKeyConstraint(
            ["workflow_run_id"],
            ["caliber_workflow_runs.workflow_run_id"],
            name="fk_evidence_archives_run",
        ),
        sa.PrimaryKeyConstraint("archive_id"),
        sa.UniqueConstraint(
            "workflow_run_id",
            "package_sha256",
            "authorization_scope_digest",
            name="uq_evidence_archive_scope_view",
        ),
        sa.CheckConstraint(
            "length(package_sha256) = 64", name="ck_evidence_archive_package_digest"
        ),
        sa.CheckConstraint(
            "length(authorization_scope_digest) = 64",
            name="ck_evidence_archive_scope_digest",
        ),
        sa.CheckConstraint(
            "length(archive_sha256) = 64", name="ck_evidence_archive_archive_digest"
        ),
        sa.CheckConstraint("size_bytes >= 0", name="ck_evidence_archive_size"),
        sa.CheckConstraint("length(object_key) > 0", name="ck_evidence_archive_object_key"),
    )
    op.create_index(
        "ix_evidence_archives_run",
        "caliber_evidence_archives",
        ["workflow_run_id", "exported_at"],
    )
    op.create_index(
        "ix_evidence_archives_expiry",
        "caliber_evidence_archives",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_archives_expiry", table_name="caliber_evidence_archives")
    op.drop_index("ix_evidence_archives_run", table_name="caliber_evidence_archives")
    op.drop_table("caliber_evidence_archives")
