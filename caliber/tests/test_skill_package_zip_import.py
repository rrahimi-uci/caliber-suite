"""Direct ZIP import, conflict handling, and package safety tests."""

from __future__ import annotations

import io
import zipfile

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException
from starlette.testclient import TestClient

from caliber.db.models import CaliberAuditLog, CaliberSkill, CaliberSkillVersion
from caliber.skill_packages import parse_skill_package_zip

PREFIX = "/ajax-api/2.0/mlflow/caliber"


def _zip(name: str = "portable-skill", body: str = "Follow the portable instructions.") -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{name}/SKILL.md",
            f"---\nname: {name}\ndescription: Portable skill\n---\n\n{body}\n",
        )
        archive.writestr(
            f"{name}/agents/openai.yaml",
            "interface:\n  short_description: Imported from a ZIP\n",
        )
        archive.writestr(f"{name}/references/guide.md", "Reference evidence.\n")
    return stream.getvalue()


def _upload(
    client: TestClient,
    data: bytes,
    *,
    strategy: str = "reject",
    rename_to: str = "",
) -> object:
    return client.post(
        f"{PREFIX}/skills/import-package.zip",
        data={"conflict_strategy": strategy, "rename_to": rename_to},
        files={"file": ("skill.zip", data, "application/zip")},
    )


def test_zip_import_creates_versioned_project_skill_and_audit(
    client: TestClient,
    db_session: Session,
) -> None:
    response = _upload(client, _zip())
    assert response.status_code == 201, response.text
    skill = response.json()["data"]
    assert skill["name"] == "portable-skill"
    assert skill["summary"] == "Imported from a ZIP"
    assert skill["version"] == 1
    assert skill["skill_metadata"]["openai_package"]["resources"] == [
        {"path": "references/guide.md", "content": "Reference evidence.\n"}
    ]
    stored = db_session.get(CaliberSkill, skill["skill_id"])
    assert stored is not None
    assert (
        db_session.execute(
            select(CaliberSkillVersion).where(CaliberSkillVersion.skill_id == skill["skill_id"])
        )
        .scalar_one()
        .version_number
        == 1
    )
    assert (
        db_session.execute(
            select(CaliberAuditLog).where(
                CaliberAuditLog.action == "import_skill_package_zip",
                CaliberAuditLog.entity_id == skill["skill_id"],
            )
        )
        .scalar_one()
        .details["conflict_strategy"]
        == "reject"
    )


def test_zip_conflict_requires_explicit_rename_or_merge(client: TestClient) -> None:
    assert _upload(client, _zip()).status_code == 201
    rejected = _upload(client, _zip(body="replacement"))
    assert rejected.status_code == 409
    assert "rename or merge" in rejected.json()["detail"]

    renamed = _upload(client, _zip(), strategy="rename", rename_to="portable-skill-copy")
    assert renamed.status_code == 201
    assert renamed.json()["data"]["name"] == "portable-skill-copy"


def test_zip_merge_is_forward_versioned(client: TestClient, db_session: Session) -> None:
    created = _upload(client, _zip()).json()["data"]
    merged = _upload(client, _zip(body="Replacement instructions."), strategy="merge")
    assert merged.status_code == 200, merged.text
    assert merged.json()["data"]["skill_id"] == created["skill_id"]
    assert merged.json()["data"]["version"] == 2
    assert merged.json()["data"]["content"] == "Replacement instructions."
    versions = (
        db_session.execute(
            select(CaliberSkillVersion)
            .where(CaliberSkillVersion.skill_id == created["skill_id"])
            .order_by(CaliberSkillVersion.version_number)
        )
        .scalars()
        .all()
    )
    assert [version.version_number for version in versions] == [1, 2]


def test_zip_import_rejects_non_zip_and_unsafe_members(client: TestClient) -> None:
    not_zip = client.post(
        f"{PREFIX}/skills/import-package.zip",
        files={"file": ("skill.txt", b"no", "text/plain")},
    )
    assert not_zip.status_code == 400

    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("../SKILL.md", "unsafe")
    with pytest.raises(HTTPException, match="unsafe package file path"):
        parse_skill_package_zip(stream.getvalue())


def test_zip_import_requires_operator(client: TestClient) -> None:
    response = client.post(
        f"{PREFIX}/skills/import-package.zip",
        files={"file": ("skill.zip", _zip(), "application/zip")},
        headers={"X-CALIBER-User": "viewer-only"},
    )
    assert response.status_code == 403
