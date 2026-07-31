"""arbitrate one open incident per objective in the database

Revision ID: 0079
Revises: 0078
Create Date: 2026-07-31

The original incident service used a read-before-insert check. Two replicas could both
observe no open row, insert one, and publish duplicate ``opened`` transitions. A partial
unique index makes SQLite and PostgreSQL choose one winner.

Existing duplicate rows cannot be repaired automatically without inventing which incident
is authoritative. The preflight therefore stops the migration with the affected objectives
and leaves operator-owned history untouched.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0079"
down_revision: str | Sequence[str] | None = "0078"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX_NAME = "uq_caliber_incidents_open_objective"


def upgrade() -> None:
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                """
            SELECT objective, COUNT(*) AS open_count
            FROM caliber_incidents
            WHERE status = 'open'
            GROUP BY objective
            HAVING COUNT(*) > 1
            ORDER BY objective
            LIMIT 10
            """
            )
        )
        .all()
    )
    if duplicates:
        preview = ", ".join(f"{row.objective!r} ({row.open_count})" for row in duplicates)
        raise RuntimeError(
            "cannot enforce one open incident per objective; resolve duplicate open "
            f"incidents first: {preview}"
        )

    op.create_index(
        _INDEX_NAME,
        "caliber_incidents",
        ["objective"],
        unique=True,
        sqlite_where=sa.text("status = 'open'"),
        postgresql_where=sa.text("status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="caliber_incidents")
