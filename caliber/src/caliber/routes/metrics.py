"""``/metrics`` — Prometheus exposition endpoint.

Returns the contents of the CALIBER metrics registry in the standard
Prometheus text format. The endpoint lives under the same
``/ajax-api/2.0/mlflow/caliber`` prefix as the rest of the API so a
single Prometheus scrape config can hit ``http://mlflow:5000/ajax-api/
2.0/mlflow/caliber/metrics`` regardless of the static prefix the host
deployment is behind.

Authentication is **opt-in**, because a Prometheus scrape config cannot
carry a session cookie and requiring one by default would break every
existing deployment's scraping on upgrade. Set
``CALIBER_METRICS_TOKEN_ENV`` to the name of a secret source and the
endpoint requires ``Authorization: Bearer <token>``; leave it unset and
the endpoint stays open, which is only safe behind a network policy that
keeps ``/metrics`` off the public interface.

The exposed series are operational counters — queue depths, request
rates, token and cost rollups — so an open endpoint is an information
disclosure about workload and spend rather than a way in.
"""

from __future__ import annotations

from hmac import compare_digest

from prometheus_client import CONTENT_TYPE_LATEST
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from caliber.observability.metrics import render

METRICS_PATH = "/ajax-api/2.0/mlflow/caliber/metrics"


def _expected_token(request: Request) -> str:
    """Resolve the configured scrape token, or empty when the gate is off."""
    config = getattr(request.app.state, "config", None)
    source = str(getattr(config, "metrics_token_env", "") or "").strip()
    if not source:
        return ""
    from caliber.secrets import resolve_secret  # noqa: PLC0415

    return str(resolve_secret(source) or "").strip()


async def metrics_endpoint(request: Request) -> Response:
    """Return the current metric set in Prometheus exposition format."""
    expected = _expected_token(request)
    if expected:
        header = request.headers.get("authorization", "")
        scheme, _, presented = header.partition(" ")
        # Constant-time: a scrape endpoint is a convenient oracle precisely
        # because it can be polled without limit.
        if scheme.lower() != "bearer" or not compare_digest(presented.strip(), expected):
            return Response(
                content="unauthorized",
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
                media_type="text/plain",
            )
    return Response(content=render(), media_type=CONTENT_TYPE_LATEST)


def register(app: Starlette) -> None:
    """Add the metrics route to the given Starlette application."""
    app.routes.append(Route(METRICS_PATH, metrics_endpoint, methods=["GET"]))
