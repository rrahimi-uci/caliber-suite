"""Unit tests for the shared reasoning-model detection (``caliber.llm.models``)."""

from __future__ import annotations

import pytest

from caliber.llm.models import (
    DEFAULT_OPENAI_MODEL,
    DEFAULT_OPENAI_REASONING_EFFORT,
    is_reasoning_model,
    reasoning_effort_for_model,
    supports_temperature,
)


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
        "gpt-5.6-luna",
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


def test_repository_default_is_luna_with_high_reasoning() -> None:
    assert DEFAULT_OPENAI_MODEL == "gpt-5.6-luna"
    assert DEFAULT_OPENAI_REASONING_EFFORT == "high"
    assert reasoning_effort_for_model(DEFAULT_OPENAI_MODEL, "HIGH") == "high"
    assert reasoning_effort_for_model("gpt-4o", "high") is None
