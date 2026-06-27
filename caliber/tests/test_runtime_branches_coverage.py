"""Branch/edge coverage for workflow runtime helpers (plan §19.16).

Targets the smaller pure helpers in :mod:`caliber.workflows.runtime` whose
error/alternate-type/empty branches are not exercised by the integration-style
tests in ``test_workflow_runtime.py``: the template path parser/lookup, the text
template renderer, the handoff turn-input normalisers, the bounded AST condition
evaluator, the wait-until parser, the router condition matcher, the loop
next-state extractor and the structured-input normalisers.
"""

from __future__ import annotations

import ast

import pytest

from caliber.workflows.ir import NodeType
from caliber.workflows.runtime import (
    NodeStep,
    ToolExecutionError,
    _bucket_io,
    _condition_matches,
    _correlation_value_from_object,
    _evaluate_handoff_expression,
    _evaluate_runtime_expression,
    _handoff_assistant_output,
    _handoff_condition_enabled,
    _handoff_input_items,
    _join_key,
    _loop_next_state,
    _maybe_json_value,
    _normalize_graph_overrides,
    _normalize_iterable,
    _normalize_message_history,
    _normalize_object_payload,
    _normalize_string_list,
    _normalize_structured_list,
    _render_handoff_filter_text,
    _render_text_template,
    _resolve_json_template_tokens,
    _resolve_wait_for_event_correlation_value,
    _route,
    _split_handoff_turn_input,
    _template_json_inside_string,
    _template_lookup_value,
    _template_path_tokens,
    _wait_until_deadline,
    _wait_until_ready,
)


def _step(output: str = "") -> NodeStep:
    return NodeStep("n", NodeType.TOOL, "ok", output=output)


# ---------------------------------------------------------------------------
# Template path tokenizer / lookup
# ---------------------------------------------------------------------------


def test_template_path_tokens_parses_dotted_and_indexed_path() -> None:
    assert _template_path_tokens('a[0].b["c"]') == ["a", 0, "b", "c"]


def test_template_path_tokens_rejects_leading_dot() -> None:
    with pytest.raises(ValueError, match="cannot start with"):
        _template_path_tokens(".a")


def test_template_path_tokens_rejects_unterminated_index() -> None:
    with pytest.raises(ValueError, match="missing a closing"):
        _template_path_tokens("a[0")


def test_template_path_tokens_rejects_empty_index() -> None:
    with pytest.raises(ValueError, match="index access cannot be empty"):
        _template_path_tokens("a[]")


def test_template_path_tokens_rejects_empty_expression() -> None:
    with pytest.raises(ValueError, match="empty"):
        _template_path_tokens("   ")


def test_template_lookup_value_traverses_nested_list_index() -> None:
    found, value = _template_lookup_value({"obj": {"x": [10, 20]}}, "obj.x[1]")
    assert (found, value) == (True, 20)


def test_template_lookup_value_returns_false_for_missing_path() -> None:
    assert _template_lookup_value({"input": "hi"}, "nope.bad") == (False, None)


def test_template_lookup_value_returns_false_for_blank_expression() -> None:
    assert _template_lookup_value({"a": 1}, "   ") == (False, None)


def test_template_lookup_value_reads_object_attribute() -> None:
    obj = type("O", (), {"val": 7})()
    assert _template_lookup_value({"o": obj}, "o.val") == (True, 7)


# ---------------------------------------------------------------------------
# Text template rendering / JSON token resolution
# ---------------------------------------------------------------------------


def test_render_text_template_preserve_mode_keeps_missing_token() -> None:
    rendered, used, missing = _render_text_template(
        "hi {{input}} {{missing}}",
        context={"input": "world"},
        missing_variable_mode="preserve",
    )
    assert rendered == "hi world {{missing}}"
    assert used == ["input"]
    assert missing == ["missing"]


def test_render_text_template_empty_mode_blanks_missing_token() -> None:
    rendered, _used, missing = _render_text_template(
        "hi {{missing}}",
        context={},
        missing_variable_mode="empty",
    )
    assert rendered == "hi "
    assert missing == ["missing"]


def test_render_text_template_error_mode_raises_on_missing() -> None:
    with pytest.raises(ToolExecutionError, match="missing variable"):
        _render_text_template(
            "{{missing}}", context={}, missing_variable_mode="error"
        )


def test_template_json_inside_string_detects_open_quote() -> None:
    template = '{"a":"hi'
    assert _template_json_inside_string(template, len(template)) is True


def test_resolve_json_template_tokens_handles_nested_containers() -> None:
    token_values = {"TOK": ("value", False), "STOK": ("s", True)}
    resolved = _resolve_json_template_tokens(
        {"k": "TOK", "l": ["STOK", "plain"]}, token_values
    )
    assert resolved == {"k": "value", "l": ["s", "plain"]}


# ---------------------------------------------------------------------------
# Handoff turn-input normalisation
# ---------------------------------------------------------------------------


def test_handoff_input_items_converts_and_skips_failures() -> None:
    class _Item:
        def to_input_item(self) -> dict[str, str]:
            return {"role": "user", "content": "hello"}

    class _Bad:
        def to_input_item(self) -> dict[str, str]:
            raise RuntimeError("boom")

    items = _handoff_input_items(
        [_Item(), _Bad(), {"role": "assistant", "content": "a"}, "skip-me"]
    )
    assert items == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "a"},
    ]


def test_handoff_input_items_rejects_non_sequence() -> None:
    assert _handoff_input_items("nope") == []


def test_split_handoff_turn_input_returns_string_unchanged() -> None:
    assert _split_handoff_turn_input("hello") == ("hello", [])


def test_split_handoff_turn_input_splits_on_last_user_message() -> None:
    text, history = _split_handoff_turn_input(
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]
    )
    assert text == "c"
    assert history == [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
    ]


def test_split_handoff_turn_input_falls_back_to_last_message_without_user() -> None:
    assert _split_handoff_turn_input([{"role": "assistant", "content": "x"}]) == ("x", [])


def test_split_handoff_turn_input_handles_empty_history() -> None:
    assert _split_handoff_turn_input([]) == ("", [])


def test_handoff_assistant_output_prefers_last_assistant_message() -> None:
    items = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "ans"}]
    assert _handoff_assistant_output(items) == "ans"


def test_handoff_assistant_output_falls_back_to_any_content() -> None:
    assert _handoff_assistant_output([{"role": "user", "content": "q"}]) == "q"


def test_handoff_assistant_output_returns_empty_for_non_list() -> None:
    assert _handoff_assistant_output("not-a-list") == ""


# ---------------------------------------------------------------------------
# Bounded AST condition evaluator
# ---------------------------------------------------------------------------


def _eval(expr: str, ctx: dict[str, object] | None = None) -> object:
    return _evaluate_handoff_expression(ast.parse(expr, mode="eval").body, ctx or {})


def test_runtime_expression_supports_boolean_and_comparison_chains() -> None:
    assert _evaluate_runtime_expression("1 < 2 and 2 <= 2", {}) is True
    assert _evaluate_runtime_expression("False or 3 > 5 or True", {}) is True


def test_runtime_expression_supports_membership_and_negation() -> None:
    assert _evaluate_runtime_expression("'a' in ['a', 'b']", {}) is True
    assert _evaluate_runtime_expression("'z' not in 'abc'", {}) is True
    assert _evaluate_runtime_expression("not 0", {}) is True


def test_runtime_expression_supports_arithmetic_operators() -> None:
    assert _evaluate_runtime_expression("1 + 2 * 3 == 7", {}) is True
    assert _evaluate_runtime_expression("10 / 2 - 1 == 4", {}) is True
    assert _evaluate_runtime_expression("7 // 2 == 3 and 7 % 2 == 1", {}) is True
    assert _evaluate_runtime_expression("-x == -5", {"x": 5}) is True
    assert _evaluate_runtime_expression("+x == 3", {"x": 3}) is True


def test_handoff_expression_supports_collections_and_subscript() -> None:
    assert _eval("1 in {1, 2}") is True
    assert _eval("(1, 2)[0] == 1") is True
    assert _eval("{'a': 1}['a'] == 1") is True
    assert _eval("lst[1] == 2", {"lst": [1, 2, 3]}) is True


def test_handoff_expression_dict_member_and_object_attribute() -> None:
    assert _eval("d.foo == 1", {"d": {"foo": 1}}) is True
    obj = type("O", (), {"val": 7})()
    assert _eval("o.val == 7", {"o": obj}) is True


def test_handoff_expression_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="unknown name"):
        _eval("missing")


def test_handoff_expression_rejects_private_attribute() -> None:
    obj = type("X", (), {"_x": 1})()
    with pytest.raises(ValueError, match="private attributes"):
        _eval("d._x", {"d": obj})


def test_handoff_expression_rejects_callable_attribute() -> None:
    with pytest.raises(ValueError, match="callable attributes"):
        _eval("s.upper", {"s": "hi"})


def test_handoff_expression_rejects_dict_unpacking() -> None:
    with pytest.raises(ValueError, match="dict unpacking"):
        _eval("{**d}", {"d": {"a": 1}})


def test_handoff_expression_rejects_unsupported_node() -> None:
    with pytest.raises(ValueError, match="unsupported expression node"):
        _eval("lambda: 1")


def test_handoff_expression_rejects_unsupported_binary_operator() -> None:
    with pytest.raises(ValueError, match="unsupported binary operator"):
        _eval("2 ** 3")


def test_handoff_condition_enabled_returns_true_for_blank() -> None:
    assert _handoff_condition_enabled("", input_text="x") is True


def test_handoff_condition_enabled_swallows_evaluation_errors() -> None:
    assert _handoff_condition_enabled("foo(", input_text="x") is False


def test_render_handoff_filter_text_passes_through_and_renders() -> None:
    assert _render_handoff_filter_text(None, input_text="in") == "in"
    assert _render_handoff_filter_text("hi {{input}}", input_text="bob") == "hi bob"


# ---------------------------------------------------------------------------
# Correlation-value extraction
# ---------------------------------------------------------------------------


def test_correlation_value_from_object_walks_nested_structures() -> None:
    assert _correlation_value_from_object({"a": {"order_id": "5"}}, "order_id") == (
        True,
        "5",
    )
    assert _correlation_value_from_object([{"order_id": "6"}], "order_id") == (True, "6")


def test_correlation_value_from_object_parses_embedded_json_string() -> None:
    assert _correlation_value_from_object('{"order_id": "7"}', "order_id") == (True, "7")


def test_correlation_value_from_object_ignores_malformed_json() -> None:
    assert _correlation_value_from_object("{not json", "order_id") == (False, None)


def test_correlation_value_from_object_returns_false_for_plain_string() -> None:
    assert _correlation_value_from_object("plain", "order_id") == (False, None)


def test_resolve_wait_for_event_correlation_value_from_inputs() -> None:
    assert (
        _resolve_wait_for_event_correlation_value(
            inputs={"order_id": "9"}, run_input="", correlation_key="order_id"
        )
        == "9"
    )


def test_resolve_wait_for_event_correlation_value_blank_key_returns_none() -> None:
    assert (
        _resolve_wait_for_event_correlation_value(
            inputs={}, run_input="", correlation_key="   "
        )
        is None
    )


def test_resolve_wait_for_event_correlation_value_falls_back_to_run_input() -> None:
    assert (
        _resolve_wait_for_event_correlation_value(
            inputs={}, run_input='{"order_id": "11"}', correlation_key="order_id"
        )
        == "11"
    )


def test_resolve_wait_for_event_correlation_value_blank_value_returns_none() -> None:
    assert (
        _resolve_wait_for_event_correlation_value(
            inputs={"order_id": "   "}, run_input="", correlation_key="order_id"
        )
        is None
    )


# ---------------------------------------------------------------------------
# Loop next-state extraction
# ---------------------------------------------------------------------------


def test_loop_next_state_unwraps_nested_result_envelope() -> None:
    assert (
        _loop_next_state(_step(), {"result": {"result": "inner", "text": "t"}}) == "inner"
    )


def test_loop_next_state_returns_plain_result_value() -> None:
    assert _loop_next_state(_step(), {"result": {"a": 1}}) == {"a": 1}


def test_loop_next_state_falls_through_to_alias_keys() -> None:
    assert _loop_next_state(_step(), {"output": "oo"}) == "oo"


def test_loop_next_state_uses_step_output_when_no_value_keys() -> None:
    assert _loop_next_state(_step(output="from-step"), {}) == "from-step"


def test_loop_next_state_returns_outputs_when_step_output_blank() -> None:
    assert _loop_next_state(_step(output=""), {"misc": 5}) == {"misc": 5}


# ---------------------------------------------------------------------------
# Structured-input normalisers
# ---------------------------------------------------------------------------


def test_normalize_iterable_handles_lists_tuples_and_caps() -> None:
    assert _normalize_iterable([1, 2, 3], max_items=2) == [1, 2]
    assert _normalize_iterable((1, 2, 3), max_items=2) == [1, 2]


def test_normalize_iterable_parses_json_and_lines() -> None:
    assert _normalize_iterable("[1, 2, 3]", max_items=2) == [1, 2]
    assert _normalize_iterable("a\nb\n\nc", max_items=5) == ["a", "b", "c"]


def test_normalize_iterable_handles_blank_none_and_scalar() -> None:
    assert _normalize_iterable("   ", max_items=5) == []
    assert _normalize_iterable(None, max_items=5) == []
    assert _normalize_iterable(42, max_items=5) == [42]


def test_normalize_string_list_splits_comma_and_dedupes() -> None:
    assert _normalize_string_list("a, b, a", max_items=10) == ["a", "b"]


def test_normalize_message_history_filters_invalid_entries() -> None:
    raw = (
        '[{"role": "user", "content": "hi"},'
        '{"role": "x", "content": "y"},'
        '{"role": "assistant", "content": "  "}]'
    )
    assert _normalize_message_history(raw) == [{"role": "user", "content": "hi"}]


def test_normalize_message_history_returns_empty_for_non_list() -> None:
    assert _normalize_message_history("5") == []


def test_normalize_graph_overrides_drops_none_values() -> None:
    assert _normalize_graph_overrides('{"a": 1, "b": null}') == {"a": 1}


def test_normalize_graph_overrides_returns_none_for_non_dict() -> None:
    assert _normalize_graph_overrides("[]") is None


def test_normalize_object_payload_stringifies_keys() -> None:
    assert _normalize_object_payload({"x": 1}) == {"x": 1}
    assert _normalize_object_payload("not-a-dict") is None


def test_normalize_structured_list_caps_and_rejects_non_list() -> None:
    assert _normalize_structured_list("[1, 2, 3]", max_items=2) == [1, 2]
    assert _normalize_structured_list("{}", max_items=2) is None


def test_maybe_json_value_parses_objects_and_passes_through() -> None:
    assert _maybe_json_value('{"a": 1}') == {"a": 1}
    assert _maybe_json_value(5) == 5
    assert _maybe_json_value("{bad") == "{bad"
    assert _maybe_json_value("   ") == "   "


# ---------------------------------------------------------------------------
# wait_until parsing
# ---------------------------------------------------------------------------


def test_wait_until_deadline_handles_now_and_blank_and_invalid() -> None:
    assert _wait_until_deadline("now") is not None
    assert _wait_until_deadline("") is None
    assert _wait_until_deadline("not-a-date") is None


def test_wait_until_deadline_applies_named_timezone_to_naive() -> None:
    assert (
        _wait_until_deadline("2030-01-01T00:00:00", timezone_name="America/New_York")
        is not None
    )


def test_wait_until_deadline_rejects_unknown_timezone() -> None:
    with pytest.raises(ValueError, match="unknown timezone"):
        _wait_until_deadline("2030-01-01T00:00:00", timezone_name="Nowhere/Bad")


def test_wait_until_ready_compares_against_now() -> None:
    assert _wait_until_ready("2000-01-01T00:00:00Z") is True
    assert _wait_until_ready("2999-01-01T00:00:00Z") is False
    assert _wait_until_ready("garbage") is False


# ---------------------------------------------------------------------------
# Router condition matcher
# ---------------------------------------------------------------------------


def test_condition_matches_combines_all_any_not() -> None:
    assert _condition_matches(
        {"all": [{"op": "contains", "value": "a", "field": "input"}]},
        {"input": "cat"},
    )
    assert _condition_matches({"any": []}, {"input": "x"}) is False
    assert _condition_matches(
        {"not": {"op": "contains", "value": "z"}}, {"input": "cat"}
    )


def test_condition_matches_exists_and_numeric_operators() -> None:
    assert _condition_matches({"op": "exists"}, {"input": "x"})
    assert _condition_matches({"op": "gt", "value": 5, "field": "n"}, {"n": 10})
    assert (
        _condition_matches({"op": "gt", "value": "x", "field": "n"}, {"n": "y"}) is False
    )


def test_condition_matches_in_operator_for_list_and_string() -> None:
    assert _condition_matches({"op": "in", "value": ["a", "b"]}, {"input": "A"})
    assert _condition_matches({"op": "in", "value": "haystack"}, {"input": "hay"})


def test_condition_matches_text_operators_and_regex() -> None:
    assert _condition_matches({"starts_with": "ca"}, {"input": "cat"})
    assert _condition_matches({"op": "regex", "value": "^c.t$"}, {"input": "cat"})
    assert _condition_matches({"op": "regex", "value": "["}, {"input": "cat"}) is False


def test_condition_matches_against_string_context() -> None:
    assert _condition_matches({"op": "contains", "value": "a"}, "cat")


def test_condition_matches_empty_and_missing_operator_are_false() -> None:
    assert _condition_matches({}, {"input": "cat"}) is False
    assert _condition_matches({"value": "x"}, {"input": "cat"}) is False
    assert _condition_matches({"op": "equals"}, {"input": "cat"}) is False


def test_route_returns_first_matching_branch_then_fallback() -> None:
    class _Branch:
        def __init__(self, to: str, condition: dict[str, object] | None) -> None:
            self.to = to
            self.condition = condition

    class _Router:
        def __init__(self, branches: list[_Branch]) -> None:
            self.branches = branches

    router = _Router(
        [
            _Branch("match", {"op": "contains", "value": "cat"}),
            _Branch("else", None),
        ]
    )
    assert _route(router, {"input": "cat"}) == "match"
    assert _route(router, {"input": "dog"}) == "else"


# ---------------------------------------------------------------------------
# Object-storage key helpers
# ---------------------------------------------------------------------------


def test_join_key_normalises_separators() -> None:
    assert _join_key("p/", "/leaf") == "p/leaf"
    assert _join_key("", "leaf") == "leaf"


def test_bucket_io_folds_bucket_into_prefix_for_local_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ``_bucket_io`` reads the ambient ``CaliberConfig.load().workflow_storage``;
    # force the local backend so the bucket-folding branch is exercised
    # deterministically regardless of an ``s3`` backend leaked by the wider suite
    # (the shipped .env sets ``CALIBER_WORKFLOW_STORAGE_BACKEND=s3``).
    monkeypatch.setenv("CALIBER_WORKFLOW_STORAGE_BACKEND", "local")
    _backend, key_prefix = _bucket_io("mybucket", "/sub/")
    assert key_prefix == "mybucket/sub"
