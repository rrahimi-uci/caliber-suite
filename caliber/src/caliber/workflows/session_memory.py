"""Workflow session-memory stores.

The workflow manifest can opt into automatic conversational memory for agent
nodes. This module provides process-local and database-backed stores that keep
per-workflow, per-node history keyed by the user-supplied ``session_id``.
"""

from __future__ import annotations

from threading import RLock
from typing import ClassVar, Protocol

from sqlalchemy.orm import Session, sessionmaker

from caliber.db.models import CaliberWorkflowSessionMemory

HistoryMessage = dict[str, str]


def _normalize_history(
    history: list[dict[str, object]] | list[HistoryMessage],
) -> list[HistoryMessage]:
    normalized: list[HistoryMessage] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role not in {"user", "assistant"}:
            continue
        if not isinstance(content, str) or not content.strip():
            continue
        normalized.append({"role": str(role), "content": content})
    return normalized


class WorkflowSessionMemoryStore(Protocol):
    """Load and persist conversation history for one workflow agent node."""

    def load_history(
        self,
        *,
        workflow_id: str,
        node_id: str,
        session_id: str,
    ) -> list[HistoryMessage]: ...

    def save_history(
        self,
        *,
        workflow_id: str,
        node_id: str,
        session_id: str,
        history: list[HistoryMessage],
    ) -> None: ...


class InMemoryWorkflowSessionMemoryStore:
    """Process-local session memory.

    Shared module-wide state keeps histories alive across repeated workflow runs
    within the same CALIBER process, while remaining intentionally ephemeral.
    """

    _lock: ClassVar[RLock] = RLock()
    _histories: ClassVar[dict[tuple[str, str, str], list[HistoryMessage]]] = {}

    def load_history(
        self,
        *,
        workflow_id: str,
        node_id: str,
        session_id: str,
    ) -> list[HistoryMessage]:
        key = (workflow_id, node_id, session_id)
        with self._lock:
            return [dict(item) for item in self._histories.get(key, [])]

    def save_history(
        self,
        *,
        workflow_id: str,
        node_id: str,
        session_id: str,
        history: list[HistoryMessage],
    ) -> None:
        key = (workflow_id, node_id, session_id)
        with self._lock:
            self._histories[key] = [dict(item) for item in _normalize_history(history)]


class SqlWorkflowSessionMemoryStore:
    """Persistent session memory stored in the metadata database."""

    def __init__(self, *, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def load_history(
        self,
        *,
        workflow_id: str,
        node_id: str,
        session_id: str,
    ) -> list[HistoryMessage]:
        with self._session_factory() as session:
            row = session.get(
                CaliberWorkflowSessionMemory,
                {
                    "workflow_id": workflow_id,
                    "node_id": node_id,
                    "session_id": session_id,
                },
            )
            if row is None:
                return []
            stored = row.message_history if isinstance(row.message_history, list) else []
            return [dict(item) for item in _normalize_history(stored)]

    def save_history(
        self,
        *,
        workflow_id: str,
        node_id: str,
        session_id: str,
        history: list[HistoryMessage],
    ) -> None:
        normalized = _normalize_history(history)
        with self._session_factory() as session:
            row = session.get(
                CaliberWorkflowSessionMemory,
                {
                    "workflow_id": workflow_id,
                    "node_id": node_id,
                    "session_id": session_id,
                },
            )
            if row is None:
                row = CaliberWorkflowSessionMemory(
                    workflow_id=workflow_id,
                    node_id=node_id,
                    session_id=session_id,
                )
                session.add(row)
            row.message_history = normalized
            row.turn_count = sum(1 for item in normalized if item.get("role") == "assistant")
            session.commit()


__all__ = [
    "HistoryMessage",
    "InMemoryWorkflowSessionMemoryStore",
    "SqlWorkflowSessionMemoryStore",
    "WorkflowSessionMemoryStore",
]
