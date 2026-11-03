"""CLI-facing helpers for offline evidence archive verification.

The verifier itself lives in ``caliber_sdk.evidence`` so the SDK and CLI use
one server-free implementation.  This module adds only filesystem behavior;
it never extracts an archive and never writes it before verification succeeds.
"""

from __future__ import annotations

from pathlib import Path

from caliber_sdk.evidence import (
    DEFAULT_LIMITS,
    EvidenceVerification,
    EvidenceVerificationError,
    VerificationLimits,
    verify_archive,
)


def verify_file(
    path: str | Path, *, limits: VerificationLimits = DEFAULT_LIMITS
) -> EvidenceVerification:
    """Verify a local archive without exposing its payload in an error."""

    try:
        archive_bytes = Path(path).read_bytes()
    except OSError as exc:
        raise EvidenceVerificationError("archive_read_failed", "archive could not be read") from exc
    return verify_archive(archive_bytes, limits=limits)


def verify_and_write(
    archive_bytes: bytes,
    path: str | Path,
    *,
    overwrite: bool = False,
    limits: VerificationLimits = DEFAULT_LIMITS,
) -> EvidenceVerification:
    """Verify bytes, then write them once; refuse accidental overwrites."""

    result = verify_archive(archive_bytes, limits=limits)
    destination = Path(path)
    try:
        with destination.open("wb" if overwrite else "xb") as stream:
            stream.write(archive_bytes)
    except OSError as exc:
        raise EvidenceVerificationError(
            "archive_write_failed" if overwrite else "archive_exists",
            "verified archive could not be written"
            if overwrite
            else "output archive already exists",
        ) from exc
    return result


__all__ = ["verify_and_write", "verify_file"]
