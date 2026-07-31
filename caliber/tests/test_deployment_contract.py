"""Static release contracts for the bundled development containers.

These checks do not pretend the Compose stack is production evidence. They prevent a
future edit from silently undoing the concrete boundaries its documentation promises:
loopback-only publication, an unprivileged read-only CALIBER container, deterministic
frontend installs, and startup logs that do not disclose embedded database credentials.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml", reason="PyYAML is needed to inspect Compose files")

REPO_ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILES = (
    REPO_ROOT / "caliber" / "deploy" / "mcp" / "docker-compose.yml",
    *sorted((REPO_ROOT / "deploy").glob("*/compose.yaml")),
)


def _compose(path: Path) -> dict[str, object]:
    parsed = yaml.safe_load(path.read_text())
    assert isinstance(parsed, dict), f"{path} is not a Compose mapping"
    return parsed


def test_every_bundled_published_port_is_loopback_only() -> None:
    published: list[tuple[Path, str, str]] = []
    for path in COMPOSE_FILES:
        services = _compose(path).get("services", {})
        assert isinstance(services, dict)
        for service_name, raw_service in services.items():
            assert isinstance(raw_service, dict)
            for raw_port in raw_service.get("ports", []) or []:
                port = str(raw_port)
                published.append((path, str(service_name), port))
                assert port.startswith("127.0.0.1:"), (
                    f"{path}:{service_name} publishes {port!r} beyond loopback"
                )
    assert published, "the deployment inventory unexpectedly contains no published ports"


def test_umbrella_compose_does_not_require_ignored_environment_files() -> None:
    """A fresh clone has examples, not the ignored root/deploy ``.env`` files."""
    umbrella = _compose(REPO_ROOT / "deploy" / "compose.yaml")
    includes = umbrella.get("include")

    assert isinstance(includes, list) and includes
    assert all(isinstance(entry, str) for entry in includes), (
        "include-level env_file entries can make an ignored .env mandatory; "
        "launchers pass optional env files through --env-file instead"
    )


def test_caliber_container_retains_its_documented_runtime_hardening() -> None:
    compose = _compose(REPO_ROOT / "deploy" / "caliber" / "compose.yaml")
    services = compose["services"]
    assert isinstance(services, dict)
    caliber = services["caliber"]
    assert isinstance(caliber, dict)

    assert caliber["read_only"] is True
    assert caliber["user"] == "65532:65532"
    assert caliber["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in caliber["security_opt"]
    assert any(str(item).startswith("/tmp:") for item in caliber["tmpfs"])
    assert any(str(item).endswith(":/data") for item in caliber["volumes"])
    assert all(str(port).startswith("127.0.0.1:") for port in caliber["ports"])


def test_caliber_image_is_reproducible_and_unprivileged() -> None:
    dockerfile = (REPO_ROOT / "deploy" / "caliber" / "Dockerfile").read_text()

    assert "RUN npm ci --no-audit --no-fund" in dockerfile
    assert "USER 65532:65532" in dockerfile


def test_suite_root_docker_context_excludes_local_secrets_and_databases() -> None:
    patterns = {
        line.strip()
        for line in (REPO_ROOT / ".dockerignore").read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {
        "**/.env",
        "**/.env.*",
        "**/*.pem",
        "**/*.key",
        "**/*.p12",
        "**/*.pfx",
        "**/*.db*",
        "**/*.sqlite*",
    } <= patterns


def test_container_entrypoint_never_logs_the_database_url() -> None:
    """Database and tracking URLs can both embed credentials."""
    entrypoint = (REPO_ROOT / "deploy" / "caliber" / "entrypoint.sh").read_text()

    assert "CALIBER_DATABASE_URL=${CALIBER_DATABASE_URL}" not in entrypoint
    assert "${MLFLOW_TRACKING_URI" not in entrypoint


def test_native_dev_launcher_never_logs_storage_urls() -> None:
    launcher = (REPO_ROOT / "caliber" / "scripts" / "run-dev.sh").read_text()

    for variable in (
        "$CALIBER_DATABASE_URL",
        "$MLFLOW_ARTIFACT_ROOT",
        "$MLFLOW_S3_ENDPOINT_URL",
    ):
        assert not any(
            variable in line and line.lstrip().startswith(("echo ", "printf "))
            for line in launcher.splitlines()
        ), f"run-dev.sh logs potentially sensitive value {variable}"


def test_tracking_server_target_never_logs_storage_urls() -> None:
    makefile = (REPO_ROOT / "caliber" / "Makefile").read_text()

    for variable in (
        "$(CALIBER_DATABASE_URL)",
        "$(MLFLOW_ARTIFACT_ROOT)",
        "$(MLFLOW_S3_ENDPOINT_URL)",
    ):
        assert not any(variable in line and "@echo" in line for line in makefile.splitlines()), (
            f"tracking-server logs potentially sensitive value {variable}"
        )


def test_distribution_name_is_distinct_from_the_import_package() -> None:
    pyproject = (REPO_ROOT / "caliber" / "pyproject.toml").read_text()
    readme = (REPO_ROOT / "caliber" / "README.md").read_text()

    assert '[project]\nname = "caliber-suite"' in pyproject
    assert (REPO_ROOT / "caliber" / "src" / "caliber" / "__init__.py").is_file()
    assert "does not currently publish a PyPI release" in readme
    assert "\npip install caliber-suite\n" not in readme


def test_root_full_suite_selects_one_venv_for_backend_and_playwright() -> None:
    runner = (REPO_ROOT / "test-all.sh").read_text()

    assert 'backend_venv=".venv"' in runner
    assert '[[ -x "$REPO_ROOT/.venv/bin/python" ]]' in runner
    assert 'backend_venv="../.venv"' in runner
    assert 'make -C caliber VENV="$backend_venv" test-allure' in runner
    assert 'VENV_DIR="$backend_venv" npm run test:e2e' in runner


def test_native_dev_launcher_defaults_reachable_cookie_to_secure() -> None:
    launcher = (REPO_ROOT / "caliber" / "scripts" / "run-dev.sh").read_text()

    assert "local_session_cookie_secure=false" in launcher
    assert "local_session_cookie_secure=true" in launcher
    assert (
        'CALIBER_AUTH_SESSION_COOKIE_SECURE="${CALIBER_AUTH_SESSION_COOKIE_SECURE:-$local_session_cookie_secure}"'
        in launcher
    )
