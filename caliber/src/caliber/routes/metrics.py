"""``/metrics`` — Prometheus exposition endpoint.

Returns the contents of the CALIBER metrics registry in the standard
Prometheus text format. The endpoint lives under the same
``/ajax-api/2.0/mlflow/caliber`` prefix as the rest of the API so a
single Prometheus scrape config can hit ``http://mlflow:5000/ajax-api/
2.0/mlflow/caliber/metrics`` regardless of the static prefix the host
deployment is behind.

Authentication is *not* applied here today — the same as MLflow's own
endpoints. When CALIBER's RBAC lands (Phase 5 §5.6) the scrape token
flow goes through the same decorator the rest of the surface uses.
"""

from __future__ import annotations

from prometheus_client import CONTENT_TYPE_LATEST
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from caliber.observability.metrics import render

METRICS_PATH = "/ajax-api/2.0/mlflow/caliber/metrics"


async def metrics_endpoint(request: Request) -> Response:
    """Return the current metric set in Prometheus exposition format."""
    _ = request  # the endpoint is parameterless; route handler signature requires it
    return Response(content=render(), media_type=CONTENT_TYPE_LATEST)


def register(app: Starlette) -> None:
    """Add the metrics route to the given Starlette application."""
    app.routes.append(Route(METRICS_PATH, metrics_endpoint, methods=["GET"]))
