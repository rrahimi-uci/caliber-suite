"""Unit tests for the shared reasoning-model detection (``caliber.llm.models``)."""

from __future__ import annotations

import pytest

from caliber.llm.models import is_reasoning_model, supports_temperature


@pytest.mark.parametrize(
    "model",
    [
        "o1",
        "o1-mini",
        "o3",
        "o4-mini",
        "O3-PRO",
        "gpt-5",
        "gpt-5.2",
        "gpt-5-mini",
        "openai:/gpt-5.2",
    ],
)
def test_reasoning_models_detected(model: str) -> None:
    assert is_reasoning_model(model) is True
    # Reasoning models reject a custom temperature.
    assert supports_temperature(model) is False


@pytest.mark.parametrize(
    "model",
    ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "claude-sonnet-4", "", None, "text-embedding-3-large"],
)
def test_non_reasoning_models(model: str | None) -> None:
    assert is_reasoning_model(model) is False
    assert supports_temperature(model) is True


def test_o_prefix_requires_a_digit() -> None:
    # "openai" / "omni" etc. must NOT be misread as o-series reasoning models.
    assert is_reasoning_model("omni-model") is False
    assert is_reasoning_model("openai-thing") is False
