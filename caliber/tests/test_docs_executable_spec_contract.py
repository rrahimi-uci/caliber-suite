"""Executable-spec checks for the published docs corpus.

The generated site is only trustworthy if its non-prose claims are anchored in
the repository that ships with it. These tests cover the contract surfaces that
are easy to make plausible and easy to get wrong: code fences, shell commands,
HTTP routes, configuration names, CLI exits, and diagram preservation.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import re
import shlex
import subprocess
import sys
from functools import cache
from pathlib import Path

import pytest
import tomllib

yaml = pytest.importorskip("yaml", reason="PyYAML is needed to validate YAML code fences")

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_SOURCE = REPO_ROOT / "docs"
DOCS_SITE = REPO_ROOT / "docs-site"
BUILD_DOCS = DOCS_SITE / "build-docs.mjs"
MODULE_ENTRY = re.compile(r'\{\s*md: "([^"]+)",\s*out: "([^"]+\.html)"')
FENCE = re.compile(r"```([^\n`]*)\n(.*?)```", re.S)
INLINE_CODE = re.compile(r"`([^`\n]+)`")
ROUTE_PHRASE = re.compile(r"(?:GET|POST|PUT|PATCH|DELETE)(?:/[A-Z]+)*\s+(/[^`\s,]+)")
RAW_CALL = re.compile(
    r"\b(?:client|caliber)\.raw\.(?:get|post|put|patch|delete|paginate|download)\(\"([^\"]+)\""
)
SHELL_ROUTE = re.compile(r"\$CALIBER_BASE_URL(?P<path>/[^\"'\s|)]+)")
REPO_IMPORT_PREFIXES = ("caliber_sdk", "caliber_plugin_sdk", "caliber_cli")
ENV_NAME = re.compile(r"\b(?:CALIBER|MLFLOW|OPENAI|ANTHROPIC)_[A-Z0-9_]+\b")

sys.path.insert(0, str(REPO_ROOT / "caliber" / "src"))
sys.path.insert(0, str(REPO_ROOT / "sdk" / "caliber-sdk" / "src"))
sys.path.insert(0, str(REPO_ROOT / "sdk" / "caliber-cli" / "src"))
sys.path.insert(0, str(REPO_ROOT / "sdk" / "caliber-plugin-sdk" / "src"))

from caliber_cli import exits
from caliber_cli.cli import build_parser
from caliber_sdk.client import ENV_BASE_URL, ENV_PROJECT, ENV_TOKEN, ENV_USER

from caliber.routes.openapi import PREFIX, build_openapi_document
from caliber.server import create_app


@cache
def _manifest_modules() -> list[tuple[Path, Path]]:
    modules: list[tuple[Path, Path]] = []
    for source_name, html_name in MODULE_ENTRY.findall(BUILD_DOCS.read_text(encoding="utf-8")):
        modules.append((DOCS_SOURCE / source_name, DOCS_SITE / html_name))
    return modules


def _published_sources() -> list[Path]:
    return [source for source, _ in _manifest_modules()]


def _fenced_blocks(path: Path, language: str | None = None) -> list[str]:
    text = path.read_text(encoding="utf-8")
    blocks = [
        body for lang, body in FENCE.findall(text) if language is None or lang.strip() == language
    ]
    return blocks


@cache
def _route_patterns() -> tuple[re.Pattern[str], ...]:
    document = build_openapi_document(create_app())
    patterns: list[re.Pattern[str]] = []
    for path in document["paths"]:
        regex = "^" + re.escape(path).replace(r"\{", "{").replace(r"\}", "}")
        regex = re.sub(r"\{[^/{}]+\}", r"[^/]+", regex) + "$"
        patterns.append(re.compile(regex))
    return tuple(patterns)


def _matches_route(path: str) -> bool:
    candidate = path.split("?", 1)[0]
    if not candidate.startswith("/"):
        return False
    if not candidate.startswith(PREFIX):
        candidate = PREFIX + candidate
    return any(pattern.fullmatch(candidate) for pattern in _route_patterns())


def _route_mentions(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    found: set[str] = set()
    for code in INLINE_CODE.findall(text):
        found.update(ROUTE_PHRASE.findall(code))
    for body in _fenced_blocks(path, "text"):
        found.update(ROUTE_PHRASE.findall(body))
    return found


@cache
def _make_targets() -> dict[Path, set[str]]:
    targets: dict[Path, set[str]] = {}
    for makefile in (REPO_ROOT / "Makefile", REPO_ROOT / "caliber" / "Makefile"):
        if not makefile.exists():
            continue
        names = {
            match.group(1)
            for match in re.finditer(
                r"^([A-Za-z0-9_.-]+):", makefile.read_text(encoding="utf-8"), re.M
            )
            if not match.group(1).startswith(".")
        }
        targets[makefile.parent] = names
    return targets


@cache
def _npm_scripts() -> dict[Path, set[str]]:
    package = REPO_ROOT / "caliber" / "caliber-ui" / "package.json"
    payload = json.loads(package.read_text(encoding="utf-8"))
    return {package.parent: set((payload.get("scripts") or {}).keys())}


@cache
def _distribution_names() -> set[str]:
    names: set[str] = set()
    for pyproject in (
        REPO_ROOT / "sdk" / "caliber-sdk" / "pyproject.toml",
        REPO_ROOT / "sdk" / "caliber-cli" / "pyproject.toml",
        REPO_ROOT / "sdk" / "caliber-plugin-sdk" / "pyproject.toml",
        REPO_ROOT / "caliber" / "pyproject.toml",
    ):
        payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        project = payload.get("project") or {}
        name = project.get("name")
        if isinstance(name, str):
            names.add(name)
    return names


@cache
def _implementation_env_names() -> set[str]:
    names: set[str] = set()
    for path in (
        REPO_ROOT / "caliber" / "src" / "caliber" / "config.py",
        REPO_ROOT / "sdk" / "caliber-sdk" / "src" / "caliber_sdk" / "client.py",
        REPO_ROOT / ".env.example",
        REPO_ROOT / "deploy" / ".env.example",
        REPO_ROOT / "start.sh",
    ):
        names.update(ENV_NAME.findall(path.read_text(encoding="utf-8", errors="ignore")))
    return names


def _parse_args(command: str) -> argparse.Namespace:
    parser = build_parser()
    return parser.parse_args(shlex.split(command)[1:])


def test_all_plain_python_fences_compile_and_import_real_repo_symbols() -> None:
    missing: list[str] = []
    for page in _published_sources():
        for block in _fenced_blocks(page, "python"):
            tree = ast.parse(block, filename=str(page), mode="exec")
            compile(block, str(page), "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.startswith(REPO_IMPORT_PREFIXES):
                            importlib.import_module(alias.name)
                if isinstance(node, ast.ImportFrom) and node.module:
                    if not node.module.startswith(REPO_IMPORT_PREFIXES):
                        continue
                    module = importlib.import_module(node.module)
                    for alias in node.names:
                        if alias.name != "*" and not hasattr(module, alias.name):
                            missing.append(f"{page.name}: {node.module}.{alias.name}")
    assert not missing, f"docs import repo symbols that do not exist: {missing}"


def test_shell_examples_are_parseable_and_reference_real_commands() -> None:  # noqa: PLR0912
    cli_failures: list[str] = []
    route_failures: list[str] = []
    command_failures: list[str] = []

    for page in _published_sources():
        for block in _fenced_blocks(page, "bash"):
            parsed = subprocess.run(
                ["bash", "-n"],
                input=block,
                text=True,
                capture_output=True,
                check=False,
            )
            assert parsed.returncode == 0, f"{page.name} has invalid bash: {parsed.stderr}"

            cwd = REPO_ROOT
            for raw_line in block.splitlines():
                stripped = raw_line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                for segment in [piece.strip() for piece in stripped.split("&&") if piece.strip()]:
                    if segment.startswith("cd "):
                        target = segment[3:].strip()
                        cwd = (
                            (cwd / target).resolve() if not target.startswith("/") else Path(target)
                        )
                        continue
                    if segment.startswith("make "):
                        tokens = shlex.split(segment, comments=True)
                        make_dir = cwd if cwd in _make_targets() else REPO_ROOT
                        targets = _make_targets().get(make_dir, set())
                        for token in tokens[1:]:
                            if token.startswith("-"):
                                continue
                            if token not in targets:
                                command_failures.append(
                                    f"{page.name}: make target {token!r} in {make_dir}"
                                )
                        continue
                    if segment.startswith("npm run "):
                        tokens = shlex.split(segment, comments=True)
                        scripts = _npm_scripts().get(cwd, set())
                        script = tokens[2] if len(tokens) > 2 else ""
                        if script not in scripts:
                            command_failures.append(f"{page.name}: npm script {script!r} in {cwd}")
                        continue
                    if segment.startswith("pip install "):
                        tokens = shlex.split(segment, comments=True)
                        packages = [token for token in tokens[2:] if not token.startswith("-")]
                        for package in packages:
                            if package not in _distribution_names():
                                command_failures.append(f"{page.name}: pip package {package!r}")

            for snippet in re.findall(r"caliberctl[^\n|)#]*", block):
                command = snippet.strip()
                if not command:
                    continue
                try:
                    _parse_args(command)
                except SystemExit as exc:  # pragma: no cover - exercised on failure
                    cli_failures.append(f"{page.name}: {command!r} exited {exc.code}")

            for match in SHELL_ROUTE.finditer(block):
                path = match.group("path")
                if not _matches_route(path):
                    route_failures.append(f"{page.name}: {path}")

    assert not command_failures, (
        f"docs commands reference missing targets/packages: {command_failures}"
    )
    assert not cli_failures, f"docs CLI examples do not parse: {cli_failures}"
    assert not route_failures, f"docs curl examples reference missing routes: {route_failures}"


def test_sdk_raw_fallback_examples_reference_real_routes() -> None:
    failures: list[str] = []
    for page in _published_sources():
        for path in RAW_CALL.findall(page.read_text(encoding="utf-8")):
            if not _matches_route(path):
                failures.append(f"{page.name}: {path}")
    assert not failures, f"raw SDK examples reference missing routes: {failures}"


def test_api_reference_pages_name_only_real_routes() -> None:
    failures: list[str] = []
    for page in sorted((DOCS_SOURCE / "api").glob("*.md")):
        for route in sorted(_route_mentions(page)):
            if not _matches_route(route):
                failures.append(f"{page.name}: {route}")
    assert not failures, f"API docs mention routes the server does not expose: {failures}"


def test_sdk_guide_configuration_table_matches_the_client_env_fallbacks() -> None:
    guide = (DOCS_SOURCE / "sdk" / "guide.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"\| `([A-Z_]+)` \|", guide))
    expected = {ENV_BASE_URL, ENV_PROJECT, ENV_TOKEN, ENV_USER}
    assert expected <= documented, (
        f"sdk guide is missing env fallbacks: {sorted(expected - documented)}"
    )


def test_cli_docs_publish_the_real_exit_codes() -> None:
    cli_doc = (DOCS_SOURCE / "sdk" / "cli.md").read_text(encoding="utf-8")
    published = {int(code) for code in re.findall(r"\| `([0-9]+)` \|", cli_doc)}
    expected = {
        exits.OK,
        exits.FAILURE,
        exits.USAGE,
        exits.AWAITING_HUMAN,
        exits.GATE_FAILED,
        exits.TIMEOUT,
        exits.UNAUTHENTICATED,
    }
    assert published == expected, (
        f"CLI docs advertise exit codes {sorted(published)}, expected {sorted(expected)}"
    )


def test_config_reference_settings_exist_in_repo_configuration() -> None:
    reference = (DOCS_SOURCE / "reference" / "config-and-environment.md").read_text(
        encoding="utf-8"
    )
    settings = sorted(
        set(re.findall(r"`((?:CALIBER|MLFLOW|OPENAI|ANTHROPIC)_[A-Z0-9_*]+)`", reference))
    )
    backing = _implementation_env_names()
    missing: list[str] = []
    for setting in settings:
        if setting.endswith("*"):
            prefix = setting[:-1]
            if not any(name.startswith(prefix) for name in backing):
                missing.append(setting)
            continue
        if setting not in backing:
            missing.append(setting)
    assert not missing, f"config reference names settings with no repo backing: {missing}"


def test_json_toml_and_yaml_fences_parse() -> None:
    failures: list[str] = []
    for page in _published_sources():
        for block in _fenced_blocks(page, "json"):
            try:
                json.loads(block)
            except json.JSONDecodeError as exc:
                failures.append(f"{page.name}: json: {exc}")
        for block in _fenced_blocks(page, "toml"):
            try:
                tomllib.loads(block)
            except tomllib.TOMLDecodeError as exc:
                failures.append(f"{page.name}: toml: {exc}")
        for block in _fenced_blocks(page, "yaml"):
            try:
                yaml.safe_load(block)
            except yaml.YAMLError as exc:
                failures.append(f"{page.name}: yaml: {exc}")
    assert not failures, f"docs contain unparsable structured examples: {failures}"


def test_mermaid_sequence_messages_do_not_use_semicolon_statement_separators() -> None:
    failures: list[str] = []
    for page in _published_sources():
        for block in _fenced_blocks(page, "mermaid"):
            lines = [line.rstrip() for line in block.splitlines()]
            if not any(line.lstrip().startswith("sequenceDiagram") for line in lines[:3]):
                continue
            for line in lines:
                stripped = line.strip()
                if "->" in stripped and ":" in stripped and ";" in stripped:
                    failures.append(f"{page.name}: {stripped}")
    assert not failures, (
        "Mermaid 11 treats semicolons in sequence-message text as statement separators; "
        f"rewrite those lines: {failures}"
    )


def test_generated_html_preserves_every_mermaid_diagram() -> None:
    failures: list[str] = []
    for source, html in _manifest_modules():
        expected = len(_fenced_blocks(source, "mermaid"))
        actual = html.read_text(encoding="utf-8").count('class="mermaid"')
        if expected != actual:
            failures.append(f"{source.name}: expected {expected}, rendered {actual}")
    assert not failures, f"mermaid diagrams were dropped during docs generation: {failures}"
