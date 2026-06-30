"""No-op-safe tracing helpers for CALIBER Assistant operations.

The generic redaction/byte-cap sanitizers now live in
:mod:`caliber.observability.mlflow_tracing` (the shared tracing helper); they are
re-exported here for backward compatibility with existing importers.
"""

from __future__ import annotations

import importlib
import logging
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any

from caliber.observability.mlflow_tracing import (
    sanitize_trace_attributes,
    sanitize_trace_value,
)

logger = logging.getLogger(__name__)

_MISSING = object()

__all__ = [
    "AssistantTraceSpan",
    "AssistantTracer",
    "sanitize_trace_attributes",
    "sanitize_trace_value",
]


@dataclass
class AssistantTraceSpan:
    """Live assistant trace/span metadata exposed to service callers."""

    name: str
    trace_id: str
    correlation_id: str
    mlflow_trace_id: str | None = None
    mlflow_run_id: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    _span: Any | None = field(default=None, repr=False)
    _max_attribute_bytes: int = field(default=4096, repr=False)

    def set_attribute(self, key: str, value: Any) -> None:
        clean = sanitize_trace_value(value, max_bytes=self._max_attribute_bytes)
        self.attributes[str(key)] = clean
        setter = getattr(self._span, "set_attribute", None)
        if not callable(setter):
            return
        try:
            setter(str(key), clean)
        except Exception:
            logger.debug("failed setting assistant span attribute %s", key, exc_info=True)


class _AssistantSpanContext:
    def __init__(
        self,
        tracer: AssistantTracer,
        *,
        name: str,
        trace_id: str,
        correlation_id: str,
        attributes: dict[str, Any] | None,
    ) -> None:
        self._tracer = tracer
        self._name = name
        self._trace_id = trace_id
        self._correlation_id = correlation_id
        self._attributes = attributes or {}
        self._cm: Any = nullcontext(None)
        self._handle = AssistantTraceSpan(
            name=name,
            trace_id=trace_id,
            correlation_id=correlation_id,
            _max_attribute_bytes=tracer.max_attribute_bytes,
        )

    def __enter__(self) -> AssistantTraceSpan:
        base_attrs = {
            "caliber.trace_id": self._trace_id,
            "caliber.correlation_id": self._correlation_id,
            "caliber.assistant.span": self._name,
        }
        attrs = sanitize_trace_attributes(
            {**base_attrs, **self._attributes},
            max_bytes=self._tracer.max_attribute_bytes,
        )
        self._handle.attributes = attrs

        mlflow_mod = self._tracer.mlflow_module()
        if mlflow_mod is None:
            return self._handle

        start_span = getattr(mlflow_mod, "start_span", None)
        if not callable(start_span):
            self._handle.mlflow_run_id = self._tracer.active_run_id(mlflow_mod)
            return self._handle

        try:
            self._cm = start_span(name=self._name, span_type="CHAIN", attributes=attrs)
            span = self._cm.__enter__()
        except Exception:
            logger.debug("unable to open assistant MLflow span %s", self._name, exc_info=True)
            self._cm = nullcontext(None)
            self._handle.mlflow_run_id = self._tracer.active_run_id(mlflow_mod)
            return self._handle

        self._handle._span = span
        self._handle.mlflow_trace_id = self._tracer.extract_mlflow_trace_id(span)
        self._handle.mlflow_run_id = self._tracer.active_run_id(mlflow_mod)
        return self._handle

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        if exc_type is not None:
            self._handle.set_attribute("caliber.status", "failed")
            self._handle.set_attribute(
                "caliber.error_type", getattr(exc_type, "__name__", str(exc_type))
            )
        else:
            self._handle.set_attribute("caliber.status", "completed")
        try:
            return bool(self._cm.__exit__(exc_type, exc, tb))
        except Exception:
            logger.debug("failed closing assistant MLflow span %s", self._name, exc_info=True)
            return False


class AssistantTracer:
    """Small adapter around optional MLflow tracing APIs."""

    def __init__(
        self,
        *,
        mlflow_module: Any = _MISSING,
        max_attribute_bytes: int = 4096,
    ) -> None:
        self._mlflow_module = mlflow_module
        self.max_attribute_bytes = max(256, int(max_attribute_bytes))

    def mlflow_module(self) -> Any | None:
        if self._mlflow_module is not _MISSING:
            return self._mlflow_module
        try:
            self._mlflow_module = importlib.import_module("mlflow")
        except ImportError:
            self._mlflow_module = None
        except Exception:
            logger.debug("unable to import MLflow for assistant tracing", exc_info=True)
            self._mlflow_module = None
        return self._mlflow_module

    def span(
        self,
        name: str,
        *,
        trace_id: str,
        correlation_id: str,
        attributes: dict[str, Any] | None = None,
    ) -> _AssistantSpanContext:
        return _AssistantSpanContext(
            self,
            name=name,
            trace_id=trace_id,
            correlation_id=correlation_id,
            attributes=attributes,
        )

    def annotate_trace(self, *, session_id: str | None = None, user: str | None = None) -> None:
        """Stamp the active trace with MLflow-native session/user metadata so
        multi-turn assistant chats group into a session. Guarded; never raises."""
        metadata: dict[str, str] = {}
        if session_id:
            metadata["mlflow.trace.session"] = str(session_id)
        if user:
            metadata["mlflow.trace.user"] = str(user)
        if not metadata:
            return
        module = self.mlflow_module()
        update = getattr(module, "update_current_trace", None) if module else None
        if not callable(update):
            return
        try:
            update(metadata=metadata)
        except Exception:
            logger.debug("failed annotating assistant trace session/user", exc_info=True)

    def active_run_id(self, mlflow_mod: Any | None = None) -> str | None:
        module = mlflow_mod if mlflow_mod is not None else self.mlflow_module()
        if module is None:
            return None
        active_run = getattr(module, "active_run", None)
        if not callable(active_run):
            return None
        try:
            run = active_run()
        except Exception:
            logger.debug("failed reading active MLflow run for assistant tracing", exc_info=True)
            return None
        info = getattr(run, "info", None)
        run_id = getattr(info, "run_id", None)
        return run_id if isinstance(run_id, str) and run_id else None

    def extract_mlflow_trace_id(self, span: Any | None) -> str | None:
        if span is None:
            return None
        for attr in ("trace_id", "request_id"):
            value = getattr(span, attr, None)
            if isinstance(value, str) and value:
                return value
        context = getattr(span, "context", None)
        if callable(context):
            try:
                context = context()
            except Exception:
                context = None
        for attr in ("trace_id", "request_id"):
            value = getattr(context, attr, None)
            if isinstance(value, str) and value:
                return value
        return None
