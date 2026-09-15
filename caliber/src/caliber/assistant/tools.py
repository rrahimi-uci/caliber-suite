"""Read-only registry tools for the assistant's tool-calling loop.

The assistant engine can call these mid-turn to ground its replies in the real
CALIBER registry (what skills/tools actually exist) instead of guessing. They
are strictly **read-only**: artifact *authoring* still flows through the
assistant service's intent-plan adapters, which carry their own validation and
approval gates. This module only lets the chat reasoning *look things up*.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import select

from caliber.auth import CaliberIdentity
from caliber.db.models import CaliberSkill, CaliberToolRegistry
from caliber.db.scoping import apply_visibility_filter

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

_MAX_ROWS = 50


class AssistantToolDispatcher(Protocol):
    """The tool surface the assistant engine drives during a turn."""

    def specs(self) -> list[dict[str, Any]]:
        """OpenAI ``tools`` specs (function schemas) to advertise to the model."""
        ...

    def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        """Execute a model-requested tool call; return a string (usually JSON)."""
        ...


def _fn(
    name: str,
    description: str,
    properties: dict[str, Any] | None = None,
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": required or [],
            },
        },
    }


class RegistryToolDispatcher:
    """Grounds assistant replies in the skills/tools registry (read-only)."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        identity: CaliberIdentity | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._identity = identity

    def for_identity(self, identity: CaliberIdentity) -> RegistryToolDispatcher:
        """Return a request-bound dispatcher without mutating shared state."""
        return type(self)(self._session_factory, identity=identity)

    def specs(self) -> list[dict[str, Any]]:
        return [
            _fn("list_skills", "List the active skills in the registry (name, summary, category)."),
            _fn(
                "get_skill",
                "Get one skill's full content + metadata by exact name.",
                {"name": {"type": "string", "description": "Skill name (kebab-case)."}},
                ["name"],
            ),
            _fn("list_tools", "List the registered tools (name, description)."),
        ]

    def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "list_skills":
            return self._list_skills()
        if name == "get_skill":
            return self._get_skill(str(arguments.get("name", "")))
        if name == "list_tools":
            return self._list_tools()
        return json.dumps({"error": f"unknown tool {name!r}"})

    def _list_skills(self) -> str:
        with self._session_factory() as session:
            stmt = select(CaliberSkill).where(CaliberSkill.status == "active")
            if self._identity is not None:
                stmt = apply_visibility_filter(
                    stmt, CaliberSkill, self._identity, self._identity.active_project_id
                )
            rows = (
                session.execute(stmt.order_by(CaliberSkill.name.asc()).limit(_MAX_ROWS))
                .scalars()
                .all()
            )
            return json.dumps(
                [{"name": r.name, "summary": r.summary or "", "category": r.category} for r in rows]
            )

    def _get_skill(self, name: str) -> str:
        if not name:
            return json.dumps({"error": "name is required"})
        with self._session_factory() as session:
            stmt = select(CaliberSkill).where(CaliberSkill.name == name)
            if self._identity is not None:
                stmt = apply_visibility_filter(
                    stmt, CaliberSkill, self._identity, self._identity.active_project_id
                )
            skill = session.execute(stmt).scalars().first()
            if skill is None:
                return json.dumps({"error": f"skill {name!r} not found"})
            return json.dumps(
                {
                    "name": skill.name,
                    "version": skill.version,
                    "category": skill.category,
                    "summary": skill.summary or "",
                    "content": skill.content,
                    "allowed_tools": skill.allowed_tools,
                    "depends_on": list(skill.depends_on or []),
                }
            )

    def _list_tools(self) -> str:
        with self._session_factory() as session:
            stmt = select(CaliberToolRegistry)
            if self._identity is not None:
                stmt = apply_visibility_filter(
                    stmt,
                    CaliberToolRegistry,
                    self._identity,
                    self._identity.active_project_id,
                )
            rows = (
                session.execute(stmt.order_by(CaliberToolRegistry.name.asc()).limit(_MAX_ROWS))
                .scalars()
                .all()
            )
            return json.dumps([{"name": r.name, "description": r.description} for r in rows])
