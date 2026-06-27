"""Tests for :mod:`caliber.eval.provider` — build_provider factory."""

from __future__ import annotations

import pytest

from caliber.eval.provider import (
    EvalProviderError,
    build_provider,
)


def test_build_provider_fake() -> None:
    provider = build_provider("fake")
    assert provider is not None


def test_build_provider_fake_case_insensitive() -> None:
    provider = build_provider("FAKE")
    assert provider is not None


def test_build_provider_mlflow() -> None:
    provider = build_provider("mlflow")
    assert provider is not None


def test_build_provider_mlflow_case_insensitive() -> None:
    provider = build_provider("MLflow")
    assert provider is not None


def test_build_provider_unknown_raises() -> None:
    with pytest.raises(EvalProviderError, match="unknown eval_provider"):
        build_provider("unknown_provider")
