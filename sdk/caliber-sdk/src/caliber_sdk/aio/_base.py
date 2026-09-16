"""Shared async resource plumbing."""

from __future__ import annotations

from typing import Any

from .transport import AsyncTransport


class _AsyncResource:
    """Unwrap transport responses for typed async resources."""

    def __init__(self, transport: AsyncTransport) -> None:
        self._transport = transport

    async def _get(self, path: str, **kwargs: Any) -> Any:
        return (await self._transport.get(path, **kwargs)).data

    async def _post(self, path: str, **kwargs: Any) -> Any:
        return (await self._transport.post(path, **kwargs)).data

    async def _patch(self, path: str, **kwargs: Any) -> Any:
        return (await self._transport.patch(path, **kwargs)).data

    async def _delete(self, path: str, **kwargs: Any) -> Any:
        return (await self._transport.delete(path, **kwargs)).data


__all__ = ["_AsyncResource"]
