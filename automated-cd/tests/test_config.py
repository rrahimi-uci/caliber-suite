from dataclasses import replace
from pathlib import Path

import pytest

from autocd.config import load


def test_defaults_and_environment(settings):
    assert settings.provider == "claude_cli"
    assert settings.max_workers == 2
    changed = load(environ={"AUTOCD_ENABLED": "true", "AUTOCD_DAILY_TIME": "23:30"})
    assert changed.enabled and changed.daily_time == "23:30"


@pytest.mark.parametrize(
    "env",
    [
        {"AUTOCD_ENABLED": "maybe"},
        {"AUTOCD_DAILY_TIME": "25:00"},
        {"AUTOCD_TIMEZONE": "No/Such_Zone"},
        {"AUTOCD_REPOSITORY": "invalid"},
        {"AUTOCD_BASE_BRANCH": "main;evil"},
        {"AUTOCD_PROVIDER": "unknown"},
        {"AUTOCD_TEMPERATURE": "0.2"},
        {"AUTOCD_MAX_OUTPUT_TOKENS": "100"},
        {"AUTOCD_MAX_WORKERS": "0"},
        {"AUTOCD_STALL_SECONDS": "999999"},
        {"AUTOCD_CI_TIMEOUT_SECONDS": "2"},
    ],
)
def test_invalid_settings_fail_closed(env):
    with pytest.raises((ValueError, KeyError)):
        load(environ=env)


def test_state_must_be_outside_repository():
    root = Path(__file__).resolve().parents[2]
    with pytest.raises(ValueError, match="outside"):
        load(environ={"AUTOCD_STATE_DIR": str(root / "state")})


def test_model_setting_is_replaceable(settings):
    assert replace(settings, model="claude-sonnet-5-5").model == "claude-sonnet-5-5"
