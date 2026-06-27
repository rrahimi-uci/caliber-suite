"""MLflow trace-fetch client for the evidence stage.

The evidence stage enriches a refinement job with the actual execution trace
of the flagged interaction (request/response/status/tool-calls), so the
diagnosis stage reasons over what really happened — not just the verifier's
free-text. This is a separate boundary from
:class:`caliber.mlflow_client.MLflowAssessmentClient` (which *polls* feedback
assessments): that one can't fetch a trace's spans.

Two implementations:

* :class:`MLflowTraceClient` — production, calls ``mlflow.get_trace``.
* :class:`FakeTraceClient` — in-memory test double.

Every fetch is best-effort: a missing trace or an unexpected trace shape
returns ``None`` (logged), never raises, so evidence collection degrades to
the row-only summary rather than failing the job.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from caliber.observability.mlflow_tracing import sanitize_trace_value

logger = logging.getLogger("caliber.trace_client")

_PREVIEW_CHARS = 2000

# Cap the redacted span IO payload sent to the FE trace viewer. Big enough for a
# readable preview, small enough that one trace can't bloat the JSON response.
_SPAN_IO_MAX_BYTES = 4096
_NS_PER_MS = 1_000_000


@dataclass(frozen=True)
class TraceSummary:
    """A compact, normalized view of an MLflow trace for diagnosis."""

    status: str = ""
    request_preview: str = ""
    response_preview: str = ""
    span_count: int = 0
    tool_calls: list[str] = field(default_factory=list)
    error: str | None = None


class TraceClient(Protocol):
    """The trace-fetch surface the evidence stage depends on."""

    def get_trace_summary(self, trace_id: str) -> TraceSummary | None: ...


def _truncate(value: Any) -> str:
    text = value if isinstance(value, str) else ("" if value is None else str(value))
    return text[:_PREVIEW_CHARS]


class MLflowTraceClient:
    """Production trace client against the installed MLflow SDK."""

    def get_trace_summary(self, trace_id: str) -> TraceSummary | None:
        if not trace_id:
            return None
        try:
            import mlflow  # noqa: PLC0415

            trace = mlflow.get_trace(trace_id, silent=True)
            if trace is None:
                return None
            data = getattr(trace, "data", None)
            info = getattr(trace, "info", None)
            spans = list(getattr(data, "spans", None) or [])

            tool_calls: list[str] = []
            error: str | None = None
            for span in spans:
                span_type = str(getattr(span, "span_type", "") or "").upper()
                if span_type == "TOOL":
                    name = getattr(span, "name", None)
                    if name:
                        tool_calls.append(str(name))
                span_status = str(getattr(getattr(span, "status", None), "status_code", "") or "")
                if error is None and "ERROR" in span_status.upper():
                    error = str(getattr(span, "name", "span")) + ": error"

            request = getattr(data, "request", None) or getattr(info, "request_preview", "")
            response = getattr(data, "response", None) or getattr(info, "response_preview", "")
            status = str(getattr(info, "status", "") or "")
            return TraceSummary(
                status=status,
                request_preview=_truncate(request),
                response_preview=_truncate(response),
                span_count=len(spans),
                tool_calls=tool_calls,
                error=error,
            )
        except Exception as exc:
            logger.warning("trace fetch failed for %s (%s); skipping trace evidence", trace_id, exc)
            return None


class FakeTraceClient:
    """In-memory trace client for tests."""

    def __init__(self, summaries: dict[str, TraceSummary] | None = None) -> None:
        self._summaries: dict[str, TraceSummary] = dict(summaries or {})

    def add(self, trace_id: str, summary: TraceSummary) -> None:
        self._summaries[trace_id] = summary

    def get_trace_summary(self, trace_id: str) -> TraceSummary | None:
        return self._summaries.get(trace_id)


# ---------------------------------------------------------------------------
# Span-tree view (in-app trace viewer)
# ---------------------------------------------------------------------------
#
# The evidence stage above only needs a *flat summary*; the in-app trace viewer
# needs the full span tree (timing, parent links, redacted IO) so it can render
# the run's spans as an indented, expandable list. ``map_trace_to_spans`` turns
# an MLflow trace object into JSON-able span dicts; ``fetch_trace_spans`` wraps
# ``mlflow.get_trace`` (guarded — MLflow absent / no trace → empty).


@dataclass(frozen=True)
class TraceTree:
    """A JSON-able span tree for one MLflow trace (in-app trace viewer)."""

    trace_id: str | None = None
    spans: list[dict[str, Any]] = field(default_factory=list)
    mlflow_url: str | None = None


def _span_id(span: Any, attr: str) -> str | None:
    value = getattr(span, attr, None)
    if value is None:
        return None
    text = str(value)
    return text or None


def _span_time_ns(span: Any, attr: str) -> int | None:
    value = getattr(span, attr, None)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _span_status(span: Any) -> str:
    status = getattr(span, "status", None)
    code = getattr(status, "status_code", None)
    if code is None:
        code = status
    text = str(getattr(code, "value", code) or "").strip()
    return text or "UNKNOWN"


def _span_io(value: Any) -> Any:
    """Redact + byte-cap a span's inputs/outputs for the FE viewer."""
    if value is None:
        return None
    return sanitize_trace_value(value, max_bytes=_SPAN_IO_MAX_BYTES)


def _span_attributes(span: Any) -> dict[str, Any]:
    raw = getattr(span, "attributes", None)
    if not isinstance(raw, dict) or not raw:
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        # MLflow stores span attributes JSON-encoded; decode opportunistically so
        # the viewer shows real values, then redact + cap. ``caliber.*`` attrs we
        # set are already sanitized at write time; re-sanitizing is idempotent.
        decoded = value
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except (ValueError, TypeError):
                decoded = value
        cleaned[str(key)] = sanitize_trace_value(decoded, max_bytes=_SPAN_IO_MAX_BYTES)
    return cleaned


def map_trace_to_spans(trace: Any) -> list[dict[str, Any]]:
    """Map an MLflow trace object to a flat list of JSON-able span dicts.

    Each span dict carries ``parent_id`` (``None`` for roots) so the FE can build
    the tree; ``start_time_ms`` / ``end_time_ms`` / ``duration_ms`` are derived
    from MLflow's nanosecond timestamps when present; IO + attributes are redacted
    and byte-capped. A malformed trace yields ``[]`` rather than raising.
    """
    if trace is None:
        return []
    data = getattr(trace, "data", None)
    raw_spans = list(getattr(data, "spans", None) or getattr(trace, "spans", None) or [])
    spans: list[dict[str, Any]] = []
    for span in raw_spans:
        start_ns = _span_time_ns(span, "start_time_ns")
        end_ns = _span_time_ns(span, "end_time_ns")
        start_ms = round(start_ns / _NS_PER_MS, 3) if start_ns is not None else None
        end_ms = round(end_ns / _NS_PER_MS, 3) if end_ns is not None else None
        duration_ms: float | None = None
        if start_ns is not None and end_ns is not None and end_ns >= start_ns:
            duration_ms = round((end_ns - start_ns) / _NS_PER_MS, 3)
        span_type = str(getattr(span, "span_type", "") or "").upper() or "UNKNOWN"
        spans.append(
            {
                "span_id": _span_id(span, "span_id"),
                "parent_id": _span_id(span, "parent_id"),
                "name": str(getattr(span, "name", "") or "span"),
                "span_type": span_type,
                "start_time_ms": start_ms,
                "end_time_ms": end_ms,
                "duration_ms": duration_ms,
                "status": _span_status(span),
                "inputs": _span_io(getattr(span, "inputs", None)),
                "outputs": _span_io(getattr(span, "outputs", None)),
                "attributes": _span_attributes(span),
            }
        )
    # Stable order: parents before children where timestamps agree.
    spans.sort(key=lambda s: (s["start_time_ms"] if s["start_time_ms"] is not None else 0.0))
    return spans


def _mlflow_trace_url(mlflow_mod: Any, trace_id: str) -> str | None:
    """Best-effort MLflow UI deep-link for a trace (``None`` when not derivable)."""
    get_uri = getattr(mlflow_mod, "get_tracking_uri", None)
    if not callable(get_uri):
        return None
    try:
        base = str(get_uri() or "").strip()
    except Exception:
        return None
    if not base or not base.lower().startswith(("http://", "https://")):
        return None
    return f"{base.rstrip('/')}/#/traces/{trace_id}"


# MLflow 3 trace metadata keys (mlflow.tracing.constant.TraceMetadataKey). Read
# defensively by literal so we don't hard-depend on the constant's import path.
_SESSION_KEY = "mlflow.trace.session"
_USER_KEY = "mlflow.trace.user"
_TOKEN_USAGE_KEY = "mlflow.trace.tokenUsage"  # noqa: S105 - metadata key, not a secret
_COST_KEY = "mlflow.trace.cost"


def _trace_metadata(info: Any) -> dict[str, Any]:
    metadata = getattr(info, "trace_metadata", None)
    return metadata if isinstance(metadata, dict) else {}


def trace_experiment_id(trace: Any) -> str | None:
    """Experiment id a trace belongs to (``info.trace_location.mlflow_experiment``)."""
    info = getattr(trace, "info", None)
    location = getattr(info, "trace_location", None)
    experiment = getattr(location, "mlflow_experiment", None)
    experiment_id = getattr(experiment, "experiment_id", None)
    return str(experiment_id) if experiment_id is not None else None


def trace_session_id(info: Any) -> str | None:
    return _trace_metadata(info).get(_SESSION_KEY) or None


def trace_user(info: Any) -> str | None:
    return _trace_metadata(info).get(_USER_KEY) or None


def trace_metadata_tokens(info: Any) -> int | None:
    """MLflow-native total token usage from ``mlflow.trace.tokenUsage`` metadata."""
    raw = _trace_metadata(info).get(_TOKEN_USAGE_KEY)
    if not raw:
        return None
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    if isinstance(obj, dict):
        for key in ("total_tokens", "total_token_count", "totalTokens", "total"):
            value = obj.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return int(value)
    return None


def trace_metadata_cost(info: Any) -> float | None:
    """MLflow-native cost from ``mlflow.trace.cost`` metadata (number or dict)."""
    raw = _trace_metadata(info).get(_COST_KEY)
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    try:
        obj = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return None
    if isinstance(obj, (int, float)) and not isinstance(obj, bool):
        return float(obj)
    if isinstance(obj, dict):
        value = obj.get("total_cost", obj.get("total"))
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


@dataclass(frozen=True)
class TraceDetail:
    """Full in-app detail for one MLflow trace (the CALIBER trace viewer).

    Mirrors what MLflow's trace UI shows so users don't have to leave CALIBER:
    span tree + status + request/response + tags + token usage / cost rollup +
    feedback assessments. Every field degrades to an empty/None default when the
    trace is missing or MLflow is unavailable.
    """

    trace_id: str | None = None
    spans: list[dict[str, Any]] = field(default_factory=list)
    mlflow_url: str | None = None
    status: str = ""
    name: str = ""
    experiment_id: str | None = None
    session_id: str | None = None
    user: str | None = None
    request: Any = None
    response: Any = None
    request_time_ms: int | None = None
    execution_time_ms: int | None = None
    total_tokens: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None
    tags: dict[str, str] = field(default_factory=dict)
    assessments: list[dict[str, Any]] = field(default_factory=list)


def _trace_state(info: Any) -> str:
    state = getattr(info, "state", None)
    if state is None:
        return str(getattr(info, "status", "") or "")
    return str(getattr(state, "name", None) or getattr(state, "value", None) or state)


def _trace_tags(info: Any) -> dict[str, str]:
    tags = getattr(info, "tags", None)
    if not isinstance(tags, dict):
        return {}
    return {str(k): str(v) for k, v in tags.items()}


def _num_attr(attributes: dict[str, Any], key: str) -> float | None:
    """Read a numeric span attribute (already decoded by ``map_trace_to_spans``)."""
    value = attributes.get(key)
    if isinstance(value, bool):
        return None
    return value if isinstance(value, (int, float)) else None


@dataclass(frozen=True)
class _Usage:
    total_tokens: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    cost_usd: float | None = None


def _rollup_usage(spans: list[dict[str, Any]]) -> _Usage:
    """Sum CALIBER's per-span token/cost attributes into trace-level totals."""
    totals = {"caliber.tokens": 0.0, "caliber.prompt_tokens": 0.0, "caliber.completion_tokens": 0.0}
    seen = dict.fromkeys(totals, False)
    cost = 0.0
    seen_cost = False
    for span in spans:
        attributes = span.get("attributes") if isinstance(span, dict) else None
        if not isinstance(attributes, dict):
            continue
        for key in totals:
            value = _num_attr(attributes, key)
            if value is not None:
                totals[key] += value
                seen[key] = True
        cost_value = _num_attr(attributes, "caliber.cost_usd")
        if cost_value is not None:
            cost += cost_value
            seen_cost = True
    return _Usage(
        total_tokens=int(totals["caliber.tokens"]) if seen["caliber.tokens"] else None,
        prompt_tokens=int(totals["caliber.prompt_tokens"]) if seen["caliber.prompt_tokens"] else None,
        completion_tokens=(
            int(totals["caliber.completion_tokens"]) if seen["caliber.completion_tokens"] else None
        ),
        cost_usd=round(cost, 6) if seen_cost else None,
    )


def _assessments(trace: Any) -> list[dict[str, Any]]:
    """Extract feedback/expectation assessments attached to the trace (guarded)."""
    info = getattr(trace, "info", None)
    raw = getattr(info, "assessments", None)
    if not raw:
        searcher = getattr(trace, "search_assessments", None)
        if callable(searcher):
            try:
                raw = searcher()
            except Exception:
                raw = None
    out: list[dict[str, Any]] = []
    for assessment in raw or []:
        try:
            value = getattr(assessment, "value", None)
            if value is None:
                feedback = getattr(assessment, "feedback", None)
                value = getattr(feedback, "value", feedback)
            if not isinstance(value, (str, int, float, bool)) and value is not None:
                value = str(value)
            source = getattr(assessment, "source", None)
            source_id = getattr(source, "source_id", None) or getattr(source, "source_type", None)
            out.append(
                {
                    "name": str(getattr(assessment, "name", "") or "assessment"),
                    "value": value,
                    "rationale": str(getattr(assessment, "rationale", "") or "") or None,
                    "source": str(source_id) if source_id else None,
                }
            )
        except Exception:
            logger.debug("skipping malformed assessment", exc_info=True)
            continue
    return out


def fetch_trace_detail(trace_id: str | None) -> TraceDetail:
    """Fetch the full in-app trace detail (guarded; empty on any failure)."""
    if not trace_id:
        return TraceDetail(trace_id=None)
    try:
        import mlflow  # noqa: PLC0415

        trace = mlflow.get_trace(trace_id, silent=True)
    except Exception as exc:
        logger.warning("trace detail fetch failed for %s (%s); empty detail", trace_id, exc)
        return TraceDetail(trace_id=trace_id)
    if trace is None:
        return TraceDetail(trace_id=trace_id)
    try:
        spans = map_trace_to_spans(trace)
        info = getattr(trace, "info", None)
        data = getattr(trace, "data", None)
        tags = _trace_tags(info)
        usage = _rollup_usage(spans)
        request = getattr(data, "request", None) or getattr(info, "request_preview", None)
        response = getattr(data, "response", None) or getattr(info, "response_preview", None)
        mlflow_url: str | None = None
        try:
            mlflow_url = _mlflow_trace_url(mlflow, trace_id)
        except Exception:
            mlflow_url = None
        return TraceDetail(
            trace_id=trace_id,
            spans=spans,
            mlflow_url=mlflow_url,
            status=_trace_state(info),
            name=tags.get("mlflow.traceName", "") or "",
            experiment_id=trace_experiment_id(trace),
            session_id=trace_session_id(info),
            user=trace_user(info),
            request=sanitize_trace_value(request, max_bytes=_SPAN_IO_MAX_BYTES),
            response=sanitize_trace_value(response, max_bytes=_SPAN_IO_MAX_BYTES),
            request_time_ms=_int_or_none(getattr(info, "request_time", None)),
            execution_time_ms=_int_or_none(getattr(info, "execution_duration", None)),
            # Prefer CALIBER's per-span rollup; fall back to MLflow-native trace
            # metadata so usage shows for any traced run (not just CALIBER's).
            total_tokens=usage.total_tokens if usage.total_tokens is not None
            else trace_metadata_tokens(info),
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=usage.cost_usd if usage.cost_usd is not None else trace_metadata_cost(info),
            tags=tags,
            assessments=_assessments(trace),
        )
    except Exception as exc:
        logger.warning("trace detail mapping failed for %s (%s); empty detail", trace_id, exc)
        return TraceDetail(trace_id=trace_id)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def fetch_trace_spans(trace_id: str | None) -> TraceTree:
    """Fetch a trace's full span tree via ``mlflow.get_trace`` (guarded).

    Returns an empty :class:`TraceTree` (``spans=[]``) when ``trace_id`` is falsy,
    MLflow is not installed, the trace is missing, or any MLflow call raises — so
    the route degrades to a friendly empty state (e.g. fake provider / tracing
    off) rather than failing.
    """
    if not trace_id:
        return TraceTree(trace_id=None, spans=[])
    try:
        import mlflow  # noqa: PLC0415

        trace = mlflow.get_trace(trace_id, silent=True)
    except Exception as exc:
        logger.warning("trace span fetch failed for %s (%s); returning empty tree", trace_id, exc)
        return TraceTree(trace_id=trace_id, spans=[])
    if trace is None:
        return TraceTree(trace_id=trace_id, spans=[])
    try:
        spans = map_trace_to_spans(trace)
    except Exception as exc:
        logger.warning("trace span mapping failed for %s (%s); returning empty tree", trace_id, exc)
        return TraceTree(trace_id=trace_id, spans=[])
    mlflow_url: str | None = None
    try:
        mlflow_url = _mlflow_trace_url(mlflow, trace_id)
    except Exception:
        mlflow_url = None
    return TraceTree(trace_id=trace_id, spans=spans, mlflow_url=mlflow_url)
