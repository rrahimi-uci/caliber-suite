"""durable calibration jobs, so a long calibration is not one long HTTP request

Revision ID: 0075
Revises: 0074
Create Date: 2026-07-30

Calibration scores every saved test case for a tool through the sandbox. With up to 200
cases, each paying a cold subprocess start, that is minutes of work — and it was one
synchronous request. An earlier pass fixed the sharp edges (the waits blocked the event
loop, and a database session was held across execution), but the shape remained: the
client holds a connection open for the whole run, a proxy timeout or a closed laptop lid
loses the result, and nothing records that the work ever happened.

A job row makes the work durable and the result addressable. Submitting returns ``202``
with a job id; a drain in the background executes queued jobs; the client polls. The
tool's ``last_calibration`` is still written on success, so nothing downstream changes.

``claimed_at``/``claimed_by`` exist so more than one process can run the drain without two
of them executing the same job. The claim is a conditional UPDATE on ``status``, the same
arbitration the webhook dead-letter replay uses, rather than a lock held across the run.

``status`` is deliberately not an enum column: SQLite and PostgreSQL disagree about enum
migration, and the set is small enough that the service is the honest place for it.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0075"
down_revision: str | Sequence[str] | None = "0074"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "caliber_calibration_jobs",
        sa.Column("job_id", sa.String(length=64), primary_key=True),
        sa.Column("tool_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="queued"),
        sa.Column("requested_by", sa.String(length=256), nullable=False, server_default=""),
        # The cases as they were at submission. Calibrating against a moving target would
        # make the result unattributable, and the synchronous path already refused a run
        # whose cases changed underneath it.
        sa.Column("test_cases", sa.JSON(), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("claimed_by", sa.String(length=128), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_caliber_calibration_jobs_status_created",
        "caliber_calibration_jobs",
        ["status", "created_at"],
    )
    op.create_index("ix_caliber_calibration_jobs_tool", "caliber_calibration_jobs", ["tool_id"])


def downgrade() -> None:
    op.drop_index("ix_caliber_calibration_jobs_tool", table_name="caliber_calibration_jobs")
    op.drop_index(
        "ix_caliber_calibration_jobs_status_created", table_name="caliber_calibration_jobs"
    )
    op.drop_table("caliber_calibration_jobs")
