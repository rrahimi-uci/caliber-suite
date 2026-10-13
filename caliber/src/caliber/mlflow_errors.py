"""Recognising "the MLflow tracking server is not reachable" (alpha gap P1-04).

With MLflow stopped, ``POST /prompts`` and Apply used to sit in MLflow's own retry loop for
about eight minutes and then answer 502 with a client stack trace as the detail. A user
cannot tell that from a bug. The fix has two parts: bound the retries (``server.py`` sets
``MLFLOW_HTTP_REQUEST_MAX_RETRIES``), and answer a connection failure with a 503 that says
what is wrong, which is what this module is for.

Detection walks the exception chain rather than matching message text alone, because MLflow
wraps ``requests`` errors in ``MlflowException`` and the original type is only on
``__cause__``.
"""

from __future__ import annotations

from starlette.exceptions import HTTPException

#: The detail a client sees. Fixed text: the raw exception carries the tracking URI, which
#: can embed credentials.
UNREACHABLE_DETAIL = (
    "MLflow tracking server unreachable: CALIBER could not reach MLflow, so nothing was "
    "changed. Check that MLflow is running and MLFLOW_TRACKING_URI is correct, then retry."
)

#: Message fragments that mean a connection-level failure when the chain has no typed error
#: (MLflow sometimes re-raises with only text).
_CONNECTION_MARKERS = (
    "max retries exceeded with url",
    "failed to establish a new connection",
    "connection refused",
    "connection aborted",
    "connection reset",
    "read timed out",
    "connect timeout",
    "name or service not known",
    "temporary failure in name resolution",
)

_MAX_CHAIN = 8


def _typed_connection_error(exc: BaseException) -> bool:
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    try:
        import requests  # noqa: PLC0415

        if isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout)):
            return True
    except ImportError:  # pragma: no cover - requests ships with mlflow
        pass
    try:
        from urllib3.exceptions import (  # noqa: PLC0415
            MaxRetryError,
            NewConnectionError,
            ReadTimeoutError,
        )

        return isinstance(exc, (MaxRetryError, NewConnectionError, ReadTimeoutError))
    except ImportError:  # pragma: no cover - urllib3 ships with requests
        return False


def is_mlflow_unreachable(exc: BaseException) -> bool:
    """Whether ``exc`` (or anything it was raised from) is a connection-level failure."""
    seen: set[int] = set()
    current: BaseException | None = exc
    for _ in range(_MAX_CHAIN):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        if _typed_connection_error(current):
            return True
        message = str(current).lower()
        if any(marker in message for marker in _CONNECTION_MARKERS):
            return True
        current = current.__cause__ or current.__context__
    return False


def unreachable_http_exception() -> HTTPException:
    """The 503 to raise when MLflow could not be reached."""
    return HTTPException(status_code=503, detail=UNREACHABLE_DETAIL)
