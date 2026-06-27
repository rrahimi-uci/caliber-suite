"""Shared runtime support and dependency advisory helpers.

These checks are intentionally lightweight and side-effect free so they can
power startup warnings, settings inventory surfaces, and tests without pulling
in heavy optional stacks eagerly.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata as importlib_metadata

SUPPORTED_PYTHON_MIN = (3, 10)
SUPPORTED_PYTHON_MAX_EXCLUSIVE = (3, 13)
SUPPORTED_PYTHON_RANGE_LABEL = "3.10-3.12"
DSPY_OPTIMIZER_OVERRIDE_ENV_VAR = "CALIBER_ALLOW_FLAGGED_DSPY_OPTIMIZERS"
LOCAL_EMBEDDING_OVERRIDE_ENV_VAR = "CALIBER_ALLOW_FLAGGED_LOCAL_EMBEDDINGS"

_DSPY_STACK_PACKAGES = frozenset({"diskcache", "litellm"})
_LOCAL_EMBEDDING_STACK_PACKAGES = frozenset({"torch"})


@dataclass(frozen=True)
class RuntimeDependencyAdvisory:
    """One known-risk dependency version present in the current runtime."""

    package_name: str
    installed_version: str
    advisory_ids: tuple[str, ...]
    summary: str
    recommended_action: str


def version_tuple_prefix(raw: str) -> tuple[int, ...]:
    """Return the numeric prefix of a version string for coarse comparisons."""

    parts: list[int] = []
    for token in raw.split("."):
        digits = "".join(ch for ch in token if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def _compare_version_prefixes(raw: str, target: tuple[int, ...]) -> int:
    """Compare a parsed version prefix to a target tuple.

    Returns ``-1`` when ``raw < target``, ``0`` when they are equal after
    zero-padding, and ``1`` when ``raw > target``.
    """

    parsed = version_tuple_prefix(raw)
    width = max(len(parsed), len(target))
    padded_parsed = parsed + (0,) * (width - len(parsed))
    padded_target = target + (0,) * (width - len(target))
    if padded_parsed < padded_target:
        return -1
    if padded_parsed > padded_target:
        return 1
    return 0


def version_at_most(raw: str, target: tuple[int, ...]) -> bool:
    """Return whether ``raw`` is less than or equal to ``target``."""

    return _compare_version_prefixes(raw, target) <= 0


def version_below(raw: str, target: tuple[int, ...]) -> bool:
    """Return whether ``raw`` is strictly less than ``target``."""

    return _compare_version_prefixes(raw, target) < 0


def get_runtime_dependency_advisories() -> list[RuntimeDependencyAdvisory]:
    """Return known dependency advisories present in the current runtime.

    The checks here reflect the versions/ranges we have direct evidence for in
    the repository's supported-matrix audit and upstream advisories. Missing
    packages simply produce no advisory.
    """

    package_versions: dict[str, str] = {}
    for package_name in ("diskcache", "torch", "litellm"):
        try:
            package_versions[package_name] = importlib_metadata.version(package_name)
        except importlib_metadata.PackageNotFoundError:
            continue

    advisories: list[RuntimeDependencyAdvisory] = []

    diskcache_version = package_versions.get("diskcache")
    if diskcache_version is not None and version_at_most(diskcache_version, (5, 6, 3)):
        advisories.append(
            RuntimeDependencyAdvisory(
                package_name="diskcache",
                installed_version=diskcache_version,
                advisory_ids=("CVE-2025-69872",),
                summary=(
                    f"diskcache {diskcache_version} is flagged and is pulled in by the optional "
                    "DSPy optimizer stack."
                ),
                recommended_action=(
                    "Avoid enabling DSPy-backed refinement flows in sensitive deployments until "
                    "upstream publishes a fixed release."
                ),
            )
        )

    torch_version = package_versions.get("torch")
    if torch_version is not None and version_at_most(torch_version, (2, 12, 0)):
        advisories.append(
            RuntimeDependencyAdvisory(
                package_name="torch",
                installed_version=torch_version,
                advisory_ids=("CVE-2025-3000",),
                summary=(
                    f"torch {torch_version} is flagged and is pulled in by the optional "
                    "local Hugging Face embedding stack."
                ),
                recommended_action=(
                    "Isolate or disable local knowledge-base embedding builds "
                    "until a fixed release is available."
                ),
            )
        )

    litellm_version = package_versions.get("litellm")
    if litellm_version is not None and version_below(litellm_version, (1, 83, 10)):
        advisories.append(
            RuntimeDependencyAdvisory(
                package_name="litellm",
                installed_version=litellm_version,
                advisory_ids=(
                    "CVE-2026-40217",
                    "CVE-2026-42203",
                    "CVE-2026-42208",
                    "CVE-2026-42271",
                ),
                summary=(f"litellm {litellm_version} is below the current safe floor 1.83.10."),
                recommended_action=(
                    "Upgrade the optional DSPy / LiteLLM stack before enabling DSPy or "
                    "other LiteLLM-backed paths."
                ),
            )
        )

    return advisories


def dspy_optimizer_runtime_advisories() -> list[RuntimeDependencyAdvisory]:
    """Return advisories that affect DSPy / LiteLLM-backed optimizers."""

    return [
        advisory
        for advisory in get_runtime_dependency_advisories()
        if advisory.package_name in _DSPY_STACK_PACKAGES
    ]


def local_embedding_runtime_advisories() -> list[RuntimeDependencyAdvisory]:
    """Return advisories that affect local Hugging Face embedding builds."""

    return [
        advisory
        for advisory in get_runtime_dependency_advisories()
        if advisory.package_name in _LOCAL_EMBEDDING_STACK_PACKAGES
    ]


def dspy_optimizer_block_reason(*, allow_flagged: bool = False) -> str | None:
    """Return the reason DSPy optimizers should be blocked, if any."""

    return _optional_stack_block_reason(
        stack_label="DSPy optimizers",
        advisories=dspy_optimizer_runtime_advisories(),
        override_env_var=DSPY_OPTIMIZER_OVERRIDE_ENV_VAR,
        allow_flagged=allow_flagged,
    )


def local_embedding_block_reason(*, allow_flagged: bool = False) -> str | None:
    """Return the reason local embedding builds should be blocked, if any."""

    return _optional_stack_block_reason(
        stack_label="Local Hugging Face embedding builds",
        advisories=local_embedding_runtime_advisories(),
        override_env_var=LOCAL_EMBEDDING_OVERRIDE_ENV_VAR,
        allow_flagged=allow_flagged,
    )


def _optional_stack_block_reason(
    *,
    stack_label: str,
    advisories: list[RuntimeDependencyAdvisory],
    override_env_var: str,
    allow_flagged: bool,
) -> str | None:
    if allow_flagged or not advisories:
        return None

    flagged = "; ".join(
        f"{advisory.package_name} {advisory.installed_version} ({', '.join(advisory.advisory_ids)})"
        for advisory in advisories
    )
    recommended_action = " ".join(
        dict.fromkeys(
            advisory.recommended_action.strip()
            for advisory in advisories
            if advisory.recommended_action.strip()
        )
    )
    return (
        f"{stack_label} are blocked because the current runtime includes flagged "
        f"dependencies: {flagged}. {recommended_action} Set {override_env_var}=true "
        "only if you explicitly accept the risk for this deployment."
    )
