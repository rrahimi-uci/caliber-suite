import shlex
from dataclasses import replace
from pathlib import Path

import pytest

from autocd.config import load


def test_defaults_and_environment(settings):
    assert load().project_view_number == 0
    assert load().daily_time == "04:00"
    assert load().daily_done_target == 6
    assert load().max_tasks_per_run == 6
    assert load().auto_merge is True
    setup = load().setup_commands
    assert "--seed" in shlex.split(setup[0])
    assert shlex.split(setup[1]) == ["caliber/.venv/bin/python", "-m", "pip", "--version"]
    assert load().qa_command_timeout_seconds == 7200
    assert (
        load().base_checks[-1] == "scripts/ci-local.sh docs-validation lint type-check test-smoke"
    )
    assert load().issue_checks[821] == (
        "caliber/.venv/bin/python -m pytest caliber/tests/test_routes_verification_queue.py caliber/tests/test_migrations.py --no-cov -q",
        "scripts/ci-local.sh backend-core",
    )
    assert settings.provider == "claude_cli"
    assert settings.max_workers == 2
    changed = load(environ={"AUTOCD_ENABLED": "true", "AUTOCD_DAILY_TIME": "23:30"})
    assert changed.enabled and changed.daily_time == "23:30"
    assert load(environ={"AUTOCD_ENABLED": "false"}).enabled is False


@pytest.mark.parametrize(
    "env",
    [
        {"AUTOCD_ENABLED": "maybe"},
        {"AUTOCD_DAILY_TIME": "25:00"},
        {"AUTOCD_TIMEZONE": "No/Such_Zone"},
        {"AUTOCD_REPOSITORY": "invalid"},
        {"AUTOCD_BASE_BRANCH": "main;evil"},
        {"AUTOCD_PROVIDER": "unknown"},
        {"AUTOCD_EFFORT": "unsupported"},
        {"AUTOCD_TEMPERATURE": "0.2"},
        {"AUTOCD_MAX_OUTPUT_TOKENS": "100"},
        {"AUTOCD_MAX_WORKERS": "0"},
        {"AUTOCD_PROJECT_VIEW_NUMBER": "-1"},
        {"AUTOCD_DAILY_DONE_TARGET": "0"},
        {"AUTOCD_AUTO_MERGE": "maybe"},
        {"AUTOCD_WORKER_USER": "bad;user"},
        {"AUTOCD_STALL_SECONDS": "999999"},
        {"AUTOCD_CI_TIMEOUT_SECONDS": "2"},
        {"AUTOCD_QA_COMMAND_TIMEOUT_SECONDS": "59"},
        {"AUTOCD_QA_COMMAND_TIMEOUT_SECONDS": "14401"},
    ],
)
def test_invalid_settings_fail_closed(env):
    with pytest.raises((ValueError, KeyError)):
        load(environ=env)


def test_state_must_be_outside_repository():
    root = Path(__file__).resolve().parents[2]
    with pytest.raises(ValueError, match="outside"):
        load(environ={"AUTOCD_STATE_DIR": str(root / "state")})
    with pytest.raises(ValueError, match="worktree_dir"):
        load(environ={"AUTOCD_WORKTREE_DIR": str(root / "worktrees")})


def test_worker_name_is_checked_after_private_state_boundary(tmp_path):
    with pytest.raises(ValueError, match="worker user"):
        load(
            environ={
                "AUTOCD_WORKER_USER": "bad;user",
                "AUTOCD_WORKTREE_DIR": str(tmp_path / "worktrees"),
            }
        )


def test_model_setting_is_replaceable(settings):
    assert replace(settings, model="claude-sonnet-5-5").model == "claude-sonnet-5-5"
