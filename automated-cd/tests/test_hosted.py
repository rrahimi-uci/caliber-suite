import runpy
from pathlib import Path

import pytest

from autocd import hosted
from autocd.state import Store


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
    hosted.run_hosted("schedule", max_seconds=120, poll_seconds=10, refill_seconds=0)
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
    hosted.run_hosted("manual", max_seconds=10, poll_seconds=10, refill_seconds=0)
    assert "will resume" in capsys.readouterr().out
    with pytest.raises(ValueError, match="AUTOCD_RUN_MODE"):
        hosted.run_hosted("invalid")


def test_hosted_refills_after_pr_reconciliation_without_retrying_issue(monkeypatch):
    calls = []

    class FakeEngine:
        def __init__(self, settings):
            self.settings = settings

        def run(self, *, scheduled, exclude_issues=frozenset(), auto_retry=False):
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
    hosted.run_hosted("schedule", max_seconds=120, poll_seconds=10, refill_seconds=0)
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


def test_hosted_entrypoint_hides_uncaught_task_details(monkeypatch, capsys):
    class FakeEngine:
        def __init__(self, settings):
            self.settings = settings

        def run(self, *, scheduled):
            raise RuntimeError("example-secret-from-command")

    monkeypatch.setattr(
        "autocd.config.load", lambda: type("Settings", (), {"max_tasks_per_run": 6})()
    )
    monkeypatch.setattr("autocd.engine.Engine", FakeEngine)
    monkeypatch.setenv("AUTOCD_RUN_MODE", "manual")
    with pytest.raises(SystemExit) as stopped:
        runpy.run_module("autocd.hosted", run_name="__main__")
    assert stopped.value.code == 1
    assert "example-secret" not in capsys.readouterr().err


def test_hosted_fails_after_blocked_task_but_still_monitors(monkeypatch):
    calls = []

    class FakeEngine:
        def __init__(self, settings):
            self.settings = settings

        def run(self, *, scheduled, exclude_issues=frozenset(), auto_retry=False):
            calls.append((scheduled, exclude_issues))
            if scheduled:
                return {"selected": [821], "results": [{"issue": 821, "status": "blocked"}]}
            return {"selected": [], "results": []}

        def monitor(self):
            calls.append("monitor")
            return {"pending": 0}

    monkeypatch.setattr(hosted, "load", lambda: type("Settings", (), {"max_tasks_per_run": 6})())
    monkeypatch.setattr(hosted, "Engine", FakeEngine)
    monkeypatch.setattr(hosted.time, "monotonic", lambda: 0)
    with pytest.raises(RuntimeError, match="blocked issues: \\[821\\]"):
        hosted.run_hosted("schedule", max_seconds=120, poll_seconds=10, refill_seconds=0)
    assert calls == [(True, frozenset()), "monitor", (False, frozenset({821}))]


def test_hosted_fails_on_blocked_recovery(monkeypatch):
    class FakeEngine:
        def __init__(self, settings):
            self.settings = settings

        def run(self, *, scheduled, exclude_issues=frozenset(), auto_retry=False):
            if not scheduled:
                return {"selected": []}
            return {"status": "waiting_on_published_prs", "recovered": []}

        def monitor(self):
            return {"pending": 0, "recovered": [{"issue": 1110, "status": "blocked_ci_or_review"}]}

    monkeypatch.setattr(hosted, "load", lambda: type("Settings", (), {"max_tasks_per_run": 6})())
    monkeypatch.setattr(hosted, "Engine", FakeEngine)
    monkeypatch.setattr(hosted.time, "monotonic", lambda: 0)
    with pytest.raises(RuntimeError, match="blocked issues: \\[1110\\]"):
        hosted.run_hosted("schedule", max_seconds=120, poll_seconds=10, refill_seconds=0)


def test_hosted_logs_hide_untrusted_errors(monkeypatch, capsys):
    secret = "example-secret-from-review"

    class FakeEngine:
        def __init__(self, settings):
            self.settings = settings

        def run(self, *, scheduled, exclude_issues=frozenset(), auto_retry=False):
            if scheduled:
                return {
                    "selected": [821],
                    "results": [{"issue": 821, "status": "blocked", "error": secret}],
                }
            return {"selected": []}

        def monitor(self):
            return {"pending": 0}

    monkeypatch.setattr(hosted, "load", lambda: type("Settings", (), {"max_tasks_per_run": 6})())
    monkeypatch.setattr(hosted, "Engine", FakeEngine)
    monkeypatch.setattr(hosted.time, "monotonic", lambda: 0)
    with pytest.raises(RuntimeError, match="blocked issues"):
        hosted.run_hosted("schedule", max_seconds=120, poll_seconds=10, refill_seconds=0)
    assert secret not in capsys.readouterr().out


def test_hosted_fails_when_pr_checks_remain_unverifiable(monkeypatch):
    class FakeEngine:
        def __init__(self, settings):
            self.settings = settings

        def run(self, *, scheduled, exclude_issues=frozenset(), auto_retry=False):
            return (
                {"status": "waiting_on_published_prs", "recovered": []}
                if scheduled
                else {"selected": []}
            )

        def monitor(self):
            return {"pending": 1, "recovered": [{"issue": 12, "status": "pr_check_error"}]}

    monkeypatch.setattr(hosted, "load", lambda: type("Settings", (), {"max_tasks_per_run": 6})())
    monkeypatch.setattr(hosted, "Engine", FakeEngine)
    monkeypatch.setattr(hosted.time, "monotonic", lambda: 0)
    with pytest.raises(RuntimeError, match="checks could not be verified"):
        hosted.run_hosted("schedule", max_seconds=10, poll_seconds=10, refill_seconds=0)


def test_hosted_manual_retry_passes_only_explicit_issue(monkeypatch):
    seen = []

    class FakeEngine:
        def __init__(self, settings):
            self.settings = settings

        def run(self, *, scheduled, retry_issue=None):
            seen.append((scheduled, retry_issue))
            return {"selected": []}

        def monitor(self):
            return {"pending": 0}

    monkeypatch.setattr(hosted, "load", lambda: type("Settings", (), {"max_tasks_per_run": 6})())
    monkeypatch.setattr(hosted, "Engine", FakeEngine)
    monkeypatch.setattr(hosted.time, "monotonic", lambda: 0)
    monkeypatch.setenv("AUTOCD_RETRY_ISSUE", "821")
    hosted.run_hosted("manual", max_seconds=120, poll_seconds=10, refill_seconds=0)
    assert seen == [(False, 821)]
    with pytest.raises(ValueError, match="positive issue number"):
        hosted.run_hosted("schedule", max_seconds=120, poll_seconds=10, refill_seconds=0)
    monkeypatch.setenv("AUTOCD_RETRY_ISSUE", "nonsense")
    with pytest.raises(ValueError, match="positive issue number"):
        hosted.run_hosted("manual", max_seconds=120, poll_seconds=10, refill_seconds=0)


def test_manual_trial_fails_when_project_has_fewer_tasks(settings, monkeypatch, capsys):
    class FakeEngine:
        def __init__(self, settings):
            self.settings = settings
            self.github = self

        def snapshot(self):
            return {"view": {"number": 0, "filter": ""}}

        def _daily_done(self, snapshot, label, day):
            return []

        def run(self, *, scheduled):
            assert not scheduled
            return {"status": "no_executable_tasks", "selected": []}

        def monitor(self):
            return {"pending": 0}

    monkeypatch.setattr(hosted, "load", lambda: settings)
    monkeypatch.setattr(hosted, "Engine", FakeEngine)
    monkeypatch.setenv("AUTOCD_TRIAL_TASKS", "5")
    monkeypatch.delenv("AUTOCD_RETRY_ISSUE", raising=False)

    with pytest.raises(RuntimeError, match="requested 5 task attempts but selected 0"):
        hosted.run_hosted("manual", max_seconds=120, poll_seconds=10, refill_seconds=0)

    assert '"status": "shortfall"' in capsys.readouterr().out


def test_hosted_retry_never_refills_unrelated_tasks(monkeypatch):
    calls = []

    class FakeEngine:
        def __init__(self, settings):
            self.settings = settings

        def run(self, *, scheduled, retry_issue=None):
            calls.append((scheduled, retry_issue))
            return {"selected": [821], "results": [{"issue": 821, "status": "done"}]}

        def monitor(self):
            return {"pending": 0}

    monkeypatch.setattr(hosted, "load", lambda: type("Settings", (), {"max_tasks_per_run": 6})())
    monkeypatch.setattr(hosted, "Engine", FakeEngine)
    monkeypatch.setattr(hosted.time, "monotonic", lambda: 0)
    monkeypatch.setenv("AUTOCD_RETRY_ISSUE", "821")
    hosted.run_hosted("manual", max_seconds=120, poll_seconds=10, refill_seconds=0)
    assert calls == [(False, 821)]


def test_measured_manual_trial_allows_five_new_attempts_after_daily_cap(
    settings, tmp_path: Path, monkeypatch
):
    store = Store(settings.state_dir)
    for index in range(6):
        run = f"prior-{index}"
        store.start(run)
        store.add_task(run, 100 + index, "release:Beta", f"prior/{index}", tmp_path)
        if index < 2:
            store.phase(run, 100 + index, "done")
    store.db.close()
    calls = []
    initialized = []

    class FakeEngine:
        def __init__(self, chosen):
            self.settings = chosen
            self.github = self
            initialized.append(chosen)

        def snapshot(self):
            return {"view": {"number": 0, "filter": ""}}

        def _daily_done(self, snapshot, label, day):
            # Human merges can put more Done tasks in Project than the normal
            # ten-attempt cap recorded in coordinator state.
            return list(range(7))

        def run(self, *, scheduled, retry_issue=None, exclude_issues=frozenset(), auto_retry=False):
            calls.append((scheduled, retry_issue, exclude_issues))
            return {"selected": [821 if len(calls) == 1 else 520 + len(calls)]}

        def monitor(self):
            return {"pending": 0}

    monkeypatch.setattr(hosted, "load", lambda: settings)
    monkeypatch.setattr(hosted, "Engine", FakeEngine)
    monkeypatch.setattr(hosted.time, "monotonic", lambda: 0)
    monkeypatch.setenv("AUTOCD_TRIAL_TASKS", "5")
    monkeypatch.setenv("AUTOCD_RETRY_ISSUE", "821")
    hosted.run_hosted("manual", max_seconds=120, poll_seconds=10, refill_seconds=0)
    assert len(calls) == 5
    assert [chosen.max_tasks_per_run for chosen in initialized] == [10, 11]
    assert initialized[-1].daily_done_target == 12
    assert calls[0][:2] == (False, 821)
    assert calls[1] == (False, None, frozenset({821}))
    assert settings.max_tasks_per_run == 10


def test_measured_trial_rejects_schedule_and_oversized_input(monkeypatch):
    monkeypatch.setenv("AUTOCD_TRIAL_TASKS", "5")
    with pytest.raises(ValueError, match="manual GitHub dispatch"):
        hosted.run_hosted("schedule")
    monkeypatch.setenv("AUTOCD_TRIAL_TASKS", "6")
    with pytest.raises(ValueError, match="0 through 5"):
        hosted.run_hosted("manual")


def test_hosted_refill_needs_a_full_task_budget_and_keeps_scheduled_retries(monkeypatch):
    calls = []

    class FakeEngine:
        def __init__(self, settings):
            self.settings = settings

        def run(self, *, scheduled, exclude_issues=frozenset(), auto_retry=False):
            calls.append((scheduled, auto_retry))
            return {"selected": [12] if scheduled else []}

        def monitor(self):
            return {"pending": 0}

    monkeypatch.setattr(hosted, "load", lambda: type("Settings", (), {"max_tasks_per_run": 6})())
    monkeypatch.setattr(hosted, "Engine", FakeEngine)
    clock = iter([0, 0, 0, 0, 0, 0])
    monkeypatch.setattr(hosted.time, "monotonic", lambda: next(clock, 0))
    hosted.run_hosted("schedule", max_seconds=1000, poll_seconds=10, refill_seconds=500)
    assert calls == [(True, False), (False, True)]
    calls.clear()
    # Less than a task's budget left: no new batch is started.
    hosted.run_hosted("schedule", max_seconds=1000, poll_seconds=10, refill_seconds=2000)
    assert calls == [(True, False)]
