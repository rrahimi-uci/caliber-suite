"""Shared base for resource modules.

A resource module is a thin, typed façade over a group of routes. Keeping the
transport out of the public surface means a caller never has to know the URL
shape, while :attr:`Resource.raw` keeps the low-level path open for anything
the façade does not cover yet -- the SDK should never be the reason something
is impossible.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters for typing
    from ..transport import Transport


class Resource:
    """Base class for every resource module."""

    def __init__(self, transport: Transport) -> None:
        self._transport = transport

    @property
    def raw(self) -> Transport:
        """Escape hatch to the transport.

        Deliberate: a typed façade that lags the server would otherwise make
        new endpoints unreachable until the SDK catches up.
        """
        return self._transport

    def _get(self, path: str, **kwargs: Any) -> Any:
        return self._transport.get(path, **kwargs).data

    def _post(self, path: str, **kwargs: Any) -> Any:
        return self._transport.post(path, **kwargs).data

    def _put(self, path: str, **kwargs: Any) -> Any:
        return self._transport.put(path, **kwargs).data

    def _patch(self, path: str, **kwargs: Any) -> Any:
        return self._transport.patch(path, **kwargs).data

    def _delete(self, path: str, **kwargs: Any) -> Any:
        return self._transport.delete(path, **kwargs).data


__all__ = ["Resource"]
