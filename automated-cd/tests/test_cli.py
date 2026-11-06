import json
import os
import runpy
import subprocess
import sys

from autocd import cli
from autocd.state import Store


def test_cli_plan_run_tick_status(settings, snapshot, monkeypatch, capsys):
    data, _ = snapshot
    calls = []

    class FakeEngine:
        def __init__(self, loaded):
            self.github = self

        def snapshot(self):
            return data

        def _daily_done(self, snapshot, label, day):
            return []

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
            assert not (settings.state_dir / "workflow.sqlite3").exists()
        elif action == "status":
            assert result == []
        else:
            assert result["scheduled"] == (action == "tick")
    assert calls == [False, True]


def test_cli_plan_matches_prior_block_and_daily_attempt_cap(
    settings, snapshot, monkeypatch, capsys
):
    data, _ = snapshot

    class FakeEngine:
        def __init__(self, loaded):
            self.github = self

        def snapshot(self):
            return data

        def _daily_done(self, snapshot, label, day):
            return []

    monkeypatch.setattr(cli, "load", lambda path: settings)
    monkeypatch.setattr(cli, "Engine", FakeEngine)
    monkeypatch.setattr(sys, "argv", ["caliber-autocd", "plan"])
    store = Store(settings.state_dir)

    def attempt(number, phase):
        run = f"run-{number}"
        assert store.start(run)
        store.add_task(run, number, "release:Beta", f"branch-{number}", settings.worktree_dir)
        store.phase(run, number, phase)

    attempt(12, "blocked")
    attempt(13, "done")
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["eligible"] == [14]
    assert result["selected"] == [14]
    assert result["requires_explicit_retry"] == [12]
    assert result["attempted_today"] == [12, 13]
    assert result["remaining_attempts"] == settings.max_tasks_per_run - 2
    assert result["pending_tasks"] == 0

    attempt(99, "claimed")
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["eligible"] == [14]
    assert result["selected"] == []
    assert result["pending_tasks"] == 1
    store.phase("run-99", 99, "done")

    for number in range(100, 100 + settings.max_tasks_per_run - 3):
        attempt(number, "done")
    store.db.close()
    cli.main()
    result = json.loads(capsys.readouterr().out)
    assert result["eligible"] == [14]
    assert result["selected"] == []
    assert result["remaining_attempts"] == 0


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
