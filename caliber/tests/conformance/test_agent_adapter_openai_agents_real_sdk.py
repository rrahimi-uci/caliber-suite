"""The OpenAI Agents step adapter on the real Agents SDK, with a fake model instead of the network (B08.5, #769).

The conformance suite runs the adapter over an emulation of the SDK's loop. This file runs it on the installed SDK itself
(``Runner``, ``Agent``, ``tool_use_behavior``, the run context's usage) so the emulation cannot drift from what the SDK does: the
model is a scripted object, nothing is sent anywhere. Skipped where the optional SDK is not installed.
"""

from __future__ import annotations

import asyncio
import json
import warnings
from collections.abc import Iterator
from typing import Any

import pytest

agents = pytest.importorskip("agents")

from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from caliber.testing.agent_step_conformance import (
    PROTECTED_TOOL,
    READ_TOOL,
    FakeStepClock,
    limits_changes,
    make_context,
)
from caliber.workflows.agent_step import run_agent_step
from caliber.workflows.runtime import OpenAIAgentsWorkflowExecutor


@pytest.fixture(autouse=True)
def _close_the_loop_the_sdk_leaves_open() -> Iterator[None]:
    """``Runner.run_sync`` keeps the thread's event loop; closing it here stops its socket pair leaking into a later test."""
    yield
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            loop = asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            return
    if not loop.is_running():
        loop.close()
        asyncio.set_event_loop(None)


class ScriptedModel(agents.models.interface.Model):  # type: ignore[misc]
    """A model that answers from a list of output-item lists and counts how often it was asked."""

    def __init__(
        self, replies: list[tuple[list[Any], tuple[int, int]]], on_call: Any = None
    ) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.on_call = on_call

    async def get_response(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        if self.on_call is not None:
            self.on_call()
        output, (prompt, completion) = self.replies.pop(0)
        usage = agents.Usage(
            requests=1,
            input_tokens=prompt,
            output_tokens=completion,
            total_tokens=prompt + completion,
        )
        return agents.ModelResponse(output=output, usage=usage, response_id=None)

    def stream_response(
        self, *args: Any, **kwargs: Any
    ) -> Any:  # pragma: no cover - never streamed
        raise NotImplementedError


class ScriptedProvider(agents.models.interface.ModelProvider):  # type: ignore[misc]
    def __init__(self, model: ScriptedModel) -> None:
        self.model = model

    def get_model(self, model_name: str | None) -> ScriptedModel:
        return self.model


def call(name: str, arguments: dict[str, Any], call_id: str) -> Any:
    return ResponseFunctionToolCall(
        arguments=json.dumps(arguments),
        call_id=call_id,
        name=name,
        type="function_call",
        id=call_id,
    )


def answer(text: str) -> Any:
    return ResponseOutputMessage(
        id="msg_1",
        content=[ResponseOutputText(annotations=[], text=text, type="output_text")],
        role="assistant",
        status="completed",
        type="message",
    )


def run(model: ScriptedModel, ctx: Any) -> Any:
    class OnTheRealSdk(OpenAIAgentsWorkflowExecutor):
        @staticmethod
        def _sdk() -> tuple[Any, ...]:
            real = list(OpenAIAgentsWorkflowExecutor._sdk())
            real[5] = lambda **_: ScriptedProvider(model)  # the OpenAI provider: no network
            return tuple(real)

    executor = OnTheRealSdk(api_key="unused", default_model="gpt-4o", step_clock=ctx.clock.now)
    return run_agent_step(executor, ctx.request, ctx.step_input, clock=ctx.clock)


def test_a_proposal_ends_the_run_before_the_model_is_asked_again() -> None:
    ctx = make_context()
    envelope = ctx.proposal_envelope()
    model = ScriptedModel(
        [([call(PROTECTED_TOOL, envelope, "c1")], (100, 20)), ([answer("never asked")], (1, 1))]
    )
    result = run(model, ctx)
    assert result.stop_reason == "proposed_action" and len(result.proposed_actions) == 1
    assert model.calls == 1 and ctx.protected_calls == []
    assert result.usage.total_tokens == 120


def test_a_spent_token_budget_ends_the_run_before_the_model_is_asked_again() -> None:
    ctx = make_context(**limits_changes(max_tokens=1000))
    model = ScriptedModel(
        [
            ([call(READ_TOOL, ctx.staged_read_args, "c1")], (500, 500)),
            ([answer("never asked")], (1, 1)),
        ]
    )
    result = run(model, ctx)
    assert (result.stop_reason, result.usage.total_tokens) == ("max_tokens", 1000)
    assert model.calls == 1 and ctx.read_calls == [ctx.staged_read_args]


def test_the_tool_call_limit_ends_the_run_before_the_model_is_asked_again() -> None:
    ctx = make_context(**limits_changes(max_tool_calls=1))
    two = [call(READ_TOOL, ctx.staged_read_args, "c1"), call(READ_TOOL, ctx.staged_read_args, "c2")]
    model = ScriptedModel([(two, (70, 10)), ([answer("never asked")], (1, 1))])
    result = run(model, ctx)
    assert result.stop_reason == "max_tool_calls" and model.calls == 1
    assert len(ctx.read_calls) == 1


def test_the_deadline_ends_the_run_before_the_model_is_asked_again() -> None:
    fake = FakeStepClock()
    ctx = make_context()
    ctx.clock = fake.step_clock()
    model = ScriptedModel(
        [
            ([call(READ_TOOL, ctx.staged_read_args, "c1")], (70, 10)),
            ([answer("never asked")], (1, 1)),
        ],
        on_call=lambda: fake.advance(700.0),
    )
    result = run(model, ctx)
    assert result.stop_reason == "deadline" and model.calls == 1


def test_a_read_tool_that_raises_ends_the_step_with_a_fixed_code_and_no_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ctx = make_context()
    base = ctx.default_input

    def fails(**_: Any) -> str:
        raise RuntimeError("a-secret-tool-message")

    from caliber.workflows.agent_step import AgentStepInput

    ctx.custom_input = AgentStepInput(base.agent, base.input_text, read_tools={READ_TOOL: fails})
    model = ScriptedModel(
        [([call(READ_TOOL, ctx.staged_read_args, "c1")], (70, 10)), ([answer("done")], (5, 5))]
    )
    result = run(model, ctx)
    assert (result.stop_reason, result.error_code) == ("error", "malformed_result")
    assert "a-secret-tool-message" not in caplog.text and "a-secret-tool-message" not in repr(
        result
    )
