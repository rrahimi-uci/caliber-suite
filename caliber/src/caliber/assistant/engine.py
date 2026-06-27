"""Engine protocol — interface every assistant backend must implement."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from caliber.assistant.models import AssistantTurnRequest, AssistantTurnResult

if TYPE_CHECKING:
    from caliber.assistant.tools import AssistantToolDispatcher


class AssistantEngine(Protocol):
    """Pluggable assistant backend.

    ``FakeAssistantEngine`` is the default for tests and demos.
    ``OpenAIAssistantEngine`` wraps the OpenAI Agents SDK.

    ``toolset`` is an optional per-turn, context-bound tool surface (read +
    execute tools gated by the caller's mode/approval policy). Engines that
    support tool-calling drive it; engines that don't simply ignore it.
    """

    def run_turn(
        self,
        request: AssistantTurnRequest,
        *,
        toolset: AssistantToolDispatcher | None = None,
    ) -> AssistantTurnResult: ...
