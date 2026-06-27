"""Prometheus metrics for CALIBER.

The metric set tracks the parity-checklist §7 fields plus a few we'll
want for the OSS demo. Three categories:

* **Counters** — monotonically increasing totals (jobs created, items
  verified, approvals decided, promotions emitted, rollbacks executed).
* **Histograms** — pipeline-duration timing per stage and LLM call
  duration. Buckets are tuned for the seconds-to-tens-of-seconds range
  CALIBER's pipeline operates in.
* **Gauges** — instantaneous queue depths (verification pending, jobs
  awaiting approval) so an oncall dashboard can show "what's queued
  right now" without a histogram.

We use ``prometheus_client`` because it's the de-facto standard and
zero-config. Metric handles are module-level singletons so any call
site can import + increment without going through a registry lookup.
For tests, :func:`reset_metrics_for_test` clears the registry between
runs so counter values don't leak across tests.
"""

from __future__ import annotations

import contextlib

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# We own the registry so we control which collectors land in our
# exposition output — no surprise process/GC metrics from the default
# global registry leaking into the response.
registry = CollectorRegistry(auto_describe=True)


def _new_counter(name: str, documentation: str, labelnames: tuple[str, ...]) -> Counter:
    return Counter(name, documentation, labelnames=labelnames, registry=registry)


def _new_histogram(
    name: str,
    documentation: str,
    labelnames: tuple[str, ...],
    *,
    buckets: tuple[float, ...] = (
        0.1,
        0.5,
        1.0,
        2.5,
        5.0,
        10.0,
        30.0,
        60.0,
        300.0,
    ),
) -> Histogram:
    return Histogram(
        name,
        documentation,
        labelnames=labelnames,
        registry=registry,
        buckets=buckets,
    )


def _new_gauge(name: str, documentation: str, labelnames: tuple[str, ...]) -> Gauge:
    return Gauge(name, documentation, labelnames=labelnames, registry=registry)


# ---------------------------------------------------------------------------
# Counters
# ---------------------------------------------------------------------------

verification_items_total = _new_counter(
    "caliber_verification_items_total",
    "Verification queue items by terminal status.",
    ("agent_id", "outcome"),  # outcome ∈ verified|dismissed|duplicate
)

jobs_total = _new_counter(
    "caliber_jobs_total",
    "Refinement jobs by terminal status.",
    ("agent_id", "artifact_type", "status"),  # status ∈ completed|failed|rejected
)

approvals_total = _new_counter(
    "caliber_approvals_total",
    "Approval decisions.",
    ("agent_id", "decision"),  # decision ∈ approved|rejected|request_changes
)

promotions_total = _new_counter(
    "caliber_promotions_total",
    "Successful artifact promotions.",
    ("agent_id", "artifact_type"),
)

rollbacks_total = _new_counter(
    "caliber_rollbacks_total",
    "Rollback operations.",
    ("agent_id",),
)

# Workflow Studio counters (ext C1).
workflow_compiles_total = _new_counter(
    "caliber_workflow_compiles_total",
    "Workflow compiler invocations.",
    ("result",),  # ok | error
)

workflow_previews_total = _new_counter(
    "caliber_workflow_previews_total",
    "Workflow preview runs.",
    ("status",),  # completed | blocked | error
)

workflow_deploy_gates_total = _new_counter(
    "caliber_workflow_deploy_gates_total",
    "Deploy-gate evaluations.",
    ("alias", "result"),  # result: passed | failed
)

workflow_patches_total = _new_counter(
    "caliber_workflow_patches_total",
    "CALIBER workflow patches generated.",
    ("patch_kind",),
)

workflow_promotions_total = _new_counter(
    "caliber_workflow_promotions_total",
    "Workflow version promotions (alias rotations).",
    ("alias",),
)

# ---------------------------------------------------------------------------
# Histograms
# ---------------------------------------------------------------------------

stage_duration_seconds = _new_histogram(
    "caliber_stage_duration_seconds",
    "Time spent in a refinement stage.",
    ("agent_id", "stage"),
)

workflow_compile_seconds = _new_histogram(
    "caliber_workflow_compile_seconds",
    "Workflow compile duration.",
    (),
    buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0, 5.0),
)

# ---------------------------------------------------------------------------
# Gauges
# ---------------------------------------------------------------------------

verification_queue_depth = _new_gauge(
    "caliber_verification_queue_depth",
    "Items currently in the verification queue.",
    ("severity",),
)

approvals_pending = _new_gauge(
    "caliber_approvals_pending",
    "Approval requests currently in the pending state.",
    (),
)

jobs_in_flight = _new_gauge(
    "caliber_jobs_in_flight",
    "Refinement jobs currently in a non-terminal status.",
    ("status",),
)


# ---------------------------------------------------------------------------
# Convenience facade — keeps call sites short.
# ---------------------------------------------------------------------------


def record_verification_outcome(agent_id: str, outcome: str) -> None:
    """Increment :data:`verification_items_total` for a closed item."""
    verification_items_total.labels(agent_id=agent_id, outcome=outcome).inc()


def record_job_terminal(agent_id: str, artifact_type: str, status: str) -> None:
    """Increment :data:`jobs_total` when a job reaches a terminal status."""
    jobs_total.labels(agent_id=agent_id, artifact_type=artifact_type, status=status).inc()


def record_approval_decision(agent_id: str, decision: str) -> None:
    """Increment :data:`approvals_total` for an approve/reject/request-changes."""
    approvals_total.labels(agent_id=agent_id, decision=decision).inc()


def record_promotion(agent_id: str, artifact_type: str) -> None:
    """Increment :data:`promotions_total` on every successful promote."""
    promotions_total.labels(agent_id=agent_id, artifact_type=artifact_type).inc()


def record_rollback(agent_id: str) -> None:
    """Increment :data:`rollbacks_total` on every successful rollback."""
    rollbacks_total.labels(agent_id=agent_id).inc()


def record_workflow_compile(*, ok: bool, duration_ms: float | None = None) -> None:
    """Record a workflow compile outcome + duration (ext C1)."""
    workflow_compiles_total.labels(result="ok" if ok else "error").inc()
    if duration_ms is not None:
        workflow_compile_seconds.observe(duration_ms / 1000.0)


def record_workflow_preview(status: str) -> None:
    """Record a preview-run outcome (completed/blocked/error)."""
    workflow_previews_total.labels(status=status).inc()


def record_deploy_gate(alias: str, *, passed: bool) -> None:
    """Record a deploy-gate evaluation result for an alias."""
    workflow_deploy_gates_total.labels(alias=alias, result="passed" if passed else "failed").inc()


def record_workflow_patch(patch_kind: str) -> None:
    """Record a generated workflow patch by kind (workflow_manifest/prompt)."""
    workflow_patches_total.labels(patch_kind=patch_kind).inc()


def record_workflow_promotion(alias: str) -> None:
    """Record a workflow version promotion (alias rotation)."""
    workflow_promotions_total.labels(alias=alias).inc()


def observe_stage_duration(agent_id: str, stage: str, seconds: float) -> None:
    """Observe a stage timing into the histogram."""
    stage_duration_seconds.labels(agent_id=agent_id, stage=stage).observe(seconds)


def set_queue_depth(severity: str, value: int) -> None:
    """Set the pending verification gauge for a severity bucket."""
    verification_queue_depth.labels(severity=severity).set(value)


def set_approvals_pending(value: int) -> None:
    approvals_pending.set(value)


def set_jobs_in_flight(status: str, value: int) -> None:
    jobs_in_flight.labels(status=status).set(value)


# ---------------------------------------------------------------------------
# Exposition
# ---------------------------------------------------------------------------


def render() -> bytes:
    """Render the current registry contents in Prometheus exposition format."""
    return generate_latest(registry)


def reset_metrics_for_test() -> None:
    """Zero out every collector — only intended for tests.

    Two paths because ``prometheus_client`` distinguishes labeled and
    unlabeled metrics:

    * Labeled metrics expose ``.clear()`` which drops every label series.
    * Unlabeled metrics (e.g. ``approvals_pending``) don't have a
      working ``.clear()`` — internally they're a single value with no
      lock. We zero them via ``.set(0)`` for gauges and re-create the
      ``_value`` for counters via the typed setter the library exposes.

    The two-arm structure isolates each metric's reset to its own
    try/except so a single broken collector doesn't taint the rest.
    """
    for collector in list(registry._collector_to_names.keys()):
        # Best-effort: a missing private attribute on any single
        # metric type shouldn't taint the whole reset cycle. Logging
        # the swallowed error would be ironic — this *is* the test
        # helper that lets the logging tests run.
        with contextlib.suppress(Exception):
            _reset_one(collector)


def _reset_one(collector: object) -> None:
    # Labeled metrics: ``_metrics`` is the dict of (labelvalues → child).
    # Dropping the entries (and any side state) is sufficient for both
    # Counter and Histogram families and matches what their ``.clear()``
    # would do if it didn't trip on missing locks.
    has_labels = bool(getattr(collector, "_metrics", None))
    if has_labels:
        clear_method = getattr(collector, "clear", None)
        if callable(clear_method):
            clear_method()
        return

    # Unlabeled gauges → set to 0 (the natural empty state).
    if isinstance(collector, Gauge):
        collector.set(0)
        return

    # Unlabeled counters: replace the internal counter object. Counter
    # exposes no public reset; touching ``_value`` is the documented
    # extension point in the test guide (see prometheus_client/values.py).
    value = getattr(collector, "_value", None)
    if value is not None:
        with contextlib.suppress(AttributeError):
            value.set(0)
        return

    # Unlabeled histograms/summaries have no ``_value`` and aren't Gauges, so the
    # arms above miss them entirely — their ``_sum`` and bucket counters would
    # otherwise leak observations across tests. Re-zero the internal samples.
    hist_sum = getattr(collector, "_sum", None)
    if hist_sum is not None:
        with contextlib.suppress(AttributeError):
            hist_sum.set(0)
    for bucket in getattr(collector, "_buckets", None) or ():
        with contextlib.suppress(AttributeError):
            bucket.set(0)


def list_metric_names() -> list[str]:
    """Return the names of every registered metric — useful for tests."""
    return [metric.name for metric in registry.collect()]


# Re-export commonly-accessed types so ``from caliber.observability.metrics
# import Counter`` works without reaching into ``prometheus_client``.
__all__ = [
    "Counter",
    "Gauge",
    "Histogram",
    "approvals_pending",
    "approvals_total",
    "jobs_in_flight",
    "jobs_total",
    "list_metric_names",
    "observe_stage_duration",
    "promotions_total",
    "record_approval_decision",
    "record_job_terminal",
    "record_promotion",
    "record_rollback",
    "record_verification_outcome",
    "registry",
    "render",
    "reset_metrics_for_test",
    "rollbacks_total",
    "set_approvals_pending",
    "set_jobs_in_flight",
    "set_queue_depth",
    "stage_duration_seconds",
    "verification_items_total",
    "verification_queue_depth",
]
