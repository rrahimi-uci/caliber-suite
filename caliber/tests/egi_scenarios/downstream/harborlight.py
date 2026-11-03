"""The pinned Harborlight fixture used to seed the existing D1 downstream.

The full case is kept under ``tests/fixtures/egi`` so later scenario and
installed-fixture tests have one source of truth. The D1 downstream is still
only a document-request system of record: this module validates the source
fixture and projects it to the small seed shape that its existing SQLite
schema accepts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CASE_ID = "MORT-2026-000184"
CASE_VERSION = 1
MISSING_DOCUMENTS = ("pay_statement",)
BOOK_COMMIT = "dacedba44fa995a70ed1f0a02ae2f339a063a69e"
BOOK_LICENSE = "MIT"
BOOK_SOURCE_PATH = "mortgage_case/fixtures/case_000184.json"
BOOK_SOURCE_SHA256 = "b0d08045874f8a0663114df288d26c55242784bb654f525c737aec11e2993fba"

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "egi"
CASE_FIXTURE_PATH = FIXTURE_DIR / "case_000184.json"
SOURCE_PATH = FIXTURE_DIR / "SOURCE.md"


def load_harborlight_case(path: str | Path | None = None) -> dict[str, Any]:
    """Load and validate the pinned synthetic case fixture.

    ``path`` is injectable so an installed distribution can be tested from a
    copied fixture directory without depending on the repository checkout.
    The returned mapping is a fresh JSON decode and is safe for callers to
    inspect or adapt without mutating a module-level object.
    """

    fixture_path = CASE_FIXTURE_PATH if path is None else Path(path)
    try:
        payload = json.loads(fixture_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{fixture_path}: invalid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{fixture_path}: fixture root must be an object")
    if payload.get("case_id") != CASE_ID:
        raise ValueError(f"{fixture_path}: case_id must be {CASE_ID!r}")
    if type(payload.get("case_version")) is not int or payload["case_version"] != CASE_VERSION:
        raise ValueError(f"{fixture_path}: case_version must be {CASE_VERSION}")
    notice = payload.get("synthetic_notice")
    if not isinstance(notice, str) or not notice.startswith("SYNTHETIC TRAINING DATA"):
        raise ValueError(f"{fixture_path}: synthetic notice is missing")
    documents = payload.get("documents")
    if (
        not isinstance(documents, list)
        or not documents
        or not all(
            isinstance(document, dict)
            and all(
                isinstance(document.get(key), str) and document[key]
                for key in ("document_id", "kind")
            )
            for document in documents
        )
    ):
        raise ValueError(f"{fixture_path}: documents must be a non-empty list")
    return payload


def load_downstream_seed(path: str | Path | None = None) -> dict[str, Any]:
    """Project the Harborlight case into the existing D1 seed contract."""

    case = load_harborlight_case(path)
    return {
        "case_id": case["case_id"],
        "version": case["case_version"],
        "missing_documents": list(MISSING_DOCUMENTS),
    }


__all__ = [
    "BOOK_COMMIT",
    "BOOK_LICENSE",
    "BOOK_SOURCE_PATH",
    "BOOK_SOURCE_SHA256",
    "CASE_FIXTURE_PATH",
    "CASE_ID",
    "CASE_VERSION",
    "FIXTURE_DIR",
    "MISSING_DOCUMENTS",
    "SOURCE_PATH",
    "load_downstream_seed",
    "load_harborlight_case",
]
