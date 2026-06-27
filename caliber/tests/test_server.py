from __future__ import annotations

from caliber import server
from caliber.runtime_advisories import RuntimeDependencyAdvisory


def _capture_warning_messages(monkeypatch) -> list[str]:
    messages: list[str] = []

    def _warning(message: str, *args) -> None:
        rendered = message % args if args else message
        messages.append(rendered)

    monkeypatch.setattr(server.logger, "warning", _warning)
    return messages


def test_warn_if_unsupported_python_logs_once(monkeypatch) -> None:
    monkeypatch.setattr(server, "_WARNED_UNSUPPORTED_PYTHON_VERSIONS", set())
    monkeypatch.setattr(server.sys, "version_info", (3, 14, 4))
    warnings = _capture_warning_messages(monkeypatch)

    server._warn_if_unsupported_python()
    server._warn_if_unsupported_python()
    assert warnings == [
        (
            "CALIBER is running on unsupported Python 3.14; supported versions are "
            "3.10-3.12. Fresh package installs are constrained to the validated "
            "range, but editable or source installs can still bypass that guard."
        )
    ]


def test_warn_if_unsupported_python_skips_supported_versions(monkeypatch) -> None:
    monkeypatch.setattr(server, "_WARNED_UNSUPPORTED_PYTHON_VERSIONS", set())
    monkeypatch.setattr(server.sys, "version_info", (3, 12, 9))
    warnings = _capture_warning_messages(monkeypatch)

    server._warn_if_unsupported_python()

    assert warnings == []


def test_warn_if_known_vulnerable_dependencies_logs_flagged_versions_once(
    monkeypatch,
) -> None:
    monkeypatch.setattr(server, "_WARNED_DEPENDENCY_ADVISORIES", set())
    monkeypatch.setattr(
        server,
        "get_runtime_dependency_advisories",
        lambda: [
            RuntimeDependencyAdvisory(
                package_name="diskcache",
                installed_version="5.6.3",
                advisory_ids=("CVE-2025-69872",),
                summary=(
                    "diskcache 5.6.3 is flagged and is pulled in by the optional "
                    "DSPy optimizer stack."
                ),
                recommended_action=(
                    "Avoid enabling DSPy-backed refinement flows in sensitive deployments until "
                    "upstream publishes a fixed release."
                ),
            ),
            RuntimeDependencyAdvisory(
                package_name="torch",
                installed_version="2.12.0",
                advisory_ids=("CVE-2025-3000",),
                summary=(
                    "torch 2.12.0 is flagged and is pulled in by the optional "
                    "local Hugging Face embedding stack."
                ),
                recommended_action=(
                    "Isolate or disable local knowledge-base embedding builds "
                    "until a fixed release is available."
                ),
            ),
            RuntimeDependencyAdvisory(
                package_name="litellm",
                installed_version="1.83.0",
                advisory_ids=(
                    "CVE-2026-40217",
                    "CVE-2026-42203",
                    "CVE-2026-42208",
                    "CVE-2026-42271",
                ),
                summary="litellm 1.83.0 is below the current safe floor 1.83.10.",
                recommended_action=(
                    "Upgrade the optional DSPy / LiteLLM stack before enabling DSPy or "
                    "other LiteLLM-backed paths."
                ),
            ),
        ],
    )
    warnings = _capture_warning_messages(monkeypatch)

    server._warn_if_known_vulnerable_dependencies()
    server._warn_if_known_vulnerable_dependencies()
    assert warnings == [
        (
            "Installed dependency diskcache 5.6.3 is flagged (CVE-2025-69872). "
            "diskcache 5.6.3 is flagged and is pulled in by the optional DSPy "
            "optimizer stack. Avoid enabling DSPy-backed refinement flows in "
            "sensitive deployments until upstream publishes a fixed release."
        ),
        (
            "Installed dependency torch 2.12.0 is flagged (CVE-2025-3000). "
            "torch 2.12.0 is flagged and is pulled in by the optional local Hugging "
            "Face embedding stack. Isolate or disable local knowledge-base embedding "
            "builds until a fixed release is available."
        ),
        (
            "Installed dependency litellm 1.83.0 is flagged (CVE-2026-40217, "
            "CVE-2026-42203, CVE-2026-42208, CVE-2026-42271). litellm 1.83.0 is below "
            "the current safe floor 1.83.10. Upgrade the optional DSPy / LiteLLM stack before "
            "enabling DSPy or other LiteLLM-backed paths."
        ),
    ]


def test_warn_if_known_vulnerable_dependencies_skips_safe_or_missing_packages(
    monkeypatch,
) -> None:
    monkeypatch.setattr(server, "_WARNED_DEPENDENCY_ADVISORIES", set())
    monkeypatch.setattr(server, "get_runtime_dependency_advisories", lambda: [])
    warnings = _capture_warning_messages(monkeypatch)

    server._warn_if_known_vulnerable_dependencies()

    assert warnings == []
