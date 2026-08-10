"""A third-party plugin registers without a single CALIBER source edit.

This is M4's acceptance criterion, and it is worth an end-to-end test rather
than an assertion about the mechanism: the registry tests use fake entry points,
which proves the loader works but not that the packaging contract does. Here a
real distribution is built into a temporary directory, installed onto
``sys.path`` the way a wheel would be, and discovered through
``importlib.metadata``.

What is deliberately *not* proved: that installing the wheel is sufficient.
It is not, and the second test is the one that matters more.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from caliber.extensibility import optimizer_registry
from caliber.extensibility.entrypoints import ALLOWLIST_ENV_VAR, available_optimizer_plugins

#: A minimal but complete third-party distribution. Written as source rather
#: than committed as a fixture package so the test reads as "what an author has
#: to write", which is the thing under test.
PLUGIN_MODULE = '''\
"""A third-party optimizer, written against caliber_plugin_sdk only."""

from __future__ import annotations


class SpecificityOptimizer:
    def optimize(self, request):
        return {"content": request.current_content, "rationale": "unchanged"}


class _Declaration:
    """Stands in for caliber_plugin_sdk.PluginDeclaration.

    Duplicated here so this test proves the *server's* contract without the
    server's tests depending on the plugin SDK being installed -- the two
    packages ship separately and CI installs them into separate environments.
    """

    name = "AcmeSpecificity"
    summary = "Makes vague instructions specific."
    artifact_types = frozenset({"prompt"})


declaration = _Declaration()
'''


def _write_distribution(root: Path, *, distribution: str, entry_point: str) -> None:
    """Lay out an installed distribution: the module plus its dist-info."""
    (root / "acme_caliber.py").write_text(PLUGIN_MODULE, encoding="utf-8")
    dist_info = root / f"{distribution.replace('-', '_')}-1.0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: 1.0.0\n", encoding="utf-8"
    )
    (dist_info / "entry_points.txt").write_text(
        textwrap.dedent(f"""\
            [caliber.optimizers]
            acme = {entry_point}
            """),
        encoding="utf-8",
    )
    # Present in every real wheel install; some metadata readers expect it.
    (dist_info / "RECORD").write_text("", encoding="utf-8")


@pytest.fixture
def installed_plugin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Build and 'install' a third-party distribution for one test.

    ``sys.path`` insertion plus ``invalidate_caches`` is what actually makes a
    newly written dist-info visible to ``importlib.metadata`` mid-process.
    """
    import importlib

    site = tmp_path / "site-packages"
    site.mkdir()

    def install(*, entry_point: str = "acme_caliber:declaration") -> None:
        _write_distribution(
            site, distribution="acme-caliber-optimizers", entry_point=entry_point
        )
        monkeypatch.syspath_prepend(str(site))
        importlib.invalidate_caches()

    registry = optimizer_registry()
    registry.reset_for_tests()
    yield install
    registry.reset_for_tests()
    sys.modules.pop("acme_caliber", None)


def test_an_installed_plugin_is_discovered_without_being_enabled(
    installed_plugin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Installed, visible, and inert -- the state that matters most.

    A wheel appearing on the path must not gain authority over production
    prompts. Any distribution can advertise into the entry-point group, including
    one pulled in transitively three levels down, and a plugin system that loaded
    on discovery would hand that dependency the ability to author what CALIBER
    promotes.

    So the default is: reported, not loaded.
    """
    monkeypatch.delenv(ALLOWLIST_ENV_VAR, raising=False)
    installed_plugin()

    listed = [item for item in available_optimizer_plugins() if item["name"] == "acme"]
    assert listed, "a real installed distribution was not discovered"
    assert listed[0]["distribution"] == "acme-caliber-optimizers"
    assert listed[0]["allowlisted"] is False

    # And it did not register.
    assert "AcmeSpecificity" not in optimizer_registry().names()


def test_allowlisting_the_distribution_registers_the_plugin(
    installed_plugin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The acceptance criterion: no CALIBER source edit, only configuration.

    The plugin module is a file in a temporary directory. Nothing in
    ``caliber/src`` mentions it. The only thing that changed between this test
    and the previous one is an environment variable.
    """
    monkeypatch.setenv(ALLOWLIST_ENV_VAR, "acme-caliber-optimizers")
    installed_plugin()

    registry = optimizer_registry()
    assert "AcmeSpecificity" in registry.names()

    spec = registry.get("AcmeSpecificity")
    assert spec.source == "plugin"
    assert spec.distribution == "acme-caliber-optimizers"
    # Experimental until the plugin contract survives a release, and the server
    # sets that rather than trusting the plugin's own claim.
    assert spec.experimental
    assert spec.can_target("prompt")
    assert not spec.can_target("skill")

    # Selectable for prompts, so the automatic rules could reach it -- which is
    # the point of registering rather than special-casing.
    assert "AcmeSpecificity" in registry.selectable("prompt")


def test_a_plugin_pointing_at_a_missing_symbol_is_recorded_not_fatal(
    installed_plugin: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo in the author's own entry point must not break the deployment.

    It must also not be silent: the operator allowlisted this, so "nothing
    happened" would read as success.
    """
    monkeypatch.setenv(ALLOWLIST_ENV_VAR, "acme-caliber-optimizers")
    installed_plugin(entry_point="acme_caliber:no_such_name")

    registry = optimizer_registry()
    assert "AcmeSpecificity" not in registry.names()
    assert "acme-caliber-optimizers" in registry.load_errors
    # Built-ins are untouched, so refinement still works.
    assert "MetaPrompt" in registry.names()
