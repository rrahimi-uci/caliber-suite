import json
import os
import runpy
import subprocess
import sys

from autocd import cli


def test_cli_plan_run_tick_status(settings, snapshot, monkeypatch, capsys):
    data, _ = snapshot
    calls = []

    class FakeEngine:
        def __init__(self, loaded):
            self.github = self

        def snapshot(self):
            return data

        def run(self, *, scheduled):
            calls.append(scheduled)
            return {"scheduled": scheduled}

    monkeypatch.setattr(cli, "load", lambda path: settings)
    monkeypatch.setattr(cli, "Engine", FakeEngine)
    for action in ("plan", "run", "tick", "status"):
        monkeypatch.setattr(sys, "argv", ["caliber-autocd", action])
        cli.main()
        result = json.loads(capsys.readouterr().out)
        if action == "plan":
            assert result["selected"] == [12]
        elif action == "status":
            assert result == []
        else:
            assert result["scheduled"] == (action == "tick")
    assert calls == [False, True]


def test_module_entrypoint_status(tmp_path):
    env = {**os.environ, "AUTOCD_STATE_DIR": str(tmp_path / "state")}
    result = subprocess.run(
        [sys.executable, "-m", "autocd.cli", "status"],
        cwd=os.getcwd(),
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout) == []


def test_module_entrypoint_in_process(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AUTOCD_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(sys, "argv", ["caliber-autocd", "status"])
    runpy.run_module("autocd.cli", run_name="__main__")
    assert json.loads(capsys.readouterr().out) == []
