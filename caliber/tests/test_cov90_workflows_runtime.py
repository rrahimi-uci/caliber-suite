"""Targeted coverage for pure helpers in :mod:`caliber.workflows.runtime`.

These exercise the structured-output samplers, the model-tool-call argument /
result adapters, the webhook sender + cURL parser, MCP/python-node text
adapters, the session-history merge/trim, the handoff resolution + AST condition
evaluator, the template render context / JSON template resolver, the correlation
extractor, the inline-target / loop-state extractors and the output-folder
writer — all reachable without real LLM/network/storage infrastructure.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from caliber.workflows.ir import (
    IRAgent,
    IRHandoff,
    IRNode,
    IRToolBinding,
    IRType,
    IRWorkflow,
    NodeType,
    PromptRef,
)
from caliber.workflows.runtime import (
    AgentTurnResult,
    NodeStep,
    RuntimePlan,
    ToolExecutionError,
    _agent_instruction_text,
    _agent_skill_names,
    _agent_structured_output_port,
    _anthropic_assistant_content,
    _base_instruction_text,
    _call_tool,
    _coerce_request_body,
    _collect_agent_handoff_specs,
    _collect_artifacts,
    _correlation_value_from_object,
    _default_webhook_sender,
    _evaluate_runtime_expression,
    _external_app_single_argument,
    _fake_agent_structured_output,
    _handoff_assistant_output,
    _handoff_condition_enabled,
    _inline_target_inputs,
    _json_compatible,
    _loop_next_state,
    _mcp_arguments_from_input,
    _mcp_result_text,
    _merge_message_history,
    _normalize_handoff_messages,
    _normalize_message_history,
    _openai_agents_output_text,
    _openai_response_format,
    _openai_response_output_text,
    _openai_text_format,
    _parse_curl,
    _parse_structured_output_text,
    _publish_declared_outputs,
    _python_node_source,
    _python_node_text,
    _render_handoff_filter_text,
    _render_json_template,
    _resolve_agent_handoff_target,
    _resolve_bound_tool_callable,
    _resolve_external_app_entrypoint,
    _resolve_json_template_tokens,
    _resolve_wait_for_event_correlation_value,
    _sample_structured_output,
    _session_memory_key,
    _stringify_inline_target_input,
    _structured_output_definition,
    _structured_output_prompt_suffix,
    _structured_output_text,
    _supports_inline_orchestration_target,
    _template_json_inside_string,
    _template_lookup_value,
    _template_path_tokens,
    _template_render_context,
    _template_text_value,
    _tool_arguments_from_node_inputs,
    _tool_node_result_text,
    _tool_result_text,
    _trim_message_history,
    _usage_cached_prompt_tokens,
    _value_for_field,
    _wait_until_deadline,
    _wait_until_ready,
    _write_output_folder_node,
    run_with_caliber_context,
    workflow_handoff_input_filter,
    workflow_handoff_is_enabled,
)
from caliber.workflows.session_memory import InMemoryWorkflowSessionMemoryStore
from caliber.workflows.tools import InMemoryToolResolver

# ---------------------------------------------------------------------------
# builders
# ---------------------------------------------------------------------------


def _agent(node_id: str = "a", **kwargs: Any) -> IRAgent:
    defaults: dict[str, Any] = {
        "node_id": node_id,
        "node_type": NodeType.AGENT,
        "name": kwargs.pop("name", node_id),
    }
    defaults.update(kwargs)
    return IRAgent(**defaults)


def _mcp_binding(name: str = "m") -> IRToolBinding:
    return IRToolBinding(
        local_name=name,
        registry_ref="mcp",
        version_constraint="*",
        requires_approval=False,
        side_effect_level="read",
        allow_in_preview=True,
        module_path="<in-memory>",
        callable_name="",
        binding_type="mcp_tool",
    )


def _resolver() -> InMemoryToolResolver:
    return InMemoryToolResolver.from_callables({})


# ---------------------------------------------------------------------------
# structured-output helpers
# ---------------------------------------------------------------------------


def test_structured_output_definition_json_object_mode() -> None:
    agent = _agent(output_type={"type": "json_object"})
    assert _structured_output_definition(agent) == {"mode": "json_object"}


def test_structured_output_definition_json_schema_wrapper() -> None:
    agent = _agent(
        output_type={
            "type": "json_schema",
            "json_schema": {
                "name": "Answer",
                "strict": False,
                "schema": {"type": "object", "properties": {"a": {"type": "string"}}},
            },
        },
    )
    definition = _structured_output_definition(agent)
    assert definition == {
        "mode": "json_schema",
        "name": "Answer",
        "strict": False,
        "schema": {"type": "object", "properties": {"a": {"type": "string"}}},
    }


def test_structured_output_definition_json_schema_without_inner_schema_key() -> None:
    # No ``schema`` key -> derive it from the remaining fields (lines 387-394).
    agent = _agent(
        output_type={
            "type": "json_schema",
            "json_schema": {"name": "X", "strict": True, "type": "object"},
        },
    )
    definition = _structured_output_definition(agent)
    assert definition is not None
    assert definition["mode"] == "json_schema"
    assert definition["schema"] == {"type": "object"}


def test_structured_output_definition_top_level_schema_key() -> None:
    agent = _agent(output_type={"name": "N", "schema": {"type": "object"}})
    definition = _structured_output_definition(agent)
    assert definition == {
        "mode": "json_schema",
        "name": "N",
        "strict": True,
        "schema": {"type": "object"},
    }


def test_structured_output_definition_bare_dict_becomes_schema() -> None:
    agent = _agent(output_type={"type": "object", "properties": {}})
    definition = _structured_output_definition(agent)
    assert definition is not None
    assert definition["mode"] == "json_schema"
    assert definition["schema"] == {"type": "object", "properties": {}}


def test_structured_output_definition_none_for_non_dict() -> None:
    assert _structured_output_definition(_agent(output_type=None)) is None
    assert _structured_output_definition(_agent(output_type={})) is None


def test_openai_response_format_variants() -> None:
    assert _openai_response_format(_agent(output_type={"type": "json_object"})) == {
        "type": "json_object"
    }
    fmt = _openai_response_format(_agent(output_type={"schema": {"type": "object"}}))
    assert fmt is not None
    assert fmt["type"] == "json_schema"
    assert _openai_response_format(_agent(output_type=None)) is None


def test_openai_text_format_variants() -> None:
    assert _openai_text_format(_agent(output_type={"type": "json_object"})) == {
        "format": {"type": "json_object"}
    }
    fmt = _openai_text_format(_agent(output_type={"schema": {"type": "object"}}))
    assert fmt is not None
    assert fmt["format"]["type"] == "json_schema"
    assert _openai_text_format(_agent(output_type=None)) is None


def test_structured_output_prompt_suffix_variants() -> None:
    assert _structured_output_prompt_suffix(_agent(output_type=None)) == ""
    assert (
        _structured_output_prompt_suffix(_agent(output_type={"type": "json_object"}))
        == "Return only valid JSON."
    )
    suffix = _structured_output_prompt_suffix(_agent(output_type={"schema": {"type": "object"}}))
    assert "matches this schema" in suffix


def test_parse_structured_output_text() -> None:
    assert _parse_structured_output_text("") is None
    assert _parse_structured_output_text("not json") is None
    assert _parse_structured_output_text('{"a": 1}') == {"a": 1}


def test_structured_output_text() -> None:
    assert _structured_output_text("plain") == "plain"
    assert _structured_output_text({"a": 1}) == '{"a": 1}'


def test_sample_structured_output_scalar_and_container_branches() -> None:
    # None schema -> default descriptor (line 488).
    assert _sample_structured_output(
        None, path="root", input_text="hi", agent_name="A", tool_call_count=2
    ) == {"input": "hi", "agent": "A", "tool_calls": 2}
    # enum / const short-circuits (491, 493).
    assert (
        _sample_structured_output(
            {"enum": ["red", "blue"]}, path="", input_text="x", agent_name="A", tool_call_count=0
        )
        == "red"
    )
    assert (
        _sample_structured_output(
            {"const": 42}, path="", input_text="x", agent_name="A", tool_call_count=0
        )
        == 42
    )
    # anyOf recursion (497-499) into an integer branch.
    assert (
        _sample_structured_output(
            {"anyOf": [{"type": "integer"}]},
            path="",
            input_text="x",
            agent_name="A",
            tool_call_count=7,
        )
        == 7
    )
    # union type list -> first non-null (508-509).
    assert (
        _sample_structured_output(
            {"type": ["null", "number"]},
            path="root.value",
            input_text="x",
            agent_name="A",
            tool_call_count=3,
        )
        == 3.0
    )
    # object with empty properties -> default descriptor (513).
    assert _sample_structured_output(
        {"type": "object", "properties": {}},
        path="root",
        input_text="hi",
        agent_name="A",
        tool_call_count=1,
    ) == {"input": "hi", "agent": "A", "tool_calls": 1}
    # array (525-526) + integer/number/boolean/null scalars (536, 538, 542).
    assert _sample_structured_output(
        {"type": "array", "items": {"type": "boolean"}},
        path="root",
        input_text="x",
        agent_name="A",
        tool_call_count=1,
    ) == [True]
    assert (
        _sample_structured_output(
            {"type": "null"}, path="root", input_text="x", agent_name="A", tool_call_count=0
        )
        is None
    )


def test_sample_structured_output_string_path_heuristics() -> None:
    schema = {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "query": {"type": "string"},
            "agent_field": {"type": "string"},
            "other_field": {"type": "string"},
        },
    }
    sampled = _sample_structured_output(
        schema, path="root", input_text="the input", agent_name="Bot", tool_call_count=0
    )
    assert sampled["message"] == "[Bot] processed: the input"  # 544-545
    assert sampled["query"] == "the input"  # 546-547
    assert sampled["agent_field"] == "Bot"  # 548-549
    assert sampled["other_field"] == "other field"  # 550


def test_fake_agent_structured_output() -> None:
    # json_object mode -> None (560-561).
    assert (
        _fake_agent_structured_output(
            _agent(output_type={"type": "json_object"}), input_text="x", tool_call_count=0
        )
        is None
    )
    # No output_type -> None.
    assert (
        _fake_agent_structured_output(_agent(output_type=None), input_text="x", tool_call_count=0)
        is None
    )
    # A real schema is sampled into a structured value (562, 565-570).
    agent = _agent(
        "bot", output_type={"type": "object", "properties": {"answer": {"type": "string"}}}
    )
    assert _fake_agent_structured_output(agent, input_text="hello", tool_call_count=1) == {
        "answer": "[bot] processed: hello"
    }


# ---------------------------------------------------------------------------
# OpenAI response / agents output extraction
# ---------------------------------------------------------------------------


def test_openai_response_output_text_direct_and_content_walk() -> None:
    direct = SimpleNamespace(output_text="direct answer", output=[])
    assert _openai_response_output_text(direct) == "direct answer"

    walked = SimpleNamespace(
        output_text="",
        output=[
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "text": "Hello "},
                    {"type": "refusal", "refusal": "cannot"},
                ],
            },
            {"type": "reasoning"},
        ],
    )
    assert _openai_response_output_text(walked) == "Hello cannot"


def test_openai_agents_output_text_variants() -> None:
    assert (
        _openai_agents_output_text(result="  spaced  ", raw_responses=[], item_helpers=None)
        == "spaced"
    )
    dumped = SimpleNamespace(model_dump=lambda: {"a": 1})
    assert _openai_agents_output_text(result=dumped, raw_responses=[], item_helpers=None) == (
        '{"a": 1}'
    )
    assert (
        _openai_agents_output_text(result={"k": "v"}, raw_responses=[], item_helpers=None)
        == '{"k": "v"}'
    )
    # empty result + no responses -> falls through to the trailing return.
    assert _openai_agents_output_text(result=None, raw_responses=[], item_helpers=None) == ""


def test_openai_agents_output_text_extractor_fallback() -> None:
    responses = [SimpleNamespace(output=["item"])]
    helpers = SimpleNamespace(
        extract_text=lambda item: "extracted body",
        extract_refusal=lambda item: "",
        extract_last_content=lambda item: "",
    )
    assert (
        _openai_agents_output_text(result="", raw_responses=responses, item_helpers=helpers)
        == "extracted body"
    )


# ---------------------------------------------------------------------------
# model tool-call argument / result adapters
# ---------------------------------------------------------------------------


def test_tool_arguments_from_node_inputs() -> None:
    assert _tool_arguments_from_node_inputs({"arguments": {"a": 1}}) == {"a": 1}
    assert _tool_arguments_from_node_inputs({"arguments": '{"b": 2}'}) == {"b": 2}
    assert _tool_arguments_from_node_inputs({"arguments": "loose text"}) == {"input": "loose text"}
    assert _tool_arguments_from_node_inputs({"arguments": None}) == {}
    assert _tool_arguments_from_node_inputs({"input": {"c": 3}}) == {"c": 3}
    assert _tool_arguments_from_node_inputs({"input": '{"d": 4}'}) == {"d": 4}


def test_call_tool_selects_kwargs_shape() -> None:
    def add(x: int, y: int) -> int:
        return x + y

    assert _call_tool(add, {"x": 1, "y": 2}, fallback_input="") == 3


def test_call_tool_dict_positional_shape() -> None:
    def echo(payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    assert _call_tool(echo, {"a": 1}, fallback_input="") == {"a": 1}


def test_call_tool_fallback_and_no_arg_shapes() -> None:
    def single(value: str) -> str:
        return value

    assert _call_tool(single, {}, fallback_input="fb") == "fb"

    def no_args() -> str:
        return "nothing"

    assert _call_tool(no_args, {}, fallback_input="fb") == "nothing"


def test_call_tool_no_shape_binds_surfaces_error() -> None:
    def needs_three(a: int, b: int, c: int) -> int:
        return a + b + c

    with pytest.raises(TypeError):
        _call_tool(needs_three, {"a": 1}, fallback_input="fb")


def test_call_tool_non_introspectable_callable() -> None:
    class NoSig:
        __signature__ = "bad"  # forces inspect.signature to raise -> sig is None

        def __call__(self) -> str:
            return "called"

    # arguments present but the callable only accepts the no-arg shape, so the
    # loop advances past the earlier shapes' binding TypeErrors (lines 1920-1926).
    assert _call_tool(NoSig(), {"a": 1}, fallback_input="fb") == "called"


def test_tool_result_text() -> None:
    assert _tool_result_text("text") == "text"
    assert _tool_result_text({"a": 1}) == '{"a": 1}'
    # tuple keys aren't JSON-encodable even with ``default=str`` -> str() fallback.
    assert "1, 2" in _tool_result_text({(1, 2): "x"})


def test_tool_node_result_text_variants() -> None:
    mcp = _tool_node_result_text(_mcp_binding(), {"result": {"text": "from mcp"}})
    assert mcp == "from mcp"

    binding = IRToolBinding(
        local_name="t",
        registry_ref="tool.t.v1",
        version_constraint="*",
        requires_approval=False,
        side_effect_level="read",
        allow_in_preview=True,
        module_path="m",
        callable_name="f",
    )
    assert _tool_node_result_text(binding, {"answer": "hi"}) == "hi"
    assert _tool_node_result_text(binding, {"n": 1}) == '{"n": 1}'


# ---------------------------------------------------------------------------
# instructions
# ---------------------------------------------------------------------------


def test_base_instruction_text_inline_and_empty() -> None:
    assert _base_instruction_text(_agent(instructions=None)) == ""
    inline = PromptRef(kind="inline", inline_text="Be helpful.")
    assert _base_instruction_text(_agent(instructions=inline)) == "Be helpful."


def test_base_instruction_text_mlflow_success(monkeypatch: pytest.MonkeyPatch) -> None:
    import mlflow.genai

    monkeypatch.setattr(
        mlflow.genai,
        "load_prompt",
        lambda uri: SimpleNamespace(template="Loaded prompt body"),
    )
    ref = PromptRef(kind="mlflow_prompt", registry_name="support", alias="prod")
    assert _base_instruction_text(_agent(instructions=ref)) == "Loaded prompt body"


def test_base_instruction_text_mlflow_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    import mlflow.genai

    def _boom(uri: str) -> Any:
        raise RuntimeError("registry unavailable")

    monkeypatch.setattr(mlflow.genai, "load_prompt", _boom)
    ref = PromptRef(kind="mlflow_prompt", registry_name="support", alias="prod")
    text = _base_instruction_text(_agent(instructions=ref))
    assert text == "Follow the MLflow prompt registered at prompts:/support@prod."


def test_agent_instruction_text_composes_skills() -> None:
    agent = _agent(
        instructions=PromptRef(kind="inline", inline_text="Base."),
        skill_instructions=["Skill one body", "  "],
    )
    composed = _agent_instruction_text(agent)
    assert composed == "Base.\n\n## Skill\nSkill one body"


def test_agent_skill_names_labels_and_skips_blank() -> None:
    agent = _agent(skill_instructions=["   ", "# Refunds\nDo refunds", "No header"])
    assert _agent_skill_names(agent) == ["Refunds", "No header"]


# ---------------------------------------------------------------------------
# webhook sender + cURL parser + request body coercion
# ---------------------------------------------------------------------------


def _install_fake_httpx(
    monkeypatch: pytest.MonkeyPatch, response: SimpleNamespace
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class FakeClient:
        def __init__(
            self,
            timeout: Any = None,
            follow_redirects: Any = None,
            transport: Any = None,
        ) -> None:
            captured["timeout"] = timeout
            # Captured, not ignored: ``follow_redirects=False`` is an SSRF control, not
            # a restated default. A double that silently accepted any value here would
            # let that regress unnoticed.
            captured["follow_redirects"] = follow_redirects
            # The primary SSRF control is now the transport, which pins the connection to
            # the address policy vetted (N4). ``follow_redirects=False`` became
            # defence in depth rather than the only defence, so the transport is the thing
            # this double must not let disappear silently.
            captured["transport"] = transport

        def __enter__(self) -> Any:
            return self

        def __exit__(self, *exc: Any) -> bool:
            return False

        def request(self, method: str, url: str, **kwargs: Any) -> SimpleNamespace:
            captured["method"] = method
            captured["url"] = url
            captured["kwargs"] = kwargs
            return response

    monkeypatch.setattr("httpx.Client", FakeClient)
    return captured


def test_default_webhook_sender_posts_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    response = SimpleNamespace(
        status_code=201,
        text='{"ok": true}',
        headers={"content-type": "application/json"},
        json=lambda: {"ok": True},
    )
    captured = _install_fake_httpx(monkeypatch, response)
    result = _default_webhook_sender(
        {
            "url": "https://example.test/hook",
            "method": "post",
            "headers": {"X-Api": 7},
            "timeout_seconds": 12,
            "body": {"event": "created"},
        }
    )
    assert result == {
        "status_code": 201,
        "text": '{"ok": true}',
        "json": {"ok": True},
        "headers": {"content-type": "application/json"},
    }
    assert captured["method"] == "POST"
    assert captured["kwargs"]["json"] == {"event": "created"}
    assert captured["kwargs"]["headers"] == {"X-Api": "7"}
    assert captured["timeout"] == 12.0
    assert captured["follow_redirects"] is False
    # The SSRF control that actually closes the rebinding gap: the client must be built on
    # the egress-guard transport, which connects to the address policy vetted rather than
    # re-resolving the name. A plain httpx.Client here would silently reopen N4.
    from caliber.egress import EgressGuardTransport

    assert isinstance(captured["transport"], EgressGuardTransport)


def test_default_webhook_sender_get_uses_params(monkeypatch: pytest.MonkeyPatch) -> None:
    response = SimpleNamespace(status_code=200, text="hi", headers={}, json=lambda: {"a": 1})
    captured = _install_fake_httpx(monkeypatch, response)
    _default_webhook_sender({"url": "https://x", "method": "GET", "body": {"q": "term"}})
    assert captured["kwargs"]["params"] == {"q": "term"}


def test_default_webhook_sender_text_body_and_non_json(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise() -> Any:
        raise ValueError("not json")

    response = SimpleNamespace(status_code=204, text="plain", headers={}, json=_raise)
    captured = _install_fake_httpx(monkeypatch, response)
    result = _default_webhook_sender({"url": "https://x", "method": "PUT", "body": "raw text"})
    assert result["json"] is None
    assert captured["kwargs"]["content"] == "raw text"


def test_parse_curl_url_flag_and_user_agent() -> None:
    parsed = _parse_curl('curl --url https://api.test/v1 -A MyAgent -X PUT -H "X-Test: 1" -d "k=v"')
    assert parsed["url"] == "https://api.test/v1"
    assert parsed["headers"]["User-Agent"] == "MyAgent"
    assert parsed["headers"]["X-Test"] == "1"
    assert parsed["method"] == "PUT"
    assert parsed["body"] == "k=v"


def test_parse_curl_data_defaults_to_post_and_merges() -> None:
    parsed = _parse_curl("curl https://x -d a=1 --data-raw b=2")
    assert parsed["method"] == "POST"
    assert parsed["body"] == "a=1&b=2"


def test_parse_curl_rejects_unbalanced_quotes() -> None:
    with pytest.raises(ToolExecutionError, match="could not parse cURL"):
        _parse_curl('curl "https://unterminated')


def test_parse_curl_requires_url() -> None:
    with pytest.raises(ToolExecutionError, match="did not contain a URL"):
        _parse_curl('curl -X POST -H "A: b"')


def test_coerce_request_body() -> None:
    assert _coerce_request_body('{"a": 1}') == {"a": 1}
    assert _coerce_request_body("not json") == "not json"
    assert _coerce_request_body("   ") == "   "
    assert _coerce_request_body(123) == 123


# ---------------------------------------------------------------------------
# external-app single-argument selection
# ---------------------------------------------------------------------------


def test_external_app_single_argument_selection() -> None:
    assert _external_app_single_argument("input", {"input": "x"}) == "x"  # 2606
    assert _external_app_single_argument("payload", {"a": 1}) == {"a": 1}  # 2607-2608
    assert _external_app_single_argument("input", {"a": 1}) == ""  # 2609-2610
    assert _external_app_single_argument("other", {"a": 1}) == {"a": 1}  # 2611


# ---------------------------------------------------------------------------
# MCP + python-node + json helpers
# ---------------------------------------------------------------------------


def test_mcp_arguments_from_input() -> None:
    assert _mcp_arguments_from_input({"a": 1}) == {"a": 1}
    assert _mcp_arguments_from_input(None) == {}
    assert _mcp_arguments_from_input(5) == {"query": "5"}
    assert _mcp_arguments_from_input("   ") == {}
    assert _mcp_arguments_from_input('{"k": "v"}') == {"k": "v"}
    assert _mcp_arguments_from_input("plain text") == {"query": "plain text"}


def test_mcp_result_text() -> None:
    assert _mcp_result_text("done") == "done"
    assert _mcp_result_text({"message": "hi"}) == "hi"
    assert _mcp_result_text({"code": 200}) == '{"code": 200}'


def test_python_node_source() -> None:
    with pytest.raises(ToolExecutionError, match="non-empty code"):
        _python_node_source("   ")
    already = "def run_python_node(inputs=None):\n    return 'x'"
    assert _python_node_source(already) == already
    wrapped = _python_node_source("return 42")
    assert wrapped.startswith("def run_python_node(")
    assert "    return 42" in wrapped


def test_python_node_text() -> None:
    assert _python_node_text("text") == "text"
    assert _python_node_text({"output": "out"}) == "out"
    assert _python_node_text(None) == ""
    assert _python_node_text({"n": 1}) == '{"n": 1}'


def test_json_compatible() -> None:
    assert _json_compatible(None) is None
    assert _json_compatible(3) == 3
    assert _json_compatible({"a": (1, 2)}) == {"a": [1, 2]}
    assert _json_compatible({1, 2}) in ([1, 2], [2, 1])
    assert _json_compatible(object()).startswith("<object")


# ---------------------------------------------------------------------------
# session history merge / trim / key
# ---------------------------------------------------------------------------


def test_merge_message_history_overlap_and_edges() -> None:
    stored = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    explicit = [{"role": "assistant", "content": "b"}, {"role": "user", "content": "c"}]
    assert _merge_message_history(stored, explicit) == [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    assert _merge_message_history([], explicit) == explicit
    assert _merge_message_history(stored, []) == stored


def test_trim_message_history() -> None:
    history = [{"role": "user", "content": str(i)} for i in range(5)]
    assert _trim_message_history(history, max_messages=2) == history[-2:]
    assert _trim_message_history(history, max_messages=0) == []


def test_session_memory_key_active_and_inactive() -> None:
    agent = _agent("a")
    inactive_plan = RuntimePlan(ir=_workflow(), resolver=_resolver())
    assert _session_memory_key(inactive_plan, agent) is None

    store = InMemoryWorkflowSessionMemoryStore()
    plan = RuntimePlan(
        ir=_workflow(session_mode="persistent"),
        resolver=_resolver(),
        session_memory_store=store,
    )
    with run_with_caliber_context(
        workflow_id="wf",
        workflow_version="1",
        entry_node_id="a",
        session_id="sess-1",
    ):
        assert _session_memory_key(plan, agent) == ("wf", "a", "sess-1")


# ---------------------------------------------------------------------------
# handoff resolution
# ---------------------------------------------------------------------------


def _workflow(**kwargs: Any) -> IRWorkflow:
    nodes: dict[str, IRNode] = kwargs.pop("nodes", {"a": _agent("a")})
    defaults: dict[str, Any] = {
        "workflow_id": "wf",
        "version": "1",
        "nodes": nodes,
        "edges": [],
        "entry_node_id": "a",
        "output_node_id": "a",
    }
    defaults.update(kwargs)
    return IRWorkflow(**defaults)


def test_collect_agent_handoff_specs() -> None:
    plan = RuntimePlan(ir=_workflow(), resolver=_resolver())
    assert (
        _collect_agent_handoff_specs(
            _agent("solo"), plan.ir, plan, preview=False, root_tool_callables={}
        )
        is None
    )

    agent_a = _agent("a", handoffs=[IRHandoff(target_node_id="b")])
    agent_b = _agent("b")
    ir = _workflow(nodes={"a": agent_a, "b": agent_b})
    plan = RuntimePlan(ir=ir, resolver=_resolver())
    specs = _collect_agent_handoff_specs(
        agent_a, ir, plan, preview=False, root_tool_callables={"t": lambda x="": x}
    )
    assert specs is not None
    assert set(specs.keys()) == {"a", "b"}


def test_collect_agent_handoff_specs_single_returns_none() -> None:
    # Handoff target that is not an agent -> only the root resolves -> None.
    agent_a = _agent("a", handoffs=[IRHandoff(target_node_id="tool")])
    tool_node = IRNode(node_id="tool", node_type=NodeType.TOOL)
    ir = _workflow(nodes={"a": agent_a, "tool": tool_node})
    plan = RuntimePlan(ir=ir, resolver=_resolver())
    assert (
        _collect_agent_handoff_specs(agent_a, ir, plan, preview=False, root_tool_callables={})
        is None
    )


def test_resolve_agent_handoff_target_executor_managed() -> None:
    agent = _agent("a", handoffs=[IRHandoff(target_node_id="b")])
    result = AgentTurnResult(final_output="x", handoffs_resolved_in_executor=True)
    assert _resolve_agent_handoff_target(agent, result, _workflow(), input_text="hi") is None


def test_resolve_agent_handoff_target_undeclared_target() -> None:
    agent = _agent("a", handoffs=[IRHandoff(target_node_id="b")])
    result = AgentTurnResult(final_output="x", handoff_target="zzz")
    ir = _workflow(nodes={"a": agent, "b": _agent("b")})
    assert _resolve_agent_handoff_target(agent, result, ir, input_text="hi") is None


def test_resolve_agent_handoff_target_non_agent_target() -> None:
    agent = _agent("a", handoffs=[IRHandoff(target_node_id="tool")])
    result = AgentTurnResult(final_output="x", handoff_target="tool")
    ir = _workflow(nodes={"a": agent, "tool": IRNode(node_id="tool", node_type=NodeType.TOOL)})
    assert _resolve_agent_handoff_target(agent, result, ir, input_text="hi") is None


def test_resolve_agent_handoff_target_disabled_condition() -> None:
    agent = _agent("a", handoffs=[IRHandoff(target_node_id="b", condition="False")])
    result = AgentTurnResult(final_output="x", handoff_target="b")
    ir = _workflow(nodes={"a": agent, "b": _agent("b")})
    assert _resolve_agent_handoff_target(agent, result, ir, input_text="hi") is None


def test_resolve_agent_handoff_target_single_fallback() -> None:
    agent = _agent("a", handoffs=[IRHandoff(target_node_id="b")])
    result = AgentTurnResult(final_output="x")  # no explicit target
    ir = _workflow(nodes={"a": agent, "b": _agent("b")})
    assert _resolve_agent_handoff_target(agent, result, ir, input_text="hi") == "b"


def test_resolve_agent_handoff_target_single_fallback_non_agent() -> None:
    agent = _agent("a", handoffs=[IRHandoff(target_node_id="tool")])
    result = AgentTurnResult(final_output="x")
    ir = _workflow(nodes={"a": agent, "tool": IRNode(node_id="tool", node_type=NodeType.TOOL)})
    assert _resolve_agent_handoff_target(agent, result, ir, input_text="hi") is None


# ---------------------------------------------------------------------------
# template render context / text value / stringify
# ---------------------------------------------------------------------------


def test_stringify_inline_target_input() -> None:
    assert _stringify_inline_target_input(None) == ""
    assert _stringify_inline_target_input("s") == "s"
    assert _stringify_inline_target_input({"a": 1}) == '{"a": 1}'
    # tuple keys aren't JSON-encodable even with default=str -> str() fallback.
    assert "1, 2" in _stringify_inline_target_input({(1, 2): "x"})


def test_template_render_context_dict_sources() -> None:
    ctx = _template_render_context(
        {"input": "", "variables": {"v1": "x"}, "context": {"c1": "y"}, "extra": "z"},
        "RUN",
    )
    assert ctx["input"] == "RUN"
    assert ctx["run_input"] == "RUN"
    assert ctx["v1"] == "x"
    assert ctx["variables"] == {"v1": "x"}
    assert ctx["c1"] == "y"
    assert ctx["context"] == {"c1": "y"}
    assert ctx["extra"] == "z"


def test_template_render_context_non_dict_sources() -> None:
    # A non-dict ``context`` value is still surfaced under the "context" key
    # (3723-3724); a missing ``variables`` key defaults to an empty dict (3716).
    ctx = _template_render_context({"context": "scalar"}, "RUN")
    assert ctx["variables"] == {}
    assert ctx["context"] == "scalar"


def test_template_text_value() -> None:
    assert _template_text_value(None) == ""
    assert _template_text_value("s") == "s"
    assert _template_text_value({"a": 1}) == '{"a": 1}'


def test_template_lookup_value_branches() -> None:
    # tokenizer failure -> (False, None) (3808-3810).
    assert _template_lookup_value({"x": 1}, "a[") == (False, None)
    # int token against non-list current (3814-3818).
    assert _template_lookup_value({"a": {"b": 1}}, "a[0]") == (False, None)
    # digit string token against a list (3824-3828).
    assert _template_lookup_value({"a": [10, 20]}, "a.0") == (True, 10)
    assert _template_lookup_value({"a": [10, 20]}, "a.5") == (False, None)
    # object attribute fallback failure (3830-3833).
    assert _template_lookup_value({"o": object()}, "o.missing") == (False, None)


# ---------------------------------------------------------------------------
# JSON template resolution
# ---------------------------------------------------------------------------


def test_template_json_inside_string_handles_escapes() -> None:
    # A backslash escape inside a string is skipped, string then closes.
    assert _template_json_inside_string('"a\\b"c', 6) is False


def test_resolve_json_template_tokens_direct() -> None:
    token_values = {"tok": (5, False), "sub": ("XX", True)}
    # A dict key that resolves to a non-string is coerced (line 3901).
    resolved = _resolve_json_template_tokens({"tok": "keep"}, token_values)
    assert resolved == {"5": "keep"}
    # In-string token substring replacement (3912-3917).
    assert _resolve_json_template_tokens("prefix sub suffix", token_values) == "prefix XX suffix"


def test_render_json_template_happy_path() -> None:
    rendered, result, used, missing = _render_json_template(
        '{"greeting": "{{name}}", "count": {{count}}}',
        context={"name": "Bob", "count": 3},
        missing_variable_mode="preserve",
    )
    assert result == {"greeting": "Bob", "count": 3}
    assert used == ["name", "count"]
    assert missing == []
    assert '"greeting": "Bob"' in rendered


def test_render_json_template_missing_variable_error() -> None:
    with pytest.raises(ToolExecutionError, match="missing variable"):
        _render_json_template('{"v": "{{absent}}"}', context={}, missing_variable_mode="error")


def test_render_json_template_invalid_json() -> None:
    with pytest.raises(ToolExecutionError, match="invalid JSON"):
        _render_json_template("{not valid", context={}, missing_variable_mode="preserve")


# ---------------------------------------------------------------------------
# handoff message normalisation + AST condition evaluation
# ---------------------------------------------------------------------------


def test_normalize_handoff_messages_skips_roleless() -> None:
    messages = _normalize_handoff_messages(
        [{"role": "", "content": "x"}, {"role": "user", "content": "hi"}]
    )
    assert messages == [{"role": "user", "content": "hi"}]


def test_handoff_assistant_output_falls_back_to_raw_items() -> None:
    # No valid role -> no messages, but raw items exist -> stringified fallback.
    out = _handoff_assistant_output([{"content": "orphan"}])
    assert "orphan" in out


def test_evaluate_runtime_expression_operators() -> None:
    assert _evaluate_runtime_expression("a and b", {"a": 0, "b": 1}) is False
    assert _evaluate_runtime_expression("a or b", {"a": 0, "b": 5}) is True
    assert _evaluate_runtime_expression("a or b", {"a": 0, "b": 0}) is False
    assert _evaluate_runtime_expression("a != b", {"a": 1, "b": 2}) is True
    assert _evaluate_runtime_expression("a >= b", {"a": 3, "b": 2}) is True
    assert _evaluate_runtime_expression("a is b", {"a": None, "b": None}) is True
    assert _evaluate_runtime_expression("a is not b", {"a": 1, "b": 2}) is True


def test_evaluate_runtime_expression_unsupported_unary() -> None:
    with pytest.raises(ValueError, match="unsupported unary"):
        _evaluate_runtime_expression("~x", {"x": 5})


def test_handoff_condition_enabled() -> None:
    assert _handoff_condition_enabled("", input_text="hi") is True
    assert _handoff_condition_enabled("'refund' in input", input_text="refund me") is True
    # An expression referencing an unknown name is swallowed -> disabled.
    assert _handoff_condition_enabled("unknown_symbol", input_text="hi") is False


def test_render_handoff_filter_text() -> None:
    assert _render_handoff_filter_text(None, input_text="passthrough") == "passthrough"
    rendered = _render_handoff_filter_text("Q: {{input}}", input_text="hello", history=[])
    assert rendered == "Q: hello"


# ---------------------------------------------------------------------------
# correlation extraction + wait-for-event resolution
# ---------------------------------------------------------------------------


def test_correlation_value_from_object() -> None:
    assert _correlation_value_from_object({"id": "42"}, "id") == (True, "42")
    assert _correlation_value_from_object({"outer": {"id": "9"}}, "id") == (True, "9")
    assert _correlation_value_from_object([{"id": "7"}], "id") == (True, "7")
    assert _correlation_value_from_object('{"id": "5"}', "id") == (True, "5")
    assert _correlation_value_from_object("{bad json", "id") == (False, None)
    assert _correlation_value_from_object("plain", "id") == (False, None)


def test_resolve_wait_for_event_correlation_value() -> None:
    assert (
        _resolve_wait_for_event_correlation_value(inputs={}, run_input="", correlation_key="  ")
        is None
    )
    assert (
        _resolve_wait_for_event_correlation_value(
            inputs={"ticket": "T1"}, run_input="", correlation_key="ticket"
        )
        == "T1"
    )
    assert (
        _resolve_wait_for_event_correlation_value(
            inputs={}, run_input='{"ticket": "T2"}', correlation_key="ticket"
        )
        == "T2"
    )
    assert (
        _resolve_wait_for_event_correlation_value(
            inputs={"ticket": None}, run_input="", correlation_key="ticket"
        )
        is None
    )
    assert (
        _resolve_wait_for_event_correlation_value(
            inputs={"ticket": "   "}, run_input="", correlation_key="ticket"
        )
        is None
    )


# ---------------------------------------------------------------------------
# inline-target inputs + loop next-state extraction
# ---------------------------------------------------------------------------


def test_inline_target_inputs_port_match_and_fallbacks() -> None:
    node = IRNode(
        node_id="t",
        node_type=NodeType.TOOL,
        inputs={"input": IRType("string"), "extra": IRType("string")},
    )
    payload, run_input = _inline_target_inputs(node, {"input": "matched", "extra": "e"})
    assert payload == {"input": "matched", "extra": "e"}

    arg_node = IRNode(
        node_id="t2", node_type=NodeType.TOOL, inputs={"arguments": IRType("structured")}
    )
    payload2, _ = _inline_target_inputs(arg_node, {"a": 1})
    assert payload2 == {"arguments": {"a": 1}}

    input_node = IRNode(node_id="t3", node_type=NodeType.TOOL, inputs={"input": IRType("string")})
    payload3, _ = _inline_target_inputs(input_node, "loose")
    assert payload3 == {"input": "loose"}

    q_node = IRNode(node_id="t4", node_type=NodeType.TOOL, inputs={"question": IRType("string")})
    payload4, _ = _inline_target_inputs(q_node, {"a": 1})
    assert payload4 == {"question": '{"a": 1}'}


def test_loop_next_state_extraction() -> None:
    step = NodeStep("n", NodeType.TOOL.value, "ok", output="fallback text")
    # nested result unwrap (4367-4374).
    assert _loop_next_state(step, {"result": {"result": "inner", "text": "t"}}) == "inner"
    # plain result value (4375).
    assert _loop_next_state(step, {"result": "top"}) == "top"
    # output/text/answer/final_output fallbacks (4376-4384).
    assert _loop_next_state(step, {"final_output": "fo"}) == "fo"
    # step.output fallback (4385-4386).
    assert _loop_next_state(step, {}) == "fallback text"
    # child_outputs fallback when no step output.
    empty_step = NodeStep("n", NodeType.TOOL.value, "ok", output="")
    assert _loop_next_state(empty_step, {"misc": {"k": 1}}) == {"misc": {"k": 1}}


# ---------------------------------------------------------------------------
# declared outputs, structured port, message-history normaliser, field lookup
# ---------------------------------------------------------------------------


def test_publish_declared_outputs() -> None:
    node = IRNode(
        node_id="n",
        node_type=NodeType.OUTPUT,
        outputs={
            "answer": IRType("string"),
            "meta": IRType("structured"),
            "other": IRType("string"),
        },
    )
    port_values: dict[tuple[str, str], Any] = {}
    _publish_declared_outputs(node, port_values, {"answer": "hi"}, fallback="FB")
    assert port_values[("n", "answer")] == "hi"
    assert port_values[("n", "meta")] == {}
    assert port_values[("n", "other")] == "FB"


def test_agent_structured_output_port() -> None:
    preferred = _agent("a", outputs={"structured_output": IRType("structured")})
    assert _agent_structured_output_port(preferred) == "structured_output"
    single = _agent("b", outputs={"my_out": IRType("structured")})
    assert _agent_structured_output_port(single) == "my_out"
    ambiguous = _agent("c", outputs={"one": IRType("structured"), "two": IRType("structured")})
    assert _agent_structured_output_port(ambiguous) is None


def test_normalize_message_history_filters() -> None:
    history = _normalize_message_history(
        [
            {"role": "user", "content": "hi"},
            "not-a-dict",
            {"role": "system", "content": "x"},
            {"role": "assistant", "content": "   "},
        ]
    )
    assert history == [{"role": "user", "content": "hi"}]


def test_value_for_field() -> None:
    context = {"a": {"b": 1}}
    assert _value_for_field(context, "") == context
    assert _value_for_field(context, "a.b") == 1
    assert _value_for_field({"a": "scalar"}, "a.b") is None


def test_anthropic_assistant_content_skips_blank_text() -> None:
    blocks = [
        SimpleNamespace(type="text", text="Hello"),
        SimpleNamespace(type="text", text="   "),
        SimpleNamespace(type="tool_use", id="t1", name="lookup", input={"q": "x"}),
    ]
    content = _anthropic_assistant_content(blocks)
    assert content == [
        {"type": "text", "text": "Hello"},
        {"type": "tool_use", "id": "t1", "name": "lookup", "input": {"q": "x"}},
    ]


def test_usage_cached_prompt_tokens() -> None:
    assert _usage_cached_prompt_tokens({"prompt_tokens_details": {"cached_tokens": 5}}) == 5
    assert _usage_cached_prompt_tokens(SimpleNamespace(cached_input_tokens=7)) == 7
    assert _usage_cached_prompt_tokens(SimpleNamespace()) == 0


# ---------------------------------------------------------------------------
# output-folder writer
# ---------------------------------------------------------------------------


def test_write_output_folder_node_sanitizes_unsafe_names(
    tmp_path: Any, confined_workflow_file_root: Any
) -> None:
    dest = tmp_path / "out"
    port_values: dict[tuple[str, str], Any] = {
        ("n", "result"): {"artifacts": {"..": "escape", "sub/keep.txt": "content"}}
    }
    written, metadata = _write_output_folder_node(
        path=str(dest), overwrite=True, port_values=port_values, direct_input=None
    )
    assert metadata["file_count"] == 1
    assert (dest / "sub" / "keep.txt").read_text() == "content"
    assert all(".." not in item for item in written)


def test_write_output_folder_node_skips_existing_without_overwrite(
    tmp_path: Any, confined_workflow_file_root: Any
) -> None:
    dest = tmp_path / "out"
    dest.mkdir()
    (dest / "keep.txt").write_text("old")
    port_values: dict[tuple[str, str], Any] = {("n", "result"): {"artifacts": {"keep.txt": "new"}}}
    written, metadata = _write_output_folder_node(
        path=str(dest), overwrite=False, port_values=port_values, direct_input=None
    )
    assert written == []
    assert metadata["file_count"] == 0
    assert (dest / "keep.txt").read_text() == "old"


# ---------------------------------------------------------------------------
# additional edge-branch coverage
# ---------------------------------------------------------------------------


def test_structured_output_definition_json_schema_empty_derived_schema() -> None:
    # ``json_schema`` wrapper with no usable inner schema -> None (line 394).
    agent = _agent(output_type={"type": "json_schema", "json_schema": {"name": "N"}})
    assert _structured_output_definition(agent) is None


def test_call_tool_non_introspectable_all_shapes_fail() -> None:
    class NoSigThree:
        __signature__ = "bad"  # forces inspect.signature to raise

        def __call__(self, a: Any, b: Any, c: Any) -> Any:
            return a

    # No shape binds and the final shape's TypeError propagates (lines 1925-1926).
    with pytest.raises(TypeError):
        _call_tool(NoSigThree(), {}, fallback_input="fb")


def test_base_instruction_text_mlflow_prompt_without_uri() -> None:
    # ``mlflow_prompt`` kind but no registry_name -> no URI -> "" (line 2018).
    ref = PromptRef(kind="mlflow_prompt", registry_name=None)
    assert _base_instruction_text(_agent(instructions=ref)) == ""


def test_base_instruction_text_mlflow_empty_template_uses_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import mlflow.genai

    monkeypatch.setattr(mlflow.genai, "load_prompt", lambda uri: SimpleNamespace(template="   "))
    ref = PromptRef(kind="mlflow_prompt", registry_name="p", alias="prod")
    text = _base_instruction_text(_agent(instructions=ref))
    assert text == "Follow the MLflow prompt registered at prompts:/p@prod."


def test_collect_artifacts_direct() -> None:
    collected = _collect_artifacts(
        {
            ("n", "skip"): "not-a-dict",
            ("n", "top"): {"artifacts": {"a.txt": "x", "b.json": {"nested": 1}}},
            ("n", "result"): {"result": {"artifacts": {"c.txt": "y"}}},
        }
    )
    assert collected["a.txt"] == "x"
    assert collected["b.json"] == '{"nested": 1}'
    assert collected["c.txt"] == "y"


def test_merge_message_history_no_overlap() -> None:
    stored = [{"role": "user", "content": "a"}]
    explicit = [{"role": "user", "content": "b"}]
    assert _merge_message_history(stored, explicit) == [*stored, *explicit]


def test_collect_agent_handoff_specs_extra_tools_and_cycle() -> None:
    agent_a = _agent("a", handoffs=[IRHandoff(target_node_id="b")])
    agent_b = _agent("b", handoffs=[IRHandoff(target_node_id="a")])  # cycle back to a
    ir = _workflow(nodes={"a": agent_a, "b": agent_b})
    plan = RuntimePlan(ir=ir, resolver=_resolver())
    specs = _collect_agent_handoff_specs(
        agent_a,
        ir,
        plan,
        preview=False,
        root_tool_callables={},
        extra_tools={"shared": lambda x="": x},
    )
    assert specs is not None
    assert set(specs.keys()) == {"a", "b"}
    # ``extra_tools`` are merged into the non-root agent's callables (line 3562).
    assert "shared" in specs["b"][1]


def test_resolve_agent_handoff_target_explicit_valid() -> None:
    agent = _agent("a", handoffs=[IRHandoff(target_node_id="b"), IRHandoff(target_node_id="c")])
    result = AgentTurnResult(final_output="x", handoff_target="c")
    ir = _workflow(nodes={"a": agent, "b": _agent("b"), "c": _agent("c")})
    assert _resolve_agent_handoff_target(agent, result, ir, input_text="hi") == "c"


def test_template_path_tokens_branches() -> None:
    assert _template_path_tokens("a[key]") == ["a", "key"]  # bare index token (3782)
    with pytest.raises(ValueError, match="empty path segment"):
        _template_path_tokens("a. .b")  # blank dotted segment (3755)
    with pytest.raises(ValueError, match="empty path segment"):
        _template_path_tokens("a. [0]")  # blank segment before '[' (3766)


def test_template_lookup_value_valid_index_and_attribute() -> None:
    assert _template_lookup_value({"a": [10, 20]}, "a[1]") == (True, 20)
    obj = SimpleNamespace(val=7)
    assert _template_lookup_value({"o": obj}, "o.val") == (True, 7)


def test_resolve_json_template_tokens_list_and_scalar() -> None:
    token_values = {"tok": ("X", True)}
    assert _resolve_json_template_tokens(["tok", "keep"], token_values) == ["X", "keep"]
    assert _resolve_json_template_tokens(5, token_values) == 5


def test_render_json_template_missing_variable_empty_mode() -> None:
    rendered, result, used, missing = _render_json_template(
        '{"v": "{{absent}}"}', context={}, missing_variable_mode="empty"
    )
    assert result == {"v": ""}
    assert missing == ["absent"]


def test_evaluate_runtime_expression_comparisons() -> None:
    assert _evaluate_runtime_expression("a == b", {"a": 1, "b": 1}) is True
    assert _evaluate_runtime_expression("a == b", {"a": 1, "b": 2}) is False
    assert _evaluate_runtime_expression("a < b", {"a": 1, "b": 2}) is True
    assert _evaluate_runtime_expression("a <= b", {"a": 2, "b": 2}) is True
    assert _evaluate_runtime_expression("a > b", {"a": 3, "b": 2}) is True
    assert _evaluate_runtime_expression("x in y", {"x": 1, "y": [1, 2]}) is True
    assert _evaluate_runtime_expression("x not in y", {"x": 9, "y": [1, 2]}) is True
    assert _evaluate_runtime_expression("a and b", {"a": 1, "b": 2}) is True


def test_correlation_value_from_object_no_match_in_list() -> None:
    assert _correlation_value_from_object([{"x": 1}], "id") == (False, None)


def test_resolve_wait_for_event_correlation_not_found() -> None:
    assert (
        _resolve_wait_for_event_correlation_value(
            inputs={"other": "v"}, run_input="", correlation_key="missing"
        )
        is None
    )


def test_workflow_handoff_is_enabled() -> None:
    always = workflow_handoff_is_enabled("")
    assert always(None, None) is True
    conditional = workflow_handoff_is_enabled("'refund' in input")
    ctx = SimpleNamespace(turn_input="refund please")
    assert conditional(ctx, None) is True


def test_workflow_handoff_input_filter_uses_input_history() -> None:
    filt = workflow_handoff_input_filter("Handled: {{input}}")
    clone_calls: dict[str, Any] = {}

    def _clone(**kwargs: Any) -> str:
        clone_calls.update(kwargs)
        return "CLONED"

    data = SimpleNamespace(
        run_context=SimpleNamespace(turn_input=[]),
        input_history="original request",
        pre_handoff_items=(),
        new_items=(),
        clone=_clone,
    )
    assert filt(data) == "CLONED"
    assert clone_calls["input_history"] == "Handled: original request"


def test_inline_target_inputs_arguments_string_item() -> None:
    node = IRNode(node_id="t", node_type=NodeType.TOOL, inputs={"arguments": IRType("structured")})
    payload, _ = _inline_target_inputs(node, "raw string")
    assert payload == {"arguments": "raw string"}


def test_loop_next_state_more_branches() -> None:
    step = NodeStep("n", NodeType.TOOL.value, "ok", output="so")
    # nested "result" present but None -> return the whole result value.
    assert _loop_next_state(step, {"result": {"result": None, "text": "t"}}) == {
        "result": None,
        "text": "t",
    }
    # None output skipped, final_output used.
    assert _loop_next_state(step, {"output": None, "final_output": "fo"}) == "fo"
    # empty-string output skipped, answer used.
    assert _loop_next_state(step, {"output": "", "answer": "ans"}) == "ans"
    # nothing usable + no child outputs -> the (empty) step output.
    empty = NodeStep("n", NodeType.TOOL.value, "ok", output="")
    assert _loop_next_state(empty, {}) == ""


def test_supports_inline_orchestration_target() -> None:
    assert _supports_inline_orchestration_target(_agent("a")) is True
    assert _supports_inline_orchestration_target(None) is False
    assert (
        _supports_inline_orchestration_target(IRNode(node_id="s", node_type=NodeType.START))
        is False
    )


def test_resolve_bound_tool_callable_required_vs_optional() -> None:
    binding = IRToolBinding(
        local_name="missing",
        registry_ref="tool.missing.v1",
        version_constraint="*",
        requires_approval=False,
        side_effect_level="read",
        allow_in_preview=True,
        module_path="<in-memory>",
        callable_name="",
    )
    with pytest.raises(ToolExecutionError, match="could not be bound"):
        _resolve_bound_tool_callable(binding, _resolver(), preview=False, required=True)
    assert _resolve_bound_tool_callable(binding, _resolver(), preview=False, required=False) is None


def test_resolve_external_app_entrypoint_dotted_and_invalid() -> None:
    # Allowlisted (C8): entrypoints fail closed by default, so pass the allowlist to
    # keep this test about spec parsing.
    fn, resolved = _resolve_external_app_entrypoint("json.dumps", allowlist="json:dumps")
    assert resolved == "json:dumps"
    assert callable(fn)
    with pytest.raises(ToolExecutionError, match="is invalid"):
        _resolve_external_app_entrypoint(":dumps", allowlist=":dumps")


def test_handoff_input_items_converts_and_skips_failures() -> None:
    class Item:
        def to_input_item(self) -> dict[str, Any]:
            return {"role": "user", "content": "converted"}

    class Bad:
        def to_input_item(self) -> dict[str, Any]:
            raise RuntimeError("nope")

    items = _normalize_handoff_messages([Item(), Bad(), {"role": "assistant", "content": "x"}])
    assert {"role": "user", "content": "converted"} in items
    assert {"role": "assistant", "content": "x"} in items


def test_handoff_assistant_output_returns_last_assistant() -> None:
    items = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "first"},
        {"role": "assistant", "content": "final answer"},
    ]
    assert _handoff_assistant_output(items) == "final answer"


def test_evaluate_runtime_expression_collections_and_arithmetic() -> None:
    assert _evaluate_runtime_expression("data['n'] + 1 > 5", {"data": {"n": 5}}) is True
    assert _evaluate_runtime_expression("[1, 2][0] == 1", {}) is True
    assert _evaluate_runtime_expression("obj.x == 1", {"obj": SimpleNamespace(x=1)}) is True
    assert _evaluate_runtime_expression("(1, 2) == (1, 2)", {}) is True
    assert _evaluate_runtime_expression("{1, 2} == {2, 1}", {}) is True
    assert _evaluate_runtime_expression("{'k': 1}['k'] == 1", {}) is True
    assert _evaluate_runtime_expression("-x < 0", {"x": 5}) is True
    assert _evaluate_runtime_expression("+x == 5", {"x": 5}) is True
    assert _evaluate_runtime_expression("10 - 3 == 7", {}) is True
    assert _evaluate_runtime_expression("6 * 2 == 12", {}) is True
    assert _evaluate_runtime_expression("8 / 2 == 4.0", {}) is True
    assert _evaluate_runtime_expression("7 // 2 == 3", {}) is True
    assert _evaluate_runtime_expression("10 % 3 == 1", {}) is True


def test_wait_until_deadline_and_ready() -> None:
    assert _wait_until_deadline("") is None
    assert _wait_until_deadline("not-a-date") is None
    assert _wait_until_deadline("now") is not None
    assert _wait_until_ready("now") is True
    assert _wait_until_ready("2999-01-01T00:00:00Z") is False
    with pytest.raises(ValueError, match="unknown timezone"):
        _wait_until_deadline("2030-01-01T00:00:00", timezone_name="Not/AZone")


def test_write_output_folder_node_requires_path() -> None:
    with pytest.raises(ValueError, match="requires a folder path"):
        _write_output_folder_node(path="", overwrite=True, port_values={}, direct_input=None)
