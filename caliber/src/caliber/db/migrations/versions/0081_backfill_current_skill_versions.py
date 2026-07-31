"""backfill current immutable skill snapshots

Revision ID: 0081
Revises: 0080
Create Date: 2026-07-31

Older refinement Apply promotions updated ``caliber_skills`` without inserting
the matching immutable ``caliber_skill_versions`` row. The overwritten history
cannot be reconstructed, but the current live content can be anchored safely so
future promotion and forward-only rollback preserve a complete chain from this
upgrade onward.
"""

from __future__ import annotations

from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "0081"
down_revision: str | Sequence[str] | None = "0080"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    skills = (
        bind.execute(
            sa.text(
                """
            SELECT skill_id, version, content, COALESCE(summary, '') AS summary
            FROM caliber_skills
            """
            )
        )
        .mappings()
        .all()
    )
    rows: list[dict[str, object]] = []
    for skill in skills:
        history = (
            bind.execute(
                sa.text(
                    """
                SELECT version_number, content, summary
                FROM caliber_skill_versions
                WHERE skill_id = :skill_id
                ORDER BY version_number DESC
                """
                ),
                {"skill_id": skill["skill_id"]},
            )
            .mappings()
            .all()
        )
        current = next(
            (row for row in history if row["version_number"] == skill["version"]),
            None,
        )
        max_history = history[0]["version_number"] if history else None
        live_is_head = max_history is None or skill["version"] >= max_history
        snapshot_matches = (
            current is not None
            and current["content"] == skill["content"]
            and current["summary"] == skill["summary"]
        )
        if snapshot_matches and live_is_head:
            continue
        if current is None and live_is_head:
            snapshot_version = skill["version"]
        else:
            snapshot_version = max(int(max_history or 0), int(skill["version"])) + 1
            bind.execute(
                sa.text(
                    """
                    UPDATE caliber_skills
                    SET version = :version
                    WHERE skill_id = :skill_id
                    """
                ),
                {"version": snapshot_version, "skill_id": skill["skill_id"]},
            )
        rows.append(
            {
                "skill_version_id": f"SKV-{uuid4().hex[:8]}",
                "skill_id": skill["skill_id"],
                "version_number": snapshot_version,
                "content": skill["content"],
                "summary": skill["summary"],
                "created_by": "migration:0081",
            }
        )
    if rows:
        bind.execute(
            sa.text(
                """
                INSERT INTO caliber_skill_versions
                    (skill_version_id, skill_id, version_number, content, summary, created_by)
                VALUES
                    (:skill_version_id, :skill_id, :version_number, :content, :summary, :created_by)
                """
            ),
            rows,
        )


def downgrade() -> None:
    # Retain the snapshots. They are valid immutable history once created, and
    # deleting them after later writes could make rollback history incomplete.
    pass
