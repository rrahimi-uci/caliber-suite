"""Naming corrections shipped behind deprecation aliases (AD-6,
sdk-completeness-plan.md).

``CaliberClient.capabilities_api`` and ``CaliberClient.datasets`` were
renamed to ``capabilities_info`` and ``eval_datasets`` -- names that say what
each resource actually is, rather than what kind of thing it is (an "API",
"data"). The old names still work: this module is what proves they still
work, that they warn, and that the canonical names stay silent.
"""

from __future__ import annotations

import asyncio
import warnings
from typing import Any

import httpx
import pytest

from caliber_sdk import CaliberClient
from caliber_sdk.aio import AsyncCaliberClient

BASE = "https://caliber.test"


def client_with(handler: Any) -> CaliberClient:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return CaliberClient(BASE, token="calpat_test", http_client=http)


def _unused_handler(_request: httpx.Request) -> httpx.Response:
    raise AssertionError("accessing a client property must not make a request")


def test_capabilities_api_warns_and_returns_the_same_resource_as_capabilities_info() -> None:
    caliber = client_with(_unused_handler)
    with pytest.warns(DeprecationWarning, match="capabilities_info"):
        aliased = caliber.capabilities_api
    assert aliased is caliber.capabilities_info


def test_datasets_warns_and_returns_the_same_resource_as_eval_datasets() -> None:
    caliber = client_with(_unused_handler)
    with pytest.warns(DeprecationWarning, match="eval_datasets"):
        aliased = caliber.datasets
    assert aliased is caliber.eval_datasets


def test_the_canonical_names_do_not_warn() -> None:
    caliber = client_with(_unused_handler)
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        _capabilities = caliber.capabilities_info
        _datasets = caliber.eval_datasets
        assert _capabilities is not None
        assert _datasets is not None


def test_async_capabilities_api_warns_and_returns_the_same_resource_as_capabilities_info() -> None:
    async def main() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(_unused_handler))
        async with AsyncCaliberClient(BASE, token="calpat_test", http_client=http) as caliber:
            with pytest.warns(DeprecationWarning, match="capabilities_info"):
                aliased = caliber.capabilities_api
            assert aliased is caliber.capabilities_info

    asyncio.run(main())
