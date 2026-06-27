"""Preview-run sandbox tests (plan §19.7, §26.3)."""

from __future__ import annotations

import pytest

from caliber.workflows.ir import IRToolBinding
from caliber.workflows.sandbox import (
    DEFAULT_PREVIEW_TOKEN_BUDGET,
    PreviewBudgetExceededError,
    TokenBudget,
    make_preview_callable,
    mock_response_for,
    should_mock_in_preview,
)


def _binding(level: str, *, allow_in_preview: bool = False, output_schema=None) -> IRToolBinding:
    return IRToolBinding(
        local_name="t",
        registry_ref="tool.t.v1",
        version_constraint="",
        requires_approval=False,
        side_effect_level=level,
        allow_in_preview=allow_in_preview,
        module_path="m",
        callable_name="f",
        output_schema=output_schema,
    )


def test_read_tool_mocked_unless_opted_in() -> None:
    assert should_mock_in_preview(_binding("read")) is True
    assert should_mock_in_preview(_binding("read", allow_in_preview=True)) is False


def test_write_and_external_always_mocked() -> None:
    assert should_mock_in_preview(_binding("write", allow_in_preview=True)) is True
    assert should_mock_in_preview(_binding("external_action", allow_in_preview=True)) is True


def test_make_preview_callable_mocks_write_tool() -> None:
    called = {"n": 0}

    def real(*_a, **_k):
        called["n"] += 1
        return {"real": True}

    fn = make_preview_callable(_binding("write"), real)
    result = fn("x")
    assert called["n"] == 0  # real callable never invoked
    assert result["_preview_mock"] is True


def test_make_preview_callable_runs_allowed_read_tool() -> None:
    fn = make_preview_callable(
        _binding("read", allow_in_preview=True), lambda *_a, **_k: {"real": True}
    )
    assert fn("x") == {"real": True}


def test_mock_response_matches_output_schema() -> None:
    schema = {
        "type": "object",
        "properties": {"policy": {"type": "string"}, "n": {"type": "integer"}},
    }
    binding = _binding("write", output_schema=schema)
    resp = mock_response_for(binding)
    assert "policy" in resp and isinstance(resp["policy"], str)
    assert "n" in resp and isinstance(resp["n"], int)
    assert resp["_preview_mock"] is True


def test_token_budget_enforced() -> None:
    budget = TokenBudget(limit=50)
    budget.charge(40)
    with pytest.raises(PreviewBudgetExceededError):
        budget.charge(20)


def test_default_token_budget() -> None:
    assert TokenBudget().limit == DEFAULT_PREVIEW_TOKEN_BUDGET
