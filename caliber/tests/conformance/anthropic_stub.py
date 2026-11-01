"""A stubbed Anthropic Messages transport and the conformance driver for ``AnthropicChatWorkflowExecutor`` (B08.anthropic, #727).

The real executor is built with this ``client``: it never imports the ``anthropic`` SDK and nothing reaches a network.
:class:`AnthropicDriver` is the :class:`tests.agent_step_conformance.AdapterDriver` the conformance suite (B08.5, #769) takes
for the Anthropic executor: ``AnthropicDriver().adapter_for(scenario, ctx)`` returns the executor whose stub answers the
scenario and calls ``ctx.provider_called()`` on every ``messages.create``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from caliber.contracts.agent_step import StepUsage
from caliber.testing.adapter_conformance import (
    MALFORMED_USAGE,
    AdapterUnderTest,
    Fault,
    Garbage,
    ProviderProbe,
    ProviderUsage,
    Script,
    ScriptItem,
    fault_exception,
)
from caliber.workflows.runtime import AnthropicChatWorkflowExecutor
from tests.agent_step_conformance import (
    PROTECTED_TOOL,
    READ_TOOL,
    SECRET_MARKER,
    Scenario,
    StepContext,
)

#: A moment before the harness request's deadline (``2026-10-26T09:10:00Z``, now in the past): the adapter reads the time to
#: enforce that deadline, so a driver gives it this clock instead of the wall clock, and nothing sleeps.
BEFORE_DEADLINE = datetime(2026, 10, 26, 9, 0, tzinfo=timezone.utc)


def fixed_clock(at: datetime = BEFORE_DEADLINE) -> Callable[[], datetime]:
    return lambda: at


class Block:
    """A content block as the SDK's objects expose it (attributes, not a mapping)."""

    def __init__(
        self,
        type_: str,
        *,
        text: str = "",
        id_: str = "toolu_1",
        name: str = "",
        input_: Any = None,
    ) -> None:
        self.type = type_
        self.text = text
        self.id = id_
        self.name = name
        self.input = {} if input_ is None else input_


class Usage:
    def __init__(
        self,
        input_tokens: Any,
        output_tokens: Any,
        *,
        cache_creation: Any = None,
        cache_read: Any = None,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation
        self.cache_read_input_tokens = cache_read


class Response:
    def __init__(self, content: Any, stop_reason: Any, usage: Any) -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage


def text_response(
    text: str = "done", usage: Usage | None = None, stop: str = "end_turn"
) -> Response:
    return Response([Block("text", text=text)], stop, usage or Usage(10, 5))


def tool_response(
    name: str, arguments: Any, usage: Usage | None = None, *, id_: str = "toolu_1"
) -> Response:
    return Response(
        [Block("tool_use", id_=id_, name=name, input_=arguments)], "tool_use", usage or Usage(10, 5)
    )


def proposal_input(ctx: StepContext, claimed_effect_class: str | None = None) -> dict[str, Any]:
    """The input a staged model gives a protected tool: its arguments only (B04.6), plus a class when the model volunteers one."""
    return ctx.proposal_envelope(
        **({} if claimed_effect_class is None else {"effect_class": claimed_effect_class})
    )


class APITimeoutError(Exception):
    """Named and placed like ``anthropic.APITimeoutError`` (the SDK recognises timeouts by class, not by import)."""


APITimeoutError.__module__ = "anthropic"


class Messages:
    """``client.messages``: answers from a script (a response, or an exception to raise), counting every call."""

    def __init__(
        self, script: list[Response | BaseException], ctx: StepContext | None = None
    ) -> None:
        self.script = list(script)
        self.ctx = ctx
        self.calls: list[dict[str, Any]] = []
        #: Runs after each call is recorded (a test advances its clock, or watches what was sent, here).
        self.on_call: Callable[[], None] | None = None

    def create(self, **kwargs: Any) -> Any:
        if self.ctx is not None:
            self.ctx.provider_called(
                tools=[t["name"] for t in kwargs.get("tools") or []],
                sent=json.dumps(kwargs.get("messages"), default=str),
            )
        self.calls.append(kwargs)
        if self.on_call is not None:
            self.on_call()
        if not self.script:
            raise AssertionError("the stub has no response left for this call")
        entry = self.script.pop(0)
        if isinstance(entry, BaseException):
            raise entry
        return entry


class Client:
    """The stubbed ``anthropic.Anthropic``: ``messages`` plus ``with_options`` (which the executor uses to turn retries off)."""

    def __init__(
        self, script: list[Response | BaseException], ctx: StepContext | None = None
    ) -> None:
        self.messages = Messages(script, ctx)
        self.options: list[dict[str, Any]] = []

    def with_options(self, **options: Any) -> Client:
        self.options.append(options)
        return self


class AnthropicDriver:
    """The conformance driver: the real executor over a client stubbed to the scenario."""

    name = "anthropic"
    # A provider cannot answer with another step's identity: the adapter builds the result's identity from the request.
    unsupported = frozenset({Scenario.ANOTHER_STEPS_RESULT})

    def script_for(self, scenario: Scenario, ctx: StepContext) -> list[Response | BaseException]:
        planted = f"model said {SECRET_MARKER}"
        usage = ctx.staged_usage
        reply = Usage(usage.prompt_tokens, usage.completion_tokens)
        claim = {**proposal_input(ctx), "state": "committed", "receipt_id": SECRET_MARKER}
        scripts: dict[Scenario, list[Response | BaseException]] = {
            Scenario.COMPLETES: [text_response(planted, reply)],
            # The staged usage is the two turns' sum: 100+50 prompt, 20+10 completion.
            Scenario.READ_TOOL_CALL: [
                tool_response(READ_TOOL, ctx.staged_read_args, Usage(100, 20)),
                text_response(planted, Usage(50, 10)),
            ],
            Scenario.PROPOSES_PROTECTED_TOOL: [
                tool_response(PROTECTED_TOOL, proposal_input(ctx), reply)
            ],
            Scenario.MODEL_LIES_ABOUT_EFFECT_CLASS: [
                tool_response(PROTECTED_TOOL, proposal_input(ctx, "read"), reply)
            ],
            Scenario.CLAIMS_COMMIT: [tool_response(PROTECTED_TOOL, claim, reply)],
            Scenario.BUDGET_EXHAUSTED: [text_response(planted, reply, stop="max_tokens")],
            Scenario.PROVIDER_ERROR: [RuntimeError(f"upstream said {SECRET_MARKER}")],
            Scenario.PROVIDER_TIMEOUT: [APITimeoutError(f"Request timed out: {SECRET_MARKER}")],
            Scenario.UNREADABLE_OUTPUT: [Response(planted, "end_turn", reply)],
        }
        return scripts[scenario]

    def client_for(self, scenario: Scenario, ctx: StepContext) -> Client:
        if scenario is Scenario.BUDGET_EXHAUSTED:
            # One call whose reply the provider cut off: the harness's default (three turns, no tool call) is not a loop.
            ctx.usage_override = StepUsage(900, 100, 1000, 1, 0)
        return Client(self.script_for(scenario, ctx), ctx)

    def adapter_for(self, scenario: Scenario, ctx: StepContext) -> object:
        return AnthropicChatWorkflowExecutor(
            api_key="unused",
            default_model="claude-test",
            client=self.client_for(scenario, ctx),
            step_clock=fixed_clock(),
        )


def executor_with(
    script: list[Response | BaseException],
    *,
    ctx: StepContext | None = None,
    clock: Callable[[], Any] | None = None,
    max_tokens: int = 4096,
) -> tuple[AnthropicChatWorkflowExecutor, Client]:
    """The real executor over a stub that answers ``script``; the stub is returned to inspect what it was sent."""
    client = Client(script, ctx)
    return (
        AnthropicChatWorkflowExecutor(
            api_key="unused",
            default_model="claude-test",
            client=client,
            step_clock=clock or fixed_clock(),
            max_tokens=max_tokens,
        ),
        client,
    )


def _anthropic_entry(item: ScriptItem) -> Response | BaseException:
    """What the stubbed Messages transport does for one step of a conformance script."""
    if isinstance(item, Fault):
        return APITimeoutError(item.text) if item.kind == "timeout" else fault_exception(item)
    if isinstance(item, Garbage):
        return Response(item.text, "end_turn", Usage(10, 5))
    if item.usage is None:
        usage: Any = None
    elif item.usage is MALFORMED_USAGE:
        usage = Usage("many", "some")
    else:
        assert isinstance(item.usage, ProviderUsage)
        usage = Usage(item.usage.prompt, item.usage.completion)
    blocks = [Block("text", text=item.text)] if item.text else []
    blocks += [
        Block("tool_use", id_=f"toolu_{n}", name=c.name, input_=c.arguments)
        for n, c in enumerate(item.calls, 1)
    ]
    stop = "max_tokens" if item.cut_off else ("tool_use" if item.calls else "end_turn")
    return Response(blocks, stop, usage)


def anthropic_subject() -> AdapterUnderTest:
    """The conformance suite's subject for ``AnthropicChatWorkflowExecutor``: the real executor over a canned client."""

    def build(script: Script, probe: ProviderProbe) -> object:
        client = Client([_anthropic_entry(item) for item in script], probe)  # type: ignore[arg-type]
        return AnthropicChatWorkflowExecutor(
            api_key="unused",
            default_model="claude-test",
            client=client,
            step_clock=probe.clock.now,
        )

    return AdapterUnderTest(name="anthropic", build=build)
