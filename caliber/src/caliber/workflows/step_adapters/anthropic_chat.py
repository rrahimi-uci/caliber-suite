"""One governed agent step through the Anthropic Messages API (B08.anthropic, #727).

:meth:`caliber.workflows.runtime.AnthropicChatWorkflowExecutor.run_step` calls :func:`run_anthropic_step`. It is the governed
counterpart of the executor's ungoverned ``run_agent`` loop, which is unchanged: same client, same model resolution, same
system prompt and tool specs, but a different contract. A step *proposes*, it never performs an effect.

**What the model may do, and what happens.** Every tool of the agent is offered to the model, in two kinds:

* a **read tool** (one the platform put in :attr:`~caliber.workflows.agent_step.AgentStepInput.read_tools`, and whose binding
  does not require approval) is offered with its own input schema and, when the model calls it, run from ``read_tools`` and
  nowhere else. The result goes back to the model for the next turn. A tool is never resolved from the agent's binding, from a
  registry or by name.
* every other tool is **never run**. It is offered as a *proposal*: its input is a ``ProposedAction`` (connector, connector
  version, resource, the tool's own arguments as ``args``, ``effect_class`` and an optional ``expected_resource_version``).
  When the model calls it the step stops with ``proposed_action`` carrying that one action. The model's ``effect_class`` and
  ``connector_version`` are **claims**: the result's action is not ``platform_bound`` and a claim that differs from the
  platform's is a ``ClaimMismatch`` at ``bound_to`` (B08.2). A proposal with a field the schema does not define (a ``state``,
  a ``receipt_id``: a model reporting that an effect happened) is not a proposal and is ``malformed_result``. A tool name the
  agent does not have is ``malformed_result`` too. The result holds at most one action (v1): when a response names several
  protected tools, the first is proposed and the rest are dropped (the next effect is the next occurrence).

**Limits come from the request** and are applied by this adapter around every provider call, as the platform will again
(B08.3): before a turn starts, ``max_turns``, the deadline and the tokens left (``max_tokens``) are checked, and a stop keeps
the usage so far; ``max_tool_calls`` is checked before each read tool runs; the tokens a call may produce are capped at what is
left; and returned usage over ``max_tokens`` is ``max_tokens`` whatever the model said. The provider call gets a timeout of at
most the time to the deadline, and **no automatic SDK retry** (``max_retries=0``): a retry is the next attempt of the step, which
the platform reserves tokens for, never a hidden second charge.

**Usage** is the provider's: ``input_tokens`` plus the cache creation and read tokens is the prompt, ``output_tokens`` the
completion, ``cache_read_input_tokens`` the cached prompt, summed over the step's turns, with ``usage_source`` ``provider``. A
stop before any call is zero with source ``estimated`` (nothing was called; nothing is invented). A failure keeps the usage of
the calls that returned, with source ``unknown`` (the failed call's is not known).

**Failures are results.** A provider error is ``provider_error``; a timeout (the builtin, ``anthropic.APITimeoutError`` and the
``httpx``/``httpcore`` timeouts, recognised by class so the SDK need not be imported) is ``adapter_timeout``; output this module
cannot read (an unknown stop reason, malformed blocks or usage, an unknown tool, a bad proposal) is ``malformed_result``; the
model refusing is ``refused`` with ``model_refused``; a client that cannot be built (the SDK is not installed) is ``refused`` with
``provider_unavailable``. **Nothing from the provider or the model is placed in a result or a log line**: not its text, a tool
name, arguments, a message or an exception's text; the exception's class name, the step id, a fixed reason token and numbers
are all that is logged.

Not covered, so not claimed: handoff targets are not offered to the model, a structured-output schema is not applied (a step's
result carries no output text), and a blocking read tool or provider call is bounded by its own timeout, not interrupted.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Final

from caliber.config import provider_request_timeout
from caliber.contracts.agent_step import (
    AgentStepRequest,
    AgentStepResult,
    ProposedAction,
    StepUsage,
    parse_step_result,
)
from caliber.contracts.registry import ContractValidationError
from caliber.effects.records import ProposalError
from caliber.workflows import agent_step as _step
from caliber.workflows.agent_step import (
    PROPOSAL_DESCRIPTION,
    AgentStepInput,
    proposal_from_call,
    proposal_parameters,
)
from caliber.workflows.ir import IRToolBinding
from caliber.workflows.runtime import (
    _agent_instruction_text,
    _anthropic_assistant_content,
    _call_tool,
    _tool_parameters,
    _tool_result_text,
)

logger = logging.getLogger("caliber.workflows.step_adapters.anthropic_chat")

#: The model refused to answer (the provider's ``refusal`` stop reason): ``refused``, with usage.
MODEL_REFUSED: Final = "model_refused"
#: The provider client could not be built (the SDK is not installed): ``refused``, no call was made.
PROVIDER_UNAVAILABLE: Final = "provider_unavailable"

#: Every ``stop_reason`` the Messages API documents that this adapter can act on. ``pause_turn`` (server tools) is not here:
#: none is offered, so it is not a readable answer.
_STOP_REASONS: Final = frozenset({"end_turn", "stop_sequence", "max_tokens", "tool_use", "refusal"})
_FINAL_STOPS: Final = frozenset({"end_turn", "stop_sequence"})
#: Provider timeouts are recognised by class (module root and name) so the SDKs need not be imported.
_TIMEOUT_MODULES: Final = frozenset({"anthropic", "httpx", "httpcore"})
_TIMEOUT_CLASSES: Final = frozenset({"APITimeoutError", "TimeoutException"})
#: The largest figure the step-result schema allows; a provider figure above it is not readable.
_MAX_FIGURE: Final = 2_147_483_647
#: What a read tool's result may put back into the prompt, in characters (a longer one is cut).
_MAX_TOOL_RESULT_CHARS: Final = 65_536
#: The shortest provider timeout: a deadline that is nearly here still gets a positive, bounded one.
_MIN_TIMEOUT_SECONDS: Final = 0.001
_DEFAULT_SYSTEM_PROMPT: Final = "You are a helpful workflow agent."

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class _Unreadable(Exception):  # noqa: N818 - a reason token, not an error to report
    """The provider's answer cannot be read; ``reason`` is a fixed token (never a value from the answer)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _ToolUse:
    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class _Reply:
    stop_reason: str
    blocks: list[Any]
    tool_uses: tuple[_ToolUse, ...]


@dataclass
class _Meter:
    """What the step has used so far, from provider figures only."""

    prompt: int = 0
    completion: int = 0
    cached: int | None = None
    turns: int = 0
    tool_calls: int = 0

    @property
    def total(self) -> int:
        return self.prompt + self.completion

    def usage(self) -> StepUsage:
        return StepUsage(
            prompt_tokens=self.prompt,
            completion_tokens=self.completion,
            total_tokens=self.total,
            turns=self.turns,
            tool_calls=self.tool_calls,
            cached_prompt_tokens=self.cached,
        )


def _figure(value: object) -> int:
    """A provider token count: a non-negative integer within the schema's bound."""
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_FIGURE:
        raise _Unreadable("usage")
    return value


def _optional_figure(value: object) -> int | None:
    return None if value is None else _figure(value)


def _timeout_class(exc: Exception) -> bool:
    return isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or any(
        cls.__name__ in _TIMEOUT_CLASSES and cls.__module__.partition(".")[0] in _TIMEOUT_MODULES
        for cls in type(exc).__mro__
    )


def _error_code(exc: Exception) -> str:
    return _step.ADAPTER_TIMEOUT if _timeout_class(exc) else _step.PROVIDER_ERROR


def _read_usage(response: object, meter: _Meter) -> None:
    """Add the turn's usage to ``meter``; nothing is added unless every figure is readable."""
    usage = getattr(response, "usage", None)
    if usage is None:
        raise _Unreadable("usage")
    fresh = _figure(getattr(usage, "input_tokens", None))
    completion = _figure(getattr(usage, "output_tokens", None))
    created = _optional_figure(getattr(usage, "cache_creation_input_tokens", None))
    read = _optional_figure(getattr(usage, "cache_read_input_tokens", None))
    meter.prompt += fresh + (created or 0) + (read or 0)
    meter.completion += completion
    if read is not None:
        meter.cached = (meter.cached or 0) + read


def _read_reply(response: object) -> _Reply:
    stop_reason = getattr(response, "stop_reason", None)
    content = getattr(response, "content", None)
    if not isinstance(stop_reason, str) or stop_reason not in _STOP_REASONS:
        raise _Unreadable("stop_reason")
    if not isinstance(content, list | tuple):
        raise _Unreadable("content")
    blocks = list(content)
    uses: list[_ToolUse] = []
    for block in blocks:
        kind = getattr(block, "type", None)
        if not isinstance(kind, str):
            raise _Unreadable("block")
        if kind != "tool_use":
            continue
        ident, name, arguments = (getattr(block, key, None) for key in ("id", "name", "input"))
        if not isinstance(ident, str) or not ident or not isinstance(name, str) or not name:
            raise _Unreadable("tool_use")
        if not isinstance(arguments, Mapping):
            raise _Unreadable("tool_use")
        uses.append(_ToolUse(ident, name, dict(arguments)))
    # A ``tool_use`` stop names at least one call and a final stop names none. A reply cut off by ``max_tokens`` may end
    # inside a call and a refusal ends the step: neither is acted on, so their blocks are not held to this.
    if (stop_reason == "tool_use" and not uses) or (stop_reason in _FINAL_STOPS and uses):
        raise _Unreadable("tool_use")
    return _Reply(stop_reason, blocks, tuple(uses))


def _proposal_schema(binding: IRToolBinding) -> dict[str, Any]:
    """The input schema a protected tool is offered with: its own arguments under ``args``, and nothing the model could claim."""
    return proposal_parameters(_tool_parameters(binding))


def _proposal_action(use: _ToolUse) -> ProposedAction:
    """The model's call of a protected tool as a proposal that names the function it called; :class:`_Unreadable` if it is not one."""
    try:
        return proposal_from_call(use.name, use.input)
    except (ProposalError, ContractValidationError, TypeError, ValueError) as exc:
        raise _Unreadable("proposal") from exc


class _Step:
    """One run of one step; every method that can end the step returns the result, never raises for the provider."""

    def __init__(
        self,
        request: AgentStepRequest,
        step_input: AgentStepInput,
        *,
        client_factory: Callable[[], Any],
        default_model: str,
        max_tokens: int,
        clock: Clock,
    ) -> None:
        self._request = request
        self._input = step_input
        self._client_factory = client_factory
        self._max_tokens = max_tokens
        self._clock = clock
        self._limits = request.limits
        self._deadline = request.limits.deadline_at
        self._meter = _Meter()
        agent = step_input.agent
        self._model = agent.model if agent.model and agent.model != "inherit" else default_model
        self._bindings = {binding.local_name: binding for binding in agent.tools}
        self._system = _agent_instruction_text(agent) or _DEFAULT_SYSTEM_PROMPT
        self._messages: list[dict[str, Any]] = [
            *step_input.history,
            {"role": "user", "content": step_input.input_text},
        ]

    # ------------------------------------------------------------------ results

    def _log(self, what: str) -> None:
        logger.warning("agent step %s: %s", self._request.step_request_id, what)

    def _stopped(self, stop_reason: str) -> AgentStepResult:
        """A limit stop: the usage so far, from the provider (or zero and ``estimated`` when no call was made)."""
        source = "provider" if self._meter.turns else "estimated"
        return self._finish(
            _step.answer(self._request, stop_reason, usage=self._meter.usage(), usage_source=source)
        )

    def _failed(self, code: str) -> AgentStepResult:
        return self._finish(_step.failure(self._request, code, usage=self._meter.usage()))

    def _finish(self, result: AgentStepResult) -> AgentStepResult:
        """The adapter returns only what the contract accepts for this step: anything else is ``malformed_result``."""
        try:
            return parse_step_result(result.record(), self._request)
        except Exception:
            self._log("the result built is not a valid step result")
            return _step.failure(self._request, _step.MALFORMED_RESULT)

    # ------------------------------------------------------------------ the loop

    def run(self) -> AgentStepResult:
        client: Any = None
        while True:
            halt = self._cannot_start_turn()
            if halt is not None:
                return self._stopped(halt)
            if client is None:
                try:
                    client = self._client_factory()
                except Exception as exc:
                    self._log(f"the provider client could not be built ({type(exc).__name__})")
                    return self._finish(_step.refusal(self._request, PROVIDER_UNAVAILABLE))
            reply = self._call(client)
            if isinstance(reply, AgentStepResult):
                return reply
            ended = self._end_of(reply)
            if ended is None:
                ended = self._tool_uses(reply)
            if ended is not None:
                return ended

    def _end_of(self, reply: _Reply) -> AgentStepResult | None:
        """The result a reply that is not a tool call ends the step with; ``None`` for a ``tool_use`` reply."""
        usage = self._meter.usage()
        if reply.stop_reason in _FINAL_STOPS:
            return self._finish(
                _step.answer(self._request, "completed", usage=usage, usage_source="provider")
            )
        if reply.stop_reason == "max_tokens":
            return self._stopped("max_tokens")
        if reply.stop_reason == "refusal":
            return self._finish(
                _step.answer(
                    self._request,
                    "refused",
                    usage=usage,
                    usage_source="provider",
                    error_code=MODEL_REFUSED,
                )
            )
        return None

    def _cannot_start_turn(self) -> str | None:
        """Why another provider call may not start (a limit's stop reason), or ``None``."""
        if self._meter.turns >= self._limits.max_turns:
            return "max_turns"
        if self._clock() >= self._deadline:
            return "deadline"
        if self._meter.total >= self._limits.max_tokens:
            return "max_tokens"
        return None

    def _is_read(self, name: str) -> bool:
        binding = self._bindings.get(name)
        return (
            binding is not None and name in self._input.read_tools and not binding.requires_approval
        )

    def _tool_specs(self) -> list[dict[str, Any]]:
        specs: list[dict[str, Any]] = []
        for binding in self._bindings.values():
            label = binding.registry_ref or binding.local_name
            if self._is_read(binding.local_name):
                specs.append(
                    {
                        "name": binding.local_name,
                        "description": label,
                        "input_schema": _tool_parameters(binding),
                    }
                )
            else:
                specs.append(
                    {
                        "name": binding.local_name,
                        "description": (
                            f"{label}. {PROPOSAL_DESCRIPTION} Nothing has happened until it is approved and performed."
                        ),
                        "input_schema": _proposal_schema(binding),
                    }
                )
        return specs

    def _call(self, client: Any) -> _Reply | AgentStepResult:
        """One provider call and its reading; the usage is added to the meter, an unreadable answer ends the step."""
        left = self._limits.max_tokens - self._meter.total
        seconds = (self._deadline - self._clock()).total_seconds()
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": min(self._max_tokens, left),
            "system": self._system,
            "messages": list(self._messages),
            "timeout": max(min(provider_request_timeout(), seconds), _MIN_TIMEOUT_SECONDS),
        }
        specs = self._tool_specs()
        if specs:
            kwargs["tools"] = specs
            kwargs["tool_choice"] = {"type": "auto", "disable_parallel_tool_use": True}
        try:
            scoped = getattr(client, "with_options", None)
            # The SDK retries by itself unless told not to; a retry is the next attempt, which the platform reserves for.
            response = (scoped(max_retries=0) if callable(scoped) else client).messages.create(
                **kwargs
            )
        except Exception as exc:
            self._log(f"the provider call failed ({type(exc).__name__})")
            return self._failed(_error_code(exc))
        self._meter.turns += 1
        try:
            _read_usage(response, self._meter)
            reply = _read_reply(response)
        except _Unreadable as bad:
            self._log(f"the provider's answer is not readable ({bad.reason})")
            return self._failed(_step.MALFORMED_RESULT)
        except Exception:
            self._log("the provider's answer is not readable")
            return self._failed(_step.MALFORMED_RESULT)
        if self._meter.total > self._limits.max_tokens:
            return self._stopped("max_tokens")
        return reply

    # ------------------------------------------------------------------ tools

    def _tool_uses(self, reply: _Reply) -> AgentStepResult | None:
        """Handle a ``tool_use`` reply: propose a protected call, or run the read calls; ``None`` continues the loop."""
        for use in reply.tool_uses:
            if use.name not in self._bindings:
                self._log("the model called a tool the agent does not have")
                return self._failed(_step.MALFORMED_RESULT)
        protected = [use for use in reply.tool_uses if not self._is_read(use.name)]
        if protected:
            return self._propose(protected)
        halt = self._cannot_start_turn()
        if halt is not None:
            # No turn is left to use a tool's result, so no tool is run for it.
            return self._stopped(halt)
        results: list[dict[str, Any]] = []
        for use in reply.tool_uses:
            if self._meter.tool_calls >= self._limits.max_tool_calls:
                return self._stopped("max_tool_calls")
            if self._clock() >= self._deadline:
                return self._stopped("deadline")
            results.append(self._run_read_tool(use))
            self._meter.tool_calls += 1
        self._messages.append(
            {"role": "assistant", "content": _anthropic_assistant_content(reply.blocks)}
        )
        self._messages.append({"role": "user", "content": results})
        return None

    def _propose(self, protected: list[_ToolUse]) -> AgentStepResult:
        if len(protected) > 1:
            logger.warning(
                "agent step %s: %d protected tool calls in one reply; the first is proposed",
                self._request.step_request_id,
                len(protected),
            )
        try:
            action = _proposal_action(protected[0])
        except _Unreadable as bad:
            self._log(f"the model's proposal is not valid ({bad.reason})")
            return self._failed(_step.MALFORMED_RESULT)
        return self._finish(
            _step.answer(
                self._request,
                "proposed_action",
                usage=self._meter.usage(),
                proposed_actions=(action,),
                usage_source="provider",
            )
        )

    def _run_read_tool(self, use: _ToolUse) -> dict[str, Any]:
        """Run one read tool from ``read_tools`` (and only there); a failure is reported to the model by class name only."""
        tool = self._input.read_tools[use.name]
        try:
            text = _tool_result_text(
                _call_tool(tool, dict(use.input), fallback_input=self._input.input_text)
            )
        except Exception as exc:
            self._log(f"a read tool failed ({type(exc).__name__})")
            return {
                "type": "tool_result",
                "tool_use_id": use.id,
                "content": json.dumps({"_error": type(exc).__name__}),
                "is_error": True,
            }
        return {
            "type": "tool_result",
            "tool_use_id": use.id,
            "content": text[:_MAX_TOOL_RESULT_CHARS],
        }


def run_anthropic_step(
    request: AgentStepRequest,
    step_input: AgentStepInput,
    *,
    client_factory: Callable[[], Any],
    default_model: str,
    max_tokens: int,
    clock: Clock | None = None,
) -> AgentStepResult:
    """Run one governed step through the Anthropic Messages API and return its typed result; it does not raise.

    ``client_factory`` builds (or returns) the provider client; it is called only once a provider call may start.
    ``max_tokens`` is the executor's per-call cap, itself capped by what the request has left. ``clock`` reads the time
    (aware); it defaults to UTC now and exists so a deadline can be tested without sleeping.
    """
    return _Step(
        request,
        step_input,
        client_factory=client_factory,
        default_model=default_model,
        max_tokens=max_tokens,
        clock=clock or _utc_now,
    ).run()


__all__ = [
    "MODEL_REFUSED",
    "PROVIDER_UNAVAILABLE",
    "run_anthropic_step",
]
