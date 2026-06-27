"""Tests for the real agentic tool-calling loop (golden-path roadmap, Wave 4).

Exercises the OpenAI and Anthropic workflow executors against fake SDK clients:
the model chooses tools (with arguments), the loop executes them through CALIBER's
resolved callables and feeds results back, terminates (incl. the iteration cap),
gates ``requires_approval`` tools, accumulates tokens, and records the model. Also
unit-tests the shared helpers and the ``build_executor`` Anthropic branch.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace

import pytest

from caliber.config import CaliberConfig
from caliber.workflows.ir import IRAgent, IRHandoff, IRToolBinding, NodeType
from caliber.workflows.promoter import build_executor
from caliber.workflows.runtime import (
    MAX_AGENT_TOOL_ITERATIONS,
    AnthropicChatWorkflowExecutor,
    OpenAIAgentsWorkflowExecutor,
    OpenAIChatWorkflowExecutor,
    OpenAIResponsesWorkflowExecutor,
    _anthropic_tool_specs,
    _call_tool,
    _openai_tool_specs,
    _parse_tool_arguments,
    _tool_parameters,
)

# --------------------------------------------------------------------------- #
# IR builders
# --------------------------------------------------------------------------- #


def _binding(
    local_name: str,
    *,
    requires_approval: bool = False,
    side_effect: str = "read",
    input_schema: dict | None = None,
) -> IRToolBinding:
    return IRToolBinding(
        local_name=local_name,
        registry_ref=f"tool.{local_name}.v1",
        version_constraint="",
        requires_approval=requires_approval,
        side_effect_level=side_effect,
        allow_in_preview=True,
        module_path="<in-memory>",
        callable_name=local_name,
        input_schema=input_schema,
    )


def _agent(*bindings: IRToolBinding, output_type: dict | None = None) -> IRAgent:
    return IRAgent(
        node_id="agent",
        node_type=NodeType.AGENT,
        name="Agent",
        tools=list(bindings),
        output_type=output_type,
    )


_QUERY_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
}


# --------------------------------------------------------------------------- #
# Fake OpenAI client
# --------------------------------------------------------------------------- #


class _OAFunc:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _OAToolCall:
    def __init__(self, id_: str, name: str, arguments: str) -> None:
        self.id = id_
        self.function = _OAFunc(name, arguments)


class _OAMsg:
    def __init__(
        self,
        content: str | None = None,
        tool_calls: list | None = None,
        *,
        refusal: str | None = None,
        parsed=None,
    ) -> None:
        self.content = content
        self.tool_calls = tool_calls
        self.refusal = refusal
        self.parsed = parsed


class _OAUsage:
    def __init__(self, p: int, c: int, t: int, cached: int = 0) -> None:
        self.prompt_tokens = p
        self.completion_tokens = c
        self.total_tokens = t
        self.prompt_tokens_details = type("Details", (), {"cached_tokens": cached})()


class _OAResp:
    def __init__(self, message: _OAMsg, usage: _OAUsage) -> None:
        self.choices = [type("C", (), {"message": message})()]
        self.usage = usage


class _OACompletions:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeOpenAI:
    def __init__(self, responses: list) -> None:
        self.chat = type("Chat", (), {"completions": _OACompletions(responses)})()


def _tool_call_resp(call_id: str, name: str, args: dict, usage=(5, 5, 10)) -> _OAResp:
    return _OAResp(
        _OAMsg(tool_calls=[_OAToolCall(call_id, name, json.dumps(args))]), _OAUsage(*usage)
    )


def _final_resp(text: str, usage=(8, 12, 20)) -> _OAResp:
    return _OAResp(_OAMsg(content=text), _OAUsage(*usage))


# --------------------------------------------------------------------------- #
# Fake OpenAI Responses client
# --------------------------------------------------------------------------- #


class _RContentPart:
    def __init__(self, text: str) -> None:
        self.type = "output_text"
        self.text = text


class _RMessage:
    def __init__(self, text: str) -> None:
        self.type = "message"
        self.content = [_RContentPart(text)]


class _RFunctionCall:
    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.type = "function_call"
        self.call_id = call_id
        self.name = name
        self.arguments = arguments


class _RUsage:
    def __init__(self, i: int, o: int, t: int, cached: int = 0) -> None:
        self.input_tokens = i
        self.output_tokens = o
        self.total_tokens = t
        self.input_tokens_details = type("Details", (), {"cached_tokens": cached})()


class _RResp:
    def __init__(
        self, response_id: str, output: list, usage: _RUsage, *, output_text: str | None = None
    ) -> None:
        self.id = response_id
        self.output = output
        self.usage = usage
        self.output_text = output_text


class _RResponses:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeOpenAIResponses:
    def __init__(self, responses: list) -> None:
        self.responses = _RResponses(responses)


def _responses_tool_call_resp(
    response_id: str,
    call_id: str,
    name: str,
    args: dict,
    usage=(5, 5, 10),
) -> _RResp:
    return _RResp(
        response_id,
        [_RFunctionCall(call_id, name, json.dumps(args))],
        _RUsage(*usage),
    )


def _responses_final_resp(response_id: str, text: str, usage=(8, 12, 20)) -> _RResp:
    return _RResp(
        response_id,
        [_RMessage(text)],
        _RUsage(*usage),
        output_text=text,
    )


# --------------------------------------------------------------------------- #
# Fake OpenAI Agents SDK module
# --------------------------------------------------------------------------- #


def _install_agents_workflow_sdk(
    monkeypatch: pytest.MonkeyPatch,
    *,
    handler,
) -> tuple[type[object], type[object], type[object], type[Exception]]:
    class FakeFunctionTool:
        def __init__(self, **kwargs: object) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

    class FakeAgent:
        instances: list[FakeAgent] = []

        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            self.name = str(kwargs.get("name") or "")
            self.tools = list(kwargs.get("tools") or [])
            self.handoffs = list(kwargs.get("handoffs") or [])
            FakeAgent.instances.append(self)

    class FakeHandoff:
        def __init__(self, agent: FakeAgent, **kwargs: object) -> None:
            self.agent = agent
            self.agent_name = agent.name
            self.tool_description = kwargs.get("tool_description_override") or ""
            self.input_filter = kwargs.get("input_filter")
            self.is_enabled = kwargs.get("is_enabled", True)

            async def _invoke(_ctx: object, _raw_input: str) -> FakeAgent:
                return agent

            self.on_invoke_handoff = _invoke

    class FakeModelSettings:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class FakeOpenAIProvider:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

    class FakeRunConfig:
        def __init__(self, **kwargs: object) -> None:
            for key, value in kwargs.items():
                setattr(self, key, value)

    class FakeItemHelpers:
        @staticmethod
        def extract_text(item: object) -> str | None:
            return getattr(item, "text", None)

        @staticmethod
        def extract_refusal(item: object) -> str | None:
            return getattr(item, "refusal", None)

        @staticmethod
        def extract_last_content(item: object) -> str:
            return getattr(item, "text", "") or getattr(item, "refusal", "") or ""

    class FakeMaxTurnsExceededError(Exception):
        def __init__(self, message: str) -> None:
            super().__init__(message)
            self.run_data = None

    class FakeRunner:
        calls: list[dict[str, object]] = []

        @staticmethod
        def run_sync(agent: object, input: object, **kwargs: object) -> object:
            FakeRunner.calls.append({"agent": agent, "input": input, **kwargs})
            return handler(agent, input, **kwargs)

    agents_mod = types.ModuleType("agents")
    agents_mod.Agent = FakeAgent
    agents_mod.FunctionTool = FakeFunctionTool
    agents_mod.ItemHelpers = FakeItemHelpers
    agents_mod.MaxTurnsExceeded = FakeMaxTurnsExceededError
    agents_mod.ModelSettings = FakeModelSettings
    agents_mod.OpenAIProvider = FakeOpenAIProvider
    agents_mod.RunConfig = FakeRunConfig
    agents_mod.Runner = FakeRunner
    agents_mod.handoff = lambda agent, **kwargs: FakeHandoff(agent, **kwargs)
    monkeypatch.setitem(sys.modules, "agents", agents_mod)
    return FakeAgent, FakeRunner, FakeOpenAIProvider, FakeMaxTurnsExceededError


class _AToolMsg:
    def __init__(self, *, text: str = "", refusal: str = "") -> None:
        self.text = text
        self.refusal = refusal


class _AToolUsage:
    def __init__(self, i: int, o: int, t: int, cached: int = 0) -> None:
        self.input_tokens = i
        self.output_tokens = o
        self.total_tokens = t
        self.input_tokens_details = type("Details", (), {"cached_tokens": cached})()


class _AToolResp:
    def __init__(self, text: str, usage: tuple[int, int, int] | tuple[int, int, int, int]) -> None:
        self.output = [_AToolMsg(text=text)]
        self.usage = _AToolUsage(*usage)


# --------------------------------------------------------------------------- #
# OpenAI executor loop
# --------------------------------------------------------------------------- #


def test_openai_loop_executes_model_chosen_tool_with_args() -> None:
    client = _FakeOpenAI(
        [
            _tool_call_resp("c1", "lookup_policy", {"query": "refund"}, usage=(10, 5, 15)),
            _final_resp("Our refund policy is 30 days.", usage=(8, 12, 20)),
        ]
    )
    executor = OpenAIChatWorkflowExecutor(api_key="x", default_model="gpt-4o", client=client)

    seen: dict[str, str] = {}

    def lookup_policy(query: str = "") -> dict:
        seen["query"] = query
        return {"policy": "30 days"}

    result = executor.run_agent(
        _agent(_binding("lookup_policy", input_schema=_QUERY_SCHEMA)),
        "what is the refund policy?",
        tool_callables={"lookup_policy": lookup_policy},
        preview=False,
    )

    assert result.final_output == "Our refund policy is 30 days."
    assert seen["query"] == "refund"  # model args passed as kwargs
    assert result.tool_calls == [{"tool": "lookup_policy", "result": {"policy": "30 days"}}]
    assert result.tokens == 35  # 15 + 20 accumulated across the loop
    assert result.prompt_tokens == 18
    assert result.completion_tokens == 17
    assert result.model == "gpt-4o"
    calls = client.chat.completions.calls
    assert len(calls) == 2
    assert "tools" in calls[0] and calls[0]["tool_choice"] == "auto"
    # The 2nd request carried the assistant tool_calls message + the tool result.
    roles = [m["role"] for m in calls[1]["messages"]]
    assert "tool" in roles and "assistant" in roles


def test_openai_loop_no_tools_single_completion() -> None:
    client = _FakeOpenAI([_final_resp("hello", usage=(3, 4, 7))])
    executor = OpenAIChatWorkflowExecutor(api_key="x", default_model="m", client=client)
    result = executor.run_agent(_agent(), "hi", tool_callables={}, preview=False)
    assert result.final_output == "hello"
    assert result.tool_calls == []
    assert "tools" not in client.chat.completions.calls[0]


def test_openai_loop_adds_response_format_and_parses_structured_output() -> None:
    schema = {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "grounded": {"type": "boolean"},
        },
        "required": ["answer", "grounded"],
    }
    payload = {"answer": "30 day refund window.", "grounded": True}
    client = _FakeOpenAI([_final_resp(json.dumps(payload), usage=(4, 7, 11))])
    executor = OpenAIChatWorkflowExecutor(api_key="x", default_model="gpt-4o", client=client)

    result = executor.run_agent(
        _agent(output_type=schema),
        "refund policy?",
        tool_callables={},
        preview=False,
    )

    assert result.structured_output == payload
    assert json.loads(result.final_output) == payload
    response_format = client.chat.completions.calls[0]["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["schema"] == schema
    assert response_format["json_schema"]["strict"] is True


def test_openai_loop_can_request_parallel_tool_calls() -> None:
    client = _FakeOpenAI([_final_resp("hello", usage=(3, 4, 7))])
    executor = OpenAIChatWorkflowExecutor(
        api_key="x",
        default_model="gpt-4o",
        client=client,
        parallel_tool_calls=True,
    )

    result = executor.run_agent(
        _agent(_binding("lookup_policy", input_schema=_QUERY_SCHEMA)),
        "refund policy?",
        tool_callables={"lookup_policy": lambda query="": {"policy": query or "30 days"}},
        preview=False,
    )

    assert result.final_output == "hello"
    assert client.chat.completions.calls[0]["parallel_tool_calls"] is True


def test_openai_loop_can_send_prompt_cache_hints_and_track_cached_prompt_tokens() -> None:
    client = _FakeOpenAI(
        [
            _tool_call_resp("c1", "lookup_policy", {"query": "refund"}, usage=(10, 5, 15, 8)),
            _final_resp("Our refund policy is 30 days.", usage=(8, 12, 20, 6)),
        ]
    )
    executor = OpenAIChatWorkflowExecutor(
        api_key="x",
        default_model="gpt-4o",
        client=client,
        prompt_cache_enabled=True,
        prompt_cache_retention="24h",
    )

    result = executor.run_agent(
        _agent(_binding("lookup_policy", input_schema=_QUERY_SCHEMA)),
        "refund policy?",
        tool_callables={"lookup_policy": lambda query="": {"policy": query or "30 days"}},
        preview=False,
    )

    assert result.cached_prompt_tokens == 14
    assert result.cost_usd == 0.000198
    calls = client.chat.completions.calls
    assert calls[0]["prompt_cache_retention"] == "24h"
    assert calls[0]["prompt_cache_key"].startswith("caliber:chat:")
    assert calls[1]["prompt_cache_key"] == calls[0]["prompt_cache_key"]


def test_openai_loop_gates_requires_approval_tool() -> None:
    client = _FakeOpenAI(
        [
            _tool_call_resp("c1", "initiate_refund", {"order_id": "7"}),
            _final_resp("done"),
        ]
    )
    executor = OpenAIChatWorkflowExecutor(api_key="x", default_model="m", client=client)
    calls = {"n": 0}

    def initiate_refund(order_id: str = "") -> dict:
        calls["n"] += 1
        return {"refunded": True}

    result = executor.run_agent(
        _agent(_binding("initiate_refund", requires_approval=True, side_effect="write")),
        "refund order 7",
        tool_callables={"initiate_refund": initiate_refund},
        preview=False,
    )
    assert calls["n"] == 0  # gated: real tool never executed
    assert result.tool_calls[0]["result"]["_gated"] is True


def test_openai_loop_unknown_tool_returns_error_marker() -> None:
    client = _FakeOpenAI(
        [
            _tool_call_resp("c1", "ghost_tool", {"x": 1}),
            _final_resp("ok"),
        ]
    )
    executor = OpenAIChatWorkflowExecutor(api_key="x", default_model="m", client=client)
    result = executor.run_agent(
        _agent(),  # no bindings → ghost_tool unknown
        "go",
        tool_callables={},
        preview=False,
    )
    assert result.tool_calls[0]["result"]["_error"].startswith("unknown tool")


def test_openai_loop_terminates_at_iteration_cap() -> None:
    # Model keeps requesting tools every turn; the loop must still terminate.
    responses = [
        _tool_call_resp(f"c{i}", "lookup_policy", {"query": str(i)})
        for i in range(MAX_AGENT_TOOL_ITERATIONS)
    ]
    responses.append(_final_resp("forced final answer"))
    client = _FakeOpenAI(responses)
    executor = OpenAIChatWorkflowExecutor(api_key="x", default_model="m", client=client)
    result = executor.run_agent(
        _agent(_binding("lookup_policy", input_schema=_QUERY_SCHEMA)),
        "loop",
        tool_callables={"lookup_policy": lambda query="": {"n": query}},
        preview=False,
    )
    assert result.final_output == "forced final answer"
    assert len(result.tool_calls) == MAX_AGENT_TOOL_ITERATIONS
    # MAX tool-iterations + one final no-tools completion.
    assert len(client.chat.completions.calls) == MAX_AGENT_TOOL_ITERATIONS + 1
    assert "tools" not in client.chat.completions.calls[-1]


# --------------------------------------------------------------------------- #
# OpenAI Responses executor loop
# --------------------------------------------------------------------------- #


def test_openai_responses_loop_executes_model_chosen_tool_with_args() -> None:
    client = _FakeOpenAIResponses(
        [
            _responses_tool_call_resp(
                "resp-1", "call-1", "lookup_policy", {"query": "refund"}, usage=(10, 5, 15)
            ),
            _responses_final_resp("resp-2", "Our refund policy is 30 days.", usage=(8, 12, 20)),
        ]
    )
    executor = OpenAIResponsesWorkflowExecutor(api_key="x", default_model="gpt-5.4", client=client)

    seen: dict[str, str] = {}

    def lookup_policy(query: str = "") -> dict:
        seen["query"] = query
        return {"policy": "30 days"}

    result = executor.run_agent(
        _agent(_binding("lookup_policy", input_schema=_QUERY_SCHEMA)),
        "what is the refund policy?",
        tool_callables={"lookup_policy": lookup_policy},
        preview=False,
    )

    assert result.final_output == "Our refund policy is 30 days."
    assert seen["query"] == "refund"
    assert result.tool_calls == [{"tool": "lookup_policy", "result": {"policy": "30 days"}}]
    assert result.tokens == 35
    assert result.prompt_tokens == 18
    assert result.completion_tokens == 17
    assert result.model == "gpt-5.4"
    calls = client.responses.calls
    assert len(calls) == 2
    assert "tools" in calls[0] and calls[0]["tool_choice"] == "auto"
    assert calls[1]["previous_response_id"] == "resp-1"
    assert calls[1]["input"] == [
        {"type": "function_call_output", "call_id": "call-1", "output": '{"policy": "30 days"}'}
    ]


def test_openai_responses_loop_no_tools_single_completion() -> None:
    client = _FakeOpenAIResponses([_responses_final_resp("resp-1", "hello", usage=(3, 4, 7))])
    executor = OpenAIResponsesWorkflowExecutor(api_key="x", default_model="m", client=client)
    result = executor.run_agent(_agent(), "hi", tool_callables={}, preview=False)
    assert result.final_output == "hello"
    assert result.tool_calls == []
    assert "tools" not in client.responses.calls[0]


def test_openai_responses_loop_gates_requires_approval_tool() -> None:
    client = _FakeOpenAIResponses(
        [
            _responses_tool_call_resp("resp-1", "call-1", "initiate_refund", {"order_id": "7"}),
            _responses_final_resp("resp-2", "done"),
        ]
    )
    executor = OpenAIResponsesWorkflowExecutor(api_key="x", default_model="m", client=client)
    calls = {"n": 0}

    def initiate_refund(order_id: str = "") -> dict:
        calls["n"] += 1
        return {"refunded": True}

    result = executor.run_agent(
        _agent(_binding("initiate_refund", requires_approval=True, side_effect="write")),
        "refund order 7",
        tool_callables={"initiate_refund": initiate_refund},
        preview=False,
    )
    assert calls["n"] == 0
    assert result.tool_calls[0]["result"]["_gated"] is True


def test_openai_responses_loop_failsoft_on_tool_body_error() -> None:
    client = _FakeOpenAIResponses(
        [
            _responses_tool_call_resp("resp-1", "call-1", "boom", {"query": "x"}),
            _responses_final_resp("resp-2", "recovered"),
        ]
    )
    executor = OpenAIResponsesWorkflowExecutor(api_key="x", default_model="m", client=client)
    calls = {"n": 0}

    def boom(query: str = "") -> dict:
        calls["n"] += 1
        raise RuntimeError("kaboom")

    result = executor.run_agent(
        _agent(_binding("boom", input_schema=_QUERY_SCHEMA)),
        "go",
        tool_callables={"boom": boom},
        preview=False,
    )
    assert calls["n"] == 1
    assert result.tool_calls[0]["result"]["_error"].startswith("RuntimeError")
    assert result.final_output == "recovered"


def test_openai_responses_loop_adds_text_format_and_parses_structured_output() -> None:
    schema = {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "grounded": {"type": "boolean"},
        },
        "required": ["answer", "grounded"],
    }
    payload = {"answer": "30 day refund window.", "grounded": True}
    client = _FakeOpenAIResponses(
        [_responses_final_resp("resp-1", json.dumps(payload), usage=(4, 7, 11))]
    )
    executor = OpenAIResponsesWorkflowExecutor(api_key="x", default_model="gpt-5.4", client=client)

    result = executor.run_agent(
        _agent(output_type=schema),
        "refund policy?",
        tool_callables={},
        preview=False,
    )

    assert result.structured_output == payload
    assert json.loads(result.final_output) == payload
    text_config = client.responses.calls[0]["text"]
    assert text_config["format"]["type"] == "json_schema"
    assert text_config["format"]["schema"] == schema
    assert text_config["format"]["strict"] is True


def test_openai_responses_loop_can_request_parallel_tool_calls() -> None:
    client = _FakeOpenAIResponses([_responses_final_resp("resp-1", "hello", usage=(3, 4, 7))])
    executor = OpenAIResponsesWorkflowExecutor(
        api_key="x",
        default_model="gpt-5.4",
        client=client,
        parallel_tool_calls=True,
    )

    result = executor.run_agent(
        _agent(_binding("lookup_policy", input_schema=_QUERY_SCHEMA)),
        "refund policy?",
        tool_callables={"lookup_policy": lambda query="": {"policy": query or "30 days"}},
        preview=False,
    )

    assert result.final_output == "hello"
    assert client.responses.calls[0]["parallel_tool_calls"] is True


def test_openai_responses_loop_can_send_prompt_cache_hints_and_track_cached_prompt_tokens() -> None:
    client = _FakeOpenAIResponses(
        [
            _responses_tool_call_resp(
                "resp-1", "call-1", "lookup_policy", {"query": "refund"}, usage=(10, 5, 15, 8)
            ),
            _responses_final_resp("resp-2", "Our refund policy is 30 days.", usage=(8, 12, 20, 6)),
        ]
    )
    executor = OpenAIResponsesWorkflowExecutor(
        api_key="x",
        default_model="gpt-5.4",
        client=client,
        prompt_cache_enabled=True,
        prompt_cache_retention="24h",
    )

    result = executor.run_agent(
        _agent(_binding("lookup_policy", input_schema=_QUERY_SCHEMA)),
        "refund policy?",
        tool_callables={"lookup_policy": lambda query="": {"policy": query or "30 days"}},
        preview=False,
    )

    assert result.cached_prompt_tokens == 14
    assert result.cost_usd == 0.000269
    calls = client.responses.calls
    assert calls[0]["prompt_cache_retention"] == "24h"
    assert calls[0]["prompt_cache_key"].startswith("caliber:responses:")
    assert calls[1]["prompt_cache_key"] == calls[0]["prompt_cache_key"]


def test_openai_responses_loop_terminates_at_iteration_cap() -> None:
    responses = [
        _responses_tool_call_resp(f"resp-{i}", f"call-{i}", "lookup_policy", {"query": str(i)})
        for i in range(MAX_AGENT_TOOL_ITERATIONS)
    ]
    responses.append(_responses_final_resp("resp-final", "forced final answer"))
    client = _FakeOpenAIResponses(responses)
    executor = OpenAIResponsesWorkflowExecutor(api_key="x", default_model="gpt-5.4", client=client)
    result = executor.run_agent(
        _agent(_binding("lookup_policy", input_schema=_QUERY_SCHEMA)),
        "loop",
        tool_callables={"lookup_policy": lambda query="": {"n": query}},
        preview=False,
    )
    assert result.final_output == "forced final answer"
    assert len(result.tool_calls) == MAX_AGENT_TOOL_ITERATIONS
    assert len(client.responses.calls) == MAX_AGENT_TOOL_ITERATIONS + 1
    assert client.responses.calls[-1]["tool_choice"] == "none"


# --------------------------------------------------------------------------- #
# OpenAI Agents SDK executor loop
# --------------------------------------------------------------------------- #


def test_openai_agents_executor_executes_sdk_tools_and_tracks_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    def lookup_policy(query: str = "") -> dict:
        seen["query"] = query
        return {"policy": "30 days"}

    def _handler(agent, input, **kwargs):
        tool = agent.tools[0]
        tool_output = asyncio.run(tool.on_invoke_tool(None, json.dumps({"query": "refund"})))
        assert json.loads(tool_output) == {"policy": "30 days"}
        return SimpleNamespace(
            final_output="Our refund policy is 30 days.",
            raw_responses=[_AToolResp("Our refund policy is 30 days.", (9, 11, 20))],
            last_agent=agent,
        )

    fake_agent, fake_runner, fake_provider, _ = _install_agents_workflow_sdk(
        monkeypatch,
        handler=_handler,
    )
    executor = OpenAIAgentsWorkflowExecutor(
        api_key="x",
        default_model="gpt-5.4",
        base_url="http://gw:5000/gateway/mlflow/v1",
    )

    result = executor.run_agent(
        _agent(_binding("lookup_policy", input_schema=_QUERY_SCHEMA)),
        "what is the refund policy?",
        tool_callables={"lookup_policy": lookup_policy},
        preview=False,
    )

    assert result.final_output == "Our refund policy is 30 days."
    assert seen["query"] == "refund"
    assert result.tool_calls == [{"tool": "lookup_policy", "result": {"policy": "30 days"}}]
    assert result.tokens == 20
    assert result.prompt_tokens == 9
    assert result.completion_tokens == 11
    assert result.cached_prompt_tokens == 0
    assert result.cost_usd == 0.000187
    assert result.model == "gpt-5.4"
    assert fake_agent.instances[0].kwargs["tool_use_behavior"] == "run_llm_again"
    provider = fake_runner.calls[0]["run_config"].model_provider
    assert isinstance(provider, fake_provider)
    assert provider.kwargs["base_url"] == "http://gw:5000/gateway/mlflow/v1"
    assert provider.kwargs["use_responses"] is True
    tool = fake_agent.instances[0].tools[0]
    assert tool.params_json_schema == _QUERY_SCHEMA


def test_openai_agents_executor_can_send_prompt_cache_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _handler(agent, input, **kwargs):
        del input, kwargs
        return SimpleNamespace(
            final_output="cached",
            raw_responses=[_AToolResp("cached", (5, 6, 11, 4))],
            last_agent=agent,
        )

    fake_agent, _, _, _ = _install_agents_workflow_sdk(monkeypatch, handler=_handler)
    executor = OpenAIAgentsWorkflowExecutor(
        api_key="x",
        default_model="gpt-5.4",
        prompt_cache_enabled=True,
        prompt_cache_retention="24h",
    )

    result = executor.run_agent(
        _agent(_binding("lookup_policy", input_schema=_QUERY_SCHEMA)),
        "refund policy?",
        tool_callables={"lookup_policy": lambda query="": {"policy": query or "30 days"}},
        preview=False,
    )

    assert result.cached_prompt_tokens == 4
    model_settings = fake_agent.instances[0].kwargs["model_settings"]
    assert model_settings.kwargs["prompt_cache_retention"] == "24h"
    assert model_settings.kwargs["extra_args"]["prompt_cache_key"].startswith("caliber:agents:")


def test_openai_agents_executor_can_request_parallel_tool_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _handler(agent, input, **kwargs):
        del input, kwargs
        return SimpleNamespace(
            final_output="parallel",
            raw_responses=[_AToolResp("parallel", (5, 6, 11))],
            last_agent=agent,
        )

    fake_agent, _, _, _ = _install_agents_workflow_sdk(monkeypatch, handler=_handler)
    executor = OpenAIAgentsWorkflowExecutor(
        api_key="x",
        default_model="gpt-5.4",
        parallel_tool_calls=True,
    )

    result = executor.run_agent(
        _agent(_binding("lookup_policy", input_schema=_QUERY_SCHEMA)),
        "refund policy?",
        tool_callables={"lookup_policy": lambda query="": {"policy": query or "30 days"}},
        preview=False,
    )

    assert result.final_output == "parallel"
    model_settings = fake_agent.instances[0].kwargs["model_settings"]
    assert model_settings.kwargs["extra_args"]["parallel_tool_calls"] is True


def test_openai_agents_executor_gates_requires_approval_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _handler(agent, input, **kwargs):
        del input, kwargs
        tool = agent.tools[0]
        tool_output = asyncio.run(tool.on_invoke_tool(None, json.dumps({"order_id": "7"})))
        gated = json.loads(tool_output)
        assert gated["_gated"] is True
        assert gated["tool"] == "initiate_refund"
        return SimpleNamespace(
            final_output="done",
            raw_responses=[_AToolResp("done", (5, 6, 11))],
            last_agent=agent,
        )

    _, fake_runner, _, _ = _install_agents_workflow_sdk(monkeypatch, handler=_handler)
    executor = OpenAIAgentsWorkflowExecutor(api_key="x", default_model="gpt-5.4")
    calls = {"n": 0}

    def initiate_refund(order_id: str = "") -> dict:
        calls["n"] += 1
        return {"refunded": True}

    result = executor.run_agent(
        _agent(
            _binding(
                "initiate_refund",
                requires_approval=True,
                side_effect="write",
                input_schema={
                    "type": "object",
                    "properties": {"order_id": {"type": "string"}},
                    "required": ["order_id"],
                },
            )
        ),
        "refund order 7",
        tool_callables={"initiate_refund": initiate_refund},
        preview=False,
    )

    assert calls["n"] == 0
    assert len(fake_runner.calls) == 1
    assert result.tool_calls[0]["result"]["_gated"] is True


def test_openai_agents_executor_failsoft_on_tool_body_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _handler(agent, input, **kwargs):
        del input, kwargs
        tool = agent.tools[0]
        tool_output = asyncio.run(tool.on_invoke_tool(None, json.dumps({"query": "x"})))
        error_payload = json.loads(tool_output)
        assert error_payload["_error"].startswith("RuntimeError")
        return SimpleNamespace(
            final_output="recovered",
            raw_responses=[_AToolResp("recovered", (5, 6, 11))],
            last_agent=agent,
        )

    _, fake_runner, _, _ = _install_agents_workflow_sdk(monkeypatch, handler=_handler)
    executor = OpenAIAgentsWorkflowExecutor(api_key="x", default_model="gpt-5.4")
    calls = {"n": 0}

    def boom(query: str = "") -> dict:
        calls["n"] += 1
        raise RuntimeError("kaboom")

    result = executor.run_agent(
        _agent(_binding("boom", input_schema=_QUERY_SCHEMA)),
        "go",
        tool_callables={"boom": boom},
        preview=False,
    )

    assert calls["n"] == 1
    assert len(fake_runner.calls) == 1
    assert result.tool_calls[0]["result"]["_error"].startswith("RuntimeError")
    assert result.final_output == "recovered"


def test_openai_agents_executor_parses_structured_output_and_augments_instructions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"answer": "30 day refund window.", "grounded": True}

    def _handler(agent, input, **kwargs):
        return SimpleNamespace(
            final_output=json.dumps(payload),
            raw_responses=[_AToolResp(json.dumps(payload), (4, 7, 11))],
            last_agent=agent,
        )

    fake_agent, _, _, _ = _install_agents_workflow_sdk(monkeypatch, handler=_handler)
    executor = OpenAIAgentsWorkflowExecutor(api_key="x", default_model="gpt-5.4")
    schema = {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "grounded": {"type": "boolean"},
        },
        "required": ["answer", "grounded"],
    }

    result = executor.run_agent(
        _agent(output_type=schema),
        "refund policy?",
        tool_callables={},
        preview=False,
    )

    assert result.structured_output == payload
    assert json.loads(result.final_output) == payload
    instructions = str(fake_agent.instances[0].kwargs["instructions"])
    assert "Return only valid JSON that matches this schema exactly." in instructions
    assert '"grounded"' in instructions


def test_openai_agents_executor_fail_softs_when_sdk_hits_turn_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _handler(agent, input, **kwargs):
        exc = fake_max_turns("too many turns")
        exc.run_data = SimpleNamespace(raw_responses=[_AToolResp("forced final answer", (3, 5, 8))])
        raise exc

    _, _, _, fake_max_turns = _install_agents_workflow_sdk(monkeypatch, handler=_handler)
    executor = OpenAIAgentsWorkflowExecutor(api_key="x", default_model="gpt-5.4")

    result = executor.run_agent(
        _agent(_binding("lookup_policy", input_schema=_QUERY_SCHEMA)),
        "loop",
        tool_callables={"lookup_policy": lambda query="": {"n": query}},
        preview=False,
    )

    assert result.final_output == "forced final answer"
    assert result.tokens == 8
    assert result.prompt_tokens == 3
    assert result.completion_tokens == 5
    assert result.tool_calls == []


def test_openai_agents_executor_builds_sdk_handoffs_for_reachable_agents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, str] = {}

    class _FakeHandoffInputData:
        def __init__(
            self,
            *,
            input_history: str | tuple[dict[str, object], ...],
            run_context: object,
        ) -> None:
            self.input_history = input_history
            self.pre_handoff_items = ()
            self.new_items = ()
            self.run_context = run_context
            self.input_items = None

        def clone(self, **kwargs: object) -> _FakeHandoffInputData:
            next_value = _FakeHandoffInputData(
                input_history=kwargs.get("input_history", self.input_history),
                run_context=kwargs.get("run_context", self.run_context),
            )
            next_value.pre_handoff_items = kwargs.get("pre_handoff_items", self.pre_handoff_items)
            next_value.new_items = kwargs.get("new_items", self.new_items)
            next_value.input_items = kwargs.get("input_items", self.input_items)
            return next_value

    def lookup_invoice(query: str = "") -> dict:
        seen["query"] = query
        return {"invoice": "paid"}

    def _handler(agent, input, **kwargs):
        del input, kwargs
        assert len(agent.handoffs) == 1
        billing_handoff = agent.handoffs[0]
        assert billing_handoff.agent_name == "billing"
        assert billing_handoff.tool_description == "Escalate billing"
        assert (
            billing_handoff.is_enabled(
                SimpleNamespace(turn_input=[{"role": "user", "content": "need billing help"}]),
                agent,
            )
            is True
        )
        filtered = billing_handoff.input_filter(
            _FakeHandoffInputData(
                input_history="need billing help",
                run_context=SimpleNamespace(
                    turn_input=[{"role": "user", "content": "need billing help"}]
                ),
            )
        )
        assert filtered.input_history == "Billing summary: need billing help"
        assert filtered.input_items == ()
        billing = asyncio.run(
            billing_handoff.on_invoke_handoff(
                SimpleNamespace(turn_input=[{"role": "user", "content": "need billing help"}]),
                "",
            )
        )
        tool = billing.tools[0]
        tool_output = asyncio.run(tool.on_invoke_tool(None, json.dumps({"query": "invoice-42"})))
        assert json.loads(tool_output) == {"invoice": "paid"}
        return SimpleNamespace(
            final_output="Billing resolved.",
            raw_responses=[_AToolResp("Billing resolved.", (5, 7, 12))],
            last_agent=billing,
        )

    fake_agent, _, _, _ = _install_agents_workflow_sdk(monkeypatch, handler=_handler)
    executor = OpenAIAgentsWorkflowExecutor(api_key="x", default_model="gpt-5.4")
    billing = IRAgent(
        node_id="billing",
        node_type=NodeType.AGENT,
        name="billing",
        tools=[_binding("lookup_invoice", input_schema=_QUERY_SCHEMA)],
    )
    root = IRAgent(
        node_id="agent",
        node_type=NodeType.AGENT,
        name="triage",
        handoffs=[
            IRHandoff(
                target_node_id="billing",
                description="Escalate billing",
                input_filter="Billing summary: {{input}}",
                condition="input == 'need billing help'",
            )
        ],
    )

    result = executor.run_agent(
        root,
        "need billing help",
        handoff_agents={
            "agent": (root, {}),
            "billing": (billing, {"lookup_invoice": lookup_invoice}),
        },
        tool_callables={},
        preview=False,
    )

    assert result.final_output == "Billing resolved."
    assert result.handoffs_resolved_in_executor is True
    assert seen["query"] == "invoice-42"
    assert fake_agent.instances[0].handoffs
    assert fake_agent.instances[0].handoffs[0].agent_name == "billing"


# --------------------------------------------------------------------------- #
# Fake Anthropic client + executor loop
# --------------------------------------------------------------------------- #


class _Block:
    def __init__(
        self, type_: str, *, text: str = "", id_: str = "", name: str = "", input_=None
    ) -> None:
        self.type = type_
        self.text = text
        self.id = id_
        self.name = name
        self.input = input_ or {}


class _AUsage:
    def __init__(self, i: int, o: int) -> None:
        self.input_tokens = i
        self.output_tokens = o


class _AResp:
    def __init__(self, content: list, usage: _AUsage) -> None:
        self.content = content
        self.usage = usage


class _AMessages:
    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._responses.pop(0)


class _FakeAnthropic:
    def __init__(self, responses: list) -> None:
        self.messages = _AMessages(responses)


def test_anthropic_loop_executes_tool_use_block() -> None:
    client = _FakeAnthropic(
        [
            _AResp(
                [_Block("tool_use", id_="tu1", name="lookup_policy", input_={"query": "refund"})],
                _AUsage(12, 6),
            ),
            _AResp([_Block("text", text="30 day refund window.")], _AUsage(4, 8)),
        ]
    )
    executor = AnthropicChatWorkflowExecutor(
        api_key="x", default_model="claude-sonnet-4", client=client
    )
    seen: dict[str, str] = {}

    def lookup_policy(query: str = "") -> dict:
        seen["query"] = query
        return {"policy": "30 days"}

    result = executor.run_agent(
        _agent(_binding("lookup_policy", input_schema=_QUERY_SCHEMA)),
        "refund policy?",
        tool_callables={"lookup_policy": lookup_policy},
        preview=False,
    )
    assert result.final_output == "30 day refund window."
    assert seen["query"] == "refund"
    assert result.tool_calls == [{"tool": "lookup_policy", "result": {"policy": "30 days"}}]
    assert result.tokens == 30  # (12+6) + (4+8)
    assert result.prompt_tokens == 16 and result.completion_tokens == 14
    assert result.model == "claude-sonnet-4"
    # 2nd request carried assistant tool_use + a user tool_result.
    second = client.messages.calls[1]["messages"]
    assert any(m["role"] == "assistant" for m in second)
    assert any(
        isinstance(m["content"], list)
        and m["content"]
        and m["content"][0].get("type") == "tool_result"
        for m in second
        if m["role"] == "user"
    )


def test_anthropic_loop_text_only() -> None:
    client = _FakeAnthropic([_AResp([_Block("text", text="hi there")], _AUsage(2, 3))])
    executor = AnthropicChatWorkflowExecutor(api_key="x", default_model="m", client=client)
    result = executor.run_agent(_agent(), "hi", tool_callables={}, preview=False)
    assert result.final_output == "hi there"
    assert result.tool_calls == []


def test_anthropic_loop_parses_structured_output_best_effort() -> None:
    schema = {
        "type": "object",
        "properties": {
            "answer": {"type": "string"},
            "grounded": {"type": "boolean"},
        },
        "required": ["answer", "grounded"],
    }
    payload = {"answer": "Policy-backed answer", "grounded": True}
    client = _FakeAnthropic([_AResp([_Block("text", text=json.dumps(payload))], _AUsage(3, 5))])
    executor = AnthropicChatWorkflowExecutor(api_key="x", default_model="m", client=client)

    result = executor.run_agent(
        _agent(output_type=schema),
        "hi",
        tool_callables={},
        preview=False,
    )

    assert result.structured_output == payload
    assert json.loads(result.final_output) == payload
    assert (
        "Return only valid JSON that matches this schema exactly."
        in client.messages.calls[0]["system"]
    )


# --------------------------------------------------------------------------- #
# build_executor anthropic branch
# --------------------------------------------------------------------------- #


def test_build_executor_anthropic_branch(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    cfg = CaliberConfig.load(
        environ={
            "CALIBER_LLM_PROVIDER": "anthropic",
            "CALIBER_LLM_API_KEY_ENV": "ANTHROPIC_API_KEY",
        }
    )
    executor = build_executor(cfg)
    assert isinstance(executor, AnthropicChatWorkflowExecutor)


def test_build_executor_anthropic_requires_key(monkeypatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = CaliberConfig.load(
        environ={
            "CALIBER_LLM_PROVIDER": "anthropic",
            "CALIBER_LLM_API_KEY_ENV": "ANTHROPIC_API_KEY",
        }
    )
    with pytest.raises(RuntimeError, match="anthropic requires"):
        build_executor(cfg)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def test_tool_parameters_uses_schema_or_generic() -> None:
    assert _tool_parameters(_binding("t", input_schema=_QUERY_SCHEMA)) == _QUERY_SCHEMA
    generic = _tool_parameters(_binding("t"))
    assert generic["type"] == "object"
    assert "input" in generic["properties"]


def test_openai_and_anthropic_specs_shape() -> None:
    agent = _agent(_binding("lookup_policy", input_schema=_QUERY_SCHEMA))
    oa = _openai_tool_specs(agent)
    assert oa[0]["type"] == "function"
    assert oa[0]["function"]["name"] == "lookup_policy"
    assert oa[0]["function"]["parameters"] == _QUERY_SCHEMA
    an = _anthropic_tool_specs(agent)
    assert an[0]["name"] == "lookup_policy"
    assert an[0]["input_schema"] == _QUERY_SCHEMA


def test_parse_tool_arguments() -> None:
    assert _parse_tool_arguments('{"a": 1}') == {"a": 1}
    assert _parse_tool_arguments({"b": 2}) == {"b": 2}
    assert _parse_tool_arguments("not json") == {}
    assert _parse_tool_arguments("") == {}
    assert _parse_tool_arguments("[1, 2]") == {}  # non-object JSON


def test_call_tool_adapter_paths() -> None:
    # kwargs path (input_schema props → tool params)
    assert _call_tool(lambda query="": query.upper(), {"query": "hi"}, fallback_input="x") == "HI"
    # MCP-style: callable takes a single positional; kwargs don't bind → dict positional
    assert _call_tool(lambda arg="": arg, {"query": "hi"}, fallback_input="x") == {"query": "hi"}
    # legacy single-string input when no args
    assert _call_tool(lambda text="": text, {}, fallback_input="ZZ") == "ZZ"
    # no-arg fallback
    assert _call_tool(lambda: "const", {}, fallback_input="ignored") == "const"


# --------------------------------------------------------------------------- #
# Review-hardening regressions (Wave 4 adversarial review)
# --------------------------------------------------------------------------- #


def test_call_tool_invokes_once_on_body_typeerror() -> None:
    # A TypeError raised INSIDE the tool body must not be mistaken for a binding
    # failure and re-invoke the (side-effecting) tool across shapes.
    calls = {"n": 0}

    def writer(order_id=None):  # body raises TypeError when order_id is None
        calls["n"] += 1
        return order_id + 1

    with pytest.raises(TypeError):
        _call_tool(writer, {"order_id": None}, fallback_input="x")
    assert calls["n"] == 1


def test_openai_loop_failsoft_on_tool_body_error() -> None:
    client = _FakeOpenAI(
        [
            _tool_call_resp("c1", "boom", {"query": "x"}),
            _final_resp("recovered"),
        ]
    )
    executor = OpenAIChatWorkflowExecutor(api_key="x", default_model="m", client=client)
    calls = {"n": 0}

    def boom(query: str = "") -> dict:
        calls["n"] += 1
        raise RuntimeError("kaboom")

    result = executor.run_agent(
        _agent(_binding("boom", input_schema=_QUERY_SCHEMA)),
        "go",
        tool_callables={"boom": boom},
        preview=False,
    )
    assert calls["n"] == 1  # invoked exactly once
    assert result.tool_calls[0]["result"]["_error"].startswith("RuntimeError")
    assert result.final_output == "recovered"  # run did NOT crash


def test_fake_executor_gates_requires_approval_tool() -> None:
    from caliber.workflows.runtime import FakeWorkflowExecutor

    calls = {"n": 0}

    def initiate_refund(order_id: str = "") -> dict:
        calls["n"] += 1
        return {"refunded": True}

    result = FakeWorkflowExecutor().run_agent(
        _agent(_binding("initiate_refund", requires_approval=True, side_effect="write")),
        "refund order 7",
        tool_callables={"initiate_refund": initiate_refund},
        preview=False,
    )
    assert calls["n"] == 0  # gated in the fake/default path too
    assert result.tool_calls[0]["result"]["_gated"] is True
    assert "used 1 tool" in result.final_output  # call count preserved


def test_anthropic_loop_terminates_at_cap_keeps_tools_and_counts_tokens() -> None:
    responses = [
        _AResp(
            [_Block("tool_use", id_=f"t{i}", name="lookup_policy", input_={"query": str(i)})],
            _AUsage(3, 2),
        )
        for i in range(MAX_AGENT_TOOL_ITERATIONS)
    ]
    # Final forced call: distinctive LARGE input_tokens to prove it's counted.
    responses.append(_AResp([_Block("text", text="final")], _AUsage(50, 7)))
    client = _FakeAnthropic(responses)
    executor = AnthropicChatWorkflowExecutor(api_key="x", default_model="m", client=client)
    result = executor.run_agent(
        _agent(_binding("lookup_policy", input_schema=_QUERY_SCHEMA)),
        "loop",
        tool_callables={"lookup_policy": lambda query="": {"n": query}},
        preview=False,
    )
    assert result.final_output == "final"
    assert len(result.tool_calls) == MAX_AGENT_TOOL_ITERATIONS
    assert len(client.messages.calls) == MAX_AGENT_TOOL_ITERATIONS + 1
    last = client.messages.calls[-1]
    assert last["tools"]  # tools still present on the final call (no 400)
    assert last["tool_choice"] == {"type": "none"}
    # Final call's input_tokens (50) ARE counted (the dropped-token bug fix).
    assert result.prompt_tokens == MAX_AGENT_TOOL_ITERATIONS * 3 + 50
    assert result.completion_tokens == MAX_AGENT_TOOL_ITERATIONS * 2 + 7
    assert result.tokens == result.prompt_tokens + result.completion_tokens


def test_called_tool_names_excludes_gated_and_error_markers() -> None:
    from caliber.workflows.guardrails import GuardrailContext

    ctx = GuardrailContext(
        tool_calls=[
            {"tool": "lookup_policy", "result": {"policy": "30 days"}},
            {"tool": "initiate_refund", "result": {"_gated": True}},
            {"tool": "ghost", "result": {"_error": "unknown tool"}},
        ]
    )
    assert ctx.called_tool_names == {"lookup_policy"}
