"""Validated configuration with explicit environment overrides."""

from __future__ import annotations

import math
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
    worktree_dir: Path
    enabled: bool
    repository: str
    base_branch: str
    daily_time: str
    timezone: str
    project_view_number: int
    daily_done_target: int
    max_tasks_per_run: int
    max_workers: int
    auto_merge: bool
    max_review_cycles: int
    max_fix_attempts: int
    max_auto_retries: int
    agent_timeout_seconds: int
    stall_seconds: int
    qa_command_timeout_seconds: int
    ci_timeout_seconds: int
    allow_overlapping_runs: bool
    worker_user: str
    require_owned_paths: bool
    issue_allowlist: tuple[int, ...]
    provider: str
    model: str
    reasoning: str
    max_budget_usd: float
    daily_budget_usd: float
    temperature: str
    prices: dict
    unknown_call_cost_usd: float
    coverage_threshold: float
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
    worktree_dir = Path(value("worktree_dir", state_dir / "worktrees")).expanduser().resolve()
    settings = Settings(
        root=root,
        state_dir=state_dir,
        worktree_dir=worktree_dir,
        enabled=_bool(value("enabled", workflow["enabled"])),
        repository=str(value("repository", workflow["repository"])),
        base_branch=str(value("base_branch", workflow["base_branch"])),
        daily_time=str(value("daily_time", workflow["daily_time"])),
        timezone=str(value("timezone", workflow["timezone"])),
        project_view_number=int(value("project_view_number", workflow["project_view_number"])),
        daily_done_target=int(value("daily_done_target", workflow["daily_done_target"])),
        max_tasks_per_run=int(value("max_tasks_per_run", workflow["max_tasks_per_run"])),
        max_workers=int(value("max_workers", workflow["max_workers"])),
        auto_merge=_bool(value("auto_merge", workflow["auto_merge"])),
        max_review_cycles=int(value("max_review_cycles", workflow["max_review_cycles"])),
        max_fix_attempts=int(value("max_fix_attempts", workflow.get("max_fix_attempts", 0))),
        max_auto_retries=int(value("max_auto_retries", workflow.get("max_auto_retries", 0))),
        agent_timeout_seconds=int(
            value("agent_timeout_seconds", workflow["agent_timeout_seconds"])
        ),
        stall_seconds=int(value("stall_seconds", workflow["stall_seconds"])),
        qa_command_timeout_seconds=int(
            value("qa_command_timeout_seconds", workflow["qa_command_timeout_seconds"])
        ),
        ci_timeout_seconds=int(value("ci_timeout_seconds", workflow["ci_timeout_seconds"])),
        allow_overlapping_runs=_bool(
            value("allow_overlapping_runs", workflow["allow_overlapping_runs"])
        ),
        worker_user=str(value("worker_user", workflow.get("worker_user", ""))),
        require_owned_paths=_bool(
            value("require_owned_paths", workflow.get("require_owned_paths", False))
        ),
        issue_allowlist=tuple(int(n) for n in workflow.get("issue_allowlist", [])),
        provider=str(value("provider", model["provider"])),
        model=str(value("model", model["name"])),
        reasoning=str(value("effort", model["reasoning"])),
        max_budget_usd=float(value("max_budget_usd", model["max_budget_usd"])),
        daily_budget_usd=float(value("daily_budget_usd", model.get("daily_budget_usd", 0))),
        temperature=str(value("temperature", model.get("temperature", "default"))),
        prices={
            key: float(value(f"price_{key}", model.get("prices", {}).get(key, 0.0)))
            for key in ("input", "cached_input", "cache_write", "output")
        },
        unknown_call_cost_usd=float(
            value("unknown_call_cost_usd", model.get("unknown_call_cost_usd", 0.0))
        ),
        coverage_threshold=float(value("coverage_threshold", checks.get("coverage_threshold", 95))),
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
    if (
        settings.project_view_number < 0
        or settings.daily_done_target < 1
        or settings.max_workers < 1
        or settings.max_tasks_per_run < 1
        or settings.max_review_cycles < 1
    ):
        raise ValueError("worker, task and review limits must be positive")
    if not 0 <= settings.max_fix_attempts <= 10:
        raise ValueError("max_fix_attempts must be between 0 and 10")
    if not 0 <= settings.max_auto_retries <= 5:
        raise ValueError("max_auto_retries must be between 0 and 5")
    if settings.stall_seconds < 1 or settings.agent_timeout_seconds <= settings.stall_seconds:
        raise ValueError("agent timeout must exceed stall timeout")
    if not 60 <= settings.qa_command_timeout_seconds <= 14400:
        raise ValueError("QA command timeout must be between one minute and four hours")
    if settings.ci_timeout_seconds < 60:
        raise ValueError("CI timeout must be at least one minute")
    if settings.provider not in {"claude_cli", "codex_cli"}:
        raise ValueError("Only the claude_cli and codex_cli providers are implemented")
    if not math.isfinite(settings.unknown_call_cost_usd) or settings.unknown_call_cost_usd < 0:
        raise ValueError("unknown_call_cost_usd must be finite and nonnegative")
    if any(not math.isfinite(price) or price < 0 for price in settings.prices.values()):
        raise ValueError("model prices must be finite and nonnegative")
    if settings.provider == "codex_cli" and not settings.prices["output"]:
        raise ValueError("codex_cli needs [model.prices] to compute spend from token usage")
    if not 50 <= settings.coverage_threshold <= 100:
        raise ValueError("coverage_threshold must be between 50 and 100")
    if settings.reasoning not in {"low", "medium", "high", "xhigh", "max"}:
        raise ValueError("Unsupported reasoning level")
    if not math.isfinite(settings.max_budget_usd) or settings.max_budget_usd < 0:
        raise ValueError("max_budget_usd must be finite and nonnegative; zero is uncapped")
    if not math.isfinite(settings.daily_budget_usd) or settings.daily_budget_usd < 0:
        raise ValueError("daily_budget_usd must be finite and nonnegative; zero is uncapped")
    # Claude Code CLI has no stable temperature or output-token flags. Fail closed.
    if settings.temperature != "default" or settings.max_output_tokens:
        raise ValueError("Claude CLI cannot enforce temperature or output-token limits")
    if settings.state_dir == settings.root or settings.root in settings.state_dir.parents:
        raise ValueError("state_dir must be outside the repository")
    if settings.worktree_dir == settings.root or settings.root in settings.worktree_dir.parents:
        raise ValueError("worktree_dir must be outside the repository")
    if settings.worker_user and (
        settings.worktree_dir == settings.state_dir
        or settings.state_dir in settings.worktree_dir.parents
    ):
        raise ValueError("isolated worker worktrees must be outside the private state directory")
    if settings.worker_user and not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", settings.worker_user):
        raise ValueError("invalid isolated worker user")
    return settings
