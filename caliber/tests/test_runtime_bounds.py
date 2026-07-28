"""Regression tests for two unbounded/silent runtime behaviours.

From the review's architecture section:

* "Direct parallel branches size a ``ThreadPoolExecutor`` to the branch count
  without a configured cap, while manifests permit large graphs." A manifest
  authoring choice could therefore spawn an unbounded number of threads.
* "Cron scheduling ... silently falls back to UTC for an invalid timezone." The
  schedule keeps firing, just at the wrong hour, with no error anywhere.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import caliber.workflows.runtime as runtime_mod
from caliber.config import CaliberConfig
from caliber.workflows.compiler import build_ir
from caliber.workflows.manifest import (
    StartTrigger,
    WaitUntilNode,
    parse_manifest,
    validate_timezone,
)
from caliber.workflows.runtime import (
    DEFAULT_PARALLEL_BRANCH_MAX_WORKERS,
    FakeWorkflowExecutor,
    RuntimePlan,
    execute,
)
from tests.workflow_helpers import fake_resolver, make_manifest

# ---------------------------------------------------------------------------
# Bounded parallel-branch fan-out
# ---------------------------------------------------------------------------


def _three_branch_manifest() -> dict[str, object]:
    """Start → parallel → three agents → join(all) → output."""
    data = make_manifest()
    data["nodes"]["parallel"] = {
        "id": "parallel",
        "type": "parallel",
        "inputs": {"input": {"type": "string"}},
        "outputs": {"output": {"type": "string"}},
    }
    for name in ("agent_two", "agent_three"):
        data["nodes"][name] = {
            "id": name,
            "type": "agent",
            "name": name.replace("_", "-"),
            "model": "inherit",
            "instructions": {"type": "inline", "text": "You are helpful."},
            "tools": [],
            "inputs": {"input": {"type": "string"}},
            "outputs": {"final_output": {"type": "string"}},
        }
    data["nodes"]["join_all"] = {
        "id": "join_all",
        "type": "join",
        "mode": "all",
        "inputs": {
            "left": {"type": "string"},
            "middle": {"type": "string"},
            "right": {"type": "string"},
        },
        "outputs": {"output": {"type": "string"}, "merged": {"type": "structured"}},
    }
    data["edges"] = [
        {"id": "e0", "from": "start", "to": "parallel", "map": {"msg": "input"}},
        {"id": "e1", "from": "parallel", "to": "agent", "map": {"output": "input"}},
        {"id": "e2", "from": "parallel", "to": "agent_two", "map": {"output": "input"}},
        {"id": "e3", "from": "parallel", "to": "agent_three", "map": {"output": "input"}},
        {"id": "e4", "from": "agent", "to": "join_all", "map": {"final_output": "left"}},
        {"id": "e5", "from": "agent_two", "to": "join_all", "map": {"final_output": "middle"}},
        {"id": "e6", "from": "agent_three", "to": "join_all", "map": {"final_output": "right"}},
        {"id": "e7", "from": "join_all", "to": "final", "map": {"output": "response"}},
    ]
    return data


def _plan(manifest_dict: dict[str, object], **plan_kwargs: object) -> RuntimePlan:
    resolver = fake_resolver()
    ir = build_ir(parse_manifest(manifest_dict), resolver, version="bounds")
    return RuntimePlan(ir=ir, resolver=resolver, **plan_kwargs)  # type: ignore[arg-type]


def _record_pool_sizes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Capture every ``max_workers`` the runtime asks a thread pool for."""
    sizes: list[int] = []
    real = runtime_mod.ThreadPoolExecutor

    def _factory(*args: object, **kwargs: object) -> object:
        workers = kwargs.get("max_workers")
        if isinstance(workers, int):
            sizes.append(workers)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(runtime_mod, "ThreadPoolExecutor", _factory)
    return sizes


def test_the_plan_carries_a_default_branch_worker_cap() -> None:
    assert _plan(make_manifest()).parallel_branch_max_workers == (
        DEFAULT_PARALLEL_BRANCH_MAX_WORKERS
    )
    assert DEFAULT_PARALLEL_BRANCH_MAX_WORKERS >= 1


def test_the_branch_cap_is_configurable() -> None:
    assert (
        CaliberConfig(workflow_parallel_branch_max_workers=3).workflow_parallel_branch_max_workers
        == 3
    )


@pytest.mark.parametrize("value", [0, -1, 65])
def test_the_branch_cap_rejects_out_of_range_values(value: int) -> None:
    """A cap of 0 would deadlock the pool; an unbounded one defeats the fix."""
    with pytest.raises(ValidationError):
        CaliberConfig(workflow_parallel_branch_max_workers=value)


def test_a_wide_fan_out_is_capped_rather_than_one_thread_per_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the pool was sized to the branch count, so a wide graph spawned
    one thread per branch with no ceiling."""
    sizes = _record_pool_sizes(monkeypatch)
    result = execute(
        _plan(_three_branch_manifest(), parallel_branch_max_workers=2),
        "hello",
        executor=FakeWorkflowExecutor(),
    )
    assert result.status == "completed"
    # Three branches, cap of two → the pool is two, and every branch still ran.
    assert 2 in sizes
    assert 3 not in sizes
    assert {step.node_id for step in result.steps} >= {"agent", "agent_two", "agent_three"}


def test_the_cap_does_not_over_allocate_for_a_narrow_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``min(branches, cap)``: a two-branch graph must not be handed eight threads."""
    sizes = _record_pool_sizes(monkeypatch)
    data = _three_branch_manifest()
    # Drop the third branch.
    del data["nodes"]["agent_three"]
    data["nodes"]["join_all"]["inputs"].pop("right")
    data["edges"] = [e for e in data["edges"] if e["id"] not in {"e3", "e6"}]

    result = execute(
        _plan(data, parallel_branch_max_workers=8), "hello", executor=FakeWorkflowExecutor()
    )
    assert result.status == "completed"
    assert 2 in sizes
    assert 8 not in sizes


# ---------------------------------------------------------------------------
# Cron / wait timezone validation
# ---------------------------------------------------------------------------


def test_an_unknown_timezone_is_rejected_at_authoring_time() -> None:
    """Regression: the scheduler resolved an unknown zone to UTC, so a mistyped
    ``Europe/Londn`` fired the schedule at the wrong hour with no error."""
    with pytest.raises(ValidationError) as excinfo:
        StartTrigger(mode="cron", cron="0 9 * * *", timezone="Europe/Londn")
    assert "unknown timezone" in str(excinfo.value)


@pytest.mark.parametrize("tz", ["UTC", "America/Los_Angeles", "Europe/London", "Asia/Tokyo"])
def test_real_iana_zones_are_accepted(tz: str) -> None:
    assert StartTrigger(mode="cron", cron="0 9 * * *", timezone=tz).timezone == tz


def test_an_empty_timezone_means_utc() -> None:
    assert validate_timezone("") == "UTC"
    assert validate_timezone("   ") == "UTC"


def test_a_wait_until_node_validates_its_timezone_too() -> None:
    """A wait boundary in an unresolvable zone resumes at the wrong wall-clock
    time rather than failing."""
    with pytest.raises(ValidationError):
        WaitUntilNode(
            id="wait",
            type="wait_until",
            wait_until="2026-01-01T09:00:00",
            timezone="Not/AZone",
        )
    ok = WaitUntilNode(
        id="wait",
        type="wait_until",
        wait_until="2026-01-01T09:00:00",
        timezone="America/New_York",
    )
    assert ok.timezone == "America/New_York"


def test_a_manual_trigger_is_not_forced_to_carry_a_valid_timezone() -> None:
    """Only cron schedules actually consume the field; validating it for a manual
    trigger would reject manifests where the value is inert."""
    assert StartTrigger(mode="manual", timezone="Not/AZone").timezone == "Not/AZone"
