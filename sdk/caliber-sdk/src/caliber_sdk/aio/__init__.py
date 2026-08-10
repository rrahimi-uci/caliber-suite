"""Asynchronous client for the CALIBER management API.

    import asyncio
    from caliber_sdk.aio import AsyncCaliberClient

    async def main() -> None:
        async with AsyncCaliberClient(token="calpat_...") as caliber:
            async for line in caliber.events.stream():
                print(line)

    asyncio.run(main())

Requires no extra dependency: ``httpx`` provides both clients.

Typed coverage here is narrower than the synchronous client's, deliberately --
see :mod:`caliber_sdk.aio.client` for what is covered, what is not, and why a
second hand-written copy of the whole resource tree would be a liability rather
than a feature. ``client.raw`` reaches everything either way.
"""

from __future__ import annotations

from .client import AsyncCaliberClient
from .transport import AsyncTransport
from .waiters import wait_for, wait_for_terminal_state

__all__ = [
    "AsyncCaliberClient",
    "AsyncTransport",
    "wait_for",
    "wait_for_terminal_state",
]
