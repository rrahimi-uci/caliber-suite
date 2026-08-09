"""Formalizing a payload must not delete fields from it.

Pydantic drops undeclared keys on serialization. So converting a handler from
``envelope_response_dict(payload)`` to ``envelope_response(Schema(...))`` will
silently remove any field the schema forgot -- the response still looks
well-formed, and only a client that needed the missing key notices.

That is not hypothetical: the first version of ``ProjectFileSchema`` dropped
``project_id`` and the first ``ProjectFolderSchema`` dropped ``file_ref``, both
from live responses, and both were caught only because an existing route test
happened to assert on them.

M0-PR2 converts roughly fifty such handlers. These tests make the failure mode
structural rather than a matter of whether a route test happened to cover the
field: each schema is validated against the real payload shape its producer
emits, and any key lost in the round trip fails.
"""

from __future__ import annotations

from typing import Any

import pytest

from caliber.schemas import (
    CalibrationJobSchema,
    CalibrationResolutionSchema,
    PersonalAccessTokenSchema,
    ProjectFileSchema,
    ProjectFolderSchema,
    ProjectSchema,
    ProjectStorageSchema,
)

#: Payloads as their producers actually emit them.
#:
#: ``CaliberFileRecord.to_api`` plus the ``project_id`` the project routes
#: attach afterwards; ``_folder_payload``; ``_project_to_schema``'s inputs; and
#: the storage-config dict for an object-store backend, which is the branch
#: carrying the most optional fields.
_FILE_PAYLOAD: dict[str, Any] = {
    "file_id": "WF-file-1",
    "file_ref": "caliber://projects/PRJ-1/input/a/b.md",
    "name": "b.md",
    "kind": "input",
    "relative_path": "a/b.md",
    "media_type": "text/markdown",
    "size_bytes": 17,
    "sha256": "a" * 64,
    "etag": "etag-1",
    "object_version_id": "ov-1",
    "version": 1,
    "status": "active",
    "storage_backend": "local",
    "producer_node_id": None,
    "created_at": "2026-08-09T00:00:00",
    "metadata": {"source": "upload"},
    "project_id": "PRJ-1",
    "immutable_ref": {
        "file_id": "WF-file-1",
        "file_ref": "caliber://projects/PRJ-1/input/a/b.md",
        "sha256": "a" * 64,
        "name": "b.md",
        "size_bytes": 17,
        "media_type": "text/markdown",
        "object_version_id": "ov-1",
    },
}

_FOLDER_PAYLOAD: dict[str, Any] = {
    "path": "datasets/raw",
    "name": "raw",
    "file_ref": "caliber://projects/PRJ-1/metadata/datasets/raw/.caliber-folder",
    "storage_backend": "local",
    "created_at": "2026-08-09T00:00:00",
}

_PROJECT_PAYLOAD: dict[str, Any] = {
    "project_id": "PRJ-1",
    "name": "Demo",
    "description": "a project",
    "owner": "@alice",
    "status": "active",
    "storage_backend": "local",
    "created_at": "2026-08-09T00:00:00",
    "updated_at": "2026-08-09T00:00:00",
    "file_count": 3,
}

_STORAGE_PAYLOAD: dict[str, Any] = {
    "backend": "s3",
    "backend_label": "S3 / MinIO",
    "available_backends": [{"value": "local", "label": "Local"}],
    "base_uri": "s3://bucket/prefix",
    "bucket": "bucket",
    "prefix": "prefix",
    "public_endpoint_url": "https://minio.example",
}


#: Shapes added in tranche 5. ``CalibrationResolutionSchema`` is here because
#: the first attempt reused ``CalibrationJobSchema`` for it, which dropped
#: ``retry_job_id`` -- the only field carrying the retry lineage.
_CALIBRATION_JOB_PAYLOAD: dict[str, Any] = {
    "job_id": "CAL-1",
    "tool_id": "TOOL-1",
    "status": "succeeded",
    "requested_by": "@alice",
    "result": {"pass_rate": 1.0},
    "error": None,
    "created_at": "2026-08-09T00:00:00",
    "claimed_at": "2026-08-09T00:00:01",
    "claimed_by": "worker-1",
    "finished_at": "2026-08-09T00:00:02",
    "retry_of_job_id": None,
    "resolution": None,
    "resolution_reason": None,
    "resolved_by": None,
    "resolved_at": None,
}

_CALIBRATION_RESOLUTION_PAYLOAD: dict[str, Any] = {
    "job_id": "CAL-1",
    "status": "resolved",
    "resolution": "retry",
    "retry_job_id": "CAL-2",
}

_TOKEN_PAYLOAD: dict[str, Any] = {
    "token_id": "PAT-1",
    "user_id": "@alice",
    "name": "ci",
    "scopes": ["caliber.operator"],
    "created_at": "2026-08-09T00:00:00",
    "created_by": "@alice",
    "expires_at": None,
    "last_used_at": None,
    "revoked_at": None,
    "revoked_reason": None,
    "rotated_from": None,
    "active": True,
}


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (ProjectFileSchema, _FILE_PAYLOAD),
        (ProjectFolderSchema, _FOLDER_PAYLOAD),
        (ProjectSchema, _PROJECT_PAYLOAD),
        (ProjectStorageSchema, _STORAGE_PAYLOAD),
        (CalibrationJobSchema, _CALIBRATION_JOB_PAYLOAD),
        (CalibrationResolutionSchema, _CALIBRATION_RESOLUTION_PAYLOAD),
        (PersonalAccessTokenSchema, _TOKEN_PAYLOAD),
    ],
    ids=[
        "file",
        "folder",
        "project",
        "storage",
        "calibration-job",
        "calibration-resolution",
        "token",
    ],
)
def test_no_field_is_lost_in_the_round_trip(schema: type, payload: dict[str, Any]) -> None:
    """Every key the producer emits survives validate -> dump."""
    dumped = schema.model_validate(payload).model_dump(mode="json", by_alias=True)
    missing = sorted(set(payload) - set(dumped))
    assert not missing, (
        f"{schema.__name__} drops {missing} from the response. Declare the field, "
        "or the endpoint silently stops returning it."
    )


def test_nested_objects_are_preserved_too() -> None:
    """A nested dict is the easiest place to lose fields unnoticed.

    ``immutable_ref`` is modelled, so a missing sub-field would be dropped
    without the parent looking any different.
    """
    dumped = ProjectFileSchema.model_validate(_FILE_PAYLOAD).model_dump(mode="json", by_alias=True)
    expected = _FILE_PAYLOAD["immutable_ref"]
    assert dumped["immutable_ref"] is not None
    missing = sorted(set(expected) - set(dumped["immutable_ref"]))
    assert not missing, f"ProjectFileSchema.immutable_ref drops {missing}"


def test_a_listed_token_never_carries_a_secret() -> None:
    """The inverse hazard: a schema inventing a key rather than dropping one.

    Modelling the token surface with a single optional ``token`` field put
    ``"token": null`` into every list response -- announcing a secret in the
    one payload that must never mention one.
    """
    dumped = PersonalAccessTokenSchema.model_validate(_TOKEN_PAYLOAD).model_dump(mode="json")
    assert "token" not in dumped


def test_the_metadata_alias_keeps_its_wire_name() -> None:
    """``metadata`` collides with model internals, so it is aliased.

    The alias must not change the wire contract: clients read ``metadata``.
    """
    dumped = ProjectFileSchema.model_validate(_FILE_PAYLOAD).model_dump(mode="json", by_alias=True)
    assert dumped["metadata"] == {"source": "upload"}
    assert "file_metadata" not in dumped
