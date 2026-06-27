"""Tests for the JSON log formatter."""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

import caliber.observability.logging as caliber_logging
from caliber.observability.logging import JsonFormatter, S3LogHandler, configure_logging
from caliber.observability.trace import bind_trace_id


class _FakeMissingBucketError(Exception):
    response = {
        "Error": {"Code": "NoSuchBucket"},
        "ResponseMetadata": {"HTTPStatusCode": 404},
    }


class _FakeS3Client:
    def __init__(self, *, bucket_exists: bool = True) -> None:
        self.bucket_exists = bucket_exists
        self.head_bucket_calls: list[str] = []
        self.create_bucket_calls: list[dict[str, Any]] = []
        self.put_object_calls: list[dict[str, Any]] = []

    def head_bucket(self, *, Bucket: str) -> None:  # noqa: N803 - boto3-compatible kwarg
        self.head_bucket_calls.append(Bucket)
        if not self.bucket_exists:
            raise _FakeMissingBucketError

    def create_bucket(self, **kwargs: Any) -> None:
        self.create_bucket_calls.append(kwargs)
        self.bucket_exists = True

    def put_object(self, **kwargs: Any) -> None:
        self.put_object_calls.append(kwargs)


def _format_record(**kwargs: Any) -> dict[str, Any]:
    """Run a record through the formatter and parse the resulting JSON."""
    formatter = JsonFormatter()
    defaults: dict[str, Any] = {
        "name": "caliber.test",
        "level": logging.INFO,
        "pathname": __file__,
        "lineno": 42,
        "msg": "hello",
        "args": (),
        "exc_info": None,
    }
    defaults.update(kwargs)
    record = logging.LogRecord(**defaults)
    line = formatter.format(record)
    # One-line JSON contract — the rest of the world parses on newline.
    assert "\n" not in line
    return json.loads(line)


def test_format_emits_minimal_fields() -> None:
    payload = _format_record(msg="hello world")
    assert payload["severity"] == "INFO"
    assert payload["logger"] == "caliber.test"
    assert payload["message"] == "hello world"
    # ISO 8601 with millisecond precision, UTC ``Z`` suffix.
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$", payload["t"])


def test_format_renders_args_into_message() -> None:
    payload = _format_record(msg="job %s advanced to %s", args=("RFN-1", "eval"))
    assert payload["message"] == "job RFN-1 advanced to eval"


def test_format_includes_trace_id_when_bound() -> None:
    with bind_trace_id("trace-abc"):
        payload = _format_record(msg="hi")
    assert payload["trace_id"] == "trace-abc"


def test_format_omits_trace_id_when_unbound() -> None:
    payload = _format_record(msg="hi")
    assert "trace_id" not in payload


def test_format_includes_exc_info_class_and_traceback() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        payload = _format_record(msg="failed", level=logging.ERROR, exc_info=sys.exc_info())
    assert payload["severity"] == "ERROR"
    assert payload["error"] == "ValueError"
    assert "ValueError: boom" in payload["stack_trace"]


def test_format_surfaces_extra_keys() -> None:
    formatter = JsonFormatter()
    logger = logging.getLogger("caliber.test.extras")
    # Use a real logger so the ``extra=`` plumbing fires the way the
    # stdlib does in production call sites.
    captured: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record)

    handler = _Capture()
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        logger.info("approved", extra={"approval_id": "AP-1", "agent_id": "agent-x"})
    finally:
        logger.removeHandler(handler)
    assert len(captured) == 1
    payload = json.loads(formatter.format(captured[0]))
    assert payload["approval_id"] == "AP-1"
    assert payload["agent_id"] == "agent-x"


def test_format_coerces_nested_structures_in_extras() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="caliber.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="x",
        args=(),
        exc_info=None,
    )
    record.payload = {"a": [1, 2, {"b": True}]}
    payload = json.loads(formatter.format(record))
    assert payload["payload"] == {"a": [1, 2, {"b": True}]}


def test_format_truncates_cyclic_extras_without_recursing() -> None:
    """A cyclic reference passed via ``extra=`` previously recursed to
    ``RecursionError`` and crashed the formatter. Depth cap replaces
    the offending subtree with ``"<truncated>"``."""
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic
    payload = _format_record(msg="cycle test")
    # _format_record doesn't accept a payload — re-build the record
    # manually to attach the cyclic value to the LogRecord.
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="caliber.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="x",
        args=(),
        exc_info=None,
    )
    record.cyclic = cyclic  # type: ignore[attr-defined]
    line = formatter.format(record)
    assert "\n" not in line
    out = json.loads(line)
    # Somewhere in the nested coerced structure, the cycle stops with
    # the truncation marker — exact depth depends on the cap.
    assert "<truncated>" in line
    # The formatter didn't drop the field entirely.
    assert "cyclic" in out
    _ = payload  # silence unused-var


def test_format_truncates_oversized_extras() -> None:
    """Large flat lists also stop emitting past the item budget so a
    single pathological log entry can't dominate a log stream."""
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="caliber.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="big",
        args=(),
        exc_info=None,
    )
    # 5000 elements — well past the 1000-item budget.
    record.big = list(range(5000))  # type: ignore[attr-defined]
    line = formatter.format(record)
    assert "\n" not in line
    out = json.loads(line)
    # Past the cap, items collapse to the marker rather than the
    # original integer.
    assert "<truncated>" in line
    # The list field is still present (truncated, not dropped).
    assert "big" in out
    assert isinstance(out["big"], list)


def test_s3_log_handler_uploads_jsonl_and_creates_missing_bucket() -> None:
    client = _FakeS3Client(bucket_exists=False)
    handler = S3LogHandler(
        client=client,
        bucket="caliber-log",
        prefix="service",
        flush_lines=1,
        auto_create_bucket=True,
        create_bucket_region="us-east-1",
    )
    handler.setFormatter(JsonFormatter())
    record = logging.LogRecord(
        name="caliber.test.s3",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="bucket %s",
        args=("ready",),
        exc_info=None,
    )

    handler.emit(record)

    assert client.head_bucket_calls == ["caliber-log"]
    assert client.create_bucket_calls == [{"Bucket": "caliber-log"}]
    assert len(client.put_object_calls) == 1
    put = client.put_object_calls[0]
    assert put["Bucket"] == "caliber-log"
    assert str(put["Key"]).startswith("service/")
    assert put["Key"].endswith(".jsonl")
    assert put["ContentType"] == "application/x-ndjson"
    body = put["Body"].decode("utf-8")
    assert body.endswith("\n")
    lines = body.splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["logger"] == "caliber.test.s3"
    assert payload["message"] == "bucket ready"


def test_s3_log_handler_batches_until_flush_line_threshold() -> None:
    client = _FakeS3Client()
    handler = S3LogHandler(
        client=client,
        bucket="caliber-log",
        prefix="",
        flush_lines=2,
        auto_create_bucket=False,
    )
    handler.setFormatter(JsonFormatter())

    for message in ("one", "two"):
        handler.emit(
            logging.LogRecord(
                name="caliber.test.s3",
                level=logging.INFO,
                pathname=__file__,
                lineno=1,
                msg=message,
                args=(),
                exc_info=None,
            )
        )

    assert len(client.put_object_calls) == 1
    lines = client.put_object_calls[0]["Body"].decode("utf-8").splitlines()
    assert [json.loads(line)["message"] for line in lines] == ["one", "two"]


def test_configure_logging_falls_back_to_stderr_when_s3_setup_fails(monkeypatch: Any) -> None:
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level

    def fail_build_s3_client(**_kwargs: Any) -> Any:
        raise RuntimeError("missing optional s3 dependency")

    monkeypatch.setattr(caliber_logging, "_build_s3_client", fail_build_s3_client)
    try:
        configure_logging(level="INFO", log_sink="s3")
        assert root.level == logging.INFO
        managed_handlers = [
            handler for handler in root.handlers if getattr(handler, "_caliber_managed", False)
        ]
        assert len(managed_handlers) == 1
        assert isinstance(managed_handlers[0], logging.StreamHandler)
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in original_handlers:
            root.addHandler(handler)
        root.setLevel(original_level)


def test_configure_logging_preserves_pytest_capture_handlers() -> None:
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level

    class _PytestCaptureHandler(logging.Handler):
        pass

    _PytestCaptureHandler.__module__ = "_pytest.logging"
    preserved = _PytestCaptureHandler()
    root.addHandler(preserved)

    try:
        configure_logging(level="INFO", log_sink="stderr")
        assert root.level == logging.INFO
        assert preserved in root.handlers
        managed_handlers = [
            handler for handler in root.handlers if getattr(handler, "_caliber_managed", False)
        ]
        assert len(managed_handlers) == 1
        assert isinstance(managed_handlers[0], logging.StreamHandler)
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in original_handlers:
            root.addHandler(handler)
        root.setLevel(original_level)
