"""Validated configuration with explicit environment overrides."""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class Settings:
    root: Path
    state_dir: Path
    enabled: bool
    repository: str
    base_branch: str
    daily_time: str
    timezone: str
    max_tasks_per_run: int
    max_workers: int
    max_review_cycles: int
    agent_timeout_seconds: int
    stall_seconds: int
    ci_timeout_seconds: int
    allow_overlapping_runs: bool
    issue_allowlist: tuple[int, ...]
    provider: str
    model: str
    reasoning: str
    max_budget_usd: float
    temperature: str
    max_output_tokens: int
    base_checks: tuple[str, ...]
    setup_commands: tuple[str, ...]
    required_checks: tuple[str, ...]
    issue_checks: dict[int, tuple[str, ...]]
    issue_paths: dict[int, frozenset[str]]


def _bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if str(value).lower() in {"true", "1", "yes"}:
        return True
    if str(value).lower() in {"false", "0", "no"}:
        return False
    raise ValueError(f"Invalid boolean: {value}")


def load(path: Path | None = None, *, environ: dict[str, str] | None = None) -> Settings:
    env = os.environ if environ is None else environ
    config_path = path or Path(
        env.get("AUTOCD_CONFIG", Path.cwd() / "automated-cd/config/workflow.toml")
    )
    data = tomllib.loads(config_path.read_text())
    workflow, model, checks = data["workflow"], data["model"], data["checks"]

    def value(key: str, default: object) -> object:
        return env.get("AUTOCD_" + key.upper(), default)

    root = Path(env.get("AUTOCD_REPO_ROOT", Path.cwd())).resolve()
    state_dir = (
        Path(value("state_dir", Path.home() / ".local/state/caliber-automated-cd"))
        .expanduser()
        .resolve()
    )
    settings = Settings(
        root=root,
        state_dir=state_dir,
        enabled=_bool(value("enabled", workflow["enabled"])),
        repository=str(value("repository", workflow["repository"])),
        base_branch=str(value("base_branch", workflow["base_branch"])),
        daily_time=str(value("daily_time", workflow["daily_time"])),
        timezone=str(value("timezone", workflow["timezone"])),
        max_tasks_per_run=int(value("max_tasks_per_run", workflow["max_tasks_per_run"])),
        max_workers=int(value("max_workers", workflow["max_workers"])),
        max_review_cycles=int(value("max_review_cycles", workflow["max_review_cycles"])),
        agent_timeout_seconds=int(
            value("agent_timeout_seconds", workflow["agent_timeout_seconds"])
        ),
        stall_seconds=int(value("stall_seconds", workflow["stall_seconds"])),
        ci_timeout_seconds=int(value("ci_timeout_seconds", workflow["ci_timeout_seconds"])),
        allow_overlapping_runs=_bool(
            value("allow_overlapping_runs", workflow["allow_overlapping_runs"])
        ),
        issue_allowlist=tuple(int(n) for n in workflow.get("issue_allowlist", [])),
        provider=str(value("provider", model["provider"])),
        model=str(value("model", model["name"])),
        reasoning=str(value("effort", model["reasoning"])),
        max_budget_usd=float(value("max_budget_usd", model["max_budget_usd"])),
        temperature=str(value("temperature", model.get("temperature", "default"))),
        max_output_tokens=int(value("max_output_tokens", model.get("max_output_tokens", 0))),
        base_checks=tuple(checks["base"]),
        setup_commands=tuple(checks.get("setup", [])),
        required_checks=tuple(checks.get("required", [])),
        issue_checks={int(k): tuple(v) for k, v in data.get("issue_checks", {}).items()},
        issue_paths={int(k): frozenset(v) for k, v in data.get("issue_paths", {}).items()},
    )
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", settings.repository):
        raise ValueError("repository must be owner/name")
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", settings.base_branch):
        raise ValueError("invalid base branch")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", settings.daily_time):
        raise ValueError("daily_time must be HH:MM")
    ZoneInfo(settings.timezone)
    if settings.max_workers < 1 or settings.max_tasks_per_run < 1 or settings.max_review_cycles < 1:
        raise ValueError("worker, task and review limits must be positive")
    if settings.stall_seconds < 1 or settings.agent_timeout_seconds <= settings.stall_seconds:
        raise ValueError("agent timeout must exceed stall timeout")
    if settings.ci_timeout_seconds < 60:
        raise ValueError("CI timeout must be at least one minute")
    if settings.provider != "claude_cli":
        raise ValueError("Only the claude_cli provider is currently implemented")
    if settings.reasoning not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError("Unsupported reasoning level")
    # Claude Code CLI has no stable temperature or output-token flags. Fail closed.
    if settings.temperature != "default" or settings.max_output_tokens:
        raise ValueError("Claude CLI cannot enforce temperature or output-token limits")
    if settings.state_dir == settings.root or settings.root in settings.state_dir.parents:
        raise ValueError("state_dir must be outside the repository")
    return settings
