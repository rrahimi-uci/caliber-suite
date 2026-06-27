"""OpenAI-compatible skill package helpers.

CALIBER stores skills in the database so agents can discover, version, and
reuse them. This module gives those rows a portable package surface matching
the OpenAI skill folder shape:

```
skill-name/
  SKILL.md
  agents/openai.yaml
  scripts/
  references/
  assets/
```

``SKILL.md`` is generated from the canonical registry fields. Optional bundled
resources live in ``skill_metadata["openai_package"]["resources"]`` as
``{"path": "references/foo.md", "content": "..."}`` records.
"""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

import yaml
from starlette.exceptions import HTTPException

from caliber.db.models import CaliberSkill
from caliber.schemas import (
    SKILL_NAME_PATTERN,
    SkillPackageFilePayload,
    SkillPackageFileSchema,
    SkillPackageSchema,
)

OPENAI_PACKAGE_METADATA_KEY = "openai_package"
OPENAI_PACKAGE_FORMAT = "openai-skill"
_SHORT_DESCRIPTION_MAX = 64
_SHORT_DESCRIPTION_TRUNCATE_AT = 61
_MIN_RESOURCE_PATH_PARTS = 2

_ALLOWED_RESOURCE_DIRS = frozenset({"scripts", "references", "assets"})
_GENERATED_FILES = frozenset({"SKILL.md", "agents/openai.yaml"})
_EXTRANEOUS_DOC_NAMES = frozenset(
    {
        "README.md",
        "CHANGELOG.md",
        "INSTALLATION_GUIDE.md",
        "QUICK_REFERENCE.md",
    }
)
_FRONTMATTER_RE = re.compile(r"\A---\s*\r?\n(?P<yaml>.*?)\r?\n---\s*\r?\n?(?P<body>.*)\Z", re.S)
_SKILL_NAME_RE = re.compile(SKILL_NAME_PATTERN)


@dataclass(frozen=True)
class ImportedSkillPackage:
    """Validated package data ready to insert as a ``CaliberSkill`` row."""

    name: str
    description: str
    summary: str
    content: str
    skill_metadata: dict[str, object]


def build_skill_package(skill: CaliberSkill) -> SkillPackageSchema:
    """Generate a portable OpenAI-style package from a skill row."""

    warnings: list[str] = []
    root = skill.name
    files = [
        _package_file(root, "SKILL.md", "skill", _skill_md(skill)),
        _package_file(root, "agents/openai.yaml", "agent-metadata", _openai_yaml(skill)),
    ]

    for resource in _resource_files(skill, warnings):
        files.append(
            _package_file(
                root,
                resource["path"],
                _kind_for_path(resource["path"]),
                resource["content"],
            )
        )

    counts = {"scripts": 0, "references": 0, "assets": 0}
    for file in files:
        relative = _relative_package_path(root, file.path)
        top = relative.split("/", 1)[0]
        if top in counts:
            counts[top] += 1

    return SkillPackageSchema(
        root=root,
        format=OPENAI_PACKAGE_FORMAT,
        files=files,
        resource_counts=counts,
        warnings=warnings,
        is_valid=not warnings,
    )


def build_skill_package_zip(skill: CaliberSkill) -> bytes:
    """Return a ZIP archive for the generated package."""

    package = build_skill_package(skill)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file in package.files:
            archive.writestr(file.path, file.content)
    return stream.getvalue()


def parse_skill_package(files: list[SkillPackageFilePayload]) -> ImportedSkillPackage:
    """Validate JSON file payloads and extract a skill row from them."""

    normalized = _normalize_import_files(files)
    skill_path = _find_skill_md(normalized)
    frontmatter, body = _parse_skill_md(normalized[skill_path])

    name = _required_frontmatter_string(frontmatter, "name")
    description = _required_frontmatter_string(frontmatter, "description")
    if not _SKILL_NAME_RE.fullmatch(name):
        raise HTTPException(status_code=400, detail="SKILL.md frontmatter name must be kebab-case")
    if not body.strip():
        raise HTTPException(status_code=400, detail="SKILL.md body must not be empty")

    root = skill_path.split("/", 1)[0] if "/" in skill_path else name
    if root != name:
        raise HTTPException(
            status_code=400,
            detail="skill folder name must match SKILL.md frontmatter name",
        )

    relative_files = _strip_root(normalized, root)
    for relative in relative_files:
        _validate_import_relative_path(relative)

    openai_yaml = _parse_openai_yaml(relative_files.get("agents/openai.yaml"))
    resources = [
        {"path": path, "content": content}
        for path, content in sorted(relative_files.items())
        if path.split("/", 1)[0] in _ALLOWED_RESOURCE_DIRS
    ]

    package_metadata: dict[str, object] = {
        "format": OPENAI_PACKAGE_FORMAT,
        "source": "imported",
        "agents": _jsonable(openai_yaml.get("interface", {})),
        "policy": _jsonable(openai_yaml.get("policy", {})),
        "dependencies": _jsonable(openai_yaml.get("dependencies", {})),
        "resources": resources,
    }
    summary = _summary_from_openai_yaml(openai_yaml) or description

    return ImportedSkillPackage(
        name=name,
        description=description,
        summary=summary,
        content=body.rstrip(),
        skill_metadata={OPENAI_PACKAGE_METADATA_KEY: package_metadata},
    )


def merge_openai_package_metadata(
    skill_metadata: dict[str, object],
    imported: ImportedSkillPackage,
) -> dict[str, object]:
    """Merge caller metadata over imported package metadata without losing files."""

    merged = dict(imported.skill_metadata)
    for key, value in skill_metadata.items():
        if key == OPENAI_PACKAGE_METADATA_KEY and isinstance(value, dict):
            existing_package = merged.get(OPENAI_PACKAGE_METADATA_KEY)
            base: dict[str, object] = {}
            if isinstance(existing_package, dict):
                base = {str(k): v for k, v in existing_package.items()}
            for metadata_key, metadata_value in value.items():
                if isinstance(metadata_key, str):
                    base[metadata_key] = metadata_value
            merged[key] = base
        else:
            merged[key] = value
    return merged


def _skill_md(skill: CaliberSkill) -> str:
    frontmatter = yaml.safe_dump(
        {"name": skill.name, "description": skill.description},
        sort_keys=False,
        default_flow_style=False,
    )
    body = skill.content.rstrip()
    return f"---\n{frontmatter}---\n\n{body}\n"


def _openai_yaml(skill: CaliberSkill) -> str:
    interface = _interface_metadata(skill)
    policy = _policy_metadata(skill)
    lines = [
        "interface:",
        f"  display_name: {_quoted(interface['display_name'])}",
        f"  short_description: {_quoted(interface['short_description'])}",
        f"  default_prompt: {_quoted(interface['default_prompt'])}",
        "policy:",
        f"  allow_implicit_invocation: {str(policy.get('allow_implicit_invocation', True)).lower()}",
        "",
    ]
    return "\n".join(lines)


def _interface_metadata(skill: CaliberSkill) -> dict[str, str]:
    configured = _configured_package_dict(skill).get("agents", {})
    interface = configured.get("interface", configured) if isinstance(configured, dict) else {}
    display_name = _string_or_default(interface.get("display_name"), _title_from_name(skill.name))
    short_description = _string_or_default(
        interface.get("short_description"),
        _short_description(skill),
    )
    default_prompt = _string_or_default(
        interface.get("default_prompt"),
        f"Use ${skill.name} to {short_description.rstrip('.').lower()}.",
    )
    if f"${skill.name}" not in default_prompt:
        default_prompt = f"Use ${skill.name}. {default_prompt}"
    return {
        "display_name": display_name,
        "short_description": short_description,
        "default_prompt": default_prompt,
    }


def _policy_metadata(skill: CaliberSkill) -> dict[str, object]:
    package = _configured_package_dict(skill)
    policy = package.get("policy")
    if isinstance(policy, dict):
        return dict(policy)
    return {"allow_implicit_invocation": True}


def _resource_files(skill: CaliberSkill, warnings: list[str]) -> list[dict[str, str]]:
    package = _configured_package_dict(skill)
    raw = package.get("resources", [])
    if not isinstance(raw, list):
        warnings.append("openai_package.resources must be a list; ignoring resources")
        return []

    seen: set[str] = set()
    resources: list[dict[str, str]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            warnings.append(f"resource #{index + 1} is not an object; skipping")
            continue
        path = item.get("path")
        content = item.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            warnings.append(f"resource #{index + 1} requires string path and content; skipping")
            continue
        try:
            clean = _clean_relative_path(path)
            _validate_resource_path(clean)
        except HTTPException as exc:
            warnings.append(f"{path}: {exc.detail}")
            continue
        if clean in seen:
            warnings.append(f"{clean}: duplicate resource path; skipping duplicate")
            continue
        seen.add(clean)
        resources.append({"path": clean, "content": content})
    return resources


def _configured_package_dict(skill: CaliberSkill) -> dict[str, Any]:
    metadata = skill.skill_metadata if isinstance(skill.skill_metadata, dict) else {}
    package = metadata.get(OPENAI_PACKAGE_METADATA_KEY, {})
    return dict(package) if isinstance(package, dict) else {}


def _package_file(root: str, relative_path: str, kind: str, content: str) -> SkillPackageFileSchema:
    path = f"{root}/{relative_path}"
    return SkillPackageFileSchema(
        path=path,
        kind=kind,
        content=content,
        size_bytes=len(content.encode("utf-8")),
    )


def _kind_for_path(relative_path: str) -> str:
    top = relative_path.split("/", 1)[0]
    return {
        "scripts": "script",
        "references": "reference",
        "assets": "asset",
    }.get(top, "asset")


def _relative_package_path(root: str, path: str) -> str:
    prefix = f"{root}/"
    return path[len(prefix) :] if path.startswith(prefix) else path


def _title_from_name(name: str) -> str:
    return " ".join(part.capitalize() for part in name.split("-"))


def _short_description(skill: CaliberSkill) -> str:
    source = (
        skill.summary
        or skill.description
        or f"Use {_title_from_name(skill.name)} in agent workflows."
    )
    compact = " ".join(source.split())
    if len(compact) <= _SHORT_DESCRIPTION_MAX:
        return compact
    return compact[:_SHORT_DESCRIPTION_TRUNCATE_AT].rstrip() + "..."


def _string_or_default(value: object, default: str) -> str:
    return value if isinstance(value, str) and value.strip() else default


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _normalize_import_files(files: list[SkillPackageFilePayload]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for file in files:
        path = _clean_import_path(file.path)
        if path in normalized:
            raise HTTPException(status_code=400, detail=f"duplicate package file path: {path}")
        normalized[path] = file.content
    return normalized


def _clean_import_path(raw: str) -> str:
    cleaned = raw.replace("\\", "/").strip()
    path = PurePosixPath(cleaned)
    if path.is_absolute() or ".." in path.parts or not cleaned or cleaned in {".", "/"}:
        raise HTTPException(status_code=400, detail=f"unsafe package file path: {raw!r}")
    parts = [part for part in path.parts if part not in {"", "."}]
    if not parts:
        raise HTTPException(status_code=400, detail=f"unsafe package file path: {raw!r}")
    return "/".join(parts)


def _clean_relative_path(raw: str) -> str:
    cleaned = _clean_import_path(raw)
    parts = cleaned.split("/")
    if (
        parts[0] not in _ALLOWED_RESOURCE_DIRS
        and len(parts) > 1
        and parts[1] in _ALLOWED_RESOURCE_DIRS
    ):
        cleaned = "/".join(parts[1:])
    return cleaned


def _find_skill_md(files: dict[str, str]) -> str:
    candidates = [path for path in files if path == "SKILL.md" or path.endswith("/SKILL.md")]
    if len(candidates) != 1:
        raise HTTPException(status_code=400, detail="package must contain exactly one SKILL.md")
    return candidates[0]


def _parse_skill_md(text: str) -> tuple[dict[str, object], str]:
    match = _FRONTMATTER_RE.match(text)
    if match is None:
        raise HTTPException(status_code=400, detail="SKILL.md must start with YAML frontmatter")
    try:
        parsed = yaml.safe_load(match.group("yaml"))
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"invalid SKILL.md frontmatter: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="SKILL.md frontmatter must be a mapping")
    return parsed, match.group("body")


def _required_frontmatter_string(frontmatter: dict[str, object], key: str) -> str:
    value = frontmatter.get(key)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail=f"SKILL.md frontmatter requires {key!r}")
    return value.strip()


def _strip_root(files: dict[str, str], root: str) -> dict[str, str]:
    stripped: dict[str, str] = {}
    prefix = f"{root}/"
    for path, content in files.items():
        if path.startswith(prefix):
            stripped[path[len(prefix) :]] = content
        elif "/" in path:
            raise HTTPException(
                status_code=400, detail="all package files must share one root folder"
            )
        else:
            stripped[path] = content
    return stripped


def _validate_import_relative_path(path: str) -> None:
    if path in _EXTRANEOUS_DOC_NAMES:
        raise HTTPException(status_code=400, detail=f"{path} is not allowed in a skill package")
    if path in _GENERATED_FILES:
        return
    _validate_resource_path(path)


def _validate_resource_path(path: str) -> None:
    parts = path.split("/")
    if len(parts) < _MIN_RESOURCE_PATH_PARTS or parts[0] not in _ALLOWED_RESOURCE_DIRS:
        raise HTTPException(
            status_code=400,
            detail="resource files must be under scripts/, references/, or assets/",
        )
    if parts[-1] in _EXTRANEOUS_DOC_NAMES:
        raise HTTPException(
            status_code=400, detail=f"{parts[-1]} is not allowed in a skill package"
        )


def _parse_openai_yaml(text: str | None) -> dict[str, object]:
    if text is None:
        return {}
    try:
        parsed = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(status_code=400, detail=f"invalid agents/openai.yaml: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=400, detail="agents/openai.yaml must be a mapping")
    return parsed


def _summary_from_openai_yaml(openai_yaml: dict[str, object]) -> str | None:
    interface = openai_yaml.get("interface")
    if not isinstance(interface, dict):
        return None
    value = interface.get("short_description")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _jsonable(value: object) -> object:
    return json.loads(json.dumps(value))


__all__ = [
    "OPENAI_PACKAGE_FORMAT",
    "OPENAI_PACKAGE_METADATA_KEY",
    "ImportedSkillPackage",
    "build_skill_package",
    "build_skill_package_zip",
    "merge_openai_package_metadata",
    "parse_skill_package",
]
