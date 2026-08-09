"""Untyped access to any management endpoint.

Every typed module in M2/M3 is a convenience over this. It ships first, and
stays, because a client that can only reach what someone modelled is a client
that blocks its users on the SDK's release schedule.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from ._base import Resource


class RawAPI(Resource):
    """Call any path under ``/ajax-api/2.0/mlflow/caliber``."""

    def get(self, path: str, **kwargs: Any) -> Any:
        return self._get(path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self._post(path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self._put(path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Any:
        return self._patch(path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self._delete(path, **kwargs)

    def paginate(
        self, path: str, *, params: Mapping[str, Any] | None = None, limit: int = 100
    ) -> Iterator[Any]:
        return self._transport.paginate(path, params=params, limit=limit)


__all__ = ["RawAPI"]
