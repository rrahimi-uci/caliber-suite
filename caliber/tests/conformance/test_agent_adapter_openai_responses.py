"""Offline conformance and boundary tests for the OpenAI Responses step adapter."""

from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from caliber.contracts.agent_step import AgentStepResult, parse_step_result
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
from caliber.workflows import step_openai_responses as adapter
from caliber.workflows.agent_step import run_agent_step
from caliber.workflows.runtime import OpenAIResponsesWorkflowExecutor
from caliber.workflows.step_openai_responses import ResponsesStepConfig, run_openai_responses_step
from tests.agent_step_conformance import (
    PROTECTED_CLASS,
    PROTECTED_TOOL,
    READ_TOOL,
    SCENARIOS,
    SECRET_MARKER,
    Scenario,
    StepContext,
    make_context,
    run_scenario,
)

NOW = datetime(2026, 10, 26, 9, 0, 0, tzinfo=timezone.utc)
DEADLINE = "2026-10-26T09:10:00Z"


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now
        self.reads = 0

    def __call__(self) -> datetime:
        self.reads += 1
        return self.now


def function_call(name: str, arguments: Any, call_id: str = "call_1") -> SimpleNamespace:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return SimpleNamespace(type="function_call", name=name, arguments=raw, call_id=call_id)


def response(
    *,
    output: list[Any] | None = None,
    text: str | None = None,
    usage: tuple[int, int, int] | None = (10, 5, 15),
    response_id: str = "resp_1",
    incomplete_reason: str | None = None,
) -> SimpleNamespace:
    if output is None:
        output = [
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text=text or "done")],
            )
        ]
    result = SimpleNamespace(id=response_id, output=output, output_text=text)
    if usage is not None:
        result.usage = SimpleNamespace(
            input_tokens=usage[0], output_tokens=usage[1], total_tokens=usage[2]
        )
    if incomplete_reason is not None:
        result.incomplete_details = SimpleNamespace(reason=incomplete_reason)
    return result


class StubClient:
    def __init__(self, script: list[Any], ctx: StepContext | None = None) -> None:
        self.script = list(script)
        self.ctx = ctx
        self.requests: list[dict[str, Any]] = []
        self.options: list[dict[str, Any]] = []
        self.responses = SimpleNamespace(create=self._create)

    def with_options(self, **options: Any) -> StubClient:
        self.options.append(options)
        return self

    def _create(self, **kwargs: Any) -> Any:
        if self.ctx is not None:
            self.ctx.provider_called()
        self.requests.append(copy.deepcopy(kwargs))
        entry = self.script.pop(0)
        if isinstance(entry, BaseException):
            raise entry
        return entry(kwargs) if callable(entry) else entry


def envelope(ctx: StepContext, **changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "connector": "document-request",
        "connector_version": "1.0.0",
        "effect_class": PROTECTED_CLASS,
        "resource": "cases/CASE-1/document-requests",
        "args": ctx.staged_args,
    }
    value.update(changes)
    return value


def limits(**changes: Any) -> dict[str, Any]:
    value = {"max_turns": 8, "max_tool_calls": 4, "max_tokens": 20000, "deadline": DEADLINE}
    value.update(changes)
    return value


def executor_for(
    client: Any,
    clock: Clock | None = None,
    default_model: str = "gpt-4o",
    **options: Any,
) -> OpenAIResponsesWorkflowExecutor:
    return OpenAIResponsesWorkflowExecutor(
        api_key="unused",
        default_model=default_model,
        client=client,
        step_clock=clock or Clock(),
        **options,
    )


def step(
    script: list[Any],
    scenario: Scenario = Scenario.COMPLETES,
    *,
    step_limits: dict[str, Any] | None = None,
    clock: Clock | None = None,
    **options: Any,
) -> tuple[AgentStepResult, StepContext, StubClient]:
    ctx = make_context(scenario, limits=step_limits or limits())
    client = StubClient(script, ctx)
    result = run_agent_step(executor_for(client, clock, **options), ctx.request, ctx.step_input)
    assert parse_step_result(result.record(), ctx.request) == result
    assert ctx.protected_calls == []
    return result, ctx, client


class ResponsesDriver:
    name = "openai_responses"
    unsupported = frozenset({Scenario.ANOTHER_STEPS_RESULT})

    def adapter_for(self, scenario: Scenario, ctx: StepContext) -> object:
        usage = ctx.staged_usage
        staged: dict[Scenario, list[Any]] = {
            Scenario.COMPLETES: [
                response(
                    text="done",
                    usage=(usage.prompt_tokens, usage.completion_tokens, usage.total_tokens),
                )
            ],
            Scenario.READ_TOOL_CALL: [
                response(
                    output=[function_call(READ_TOOL, ctx.staged_read_args)], usage=(70, 10, 80)
                ),
                response(text="the case is open", usage=(80, 20, 100)),
            ],
            Scenario.PROPOSES_PROTECTED_TOOL: [
                response(
                    output=[function_call(PROTECTED_TOOL, envelope(ctx))], usage=(100, 20, 120)
                )
            ],
            Scenario.MODEL_LIES_ABOUT_EFFECT_CLASS: [
                response(
                    output=[function_call(PROTECTED_TOOL, envelope(ctx, effect_class="read"))],
                    usage=(100, 20, 120),
                )
            ],
            Scenario.BUDGET_EXHAUSTED: [
                response(
                    text="cut off", usage=(900, 100, 1000), incomplete_reason="max_output_tokens"
                )
            ],
            Scenario.PROVIDER_ERROR: [RuntimeError(f"upstream said {SECRET_MARKER}")],
            Scenario.PROVIDER_TIMEOUT: [httpx.ReadTimeout(SECRET_MARKER)],
            Scenario.UNREADABLE_OUTPUT: [
                response(output=[SimpleNamespace(type="unsupported_output")])
            ],
            Scenario.CLAIMS_COMMIT: [
                response(
                    output=[
                        function_call(
                            PROTECTED_TOOL,
                            {**envelope(ctx), "state": "committed", "receipt_id": SECRET_MARKER},
                        )
                    ],
                    usage=None,
                )
            ],
        }
        return executor_for(StubClient(staged[scenario], ctx))


def _responses_entry(item: ScriptItem, number: int) -> Any:
    """What the stubbed Responses transport does for one step of a conformance script."""
    if isinstance(item, Fault):
        return httpx.ReadTimeout(item.text) if item.kind == "timeout" else fault_exception(item)
    if isinstance(item, Garbage):
        return response(output=[SimpleNamespace(type="unsupported_output", text=item.text)])
    if item.usage is None:
        usage: Any = None
    elif item.usage is MALFORMED_USAGE:
        usage = ("many", "some", "lots")
    else:
        assert isinstance(item.usage, ProviderUsage)
        usage = (item.usage.prompt, item.usage.completion, item.usage.total_tokens)
    calls = [function_call(c.name, c.arguments, f"call_{n}") for n, c in enumerate(item.calls, 1)]
    return response(
        output=calls or None,
        text=item.text,
        usage=usage,
        response_id=f"resp_{number}",
        incomplete_reason="max_output_tokens" if item.cut_off else None,
    )


def responses_subject() -> AdapterUnderTest:
    """The conformance suite's subject for ``OpenAIResponsesWorkflowExecutor``: the real executor over a canned transport."""

    def build(script: Script, probe: ProviderProbe) -> object:
        client = StubClient([_responses_entry(item, n) for n, item in enumerate(script, 1)], probe)  # type: ignore[arg-type]
        return executor_for(client, probe.clock.now)  # type: ignore[arg-type]

    return AdapterUnderTest(name="openai_responses", build=build)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_openai_responses_executor_conforms_to_the_shared_harness(scenario: Scenario) -> None:
    result, ctx = run_scenario(ResponsesDriver(), scenario)
    assert result.step_request_id == ctx.request.step_request_id


def test_responses_request_uses_function_specs_and_previous_response_id() -> None:
    result, ctx, client = step(
        [
            response(output=[function_call(READ_TOOL, {"case_id": "case"})], usage=(10, 2, 12)),
            response(text="done", usage=(20, 3, 23)),
        ]
    )
    assert result.stop_reason == "completed"
    assert len(client.requests) == 2
    first = {item["name"]: item for item in client.requests[0]["tools"]}
    assert set(first) == {READ_TOOL, PROTECTED_TOOL}
    assert first[READ_TOOL]["parameters"]["type"] == "object"
    assert first[PROTECTED_TOOL]["parameters"]["additionalProperties"] is False
    assert client.requests[1]["previous_response_id"] == "resp_1"
    assert client.requests[1]["input"] == [
        {"type": "function_call_output", "call_id": "call_1", "output": "data"}
    ]
    assert client.options and client.options[0]["max_retries"] == 0
    assert ctx.read_calls == [{"case_id": "case"}]


def test_protected_call_is_proposed_before_any_read_call_in_same_response() -> None:
    staged = {
        "connector": "document-request",
        "connector_version": "1.0.0",
        "effect_class": PROTECTED_CLASS,
        "resource": "cases/CASE-1/document-requests",
        "args": {"document_type": "pay_statement-completes"},
    }
    result, ctx, _ = step(
        [
            response(
                output=[
                    function_call(PROTECTED_TOOL, staged),
                    function_call(READ_TOOL, {"case_id": "ignored"}, "call_2"),
                ],
                usage=(10, 2, 12),
            )
        ]
    )
    assert result.stop_reason == "proposed_action"
    assert ctx.read_calls == []


def test_limits_are_checked_before_next_response_and_before_provider() -> None:
    result, _, client = step(
        [
            response(output=[function_call(READ_TOOL, {"case_id": "one"})], usage=(10, 2, 12)),
            response(text="not called", usage=(10, 2, 12)),
        ],
        step_limits=limits(max_turns=1),
    )
    assert result.stop_reason == "max_turns"
    assert len(client.requests) == 1

    deadline = Clock(NOW)
    result, _, client = step(
        [response(text="not called")],
        step_limits=limits(
            deadline=(NOW - timedelta(seconds=1)).isoformat().replace("+00:00", "Z")
        ),
        clock=deadline,
    )
    assert result.stop_reason == "deadline"
    assert client.requests == []


def test_tool_call_limit_returns_a_bounded_stop_without_running_the_read_tool() -> None:
    result, ctx, client = step(
        [response(output=[function_call(READ_TOOL, {"case_id": "one"})], usage=(10, 2, 12))],
        step_limits=limits(max_tool_calls=0),
    )
    assert result.stop_reason == "max_tool_calls"
    assert ctx.read_calls == []
    assert len(client.requests) == 1


def test_missing_usage_is_explicitly_estimated_and_cached_usage_is_preserved() -> None:
    result, _, _ = step([response(text="done", usage=None)])
    assert result.stop_reason == "completed"
    assert result.usage_source == "estimated"
    assert result.usage.total_tokens > 0

    cached = SimpleNamespace(input_tokens_details=SimpleNamespace(cached_tokens=3))
    client = StubClient([response(text="done")])
    client.script[0].usage = SimpleNamespace(
        input_tokens=10,
        output_tokens=2,
        total_tokens=12,
        input_tokens_details=cached.input_tokens_details,
    )
    ctx = make_context()
    result = run_agent_step(executor_for(client), ctx.request, ctx.step_input)
    assert result.usage.cached_prompt_tokens == 3


def test_malformed_usage_and_missing_continuation_id_are_typed_failures() -> None:
    bad = response(text="done")
    bad.usage = SimpleNamespace(input_tokens="ten", output_tokens=1, total_tokens=2)
    result, _, _ = step([bad])
    assert result.stop_reason == "error"
    assert result.error_code == "malformed_result"

    first = response(output=[function_call(READ_TOOL, {"case_id": "one"})])
    first.id = ""
    result, _, _ = step([first])
    assert result.stop_reason == "error"
    assert result.error_code == "malformed_result"


def test_timeout_is_bounded_and_provider_payload_is_not_logged_or_returned(caplog: Any) -> None:
    result, _, client = step([httpx.ReadTimeout(SECRET_MARKER)])
    assert result.stop_reason == "error"
    assert result.error_code == "adapter_timeout"
    assert SECRET_MARKER not in caplog.text
    assert client.options[0]["timeout"] <= 600


def test_executor_run_step_is_not_the_legacy_run_agent_path() -> None:
    client = StubClient([response(text="done")])
    executor = executor_for(client)
    ctx = make_context()
    result = executor.run_step(ctx.request, ctx.step_input)
    assert type(result) is AgentStepResult
    assert result.stop_reason == "completed"


def test_config_direct_call_keeps_provider_error_typed() -> None:
    ctx = make_context()
    result = run_openai_responses_step(
        ResponsesStepConfig(
            StubClient([RuntimeError("secret")]), "gpt-4o", "medium", clock=Clock()
        ),
        ctx.request,
        ctx.step_input,
    )
    assert result.error_code == "provider_error"
    assert result.stop_reason == "error"


def test_refusal_and_unknown_tool_are_not_completed() -> None:
    refused = response(
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="refusal", refusal="not allowed")],
            )
        ]
    )
    result, _, _ = step([refused])
    assert result.stop_reason == "refused"
    assert result.error_code == adapter.MODEL_REFUSED

    unknown = response(output=[function_call("not_bound_to_agent", {})])
    result, _, _ = step([unknown])
    assert result.stop_reason == "error"
    assert result.error_code == "malformed_result"


def test_reasoning_parallel_and_prompt_cache_request_options() -> None:
    result, _, client = step(
        [response(text="done")],
        default_model="o1",
        reasoning_effort="low",
        parallel_tool_calls=True,
        prompt_cache_enabled=True,
        prompt_cache_retention="24h",
    )
    assert result.stop_reason == "completed"
    request = client.requests[0]
    assert request["reasoning"] == {"effort": "low"}
    assert request["parallel_tool_calls"] is True
    assert request["prompt_cache_key"]
    assert request["prompt_cache_retention"] == "24h"


def test_usage_and_response_shape_validation_fail_closed() -> None:
    oversized = response(text="done")
    oversized.usage = SimpleNamespace(input_tokens=2_147_483_648, output_tokens=0, total_tokens=0)
    result, _, _ = step([oversized])
    assert result.error_code == "malformed_result"

    no_figures = response(text="done")
    no_figures.usage = SimpleNamespace()
    result, _, _ = step([no_figures])
    assert result.usage_source == "estimated"

    invalid_json = response(output=[function_call(PROTECTED_TOOL, "{")])
    result, _, _ = step([invalid_json])
    assert result.error_code == "malformed_result"

    invalid_call = response(
        output=[SimpleNamespace(type="function_call", name="x", arguments="{}", call_id="")]
    )
    result, _, _ = step([invalid_call])
    assert result.error_code == "malformed_result"

    invalid_text = response(
        output=[
            SimpleNamespace(
                type="message",
                content=[SimpleNamespace(type="output_text", text=object())],
            )
        ]
    )
    result, _, _ = step([invalid_text])
    assert result.error_code == "malformed_result"

    invalid_incomplete = response(text="done")
    invalid_incomplete.incomplete_details = SimpleNamespace(reason=object())
    result, _, _ = step([invalid_incomplete])
    assert result.error_code == "malformed_result"


def test_builtin_timeout_and_default_clock_are_supported() -> None:
    result, _, _ = step([TimeoutError(SECRET_MARKER)])
    assert result.error_code == "adapter_timeout"

    ctx = make_context(
        limits={
            "max_turns": 8,
            "max_tool_calls": 4,
            "max_tokens": 20000,
            "deadline": "2030-01-01T00:00:00Z",
        }
    )
    client = StubClient([response(text="done")])
    result = run_agent_step(
        OpenAIResponsesWorkflowExecutor(api_key="unused", default_model="gpt-4o", client=client),
        ctx.request,
        ctx.step_input,
    )
    assert result.stop_reason == "completed"


def test_empty_response_and_unknown_content_are_malformed() -> None:
    empty = SimpleNamespace(
        output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="unknown")])],
        usage=None,
    )
    result, _, _ = step([empty])
    assert result.error_code == "malformed_result"

    unsupported = response(output=[SimpleNamespace(type="computer_call")])
    result, _, _ = step([unsupported])
    assert result.error_code == "malformed_result"


def test_tally_overflow_and_imported_timeout_type_are_defensive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tally = adapter._Tally()
    with pytest.raises(adapter._Unreadable):
        tally.add(2_147_483_647, 1, 2_147_483_647, None, estimated=False)

    real_import = adapter.importlib.import_module

    def missing(name: str) -> Any:
        if name == "openai":
            raise ImportError("optional")
        return real_import(name)

    monkeypatch.setattr(adapter.importlib, "import_module", missing)
    assert adapter._is_timeout(httpx.ReadTimeout("payload"))
