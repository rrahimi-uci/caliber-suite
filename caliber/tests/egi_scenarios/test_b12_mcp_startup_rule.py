"""B12.3b (#746): a reference process refuses to start while an MCP allowlist holds an entry the profile does not list.

Real CALIBER processes, started the way an operator starts one, on the reference profile (PostgreSQL only: the profile
refuses SQLite). The refusal is the process exiting before it serves anything, with every violation listed by its stable
code and the variable to change; the same environment with both allowlists empty starts and answers.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from tests.egi_scenarios.conftest import ReferenceApp
from tests.egi_scenarios.harness.process import HEALTH_PATH, ServerProcess, ServerStartError

CALIBER_DIR = Path(__file__).resolve().parents[2]
CODE = "mcp_allowlist_not_in_profile"
STDIO = "CALIBER_MCP_STDIO_COMMAND_ALLOWLIST"
REMOTE = "CALIBER_MCP_REMOTE_HOST_ALLOWLIST"


def _server(reference_app: ReferenceApp, tmp_path: Path, **env: str | None) -> ServerProcess:
    """A process on the scenario's reference environment with ``env`` applied (``None`` removes the variable)."""
    if reference_app.dialect != "postgresql":
        pytest.skip("the reference profile, and so its startup refusal, needs PostgreSQL")
    merged = {**reference_app.env, **{k: v for k, v in env.items() if v is not None}}
    for key, value in env.items():
        if value is None:
            merged.pop(key, None)
    return ServerProcess(merged, app_dir=CALIBER_DIR, log_path=tmp_path / "server.log")


@pytest.mark.parametrize(
    ("env", "named"),
    [
        pytest.param({REMOTE: "example.com"}, [REMOTE], id="remote_host"),
        pytest.param({STDIO: "npx"}, [STDIO], id="stdio_command"),
        pytest.param({REMOTE: "localhost,127.0.0.1,::1"}, [REMOTE], id="loopback_hosts"),
        pytest.param({STDIO: "npx", REMOTE: "example.com"}, [STDIO, REMOTE], id="both"),
        # Unset is the shipped default (the interpreter and loopback): not empty, so refused too.
        pytest.param({STDIO: None, REMOTE: None}, [STDIO, REMOTE], id="shipped_defaults"),
    ],
)
def test_an_unlisted_mcp_entry_refuses_startup_before_serving(
    reference_app: ReferenceApp, tmp_path: Path, env: dict[str, str | None], named: list[str]
) -> None:
    server = _server(reference_app, tmp_path, **env)
    with pytest.raises(ServerStartError):
        server.start()
    assert server.exit_code not in (None, 0), "the process must exit, not keep running"
    log = server.log_tail(80)
    assert "reference profile refused (1 violation)" in log, log
    assert f"[{CODE}]" in log, log
    listed = [line for line in log.splitlines() if line.startswith("- ")]
    assert len(listed) == 1, log
    for variable in named:
        assert variable in listed[0]
    for entry in ("example.com", "npx"):
        assert entry not in listed[0], "the refusal names what to change, not what was configured"
    with pytest.raises(httpx.TransportError):
        httpx.get(server.base_url + HEALTH_PATH, timeout=2.0)


def test_empty_mcp_allowlists_start_and_serve(reference_app: ReferenceApp, tmp_path: Path) -> None:
    assert reference_app.env[STDIO] == "" and reference_app.env[REMOTE] == ""
    server = _server(reference_app, tmp_path)
    with server:
        assert httpx.get(server.base_url + HEALTH_PATH, timeout=5.0).status_code == 200
