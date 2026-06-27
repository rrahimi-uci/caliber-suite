"""Observability primitives for CALIBER.

Three independent concerns live here:

* :mod:`caliber.observability.trace` — request-scoped trace IDs that
  flow into log lines and metric labels.
* :mod:`caliber.observability.logging` — single-line JSON log
  formatter + :func:`configure_logging` helper.
* :mod:`caliber.observability.metrics` — Prometheus counters,
  histograms, gauges, and the registry the ``/metrics`` route exposes.
"""

from __future__ import annotations

from caliber.observability.logging import JsonFormatter, configure_logging
from caliber.observability.metrics import registry as metrics_registry
from caliber.observability.metrics import render as render_metrics
from caliber.observability.trace import (
    TraceIdMiddleware,
    bind_trace_id,
    current_trace_id,
    new_trace_id,
)

__all__ = [
    "JsonFormatter",
    "TraceIdMiddleware",
    "bind_trace_id",
    "configure_logging",
    "current_trace_id",
    "metrics_registry",
    "new_trace_id",
    "render_metrics",
]
