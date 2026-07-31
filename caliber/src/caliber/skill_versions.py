"""Shared immutable-history helpers for skill mutations.

Every caller that changes a :class:`~caliber.db.models.CaliberSkill` must use
these helpers in the same database transaction as the live-row mutation.  That
keeps ``caliber_skill_versions`` authoritative for history, diff, and
forward-only rollback regardless of whether the write originated in the
Skills API or the refinement Apply flow.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from caliber.db.models import CaliberSkill, CaliberSkillVersion
from caliber.ids import new_skill_version_id


def record_skill_version(session: Session, skill: CaliberSkill, *, created_by: str) -> None:
    """Snapshot the skill's current content and summary at its live version."""
    session.add(
        CaliberSkillVersion(
            skill_version_id=new_skill_version_id(),
            skill_id=skill.skill_id,
            version_number=skill.version,
            content=skill.content,
            summary=skill.summary or "",
            created_by=created_by,
        )
    )


def ensure_skill_version_snapshot(
    session: Session,
    skill_id: str,
    version_number: int,
    content: str,
    summary: str,
    *,
    created_by: str,
) -> int:
    """Return a safe history head for the supplied live payload.

    Missing history is backfilled at ``version_number`` when that number is
    still the head. If legacy decrement/reuse left a conflicting snapshot or a
    newer history row, append the live payload at ``max(history, live)+1``
    instead of overwriting immutable history.
    """
    existing = (
        session.execute(
            select(CaliberSkillVersion)
            .where(CaliberSkillVersion.skill_id == skill_id)
            .where(CaliberSkillVersion.version_number == version_number)
            .limit(1)
        )
        .scalars()
        .first()
    )
    max_history = session.execute(
        select(func.max(CaliberSkillVersion.version_number)).where(
            CaliberSkillVersion.skill_id == skill_id
        )
    ).scalar_one()
    normalized_summary = summary or ""
    if (
        existing is not None
        and existing.content == content
        and existing.summary == normalized_summary
        and (max_history is None or max_history <= version_number)
    ):
        return version_number
    if existing is None and (max_history is None or max_history < version_number):
        snapshot_version = version_number
    else:
        snapshot_version = max(max_history or 0, version_number) + 1
    session.add(
        CaliberSkillVersion(
            skill_version_id=new_skill_version_id(),
            skill_id=skill_id,
            version_number=snapshot_version,
            content=content,
            summary=normalized_summary,
            created_by=created_by,
        )
    )
    return snapshot_version


def previous_skill_version(session: Session, skill: CaliberSkill) -> CaliberSkillVersion | None:
    """Return the newest immutable snapshot older than the live version."""
    return (
        session.execute(
            select(CaliberSkillVersion)
            .where(CaliberSkillVersion.skill_id == skill.skill_id)
            .where(CaliberSkillVersion.version_number < skill.version)
            .order_by(CaliberSkillVersion.version_number.desc())
            .limit(1)
        )
        .scalars()
        .first()
    )
