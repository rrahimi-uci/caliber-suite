"""Logic tests for ``caliber.skill_packages``.

This exercises the OpenAI-compatible skill-package helpers directly —
``build_skill_package`` / ``build_skill_package_zip`` (export),
``parse_skill_package`` (import + validation), and
``merge_openai_package_metadata``. These functions are pure (they read a
detached ``CaliberSkill`` and plain payloads), so no DB session is needed.

NOTE: the HTTP route surface for skill packages is exercised by
``test_skill_packages.py``, which currently fails to import (it references
route constants that no longer exist in ``routes/skills.py``). This file gives
the package *logic* direct coverage regardless of that route question.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from starlette.exceptions import HTTPException

from caliber.db.models import CaliberSkill
from caliber.schemas import SkillPackageFilePayload
from caliber.skill_packages import (
    OPENAI_PACKAGE_FORMAT,
    OPENAI_PACKAGE_METADATA_KEY,
    ImportedSkillPackage,
    build_skill_package,
    build_skill_package_zip,
    merge_openai_package_metadata,
    parse_skill_package,
)


def make_skill(
    *,
    name: str = "data-cleaner",
    description: str = "Cleans messy CSV data before analysis.",
    content: str = "# How to clean\n\nNormalize columns, drop dupes.",
    summary: str = "",
    skill_metadata: dict | None = None,
) -> CaliberSkill:
    return CaliberSkill(
        name=name,
        description=description,
        content=content,
        summary=summary,
        skill_metadata=skill_metadata if skill_metadata is not None else {},
    )


def payloads(files: dict[str, str]) -> list[SkillPackageFilePayload]:
    return [SkillPackageFilePayload(path=p, content=c) for p, c in files.items()]


def valid_skill_md(name: str = "data-cleaner", description: str = "Cleans data.") -> str:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n# Body\n\nInstructions here.\n"


# --------------------------------------------------------------------------- #
# build_skill_package
# --------------------------------------------------------------------------- #
def test_build_package_basic_shape() -> None:
    pkg = build_skill_package(make_skill())
    assert pkg.root == "data-cleaner"
    assert pkg.format == OPENAI_PACKAGE_FORMAT
    assert pkg.is_valid is True
    assert pkg.warnings == []
    paths = {f.path for f in pkg.files}
    assert "data-cleaner/SKILL.md" in paths
    assert "data-cleaner/agents/openai.yaml" in paths


def test_build_package_skill_md_has_frontmatter_and_body() -> None:
    pkg = build_skill_package(make_skill())
    skill_md = next(f.content for f in pkg.files if f.path.endswith("SKILL.md"))
    assert skill_md.startswith("---\n")
    assert "name: data-cleaner" in skill_md
    assert "description: Cleans messy CSV data before analysis." in skill_md
    assert "Normalize columns" in skill_md  # body carried through


def test_build_package_openai_yaml_interface() -> None:
    pkg = build_skill_package(make_skill())
    yaml_text = next(f.content for f in pkg.files if f.path.endswith("openai.yaml"))
    assert "display_name:" in yaml_text
    assert "short_description:" in yaml_text
    assert "$data-cleaner" in yaml_text  # default_prompt references the skill handle
    assert "allow_implicit_invocation:" in yaml_text


def test_build_package_includes_resources_and_counts() -> None:
    skill = make_skill(
        skill_metadata={
            OPENAI_PACKAGE_METADATA_KEY: {
                "resources": [
                    {"path": "references/api.md", "content": "ref"},
                    {"path": "scripts/run.py", "content": "print(1)"},
                ]
            }
        }
    )
    pkg = build_skill_package(skill)
    paths = {f.path for f in pkg.files}
    assert "data-cleaner/references/api.md" in paths
    assert "data-cleaner/scripts/run.py" in paths
    assert pkg.resource_counts["references"] == 1
    assert pkg.resource_counts["scripts"] == 1
    assert pkg.resource_counts["assets"] == 0
    assert pkg.is_valid is True


def test_build_package_bad_resources_list_warns() -> None:
    skill = make_skill(skill_metadata={OPENAI_PACKAGE_METADATA_KEY: {"resources": "not-a-list"}})
    pkg = build_skill_package(skill)
    assert pkg.is_valid is False
    assert any("must be a list" in w for w in pkg.warnings)


def test_build_package_resource_outside_allowed_dir_warns_and_skips() -> None:
    skill = make_skill(
        skill_metadata={
            OPENAI_PACKAGE_METADATA_KEY: {
                "resources": [{"path": "secrets/key.txt", "content": "x"}]
            }
        }
    )
    pkg = build_skill_package(skill)
    assert pkg.is_valid is False
    assert any("secrets/key.txt" in w for w in pkg.warnings)
    assert all(not f.path.endswith("secrets/key.txt") for f in pkg.files)


def test_build_package_duplicate_resource_warns() -> None:
    skill = make_skill(
        skill_metadata={
            OPENAI_PACKAGE_METADATA_KEY: {
                "resources": [
                    {"path": "references/a.md", "content": "1"},
                    {"path": "references/a.md", "content": "2"},
                ]
            }
        }
    )
    pkg = build_skill_package(skill)
    assert any("duplicate resource path" in w for w in pkg.warnings)
    ref_files = [f for f in pkg.files if f.path.endswith("references/a.md")]
    assert len(ref_files) == 1


# --------------------------------------------------------------------------- #
# build_skill_package_zip
# --------------------------------------------------------------------------- #
def test_build_zip_is_valid_archive_with_expected_members() -> None:
    skill = make_skill(
        skill_metadata={
            OPENAI_PACKAGE_METADATA_KEY: {
                "resources": [{"path": "references/api.md", "content": "ref"}]
            }
        }
    )
    raw = build_skill_package_zip(skill)
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        names = set(archive.namelist())
        assert "data-cleaner/SKILL.md" in names
        assert "data-cleaner/agents/openai.yaml" in names
        assert "data-cleaner/references/api.md" in names
        assert archive.read("data-cleaner/references/api.md").decode() == "ref"


# --------------------------------------------------------------------------- #
# parse_skill_package — happy paths
# --------------------------------------------------------------------------- #
def test_parse_minimal_valid_package() -> None:
    imported = parse_skill_package(payloads({"data-cleaner/SKILL.md": valid_skill_md()}))
    assert isinstance(imported, ImportedSkillPackage)
    assert imported.name == "data-cleaner"
    assert imported.description == "Cleans data."
    assert "Instructions here." in imported.content
    assert OPENAI_PACKAGE_METADATA_KEY in imported.skill_metadata


def test_export_import_round_trip() -> None:
    skill = make_skill(
        content="# Title\n\nReal instructions.",
        skill_metadata={
            OPENAI_PACKAGE_METADATA_KEY: {
                "resources": [{"path": "references/a.md", "content": "ref"}]
            }
        },
    )
    pkg = build_skill_package(skill)
    imported = parse_skill_package(payloads({f.path: f.content for f in pkg.files}))
    assert imported.name == skill.name
    assert imported.description == skill.description
    assert imported.content == skill.content.rstrip()
    # resources survive the round trip
    resources = imported.skill_metadata[OPENAI_PACKAGE_METADATA_KEY]["resources"]
    assert {r["path"] for r in resources} == {"references/a.md"}


# --------------------------------------------------------------------------- #
# parse_skill_package — validation errors (all HTTP 400)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "files,needle",
    [
        ({"agents/openai.yaml": "interface: {}"}, "exactly one SKILL.md"),
        (
            {"a/SKILL.md": valid_skill_md(), "b/SKILL.md": valid_skill_md()},
            "exactly one SKILL.md",
        ),
        ({"data-cleaner/SKILL.md": "no frontmatter here"}, "frontmatter"),
        (
            {"data-cleaner/SKILL.md": "---\ndescription: x\n---\n\nbody\n"},
            "requires 'name'",
        ),
        (
            {"Bad_Name/SKILL.md": "---\nname: Bad_Name\ndescription: x\n---\n\nbody\n"},
            "kebab-case",
        ),
        (
            {"data-cleaner/SKILL.md": "---\nname: data-cleaner\ndescription: x\n---\n\n   \n"},
            "body must not be empty",
        ),
        (
            {"wrong/SKILL.md": valid_skill_md(name="right-name")},
            "folder name must match",
        ),
    ],
)
def test_parse_rejects_invalid_packages(files: dict[str, str], needle: str) -> None:
    with pytest.raises(HTTPException) as exc:
        parse_skill_package(payloads(files))
    assert exc.value.status_code == 400
    assert needle in exc.value.detail


def test_parse_rejects_unsafe_path() -> None:
    with pytest.raises(HTTPException) as exc:
        parse_skill_package(payloads({"../evil.md": "x"}))
    assert exc.value.status_code == 400
    assert "unsafe" in exc.value.detail


def test_parse_rejects_duplicate_file_path() -> None:
    # Two payloads cleaning to the same path.
    files = [
        SkillPackageFilePayload(path="data-cleaner/SKILL.md", content=valid_skill_md()),
        SkillPackageFilePayload(path="data-cleaner/SKILL.md", content="dupe"),
    ]
    with pytest.raises(HTTPException) as exc:
        parse_skill_package(files)
    assert exc.value.status_code == 400
    assert "duplicate package file path" in exc.value.detail


def test_parse_rejects_extraneous_doc() -> None:
    files = {
        "data-cleaner/SKILL.md": valid_skill_md(),
        "data-cleaner/README.md": "nope",
    }
    with pytest.raises(HTTPException) as exc:
        parse_skill_package(payloads(files))
    assert exc.value.status_code == 400
    assert "README.md" in exc.value.detail


def test_parse_rejects_resource_outside_allowed_dirs() -> None:
    files = {
        "data-cleaner/SKILL.md": valid_skill_md(),
        "data-cleaner/lib/util.py": "code",
    }
    with pytest.raises(HTTPException) as exc:
        parse_skill_package(payloads(files))
    assert exc.value.status_code == 400
    assert "scripts/, references/, or assets/" in exc.value.detail


# --------------------------------------------------------------------------- #
# merge_openai_package_metadata
# --------------------------------------------------------------------------- #
def test_merge_preserves_imported_resources_and_adds_caller_keys() -> None:
    imported = ImportedSkillPackage(
        name="x",
        description="d",
        summary="s",
        content="c",
        skill_metadata={
            OPENAI_PACKAGE_METADATA_KEY: {
                "format": OPENAI_PACKAGE_FORMAT,
                "resources": [{"path": "references/a.md", "content": "A"}],
            }
        },
    )
    merged = merge_openai_package_metadata(
        {OPENAI_PACKAGE_METADATA_KEY: {"source": "manual"}, "other": 1}, imported
    )
    pkg = merged[OPENAI_PACKAGE_METADATA_KEY]
    assert pkg["source"] == "manual"  # caller key added
    assert pkg["format"] == OPENAI_PACKAGE_FORMAT  # imported key kept
    assert pkg["resources"] == [{"path": "references/a.md", "content": "A"}]  # not lost
    assert merged["other"] == 1  # unrelated top-level key carried over


def test_merge_non_dict_override_replaces_value() -> None:
    imported = ImportedSkillPackage(
        name="x",
        description="d",
        summary="s",
        content="c",
        skill_metadata={OPENAI_PACKAGE_METADATA_KEY: {"format": OPENAI_PACKAGE_FORMAT}},
    )
    merged = merge_openai_package_metadata({OPENAI_PACKAGE_METADATA_KEY: "replaced"}, imported)
    assert merged[OPENAI_PACKAGE_METADATA_KEY] == "replaced"
