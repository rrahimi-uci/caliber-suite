"""enforce one active GitHub App connection per installation

P4-E webhook-ingress slice: a real GitHub App installation is only ever
legitimately bound to one target, so this makes that a real DB constraint
rather than a structurally ambiguous column. An inbound webhook's untrusted
``installation.id`` claim therefore resolves to at most one active
connection candidate; HMAC signature verification against that candidate's
own stored secret remains the actual trust boundary, never this lookup by
itself.

Revision ID: 0109
Revises: 0108
Create Date: 2026-09-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0109"
down_revision: str | Sequence[str] | None = "0108"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "uq_workspace_source_connection_active_installation",
        "caliber_workspace_source_connections",
        ["provider", "installation_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_workspace_source_connection_active_installation",
        table_name="caliber_workspace_source_connections",
    )
