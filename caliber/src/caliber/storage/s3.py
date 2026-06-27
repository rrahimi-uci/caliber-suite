"""S3 / MinIO :class:`StorageBackend` (storage doc §3.5, Phase 2).

boto3 is imported lazily and only when ``backend == "s3"`` so the core package
(and the local-backend MVP) never depends on it. Credentials are resolved from
*sources* via :func:`caliber.secrets.resolve_secret` (``env://VAR`` /
``file:///path`` / bare env-var), never literal keys in config.

Signed URLs for browser upload/download are generated against the
``public_endpoint_url`` when configured, never the internal address the server
uses for object I/O (SSRF / internal-endpoint-leak defense, storage doc §8.3).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, BinaryIO, Literal

from caliber.secrets import resolve_secret
from caliber.storage.base import (
    FileObjectMetadata,
    SignedUrl,
    StorageBackendName,
    StorageConflictError,
    StorageError,
    StorageNotFoundError,
    StorageObjectRef,
    StorageValidationError,
    safe_relative_path,
)

if TYPE_CHECKING:
    from caliber.config import WorkflowStorageConfig


def _client(endpoint: str | None, config: WorkflowStorageConfig) -> Any:
    import boto3  # noqa: PLC0415
    from botocore.config import Config  # noqa: PLC0415

    access_key = resolve_secret(config.access_key_source) if config.access_key_source else None
    secret_key = resolve_secret(config.secret_key_source) if config.secret_key_source else None
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=config.region,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(
            s3={"addressing_style": "path" if config.force_path_style else "auto"},
            retries={"max_attempts": 3, "mode": "standard"},
        ),
    )


class S3StorageBackend:
    """Object store backed by S3 or any S3-compatible service (MinIO)."""

    name: StorageBackendName = "s3"

    def __init__(self, config: WorkflowStorageConfig) -> None:
        if not config.bucket:
            raise StorageValidationError("s3 backend requires CALIBER_WORKFLOW_STORAGE_BUCKET")
        self._config = config
        self._bucket = config.bucket
        self._client = _client(config.internal_endpoint_url, config)
        # A separate client bound to the public endpoint for browser-facing URLs.
        public_endpoint = config.public_endpoint_url or config.internal_endpoint_url
        self._public_client = (
            _client(public_endpoint, config)
            if public_endpoint != config.internal_endpoint_url
            else self._client
        )

    # ----- helpers --------------------------------------------------------- #
    def _key(self, key: str) -> str:
        safe = safe_relative_path(key)  # rejects traversal/absolute
        prefix = self._config.prefix.strip("/")
        return f"{prefix}/{safe}" if prefix else safe

    def _meta(self, key: str, head: dict[str, Any], *, sha256: str | None) -> FileObjectMetadata:
        ts = head.get("LastModified") or datetime.now(timezone.utc)
        return FileObjectMetadata(
            ref=StorageObjectRef(
                backend="s3",
                bucket=self._bucket,
                key=self._key(key),
                uri=f"s3://{self._bucket}/{self._key(key)}",
            ),
            name=key.rsplit("/", 1)[-1],
            kind="work",
            size_bytes=int(head.get("ContentLength", 0)),
            media_type=head.get("ContentType"),
            sha256=sha256 or head.get("Metadata", {}).get("sha256"),
            etag=(head.get("ETag") or "").strip('"') or None,
            object_version_id=head.get("VersionId"),
            created_at=ts,
            updated_at=ts,
        )

    def _err(self, exc: Exception, key: str) -> StorageError:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
        if code in {"NoSuchKey", "404", "NoSuchBucket"}:
            return StorageNotFoundError(f"object not found: {key!r}")
        return StorageError(f"s3 error for {key!r}: {exc}")

    # ----- writes ---------------------------------------------------------- #
    def write_bytes(
        self,
        key: str,
        data: bytes,
        *,
        media_type: str | None = None,
        metadata: dict[str, str] | None = None,
        overwrite: bool = False,
    ) -> FileObjectMetadata:
        full = self._key(key)
        if not overwrite and self.exists(key):
            raise StorageConflictError(f"object already exists: {key!r}")
        digest = hashlib.sha256(data).hexdigest()
        meta = {"sha256": digest, **(metadata or {})}
        extra: dict[str, Any] = {"Metadata": meta}
        if media_type:
            extra["ContentType"] = media_type
        try:
            self._client.put_object(Bucket=self._bucket, Key=full, Body=data, **extra)
            head = self._client.head_object(Bucket=self._bucket, Key=full)
        except Exception as exc:
            raise self._err(exc, key) from exc
        return self._meta(key, head, sha256=digest)

    def write_stream(
        self,
        key: str,
        stream: BinaryIO,
        *,
        media_type: str | None = None,
        metadata: dict[str, str] | None = None,
        overwrite: bool = False,
    ) -> FileObjectMetadata:
        return self.write_bytes(
            key, stream.read(), media_type=media_type, metadata=metadata, overwrite=overwrite
        )

    # ----- reads ----------------------------------------------------------- #
    def read_bytes(self, key: str, *, byte_range: tuple[int, int] | None = None) -> bytes:
        kwargs: dict[str, Any] = {"Bucket": self._bucket, "Key": self._key(key)}
        if byte_range is not None:
            start, end = byte_range
            kwargs["Range"] = f"bytes={start}-{end}"
        try:
            resp = self._client.get_object(**kwargs)
            return resp["Body"].read()  # type: ignore[no-any-return]
        except Exception as exc:
            raise self._err(exc, key) from exc

    def open_read(self, key: str) -> BinaryIO:
        try:
            resp = self._client.get_object(Bucket=self._bucket, Key=self._key(key))
            return resp["Body"]  # type: ignore[no-any-return]
        except Exception as exc:
            raise self._err(exc, key) from exc

    def exists(self, key: str) -> bool:
        try:
            self._client.head_object(Bucket=self._bucket, Key=self._key(key))
            return True
        except Exception:
            return False

    def stat(self, key: str) -> FileObjectMetadata:
        try:
            head = self._client.head_object(Bucket=self._bucket, Key=self._key(key))
        except Exception as exc:
            raise self._err(exc, key) from exc
        return self._meta(key, head, sha256=None)

    def list(
        self,
        prefix: str,
        *,
        recursive: bool = True,
        limit: int = 1000,
        cursor: str | None = None,
    ) -> tuple[list[FileObjectMetadata], str | None]:
        kwargs: dict[str, Any] = {
            "Bucket": self._bucket,
            "Prefix": self._key(prefix),
            "MaxKeys": limit,
        }
        if cursor:
            kwargs["ContinuationToken"] = cursor
        try:
            resp = self._client.list_objects_v2(**kwargs)
        except Exception as exc:
            raise self._err(exc, prefix) from exc
        base = self._config.prefix.strip("/")
        items: list[FileObjectMetadata] = []
        for obj in resp.get("Contents", []):
            full = obj["Key"]
            rel = full[len(base) + 1 :] if base and full.startswith(base + "/") else full
            items.append(
                FileObjectMetadata(
                    ref=StorageObjectRef(
                        backend="s3",
                        bucket=self._bucket,
                        key=full,
                        uri=f"s3://{self._bucket}/{full}",
                    ),
                    name=rel.rsplit("/", 1)[-1],
                    kind="work",
                    size_bytes=int(obj.get("Size", 0)),
                    media_type=None,
                    sha256=None,
                    etag=(obj.get("ETag") or "").strip('"') or None,
                    object_version_id=None,
                    created_at=obj.get("LastModified") or datetime.now(timezone.utc),
                    updated_at=obj.get("LastModified") or datetime.now(timezone.utc),
                )
            )
        next_cursor = resp.get("NextContinuationToken") if resp.get("IsTruncated") else None
        return items, next_cursor

    # ----- mutations ------------------------------------------------------- #
    def delete(self, key: str, *, hard: bool = False) -> None:
        try:
            self._client.delete_object(Bucket=self._bucket, Key=self._key(key))
        except Exception as exc:
            raise self._err(exc, key) from exc

    def copy(self, src_key: str, dst_key: str, *, overwrite: bool = False) -> FileObjectMetadata:
        if not overwrite and self.exists(dst_key):
            raise StorageConflictError(f"object already exists: {dst_key!r}")
        try:
            self._client.copy_object(
                Bucket=self._bucket,
                Key=self._key(dst_key),
                CopySource={"Bucket": self._bucket, "Key": self._key(src_key)},
            )
        except Exception as exc:
            raise self._err(exc, src_key) from exc
        return self.stat(dst_key)

    def move(self, src_key: str, dst_key: str, *, overwrite: bool = False) -> FileObjectMetadata:
        if self._key(src_key) == self._key(dst_key):
            # Moving an object onto itself is a no-op — copy-then-delete would
            # delete the only copy and lose the data.
            return self.stat(src_key)
        meta = self.copy(src_key, dst_key, overwrite=overwrite)
        self.delete(src_key)
        return meta

    def signed_url(
        self,
        key: str,
        *,
        method: Literal["GET", "PUT"] = "GET",
        expires_seconds: int = 900,
        media_type: str | None = None,
    ) -> SignedUrl:
        capped = min(expires_seconds, self._config.signed_url_max_ttl_seconds)
        client_method = "get_object" if method == "GET" else "put_object"
        params: dict[str, Any] = {"Bucket": self._bucket, "Key": self._key(key)}
        if method == "PUT" and media_type:
            params["ContentType"] = media_type
        try:
            url = self._public_client.generate_presigned_url(
                client_method, Params=params, ExpiresIn=capped
            )
        except Exception as exc:
            raise self._err(exc, key) from exc
        return SignedUrl(
            url=url,
            method=method,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=capped),
            headers={"Content-Type": media_type} if (method == "PUT" and media_type) else {},
        )
