"""B12.4b (#763): a reference process refuses an installed plugin it was not told to trust, and a host path outside its root.

Real CALIBER processes, started the way an operator starts one, on the reference profile (PostgreSQL only: the profile
refuses SQLite), with a **real installed distribution** (a ``dist-info`` directory and a module on ``PYTHONPATH``, discovered
through ``importlib.metadata``). The plugin's module writes a marker file when it is imported, so "nothing was imported from
a refused plugin" is observed, not assumed:

* an unlisted plugin: the process exits before it serves, with the stable code ``plugin_not_allowlisted`` naming the
  distribution, and the marker was never written;
* the same environment with the distribution named in ``CALIBER_PLUGIN_ALLOWLIST``: it starts, the plugin loads on first use
  and ``GET /capabilities`` lists it as allowlisted and its optimizer as dispatchable;
* ``CALIBER_WORKFLOW_FILE_ROOT=unconfined``: the process exits with ``workflow_file_root_unconfined``;
* a plugin installed *after* the process started (the registry's own refusal): ``GET /capabilities`` still answers and says
  why, and the optimization route answers ``503`` with ``unsupported_in_reference_profile``, with no job or review item
  written; once the plugin is gone the same request is accepted again.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from caliber.db.models import CaliberRefinementJob, CaliberVerificationItem
from caliber.extensibility import entrypoints, optimizer_registry
from caliber.workflows.code_policy import REFUSAL_CODE
from tests.egi_scenarios.conftest import EDITOR_A, PROJECT_A, ReferenceApp, Workspaces
from tests.egi_scenarios.harness.process import HEALTH_PATH, ServerProcess, ServerStartError

pytestmark = pytest.mark.xdist_group("egi-scenario-processes")

CALIBER_DIR = Path(__file__).resolve().parents[2]
PREFIX = "/ajax-api/2.0/mlflow/caliber"
DISTRIBUTION = "acme-b763-optimizers"
MODULE = "acme_b763_plugin"
OPTIMIZER = "AcmeB763"


def _need_reference(app: ReferenceApp) -> None:
    if app.dialect != "postgresql":
        pytest.skip("the reference profile, and so its refusals, need PostgreSQL")


def _install_distribution(site: Path, marker: Path) -> None:
    """A real installed distribution: a module that records its own import, and the metadata that advertises it."""
    site.mkdir()
    (site / f"{MODULE}.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported')\n"
        "class _Declaration:\n"
        f"    name = {OPTIMIZER!r}\n"
        "    summary = 'a scripted third-party optimizer'\n"
        "    artifact_types = frozenset({'prompt'})\n"
        "declaration = _Declaration()\n",
        encoding="utf-8",
    )
    dist_info = site / f"{DISTRIBUTION.replace('-', '_')}-1.0.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {DISTRIBUTION}\nVersion: 1.0.0\n", encoding="utf-8"
    )
    (dist_info / "entry_points.txt").write_text(
        f"[caliber.optimizers]\nacme = {MODULE}:declaration\n", encoding="utf-8"
    )


def _server(app: ReferenceApp, tmp_path: Path, site: Path | None, **env: str) -> ServerProcess:
    """A process on the scenario's reference environment, with ``site`` on its ``PYTHONPATH`` and ``env`` applied."""
    _need_reference(app)
    merged = {**app.env, **env}
    if site is not None:
        merged["PYTHONPATH"] = os.pathsep.join(
            [str(site), *filter(None, [os.environ.get("PYTHONPATH")])]
        )
    return ServerProcess(merged, app_dir=CALIBER_DIR, log_path=tmp_path / "server.log")


def _violations(log: str) -> list[str]:
    return [line for line in log.splitlines() if line.startswith("- ")]


def test_an_unlisted_installed_plugin_refuses_startup_and_is_never_imported(
    reference_app: ReferenceApp, tmp_path: Path
) -> None:
    marker = tmp_path / "imported.marker"
    site = tmp_path / "site"
    _install_distribution(site, marker)
    server = _server(reference_app, tmp_path, site)

    with pytest.raises(ServerStartError):
        server.start()

    assert server.exit_code not in (None, 0), "the process must exit, not keep running"
    log = server.log_tail(80)
    assert "reference profile refused (1 violation)" in log, log
    (line,) = _violations(log)
    assert "[plugin_not_allowlisted]" in line and DISTRIBUTION in line, line
    assert "CALIBER_PLUGIN_ALLOWLIST" in line
    assert not marker.exists(), "the plugin's code ran although the process refused it"
    with pytest.raises(httpx.TransportError):
        httpx.get(server.base_url + HEALTH_PATH, timeout=2.0)


def test_naming_the_distribution_admits_it_and_it_loads_on_first_use(
    reference_app: ReferenceApp, tmp_path: Path
) -> None:
    marker = tmp_path / "imported.marker"
    site = tmp_path / "site"
    _install_distribution(site, marker)
    server = _server(reference_app, tmp_path, site, CALIBER_PLUGIN_ALLOWLIST=DISTRIBUTION)

    with server:
        assert not marker.exists(), "starting must not import a plugin (the load is deferred)"
        response = httpx.get(
            server.base_url + f"{PREFIX}/capabilities",
            headers=reference_app.headers(EDITOR_A, PROJECT_A),
            timeout=10.0,
        )
        assert response.status_code == 200, response.text
        extensibility = response.json()["data"]["extensibility"]
        (plugin,) = extensibility["plugins"]
        assert plugin["distribution"] == DISTRIBUTION
        assert plugin["allowlisted"] is True and plugin["error"] is None
        names = [o["name"] for o in extensibility["optimizers"]]
        assert OPTIMIZER in names and "MetaPrompt" in names
        assert marker.read_text() == "imported"


def test_an_unconfined_file_root_refuses_startup(
    reference_app: ReferenceApp, tmp_path: Path
) -> None:
    server = _server(reference_app, tmp_path, None, CALIBER_WORKFLOW_FILE_ROOT="unconfined")
    with pytest.raises(ServerStartError):
        server.start()
    log = server.log_tail(80)
    assert "reference profile refused (1 violation)" in log, log
    (line,) = _violations(log)
    assert "[workflow_file_root_unconfined]" in line and "CALIBER_WORKFLOW_FILE_ROOT" in line
    with pytest.raises(httpx.TransportError):
        httpx.get(server.base_url + HEALTH_PATH, timeout=2.0)


def test_a_confined_file_root_and_an_unset_one_both_start(
    reference_app: ReferenceApp, tmp_path: Path
) -> None:
    root = tmp_path / "files"
    root.mkdir()
    with _server(reference_app, tmp_path, None, CALIBER_WORKFLOW_FILE_ROOT=str(root)) as server:
        assert httpx.get(server.base_url + HEALTH_PATH, timeout=5.0).status_code == 200
    with _server(reference_app, tmp_path, None) as server:
        assert httpx.get(server.base_url + HEALTH_PATH, timeout=5.0).status_code == 200


# -- a plugin installed after the process started: the registry's own refusal, through the public routes ---------------


def _rows(app: ReferenceApp) -> tuple[int, int]:
    with app.session_factory() as session:
        return (
            session.execute(select(func.count()).select_from(CaliberRefinementJob)).scalar_one(),
            session.execute(select(func.count()).select_from(CaliberVerificationItem)).scalar_one(),
        )


def _optimization_request(app: ReferenceApp) -> httpx.Response:
    headers = app.headers(EDITOR_A, PROJECT_A)
    token = app.client.get(f"{PREFIX}/csrf", headers=headers).json()["data"]["token"]
    return app.client.post(
        f"{PREFIX}/prompts/optimization/runs",
        json={
            "agent_id": "no-such-agent",
            "eval_dataset_id": "EDS-NONE",
            "optimizer_type": "GEPA",
            "scorers": [{"name": "Correctness", "weight": 1.0, "config": {}}],
        },
        headers={**headers, "X-CALIBER-CSRF": token},
    )


def test_a_plugin_installed_after_start_is_refused_by_the_registry_and_writes_nothing(
    reference_app: ReferenceApp, two_workspaces: Workspaces, monkeypatch: pytest.MonkeyPatch
) -> None:
    _need_reference(reference_app)
    registry = optimizer_registry()
    registry.reset_for_tests()
    entries: list[object] = []
    monkeypatch.setattr(entrypoints, "_entry_points", lambda: list(entries))
    monkeypatch.delenv(entrypoints.ALLOWLIST_ENV_VAR, raising=False)
    loads: list[str] = []

    class _Late:
        name = "late"
        value = "late_plugin:declaration"
        dist = type("_Dist", (), {"name": "late-plugin"})()

        def load(self) -> object:
            loads.append("imported")
            raise AssertionError("a refused plugin must not be imported")

    try:
        entries.append(_Late())
        before = _rows(reference_app)

        capabilities = reference_app.client.get(
            f"{PREFIX}/capabilities", headers=reference_app.headers(EDITOR_A, PROJECT_A)
        )
        assert capabilities.status_code == 200, capabilities.text
        extensibility = capabilities.json()["data"]["extensibility"]
        assert extensibility["optimizers"] == []
        (plugin,) = extensibility["plugins"]
        assert plugin["distribution"] == "late-plugin" and plugin["allowlisted"] is False
        assert plugin["error"].startswith(f"{REFUSAL_CODE}: ")

        refused = _optimization_request(reference_app)
        assert refused.status_code == 503, refused.text
        assert refused.json()["detail"].startswith(f"{REFUSAL_CODE}: ")
        assert "late-plugin" in refused.json()["detail"]
        assert _rows(reference_app) == before, "a refused request must write no job or review item"
        assert loads == []

        # Recovery: the plugin is gone (or allowlisted); a restart rebuilds the registry. The same request is then
        # accepted past the optimizer check (it fails on the agent it names, which does not exist).
        entries.clear()
        registry.reset_for_tests()
        accepted = _optimization_request(reference_app)
        assert accepted.status_code != 503, accepted.text
    finally:
        registry.reset_for_tests()
