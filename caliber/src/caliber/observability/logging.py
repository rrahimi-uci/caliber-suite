"""Single-line JSON log formatter and sink wiring.

CALIBER's logs go through stdlib ``logging``; this module supplies a
:class:`JsonFormatter` that turns each :class:`logging.LogRecord` into a
single JSON object on one line, plus a :func:`configure_logging` helper
that wires it into the root logger.

Schema:

* ``t``         — ISO-8601 timestamp with millisecond precision (UTC).
* ``severity``  — Standard severity (``INFO``, ``WARNING``, …).
* ``logger``    — The logger name (e.g. ``caliber.routes.approvals``).
* ``message``   — The rendered log message.
* ``trace_id``  — Optional. Populated from
  :func:`caliber.observability.trace.current_trace_id` when bound.
* ``error``     — Optional. Exception class name when ``exc_info`` is
  attached.
* ``stack_trace`` — Optional. Formatted traceback when ``exc_info`` is
  attached.

Single-line JSON is what most log aggregators expect (Loki, Datadog,
Cloud Logging); the parseability invariant is enforced by the
``logs are JSON-parseable`` test in ``tests/test_observability_logging.py``.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import uuid
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Final, cast

from caliber.observability.trace import current_trace_id
from caliber.secrets import resolve_secret

# Fields the stdlib ``LogRecord`` adds that we never want to surface in
# the JSON output — they're either redundant or noisy ("threadName"
# rarely helps debug an async ASGI app).
# Caps on the recursive log-extra coercion. Without these, a cyclic
# reference (or a pathologically deep nested dict) passed via
# ``logger.info("...", extra={"obj": cyclic})`` would recurse to
# ``RecursionError`` and take down the formatter. ``MAX_DEPTH`` is
# generous enough for realistic log payloads (job → diagnosis →
# evidence_summary → details); ``MAX_ITEMS`` bounds the total node
# count so a giant flat list also can't dominate the entry.
_MAX_COERCE_DEPTH: Final[int] = 10
_MAX_COERCE_ITEMS: Final[int] = 1000
_TRUNCATED_MARKER: Final[str] = "<truncated>"
_LOG_CONTENT_TYPE: Final[str] = "application/x-ndjson"
_HTTP_NOT_FOUND: Final[int] = 404
_PYTEST_LOGGING_MODULE_PREFIX: Final[str] = "_pytest.logging"


_RESERVED_RECORD_FIELDS: Final[frozenset[str]] = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class JsonFormatter(logging.Formatter):
    """Render :class:`logging.LogRecord` instances as single-line JSON.

    Custom fields attached via the standard ``extra=`` kwarg to logger
    calls flow through to the JSON object — any key not in
    :data:`_RESERVED_RECORD_FIELDS` is included. This is what enables
    structured fields like ``extra={"job_id": "RFN-1"}`` to land in the
    record under their own key rather than being lost in the message.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "t": _iso_timestamp(record.created),
            "severity": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        trace_id = current_trace_id()
        if trace_id:
            payload["trace_id"] = trace_id

        if record.exc_info:
            exc_type = record.exc_info[0]
            if exc_type is not None:
                payload["error"] = exc_type.__name__
            payload["stack_trace"] = self.formatException(record.exc_info)

        # Surface any ``extra=...`` fields that aren't part of the
        # stdlib LogRecord. Skip private attributes so a debug import
        # doesn't leak its internals.
        budget = [_MAX_COERCE_ITEMS]  # mutable cell shared across coercions
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_FIELDS or key.startswith("_"):
                continue
            if key in payload:
                continue
            payload[key] = _coerce_jsonable(value, depth=0, budget=budget)

        return json.dumps(payload, separators=(",", ":"), default=str)


class S3LogHandler(logging.Handler):
    """Write formatted log records to S3 / MinIO as JSONL objects.

    S3-compatible stores do not support appending to an existing object, so this
    handler batches formatted lines and writes a new object for each flush. The
    standard stderr handler remains installed too; if bucket delivery fails, the
    application still has local process logs.
    """

    def __init__(
        self,
        *,
        client: Any,
        bucket: str,
        prefix: str = "",
        flush_lines: int = 1,
        auto_create_bucket: bool = True,
        create_bucket_region: str | None = None,
    ) -> None:
        super().__init__()
        if not bucket:
            raise ValueError("S3LogHandler requires a non-empty bucket")
        if flush_lines < 1:
            raise ValueError("S3LogHandler flush_lines must be at least 1")
        self._client = client
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._flush_lines = flush_lines
        self._auto_create_bucket = auto_create_bucket
        self._create_bucket_region = create_bucket_region
        self._bucket_ready = False
        self._buffer: list[str] = []
        self._sequence = 0
        self._session_id = uuid.uuid4().hex
        self._hostname = socket.gethostname().split(".", 1)[0] or "host"

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buffer.append(self.format(record))
            if len(self._buffer) >= self._flush_lines:
                self.flush()
        except Exception:
            # Logging must never break request handling or app startup.
            self.handleError(record)

    def flush(self) -> None:
        self.acquire()
        try:
            if not self._buffer:
                return
            lines = self._buffer
            self._buffer = []
            try:
                self._ensure_bucket()
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=self._next_key(),
                    Body=("\n".join(lines) + "\n").encode("utf-8"),
                    ContentType=_LOG_CONTENT_TYPE,
                    Metadata={"caliber-log-session": self._session_id},
                )
            except Exception:
                # Keep stderr as the durable fallback and avoid unbounded memory growth
                # if the object store is unavailable for an extended period.
                return
        finally:
            self.release()

    def close(self) -> None:
        try:
            self.flush()
        finally:
            super().close()

    def _ensure_bucket(self) -> None:
        if self._bucket_ready:
            return
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except Exception as exc:
            if not self._auto_create_bucket or not _is_missing_bucket_error(exc):
                raise
            kwargs: dict[str, Any] = {"Bucket": self._bucket}
            if self._create_bucket_region and self._create_bucket_region != "us-east-1":
                kwargs["CreateBucketConfiguration"] = {
                    "LocationConstraint": self._create_bucket_region
                }
            self._client.create_bucket(**kwargs)
        self._bucket_ready = True

    def _next_key(self) -> str:
        now = datetime.now(timezone.utc)
        self._sequence += 1
        dated_prefix = now.strftime("%Y/%m/%d")
        filename = (
            f"{now:%H%M%S}-{self._hostname}-{os.getpid()}-"
            f"{self._session_id}-{self._sequence:06d}.jsonl"
        )
        key = f"{dated_prefix}/{filename}"
        return f"{self._prefix}/{key}" if self._prefix else key


def _iso_timestamp(epoch_seconds: float) -> str:
    """Render epoch seconds as ``2026-05-15T12:34:56.789Z``.

    UTC, millisecond precision, ``Z`` suffix (the convention every log
    aggregator I've used understands). Avoids the ``+00:00`` form
    :meth:`datetime.isoformat` produces.
    """
    dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _coerce_jsonable(value: Any, *, depth: int, budget: list[int]) -> Any:
    """Best-effort conversion of arbitrary log-extra values to JSON.

    Most values pass through unchanged. Objects with ``__dict__`` or
    custom ``__repr__`` fall back to ``str()``. Rendering nested
    structures here keeps ``dict``/``list`` shape intact rather than
    collapsing them to strings.

    Guards against pathological inputs:

    * **Recursion depth** capped at :data:`_MAX_COERCE_DEPTH` so a
      cyclic reference passed via ``extra=`` can't recurse to
      ``RecursionError`` and crash the formatter.
    * **Total node count** capped at :data:`_MAX_COERCE_ITEMS`
      (tracked in the ``budget`` cell threaded through the
      recursion) so a giant flat list also can't dominate the
      entry.

    Past either cap, the offending subtree is replaced with the
    string :data:`_TRUNCATED_MARKER`.
    """
    if depth >= _MAX_COERCE_DEPTH:
        return _TRUNCATED_MARKER
    if budget[0] <= 0:
        return _TRUNCATED_MARKER
    budget[0] -= 1
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, list | tuple):
        return [_coerce_jsonable(v, depth=depth + 1, budget=budget) for v in value]
    if isinstance(value, dict):
        return {
            str(k): _coerce_jsonable(v, depth=depth + 1, budget=budget) for k, v in value.items()
        }
    return str(value)


def configure_logging(
    level: str = "INFO",
    *,
    log_sink: str = "s3",
    s3_bucket: str = "caliber-log",
    s3_prefix: str = "service",
    s3_endpoint_url: str | None = None,
    s3_region: str | None = None,
    s3_force_path_style: bool = False,
    s3_access_key_source: str | None = None,
    s3_secret_key_source: str | None = None,
    s3_auto_create_bucket: bool = True,
    s3_flush_lines: int = 1,
    s3_client: Any | None = None,
) -> None:
    """Replace the stdlib root handler with one that emits JSON.

    Idempotent — re-configuring at startup (or in tests that build
    multiple apps) doesn't accumulate handlers.
    """
    root = logging.getLogger()
    # Drop any handlers a prior call installed so re-configuration
    # doesn't double-log. Preserve pytest's capture handlers so tests
    # using ``caplog`` keep observing warnings emitted after app startup
    # reconfigures logging.
    for existing in list(root.handlers):
        if _is_pytest_logging_handler(existing):
            continue
        with suppress(Exception):
            existing.flush()
        root.removeHandler(existing)
    formatter = JsonFormatter()
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    cast(Any, stream_handler)._caliber_managed = True
    root.addHandler(stream_handler)
    root.setLevel(level)
    if log_sink == "s3":
        try:
            s3_handler = S3LogHandler(
                client=s3_client
                or _build_s3_client(
                    endpoint_url=s3_endpoint_url,
                    region=s3_region,
                    force_path_style=s3_force_path_style,
                    access_key_source=s3_access_key_source,
                    secret_key_source=s3_secret_key_source,
                ),
                bucket=s3_bucket,
                prefix=s3_prefix,
                flush_lines=s3_flush_lines,
                auto_create_bucket=s3_auto_create_bucket,
                create_bucket_region=s3_region,
            )
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "S3 log sink unavailable; continuing with stderr only: %s",
                exc,
            )
        else:
            s3_handler.setFormatter(formatter)
            cast(Any, s3_handler)._caliber_managed = True
            root.addHandler(s3_handler)


def _is_pytest_logging_handler(handler: logging.Handler) -> bool:
    return handler.__class__.__module__.startswith(_PYTEST_LOGGING_MODULE_PREFIX)


def _build_s3_client(
    *,
    endpoint_url: str | None,
    region: str | None,
    force_path_style: bool,
    access_key_source: str | None,
    secret_key_source: str | None,
) -> Any:
    try:
        import boto3  # noqa: PLC0415
        from botocore.config import Config  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "CALIBER_LOG_SINK=s3 requires installing the S3 extra "
            '(for example: pip install -e ".[s3]").'
        ) from exc

    access_key = resolve_secret(access_key_source) if access_key_source else None
    secret_key = resolve_secret(secret_key_source) if secret_key_source else None
    return boto3.client(
        "s3",
        endpoint_url=endpoint_url or None,
        region_name=region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(
            s3={"addressing_style": "path" if force_path_style else "auto"},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


def _is_missing_bucket_error(exc: Exception) -> bool:
    response = getattr(exc, "response", {})
    error = response.get("Error", {}) if isinstance(response, dict) else {}
    metadata = response.get("ResponseMetadata", {}) if isinstance(response, dict) else {}
    code = str(error.get("Code", ""))
    status = metadata.get("HTTPStatusCode")
    return code in {"404", "NoSuchBucket", "NotFound"} or status == _HTTP_NOT_FOUND
