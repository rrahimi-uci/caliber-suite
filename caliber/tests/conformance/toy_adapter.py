"""A small, correct ``AgentStepAdapter`` for a made-up provider: the worked example of the conformance guide, and the suite's own reference.

``ToyAdapter`` talks to ``provider(messages, tools) -> reply`` where a reply is a plain mapping
(``text``, ``tool_calls`` as ``{"name", "arguments"}``, ``usage`` as ``{"prompt", "completion"}``, ``finish``). It is what a third
party's adapter looks like, in about 150 lines: it runs only the read tools it is handed, proposes the first protected call as
one unbound action, honours the request's limits itself, reports the provider's usage honestly, never raises for what the
provider does and never puts the provider's text anywhere. ``toy_subject()`` is the ``build`` function that stages the suite's
scripts on its provider; ``tests/conformance/broken_adapters.py`` derives one deliberately broken adapter per rule from it.

The code between the ``# --8<--`` markers is quoted in ``docs/operate/reference-profile.md`` and a test keeps the two equal.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from caliber.contracts.agent_step import (
    AgentStepRequest,
    AgentStepResult,
    ProposedAction,
    StepUsage,
)
from caliber.contracts.registry import ContractValidationError
from caliber.effects.records import ProposalError
from caliber.testing.adapter_conformance import (
    MALFORMED_USAGE,
    AdapterUnderTest,
    Call,
    Fault,
    Garbage,
    ProviderProbe,
    ProviderUsage,
    Reply,
    Script,
    fault_exception,
)
from caliber.workflows.agent_step import (
    ADAPTER_TIMEOUT,
    MALFORMED_RESULT,
    PROVIDER_ERROR,
    AgentStepInput,
    StepDeadlineReached,
    StepToolBudgetExceeded,
    answer,
    failure,
)

logger = logging.getLogger("tests.conformance.toy_adapter")

# --8<-- adapter
_ENVELOPE = {"connector", "connector_version", "effect_class", "resource", "args"}
_OPTIONAL = {"expected_resource_version"}


class _Unreadable(Exception):  # noqa: N818 - what the provider sent cannot be read; never carries its text
    pass


@dataclass
class _Tally:
    prompt: int = 0
    completion: int = 0
    turns: int = 0
    tool_calls: int = 0
    estimated: bool = False

    def usage(self) -> StepUsage:
        total = self.prompt + self.completion
        return StepUsage(self.prompt, self.completion, total, self.turns, self.tool_calls)

    @property
    def source(self) -> str:
        return "estimated" if self.estimated else "provider"


def _count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 2_147_483_647:
        raise _Unreadable
    return value


class ToyAdapter:
    """One governed step against a provider callable; the example every rule of the conformance suite is run on."""

    def __init__(
        self,
        provider: Callable[[list[dict[str, Any]], list[str]], Any],
        *,
        now: Callable[[], datetime],
    ):
        self._provider = provider
        self._now = now

    def run_step(  # noqa: PLR0911 - one loop, one exit per way a step can end
        self, request: AgentStepRequest, step_input: AgentStepInput
    ) -> AgentStepResult:
        tally = _Tally()
        read_tools = dict(step_input.read_tools)
        messages = [*step_input.history, {"role": "user", "content": step_input.input_text}]
        while True:
            if (limit := self._limit_reached(request, tally)) is not None:
                return self._stop(request, limit, tally)
            try:
                reply = self._call_provider(messages, sorted(read_tools))
            except TimeoutError:
                return failure(request, ADAPTER_TIMEOUT)
            except Exception as exc:
                return self._provider_failed(request, exc)
            try:
                calls = self._read_reply(reply, tally, step_input)
                if reply.get("finish") == "length":
                    return self._stop(request, "max_tokens", tally)
                protected = [call for call in calls if call["name"] not in read_tools]
                if protected:
                    return self._propose(request, protected[0], tally, step_input)
                if not calls:
                    return answer(
                        request, "completed", usage=tally.usage(), usage_source=tally.source
                    )
                for call in calls:
                    if self._tool_limit_reached(request, tally):
                        return self._stop(request, "max_tool_calls", tally)
                    tally.tool_calls += 1
                    try:
                        result = self._run_read_tool(read_tools[call["name"]], call["arguments"])
                    except StepToolBudgetExceeded:
                        return self._stop(request, "max_tool_calls", tally)
                    except StepDeadlineReached:
                        return self._stop(request, "deadline", tally)
                    messages.append({"role": "tool", "content": str(result)})
            except _Unreadable:
                return failure(request, MALFORMED_RESULT)

    @staticmethod
    def _stop(request: AgentStepRequest, reason: str, tally: _Tally) -> AgentStepResult:
        return answer(request, reason, usage=tally.usage(), usage_source=tally.source)

    def _limit_reached(self, request: AgentStepRequest, tally: _Tally) -> str | None:
        """A limit that forbids another provider call: turns, tokens, then the deadline."""
        limits = request.limits
        if tally.turns >= limits.max_turns:
            return "max_turns"
        if tally.prompt + tally.completion >= limits.max_tokens:
            return "max_tokens"
        if self._now() >= limits.deadline_at:
            return "deadline"
        return None

    @staticmethod
    def _tool_limit_reached(request: AgentStepRequest, tally: _Tally) -> bool:
        return tally.tool_calls >= request.limits.max_tool_calls

    def _call_provider(self, messages: list[dict[str, Any]], tools: list[str]) -> Any:
        return self._provider(messages, tools)

    @staticmethod
    def _provider_failed(request: AgentStepRequest, exc: Exception) -> AgentStepResult:
        # Only the exception's class is logged: its text is the provider's, and may quote the prompt.
        logger.warning("provider call failed (%s)", type(exc).__name__)
        return failure(request, PROVIDER_ERROR)

    @staticmethod
    def _run_read_tool(tool: Callable[..., Any], arguments: dict[str, Any]) -> Any:
        return tool(**arguments)

    def _read_reply(
        self, reply: Any, tally: _Tally, step_input: AgentStepInput
    ) -> list[dict[str, Any]]:
        """The reply's tool calls (name and decoded arguments); its usage is added to ``tally``."""
        if not isinstance(reply, Mapping):
            raise _Unreadable
        usage = reply.get("usage")
        if usage is None:
            tally.estimated = True
            tally.prompt += len(step_input.input_text.split())
            tally.completion += len(str(reply.get("text") or "").split())
        elif isinstance(usage, Mapping):
            tally.prompt += _count(usage.get("prompt"))
            tally.completion += _count(usage.get("completion"))
        else:
            raise _Unreadable
        tally.turns += 1
        names = {binding.local_name for binding in step_input.agent.tools}
        calls = []
        try:
            for raw in reply.get("tool_calls") or []:
                text = raw["arguments"]
                arguments = json.loads(text) if isinstance(text, str) else text
                if not isinstance(arguments, dict) or raw["name"] not in names:
                    raise _Unreadable
                calls.append({"name": raw["name"], "arguments": arguments})
        except (KeyError, TypeError, ValueError):
            raise _Unreadable from None
        return calls

    def _propose(
        self,
        request: AgentStepRequest,
        call: dict[str, Any],
        tally: _Tally,
        step_input: AgentStepInput,
    ) -> AgentStepResult:
        """The model's protected call as one unbound proposal: its claims are kept exactly as it made them."""
        del step_input
        envelope = call["arguments"]
        if not _ENVELOPE <= set(envelope) <= _ENVELOPE | _OPTIONAL:
            raise _Unreadable
        try:
            action = ProposedAction(
                connector=envelope["connector"],
                connector_version=envelope["connector_version"],
                effect_class=envelope["effect_class"],
                resource=envelope["resource"],
                args=envelope["args"],
                expected_resource_version=envelope.get("expected_resource_version"),
            )
        except (ProposalError, ContractValidationError, TypeError, ValueError):
            raise _Unreadable from None
        return answer(
            request,
            "proposed_action",
            usage=tally.usage(),
            proposed_actions=(action,),
            usage_source=tally.source,
        )


# --8<-- end adapter


# --8<-- build
def toy_provider(
    script: Script, probe: ProviderProbe
) -> Callable[[list[dict[str, Any]], list[str]], Any]:
    """The made-up provider the suite's scripts are played on."""
    remaining = list(script)

    def provider(messages: list[dict[str, Any]], tools: list[str]) -> Any:
        del messages, tools
        probe.provider_called()
        if not remaining:
            raise RuntimeError("the script has no reply left")
        item = remaining.pop(0)
        if isinstance(item, Fault):
            raise TimeoutError(item.text) if item.kind == "timeout" else fault_exception(item)
        if isinstance(item, Garbage):
            return ["not", "a", "reply", item.text]
        return _wire(item)

    return provider


def _wire(reply: Reply) -> dict[str, Any]:
    wire: dict[str, Any] = {
        "text": reply.text,
        "tool_calls": [_wire_call(call) for call in reply.calls],
        "finish": "length" if reply.cut_off else "stop",
    }
    if reply.usage is MALFORMED_USAGE:
        wire["usage"] = {"prompt": "many", "completion": "some"}
    elif isinstance(reply.usage, ProviderUsage):
        wire["usage"] = {"prompt": reply.usage.prompt, "completion": reply.usage.completion}
    return wire


def _wire_call(call: Call) -> dict[str, Any]:
    text = call.arguments if isinstance(call.arguments, str) else json.dumps(call.arguments)
    return {"name": call.name, "arguments": text}


def toy_subject() -> AdapterUnderTest:
    """The conformance subject for :class:`ToyAdapter`: ``build`` returns a fresh adapter over a provider playing the script."""
    return AdapterUnderTest(
        name="toy",
        build=lambda script, probe: ToyAdapter(toy_provider(script, probe), now=probe.clock.now),
    )


# --8<-- end build
