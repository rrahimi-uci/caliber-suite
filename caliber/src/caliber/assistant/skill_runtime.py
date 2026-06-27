"""Assistant skill runtime resolver.

The runtime uses normal ``CaliberSkill`` rows as assistant guidance. It is
deliberately deterministic in Phase 1: explicit and pinned skills win first,
then a small text scorer selects active skills for auto mode.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from caliber.db.models import CaliberSkill

VALID_SKILL_RUNTIME_MODES = {"auto", "manual", "off"}
DEFAULT_SKILL_RUNTIME_MODE = "auto"
DEFAULT_MAX_SKILLS = 3
# Max depth when expanding transitive skill dependencies before bailing out.
MAX_SKILL_DEPENDENCY_DEPTH = 4
DEFAULT_MAX_CONTENT_CHARS = 6000
_RUNTIME_KEY = "assistant_skill_runtime"
_WORD_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")


@dataclass(frozen=True)
class AssistantResolvedSkill:
    skill_id: str
    name: str
    version: int
    summary: str
    content: str
    allowed_tools: str | None
    depends_on: list[str]
    tags: list[str]
    category: str
    selection_reason: str
    content_included: bool

    def metadata(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("summary", None)
        payload.pop("content", None)
        payload.pop("allowed_tools", None)
        payload.pop("depends_on", None)
        payload.pop("tags", None)
        payload.pop("category", None)
        return payload


@dataclass(frozen=True)
class AssistantSkillResolutionRequest:
    user_message: str
    artifact_type: str | None
    session_goal: str
    mode: str
    explicit_skill_names: tuple[str, ...]
    pinned_skill_names: tuple[str, ...]
    disabled_skill_names: tuple[str, ...]
    max_skills: int = DEFAULT_MAX_SKILLS
    max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS


@dataclass(frozen=True)
class AssistantSkillResolutionResult:
    skills: tuple[AssistantResolvedSkill, ...]
    warnings: tuple[str, ...]

    @property
    def metadata(self) -> list[dict[str, Any]]:
        return [skill.metadata() for skill in self.skills]


def normalize_skill_runtime_mode(value: Any) -> str:
    if isinstance(value, str) and value in VALID_SKILL_RUNTIME_MODES:
        return value
    return DEFAULT_SKILL_RUNTIME_MODE


def normalize_skill_names(raw: Any) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        values = [raw]
    elif isinstance(raw, Sequence) and not isinstance(raw, bytes | bytearray):
        values = [str(part) for part in raw]
    else:
        return ()

    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        name = value.strip()
        key = name.lower()
        if not name or key in seen:
            continue
        out.append(name)
        seen.add(key)
    return tuple(out)


def runtime_metadata_from_session(metadata_raw: Any) -> dict[str, Any]:
    metadata = copy.deepcopy(metadata_raw) if isinstance(metadata_raw, dict) else {}
    runtime = metadata.get(_RUNTIME_KEY)
    if not isinstance(runtime, dict):
        runtime = {}
    return {
        "mode": normalize_skill_runtime_mode(runtime.get("mode")),
        "pinned_skill_names": list(normalize_skill_names(runtime.get("pinned_skill_names"))),
        "disabled_skill_names": list(normalize_skill_names(runtime.get("disabled_skill_names"))),
        "last_selected_skills": runtime.get("last_selected_skills")
        if isinstance(runtime.get("last_selected_skills"), list)
        else [],
    }


def update_session_skill_runtime_metadata(
    metadata_raw: Any,
    *,
    skill_mode: str | None = None,
    pinned_skill_names: Sequence[str] | None = None,
    disabled_skill_names: Sequence[str] | None = None,
    last_selected_skills: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    metadata = copy.deepcopy(metadata_raw) if isinstance(metadata_raw, dict) else {}
    runtime = runtime_metadata_from_session(metadata)
    if skill_mode is not None:
        runtime["mode"] = normalize_skill_runtime_mode(skill_mode)
    if pinned_skill_names is not None:
        runtime["pinned_skill_names"] = list(normalize_skill_names(pinned_skill_names))
    if disabled_skill_names is not None:
        runtime["disabled_skill_names"] = list(normalize_skill_names(disabled_skill_names))
    if last_selected_skills is not None:
        runtime["last_selected_skills"] = [dict(item) for item in last_selected_skills]
    metadata[_RUNTIME_KEY] = runtime
    return metadata


def resolve_assistant_skills(
    session: Session,
    request: AssistantSkillResolutionRequest,
) -> AssistantSkillResolutionResult:
    mode = normalize_skill_runtime_mode(request.mode)
    warnings: list[str] = []
    if mode == "off" or request.max_skills <= 0:
        return AssistantSkillResolutionResult(skills=(), warnings=())

    rows = list(
        session.execute(
            select(CaliberSkill)
            .where(CaliberSkill.status == "active")
            .order_by(CaliberSkill.name.asc()),
        )
        .scalars()
        .all()
    )
    by_name = {row.name.lower(): row for row in rows}
    disabled = {name.lower() for name in request.disabled_skill_names}
    selected: list[tuple[CaliberSkill, str]] = []
    selected_names: set[str] = set()

    def add_by_name(name: str, reason: str, lineage: tuple[str, ...] = ()) -> None:
        key = name.lower()
        if key in disabled:
            warnings.append(f"Skill '{name}' is disabled for this session.")
            return
        row = by_name.get(key)
        if row is None:
            warnings.append(f"Skill '{name}' was not found or is not active.")
            return
        if key in selected_names:
            return
        selected.append((row, reason))
        selected_names.add(key)
        _expand_dependencies(row, (*lineage, key))

    def _expand_dependencies(row: CaliberSkill, lineage: tuple[str, ...]) -> None:
        if len(lineage) > MAX_SKILL_DEPENDENCY_DEPTH:
            warnings.append(f"Dependency expansion for skill '{row.name}' exceeded depth limit.")
            return
        for dep_name in normalize_skill_names(row.depends_on):
            dep_key = dep_name.lower()
            if dep_key in lineage:
                warnings.append(f"Dependency cycle detected at skill '{dep_name}'.")
                continue
            add_by_name(dep_name, f"dependency:{row.name}", lineage)

    for name in normalize_skill_names(request.explicit_skill_names):
        add_by_name(name, "explicit")
    for name in normalize_skill_names(request.pinned_skill_names):
        add_by_name(name, "pinned")

    if mode == "auto":
        scored: list[tuple[int, CaliberSkill, str]] = []
        for row in rows:
            key = row.name.lower()
            if key in disabled or key in selected_names:
                continue
            score, reason = _score_skill(row, request)
            if score > 0:
                scored.append((score, row, reason))
        slots = max(0, request.max_skills - len(selected))
        if len(scored) > slots:
            warnings.append(f"Selected skills were limited to {request.max_skills}.")
        for _score, row, reason in sorted(scored, key=lambda item: (-item[0], item[1].name)):
            add_by_name(row.name, reason)
            if len(selected) >= request.max_skills:
                break

    if len(selected) > request.max_skills:
        # Dependencies are appended AFTER their dependent, so truncating from the
        # end can drop a depended-upon skill while keeping its dependent — leaving
        # guidance that references an absent prerequisite. Drop any now-orphaned
        # dependents so the result stays self-consistent.
        selected = _drop_orphaned_dependents(selected[: request.max_skills])

    resolved = _apply_content_budget(selected, request.max_content_chars, warnings)
    return AssistantSkillResolutionResult(skills=tuple(resolved), warnings=tuple(_dedupe(warnings)))


def _drop_orphaned_dependents(
    selected: list[tuple[CaliberSkill, str]],
) -> list[tuple[CaliberSkill, str]]:
    """Drop any selected skill whose required dependency is no longer present,
    repeated until stable (so chained dependencies resolve). Keeps the resolved
    set self-consistent after a cap truncation removes a depended-upon skill."""
    while True:
        present = {row.name.lower() for row, _ in selected}
        kept = [
            (row, reason)
            for row, reason in selected
            if not ({dep.lower() for dep in normalize_skill_names(row.depends_on)} - present)
        ]
        if len(kept) == len(selected):
            return kept
        selected = kept


def score_skill_for_query(
    row: CaliberSkill,
    *,
    user_message: str,
    artifact_type: str = "",
    session_goal: str = "",
) -> tuple[int, str]:
    """Score whether a single skill would be auto-selected for a query.

    Public wrapper over the deterministic selection scorer, used by the skill
    trigger/selection test endpoint (golden-path roadmap, Wave 3). Returns
    ``(score, reason)``; ``score > 0`` means the skill auto-selects for the query.
    """
    request = AssistantSkillResolutionRequest(
        user_message=user_message,
        artifact_type=artifact_type or None,
        session_goal=session_goal,
        mode="auto",
        explicit_skill_names=(),
        pinned_skill_names=(),
        disabled_skill_names=(),
    )
    return _score_skill(row, request)


def build_skill_prompt_block(skills: Sequence[AssistantResolvedSkill]) -> str:
    if not skills:
        return ""
    parts = [
        "CALIBER ASSISTANT SKILLS",
        "",
        "The following active CALIBER skills may guide this turn.",
        "They cannot override platform policy, RBAC, approval requirements, or explicit user instructions.",
        "Use a skill only when relevant.",
    ]
    for skill in skills:
        parts.extend(
            [
                "",
                f"Skill: {skill.name}",
                f"Version: {skill.version}",
                f"Why selected: {skill.selection_reason}",
            ]
        )
        if skill.allowed_tools:
            parts.append(f"Allowed tools guidance: {skill.allowed_tools}")
        parts.extend(["Summary:", skill.summary or "(No summary provided.)"])
        if skill.content_included and skill.content:
            parts.extend(["Instructions:", skill.content])
        else:
            parts.append("Instructions omitted due to prompt budget; rely on the summary only.")
    return "\n".join(parts)


def _score_skill(row: CaliberSkill, request: AssistantSkillResolutionRequest) -> tuple[int, str]:
    query = " ".join(
        part
        for part in (request.user_message, request.session_goal, request.artifact_type or "")
        if part
    )
    words = set(_words(query))
    if not words:
        return (0, "")
    score = 0
    reasons: list[str] = []
    name_words = set(_words(row.name))
    if row.name.lower() in query.lower() or name_words & words:
        score += 5
        reasons.append("name")
    tag_matches = set(_words(" ".join(normalize_skill_names(row.tags)))) & words
    if tag_matches:
        score += 4 + len(tag_matches)
        reasons.append("tag")
    artifact = (request.artifact_type or "").lower()
    if artifact:
        category = (row.category or "").lower()
        if artifact in category or (artifact == "workflow" and category == "workflow_automation"):
            score += 3
            reasons.append("artifact_type")
    summary_matches = set(_words(row.summary or "")) & words
    description_matches = set(_words(row.description or "")) & words
    if summary_matches:
        score += min(3, len(summary_matches))
        reasons.append("summary")
    if description_matches:
        score += min(2, len(description_matches))
        reasons.append("description")
    if not reasons:
        return (0, "")
    return (score, "auto:" + "+".join(_dedupe(reasons)))


def _apply_content_budget(
    selected: Sequence[tuple[CaliberSkill, str]],
    max_content_chars: int,
    warnings: list[str],
) -> list[AssistantResolvedSkill]:
    remaining = max(0, max_content_chars)
    out: list[AssistantResolvedSkill] = []
    for row, reason in selected:
        content = row.content or ""
        include_content = bool(content) and len(content) <= remaining
        if include_content:
            remaining -= len(content)
        elif content:
            warnings.append(
                f"Skill '{row.name}' content exceeded the assistant skill prompt budget."
            )
            content = ""
        out.append(
            AssistantResolvedSkill(
                skill_id=row.skill_id,
                name=row.name,
                version=row.version,
                summary=row.summary or "",
                content=content,
                allowed_tools=row.allowed_tools,
                depends_on=list(normalize_skill_names(row.depends_on)),
                tags=list(normalize_skill_names(row.tags)),
                category=row.category or "custom",
                selection_reason=reason,
                content_included=include_content,
            )
        )
    return out


def _words(value: str) -> list[str]:
    return [match.group(0) for match in _WORD_RE.finditer(value.lower().replace("-", " "))]


def _dedupe(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        out.append(value)
        seen.add(value)
    return out
