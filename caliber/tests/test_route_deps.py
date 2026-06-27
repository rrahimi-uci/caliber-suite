"""Unit tests for shared route helpers in ``caliber.routes._deps``."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from caliber.routes._deps import list_limit


def _request(**params: str) -> Any:
    """A minimal Request stand-in exposing only ``.query_params.get``."""
    return SimpleNamespace(query_params=dict(params))


def test_list_limit_defaults_when_absent() -> None:
    assert list_limit(_request()) == (500, 0)


def test_list_limit_reads_explicit_values() -> None:
    assert list_limit(_request(limit="25", offset="10")) == (25, 10)


def test_list_limit_caps_oversized_limit() -> None:
    assert list_limit(_request(limit="999999")) == (2000, 0)


def test_list_limit_floors_limit_at_one() -> None:
    # A request for zero/negative rows still returns at least one — a list
    # endpoint that returned nothing for ``?limit=0`` would look broken.
    assert list_limit(_request(limit="0")) == (1, 0)
    assert list_limit(_request(limit="-5")) == (1, 0)


def test_list_limit_clamps_negative_offset_to_zero() -> None:
    assert list_limit(_request(offset="-3")) == (500, 0)


@pytest.mark.parametrize("bad", ["abc", "", "1.5", "ten"])
def test_list_limit_falls_back_on_garbage(bad: str) -> None:
    # Garbage values must not 500 the endpoint — they fall back to defaults.
    assert list_limit(_request(limit=bad, offset=bad)) == (500, 0)


def test_list_limit_honors_custom_default_and_cap() -> None:
    assert list_limit(_request(), default=50, cap=100) == (50, 0)
    assert list_limit(_request(limit="500"), default=50, cap=100) == (100, 0)
