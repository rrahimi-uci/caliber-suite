"""durable accept record so abrupt process loss does not lose webhook events

Revision ID: 0073
Revises: 0072
Create Date: 2026-07-30

Graceful shutdown already drains queued and in-flight events into
``caliber_webhook_dead_letters``. Abrupt loss — ``SIGKILL``, an OOM kill, a container
eviction — does not run that path, and both the pending queue and the in-flight map live
only in memory. Every review since the dead-letter work landed has recorded this as the
remaining half: an operator could still not distinguish "nothing happened" from "we failed
to tell you", for exactly the failure mode where it matters most.

One row per (event, url) written when the event is accepted, deleted when that target
settles — delivered, exhausted, or dead-lettered. Anything still present at startup is by
definition an event the previous process accepted and never finished, so the dispatcher
sweeps it into the dead-letter record on boot.

Writes are confined to deployments that configured webhooks at all: the dispatcher is
inert without both URLs and a secret, so a deployment not using webhooks pays nothing.

Note the ordering requirement this table encodes: the accept row must be committed
*before* the event is queued. Written afterwards, a crash in between would leave the event
in flight with no record, which is the case this exists to remove.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0073"
down_revision: str | Sequence[str] | None = "0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_webhook_accepted_events",
        sa.Column("accepted_id", sa.String(length=64), primary_key=True),
        sa.Column("url", sa.String(length=1024), nullable=False),
        sa.Column("event_type", sa.String(length=128), nullable=True),
        sa.Column("event", sa.JSON(), nullable=True),
        sa.Column("accepted_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_caliber_webhook_accepted_events_accepted_at",
        "caliber_webhook_accepted_events",
        ["accepted_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_caliber_webhook_accepted_events_accepted_at",
        table_name="caliber_webhook_accepted_events",
    )
    op.drop_table("caliber_webhook_accepted_events")
