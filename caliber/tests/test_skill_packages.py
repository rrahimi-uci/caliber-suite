"""OpenAI-compatible package tests for CALIBER skills."""

from __future__ import annotations

import zipfile
from io import BytesIO

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from caliber.db.models import CaliberAuditLog, CaliberSkill
from caliber.routes.skills import IMPORT_PACKAGE_PATH, PACKAGE_PATH, PACKAGE_ZIP_PATH


def _skill_package_url(skill_id: str) -> str:
    return PACKAGE_PATH.replace("{skill_id}", skill_id)


def _skill_package_zip_url(skill_id: str) -> str:
    return PACKAGE_ZIP_PATH.replace("{skill_id}", skill_id)


def _insert_skill(session: Session, **overrides: object) -> CaliberSkill:
    defaults: dict[str, object] = {
        "skill_id": "SK-package",
        "name": "tool-grounding",
        "description": "Ground claims in tool output before answering.",
        "summary": "Use when a claim must be checked with tools.",
        "content": "# Tool Grounding\n\nCall the tool before asserting facts.",
        "owner": "@caliber",
        "category": "workflow_automation",
        "tags": ["tools"],
        "skill_metadata": {
            "openai_package": {
                "resources": [
                    {
                        "path": "references/checklist.md",
                        "content": "# Checklist\n\n- Tool result cited",
                    },
                    {
                        "path": "scripts/normalize.py",
                        "content": "print('ok')\n",
                    },
                ]
            }
        },
        "allowed_tools": None,
        "depends_on": [],
        "status": "active",
        "version": 1,
    }
    defaults.update(overrides)
    skill = CaliberSkill(**defaults)
    session.add(skill)
    session.commit()
    return skill


def _valid_import_files(
    *,
    name: str = "folder-reader",
    description: str = "Read all files in a folder before answering.",
    body: str = "# Folder Reader\n\nInspect files and summarize them.",
) -> list[dict[str, str]]:
    return [
        {
            "path": f"{name}/SKILL.md",
            "content": f"---\nname: {name}\ndescription: {description}\n---\n\n{body}",
        }
    ]


def test_skill_package_preview_includes_openai_files(
    client: TestClient,
    db_session: Session,
) -> None:
    _insert_skill(db_session)

    response = client.get(_skill_package_url("SK-package"))

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["root"] == "tool-grounding"
    assert data["format"] == "openai-skill"
    paths = {file["path"]: file for file in data["files"]}
    assert "tool-grounding/SKILL.md" in paths
    assert "tool-grounding/agents/openai.yaml" in paths
    assert "tool-grounding/references/checklist.md" in paths
    assert "name: tool-grounding" in paths["tool-grounding/SKILL.md"]["content"]
    assert "interface:" in paths["tool-grounding/agents/openai.yaml"]["content"]
    assert "$tool-grounding" in paths["tool-grounding/agents/openai.yaml"]["content"]
    assert data["resource_counts"] == {"scripts": 1, "references": 1, "assets": 0}
    assert data["is_valid"] is True


def test_skill_package_preview_uses_metadata_overrides_and_reports_bad_resources(
    client: TestClient,
    db_session: Session,
) -> None:
    _insert_skill(
        db_session,
        skill_metadata={
            "openai_package": {
                "agents": {
                    "interface": {
                        "display_name": "Grounded Tool Use",
                        "short_description": "A custom package blurb",
                        "default_prompt": "Check every answer with tools.",
                    }
                },
                "policy": {"allow_implicit_invocation": False},
                "resources": [
                    {"path": "tool-grounding/references/rooted.md", "content": "ok"},
                    "not-an-object",
                    {"path": "references/missing-content.md"},
                    {"path": "notes.md", "content": "bad dir"},
                    {"path": "references/rooted.md", "content": "duplicate"},
                ],
            }
        },
    )

    response = client.get(_skill_package_url("SK-package"))

    assert response.status_code == 200
    data = response.json()["data"]
    yaml_file = next(file for file in data["files"] if file["path"].endswith("agents/openai.yaml"))
    assert 'display_name: "Grounded Tool Use"' in yaml_file["content"]
    assert (
        'default_prompt: "Use $tool-grounding. Check every answer with tools."'
        in yaml_file["content"]
    )
    assert "allow_implicit_invocation: false" in yaml_file["content"]
    assert any(file["path"] == "tool-grounding/references/rooted.md" for file in data["files"])
    assert data["is_valid"] is False
    assert len(data["warnings"]) == 4


def test_skill_package_preview_handles_non_list_resources(
    client: TestClient,
    db_session: Session,
) -> None:
    _insert_skill(
        db_session,
        skill_metadata={"openai_package": {"resources": {"path": "references/a.md"}}},
    )

    response = client.get(_skill_package_url("SK-package"))

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["is_valid"] is False
    assert "resources must be a list" in data["warnings"][0]


def test_skill_package_preview_truncates_long_summary(
    client: TestClient,
    db_session: Session,
) -> None:
    _insert_skill(db_session, summary=" ".join(["verylong"] * 20))

    response = client.get(_skill_package_url("SK-package"))

    assert response.status_code == 200
    yaml_file = next(
        file for file in response.json()["data"]["files"] if file["path"].endswith("openai.yaml")
    )
    assert "..." in yaml_file["content"]


def test_skill_package_preview_404s_for_missing_skill(client: TestClient) -> None:
    response = client.get(_skill_package_url("SK-missing"))

    assert response.status_code == 404


def test_skill_package_zip_exports_folder(
    client: TestClient,
    db_session: Session,
) -> None:
    _insert_skill(db_session)

    response = client.get(_skill_package_zip_url("SK-package"))

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert 'filename="tool-grounding.zip"' in response.headers["content-disposition"]
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        names = set(archive.namelist())
        assert "tool-grounding/SKILL.md" in names
        assert "tool-grounding/agents/openai.yaml" in names
        assert "tool-grounding/references/checklist.md" in names
        assert archive.read("tool-grounding/SKILL.md").decode().startswith("---\n")


def test_skill_package_zip_404s_for_missing_skill(client: TestClient) -> None:
    response = client.get(_skill_package_zip_url("SK-missing"))

    assert response.status_code == 404


def test_import_skill_package_creates_registry_row(
    client: TestClient,
    db_session: Session,
) -> None:
    response = client.post(
        IMPORT_PACKAGE_PATH,
        json={
            "owner": "@team",
            "category": "workflow_automation",
            "tags": ["imported"],
            "files": [
                {
                    "path": "folder-reader/SKILL.md",
                    "content": (
                        "---\n"
                        "name: folder-reader\n"
                        "description: Read all files in a folder before answering.\n"
                        "---\n"
                        "\n"
                        "# Folder Reader\n\nInspect files and summarize them."
                    ),
                },
                {
                    "path": "folder-reader/agents/openai.yaml",
                    "content": (
                        "interface:\n"
                        '  display_name: "Folder Reader"\n'
                        '  short_description: "Read folder files for context"\n'
                        '  default_prompt: "Use $folder-reader to inspect this folder."\n'
                    ),
                },
                {
                    "path": "folder-reader/references/limits.md",
                    "content": "Keep summaries short.",
                },
            ],
        },
    )

    assert response.status_code == 201
    data = response.json()["data"]
    assert data["name"] == "folder-reader"
    assert data["summary"] == "Read folder files for context"
    assert data["content"].startswith("# Folder Reader")
    assert data["tags"] == ["imported"]

    skill = db_session.execute(select(CaliberSkill)).scalar_one()
    assert skill.name == "folder-reader"
    assert skill.skill_metadata["openai_package"]["resources"] == [
        {
            "path": "references/limits.md",
            "content": "Keep summaries short.",
        }
    ]

    audit = db_session.execute(select(CaliberAuditLog)).scalar_one()
    assert audit.action == "import_skill_package"


def test_import_skill_package_accepts_rootless_payload(
    client: TestClient,
    db_session: Session,
) -> None:
    response = client.post(
        IMPORT_PACKAGE_PATH,
        json={
            "owner": "@team",
            "files": [
                {
                    "path": "SKILL.md",
                    "content": "---\nname: rootless-skill\ndescription: Rootless import.\n---\nBody",
                }
            ],
        },
    )

    assert response.status_code == 201
    skill = db_session.execute(select(CaliberSkill)).scalar_one()
    assert skill.name == "rootless-skill"


def test_import_skill_package_rejects_path_traversal(client: TestClient) -> None:
    response = client.post(
        IMPORT_PACKAGE_PATH,
        json={
            "owner": "@team",
            "files": [
                {
                    "path": "../bad/SKILL.md",
                    "content": "---\nname: bad\ndescription: Bad.\n---\nBody",
                }
            ],
        },
    )

    assert response.status_code == 400
    assert "unsafe package file path" in response.json()["detail"]


def test_import_skill_package_rejects_empty_normalized_path(client: TestClient) -> None:
    response = client.post(
        IMPORT_PACKAGE_PATH,
        json={"owner": "@team", "files": [{"path": "./", "content": "x"}]},
    )

    assert response.status_code == 400
    assert "unsafe package file path" in response.json()["detail"]


def test_import_skill_package_rejects_folder_name_mismatch(client: TestClient) -> None:
    response = client.post(
        IMPORT_PACKAGE_PATH,
        json={
            "owner": "@team",
            "files": [
                {
                    "path": "wrong-name/SKILL.md",
                    "content": "---\nname: right-name\ndescription: Right.\n---\nBody",
                }
            ],
        },
    )

    assert response.status_code == 400
    assert "folder name must match" in response.json()["detail"]


def test_import_skill_package_rejects_duplicate_paths(client: TestClient) -> None:
    files = _valid_import_files()
    response = client.post(
        IMPORT_PACKAGE_PATH,
        json={"owner": "@team", "files": [files[0], files[0]]},
    )

    assert response.status_code == 400
    assert "duplicate package file path" in response.json()["detail"]


def test_import_skill_package_rejects_multiple_skill_md_files(client: TestClient) -> None:
    response = client.post(
        IMPORT_PACKAGE_PATH,
        json={
            "owner": "@team",
            "files": [
                *_valid_import_files(name="first-skill"),
                *_valid_import_files(name="second-skill"),
            ],
        },
    )

    assert response.status_code == 400
    assert "exactly one SKILL.md" in response.json()["detail"]


def test_import_skill_package_rejects_mixed_roots(client: TestClient) -> None:
    response = client.post(
        IMPORT_PACKAGE_PATH,
        json={
            "owner": "@team",
            "files": [
                *_valid_import_files(name="root-skill"),
                {"path": "other-root/references/a.md", "content": "wrong root"},
            ],
        },
    )

    assert response.status_code == 400
    assert "share one root folder" in response.json()["detail"]


def test_import_skill_package_rejects_extraneous_docs(client: TestClient) -> None:
    response = client.post(
        IMPORT_PACKAGE_PATH,
        json={
            "owner": "@team",
            "files": [
                {
                    "path": "doc-skill/SKILL.md",
                    "content": "---\nname: doc-skill\ndescription: Doc skill.\n---\nBody",
                },
                {"path": "doc-skill/README.md", "content": "extra docs"},
            ],
        },
    )

    assert response.status_code == 400
    assert "README.md" in response.json()["detail"]


def test_import_skill_package_rejects_resource_readme(client: TestClient) -> None:
    response = client.post(
        IMPORT_PACKAGE_PATH,
        json={
            "owner": "@team",
            "files": [
                *_valid_import_files(name="doc-skill"),
                {"path": "doc-skill/references/README.md", "content": "extra docs"},
            ],
        },
    )

    assert response.status_code == 400
    assert "README.md" in response.json()["detail"]


def test_import_skill_package_rejects_resource_outside_allowed_dirs(client: TestClient) -> None:
    response = client.post(
        IMPORT_PACKAGE_PATH,
        json={
            "owner": "@team",
            "files": [
                *_valid_import_files(name="bad-resource"),
                {"path": "bad-resource/docs/guide.md", "content": "wrong dir"},
            ],
        },
    )

    assert response.status_code == 400
    assert "resource files must be under" in response.json()["detail"]


def test_import_skill_package_rejects_missing_frontmatter(client: TestClient) -> None:
    response = client.post(
        IMPORT_PACKAGE_PATH,
        json={
            "owner": "@team",
            "files": [{"path": "bad-skill/SKILL.md", "content": "# No frontmatter"}],
        },
    )

    assert response.status_code == 400
    assert "must start with YAML frontmatter" in response.json()["detail"]


def test_import_skill_package_rejects_invalid_frontmatter_yaml(client: TestClient) -> None:
    response = client.post(
        IMPORT_PACKAGE_PATH,
        json={
            "owner": "@team",
            "files": [{"path": "bad-skill/SKILL.md", "content": "---\nname: [\n---\nBody"}],
        },
    )

    assert response.status_code == 400
    assert "invalid SKILL.md frontmatter" in response.json()["detail"]


def test_import_skill_package_rejects_non_mapping_frontmatter(client: TestClient) -> None:
    response = client.post(
        IMPORT_PACKAGE_PATH,
        json={
            "owner": "@team",
            "files": [{"path": "bad-skill/SKILL.md", "content": "---\n- item\n---\nBody"}],
        },
    )

    assert response.status_code == 400
    assert "frontmatter must be a mapping" in response.json()["detail"]


def test_import_skill_package_rejects_missing_required_frontmatter(client: TestClient) -> None:
    response = client.post(
        IMPORT_PACKAGE_PATH,
        json={
            "owner": "@team",
            "files": [{"path": "bad-skill/SKILL.md", "content": "---\nname: bad-skill\n---\nBody"}],
        },
    )

    assert response.status_code == 400
    assert "requires 'description'" in response.json()["detail"]


def test_import_skill_package_rejects_bad_name_and_empty_body(client: TestClient) -> None:
    bad_name = client.post(
        IMPORT_PACKAGE_PATH,
        json={
            "owner": "@team",
            "files": [
                {
                    "path": "BadSkill/SKILL.md",
                    "content": "---\nname: BadSkill\ndescription: Bad.\n---\nBody",
                }
            ],
        },
    )
    empty_body = client.post(
        IMPORT_PACKAGE_PATH,
        json={
            "owner": "@team",
            "files": [
                {
                    "path": "empty-skill/SKILL.md",
                    "content": "---\nname: empty-skill\ndescription: Empty.\n---\n   ",
                }
            ],
        },
    )

    assert bad_name.status_code == 400
    assert "must be kebab-case" in bad_name.json()["detail"]
    assert empty_body.status_code == 400
    assert "body must not be empty" in empty_body.json()["detail"]


def test_import_skill_package_rejects_invalid_openai_yaml(client: TestClient) -> None:
    response = client.post(
        IMPORT_PACKAGE_PATH,
        json={
            "owner": "@team",
            "files": [
                *_valid_import_files(name="yaml-skill"),
                {"path": "yaml-skill/agents/openai.yaml", "content": "interface: ["},
            ],
        },
    )

    assert response.status_code == 400
    assert "invalid agents/openai.yaml" in response.json()["detail"]


def test_import_skill_package_rejects_non_mapping_openai_yaml(client: TestClient) -> None:
    response = client.post(
        IMPORT_PACKAGE_PATH,
        json={
            "owner": "@team",
            "files": [
                *_valid_import_files(name="yaml-skill"),
                {"path": "yaml-skill/agents/openai.yaml", "content": "- item"},
            ],
        },
    )

    assert response.status_code == 400
    assert "openai.yaml must be a mapping" in response.json()["detail"]


def test_import_skill_package_merges_caller_metadata(
    client: TestClient,
    db_session: Session,
) -> None:
    response = client.post(
        IMPORT_PACKAGE_PATH,
        json={
            "owner": "@team",
            "skill_metadata": {
                "reviewed_by": "@qa",
                "openai_package": {"source": "operator-upload"},
            },
            "files": _valid_import_files(name="merge-skill", description="Imported merge skill."),
        },
    )

    assert response.status_code == 201
    skill = db_session.execute(select(CaliberSkill)).scalar_one()
    assert skill.skill_metadata["reviewed_by"] == "@qa"
    assert skill.skill_metadata["openai_package"]["source"] == "operator-upload"
    assert skill.skill_metadata["openai_package"]["resources"] == []
