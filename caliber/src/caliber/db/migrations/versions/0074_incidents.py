"""incident history for SLO objectives

Revision ID: 0074
Revises: 0073
Create Date: 2026-07-30

SLO *detection* already existed: ``observability/slo.py`` evaluates objectives and returns
``AlertState`` for each, and ``/system/slo`` renders them. What did not exist was any
memory. Every review recorded "no alert routing, escalation, silencing, incident history"
as open, and the missing half was state: a breach was only ever visible to whoever
happened to poll the endpoint while it was still true.

An incident is opened the first time an objective fires and resolved when it stops. That
turns a stateless gauge into a record you can ask questions of — when did this start, how
long did it last, has it happened before, is it happening right now.

``objective`` is the natural key for the open incident: one objective has at most one open
incident at a time, enforced by a partial-style uniqueness check in the service rather than
a DB constraint, because SQLite and PostgreSQL disagree on partial unique indexes and the
service already serialises the transition.

``silenced_until`` lives here rather than in configuration so silencing an alert does not
require a redeploy — the operator action during an incident is exactly when editing
environment variables is least appropriate.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0074"
down_revision: str | Sequence[str] | None = "0073"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_incidents",
        sa.Column("incident_id", sa.String(length=64), primary_key=True),
        sa.Column("objective", sa.String(length=256), nullable=False),
        sa.Column("signal", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.String(length=16), nullable=False, server_default="warning"),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("detail", sa.Text(), nullable=False, server_default=""),
        sa.Column("observed", sa.Float(), nullable=True),
        sa.Column("target", sa.Float(), nullable=True),
        sa.Column("opened_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(), nullable=True),
        sa.Column("acknowledged_by", sa.String(length=256), nullable=True),
        sa.Column("silenced_until", sa.DateTime(), nullable=True),
        sa.Column("notified_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_caliber_incidents_objective_status",
        "caliber_incidents",
        ["objective", "status"],
    )
    op.create_index("ix_caliber_incidents_opened_at", "caliber_incidents", ["opened_at"])


def downgrade() -> None:
    op.drop_index("ix_caliber_incidents_opened_at", table_name="caliber_incidents")
    op.drop_index("ix_caliber_incidents_objective_status", table_name="caliber_incidents")
    op.drop_table("caliber_incidents")
