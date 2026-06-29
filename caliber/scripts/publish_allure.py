#!/usr/bin/env python3
"""Publish a generated Allure report to object storage (MinIO/S3).

Uploads every file under the report directory to ``s3://<bucket>/<prefix>`` so a
CALIBER deployment can serve it in-app from shared storage — no shared
filesystem between the report builder and the server node. Point the running
CALIBER at the same location with ``CALIBER_ALLURE_REPORT_DIR=s3://<bucket>/<prefix>``.

Env (with sensible local-MinIO defaults):
  CALIBER_ALLURE_REPORT_DIR   target ``s3://bucket/prefix`` (or pass as argv[1]);
                              default ``s3://caliber-suite/allure-report``
  CALIBER_ALLURE_PUBLISH_DIR  local report dir (default caliber/caliber-ui/allure-report)
  CALIBER_OBJECT_STORE_ENDPOINT_URL / CALIBER_ALLURE_PUBLISH_ENDPOINT
                              object-store endpoint (default http://localhost:9000)
  AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY (or MINIO_ROOT_USER / MINIO_ROOT_PASSWORD)
"""

from __future__ import annotations

# This is a CLI tool — stdout is its interface, so print() is intentional.
# ruff: noqa: T201
import contextlib
import mimetypes
import os
import sys
from pathlib import Path

_DEFAULT_TARGET = "s3://caliber-suite/allure-report"
_DEFAULT_DIR = "caliber/caliber-ui/allure-report"
_DEFAULT_ENDPOINT = "http://localhost:9000"


def _target() -> str:
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    configured = os.environ.get("CALIBER_ALLURE_REPORT_DIR", "").strip()
    if configured.startswith("s3://"):
        return configured
    return _DEFAULT_TARGET


def _credentials() -> tuple[str, str]:
    access = (
        os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("MINIO_ROOT_USER") or "minioadmin"
    )
    secret = (
        os.environ.get("AWS_SECRET_ACCESS_KEY")
        or os.environ.get("MINIO_ROOT_PASSWORD")
        or "minioadmin"
    )
    return access, secret


def main() -> int:
    target = _target()
    if not target.startswith("s3://"):
        print(f"error: target must be an s3:// URI, got {target!r}", file=sys.stderr)
        return 2
    bucket, _, prefix = target[len("s3://") :].partition("/")
    prefix = prefix.strip("/")

    report_dir = Path(os.environ.get("CALIBER_ALLURE_PUBLISH_DIR", _DEFAULT_DIR))
    index = report_dir / "index.html"
    if not index.is_file():
        print(
            f"error: no report at {report_dir} (run `make allure-report` first)",
            file=sys.stderr,
        )
        return 1

    endpoint = (
        os.environ.get("CALIBER_ALLURE_PUBLISH_ENDPOINT")
        or os.environ.get("CALIBER_OBJECT_STORE_ENDPOINT_URL")
        or _DEFAULT_ENDPOINT
    )
    access, secret = _credentials()

    import boto3  # noqa: PLC0415
    from botocore.config import Config as BotoConfig  # noqa: PLC0415

    client = boto3.client(
        "s3",
        endpoint_url=endpoint or None,
        region_name=os.environ.get("CALIBER_OBJECT_STORE_REGION", "us-east-1"),
        aws_access_key_id=access,
        aws_secret_access_key=secret,
        config=BotoConfig(s3={"addressing_style": "path"}),
    )

    # Bucket may already exist (owned-by-you) — that's fine.
    with contextlib.suppress(Exception):
        client.create_bucket(Bucket=bucket)

    uploaded = 0
    for path in sorted(report_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(report_dir).as_posix()
        key = f"{prefix}/{rel}" if prefix else rel
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        client.put_object(
            Bucket=bucket,
            Key=key,
            Body=path.read_bytes(),
            ContentType=content_type,
        )
        uploaded += 1

    print(f"Published {uploaded} files to {target} (endpoint {endpoint}).")
    print("Serve it by setting on the CALIBER service:")
    print(f"  CALIBER_ALLURE_REPORT_DIR={target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
