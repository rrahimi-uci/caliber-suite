import runpy

import pytest

from autocd import hosted


def test_hosted_starts_and_monitors_until_clear(monkeypatch, capsys):
    events = []

    class FakeEngine:
        def __init__(self, settings):
            self.settings = settings
            events.append("init")

        def run(self, *, scheduled):
            events.append(("run", scheduled))
            return {"status": "started"}

        def monitor(self):
            events.append("monitor")
            return {"pending": 1 if events.count("monitor") == 1 else 0}

    monkeypatch.setattr(hosted, "load", lambda: type("Settings", (), {"max_tasks_per_run": 6})())
    monkeypatch.setattr(hosted, "Engine", FakeEngine)
    monkeypatch.setattr(hosted.time, "monotonic", lambda: 0)
    monkeypatch.setattr(hosted.time, "sleep", lambda seconds: events.append(("sleep", seconds)))
    hosted.run_hosted("schedule", max_seconds=120, poll_seconds=10)
    assert events == ["init", ("run", True), "monitor", ("sleep", 10), "monitor"]
    assert '"pending": 0' in capsys.readouterr().out


def test_hosted_manual_deadline_and_invalid_mode(monkeypatch, capsys):
    class FakeEngine:
        def __init__(self, settings):
            self.settings = settings

        def run(self, *, scheduled):
            assert not scheduled
            return {}

        def monitor(self):
            return {"pending": 1}

    monkeypatch.setattr(hosted, "load", lambda: type("Settings", (), {"max_tasks_per_run": 6})())
    monkeypatch.setattr(hosted, "Engine", FakeEngine)
    monkeypatch.setattr(hosted.time, "monotonic", lambda: 0)
    hosted.run_hosted("manual", max_seconds=10, poll_seconds=10)
    assert "will resume" in capsys.readouterr().out
    with pytest.raises(ValueError, match="AUTOCD_RUN_MODE"):
        hosted.run_hosted("invalid")


def test_hosted_refills_after_pr_reconciliation_without_retrying_issue(monkeypatch):
    calls = []

    class FakeEngine:
        def __init__(self, settings):
            self.settings = settings

        def run(self, *, scheduled, exclude_issues=frozenset()):
            calls.append((scheduled, exclude_issues))
            if scheduled:
                return {"selected": [12], "remaining_to_target": 6}
            if exclude_issues == frozenset({12}):
                return {"selected": [13], "remaining_to_target": 5}
            return {"selected": [], "remaining_to_target": 4}

        def monitor(self):
            return {"pending": 0}

    monkeypatch.setattr(hosted, "load", lambda: type("Settings", (), {"max_tasks_per_run": 6})())
    monkeypatch.setattr(hosted, "Engine", FakeEngine)
    monkeypatch.setattr(hosted.time, "monotonic", lambda: 0)
    hosted.run_hosted("schedule", max_seconds=120, poll_seconds=10)
    assert calls == [
        (True, frozenset()),
        (False, frozenset({12})),
        (False, frozenset({12, 13})),
    ]


def test_hosted_module_entrypoint_uses_environment(monkeypatch):
    class FakeEngine:
        def __init__(self, settings):
            self.settings = settings

        def run(self, *, scheduled):
            assert not scheduled
            return {}

        def monitor(self):
            return {"pending": 0}

    monkeypatch.setattr(
        "autocd.config.load", lambda: type("Settings", (), {"max_tasks_per_run": 6})()
    )
    monkeypatch.setattr("autocd.engine.Engine", FakeEngine)
    monkeypatch.setenv("AUTOCD_RUN_MODE", "manual")
    runpy.run_module("autocd.hosted", run_name="__main__")
