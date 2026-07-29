"""encrypted-at-rest secret store with versions, rotation, and revocation

Revision ID: 0067
Revises: 0066
Create Date: 2026-07-28

Closes the durable half of C2. MCP ``env`` / ``headers`` / ``auth_config`` literals
were contained on *read* — the API returned a write-only sentinel — but the values
themselves sat in ordinary JSON columns, so a database dump, backup, or replica
exposed them. There was also no rotation or revocation lifecycle.

``caliber_secrets`` is the named series; ``caliber_secret_versions`` holds
AES-256-GCM ciphertext with a per-version nonce and the secret name bound in as
additional authenticated data, so ciphertext moved between rows fails to
authenticate. ``key_id`` records which data-encryption key wrote each row, which is
what makes key rotation possible.

Nothing is migrated automatically: existing MCP literals stay where they are and keep
working. Moving one is an explicit operator action (store the value, then replace the
literal with a ``secret://name`` reference), because silently relocating a live
credential could break a running integration in a way that is hard to diagnose.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0067"
down_revision: str | Sequence[str] | None = "0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_secrets",
        sa.Column("name", sa.String(length=128), primary_key=True),
        sa.Column("description", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("current_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_by", sa.String(length=256), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_by", sa.String(length=256), nullable=True),
    )
    op.create_table(
        "caliber_secret_versions",
        sa.Column("secret_version_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(length=32), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sa.String(length=32), nullable=True),
        sa.Column("created_by", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("name", "version", name="uq_secret_version"),
    )
    op.create_index("ix_secret_versions_name", "caliber_secret_versions", ["name"])


def downgrade() -> None:
    op.drop_index("ix_secret_versions_name", table_name="caliber_secret_versions")
    op.drop_table("caliber_secret_versions")
    op.drop_table("caliber_secrets")
