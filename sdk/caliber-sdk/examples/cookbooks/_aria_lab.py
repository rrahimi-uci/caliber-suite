"""Shared pieces of the four Aria labs (cookbooks 12 to 15).

Each lab script states an intent, lets the server plan it, answers the plan's questions the way a
person would in the Plans page, and then checks what exists afterwards. The parts they all need are
here: the goals dataset, a driver that walks a plan's interactions in a scripted order, and the
refusals every lab provokes (an empty plan, a bad answer, a step that fails, a role that is not
allowed).

Nothing in here talks to a model. Aria's planner is deterministic, so the dataset of goals in
``assets/dataset/goals.jsonl`` is checked exactly against the plan the server draws for each one.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

import httpx

from caliber_sdk import CaliberClient
from caliber_sdk.errors import CaliberAPIError, CaliberNotFoundError

API_PREFIX = "/ajax-api/2.0/mlflow/caliber"

Report = Callable[[str], None]


class VerificationError(AssertionError):
    """A documented outcome was not observed."""


class SetupError(RuntimeError):
    """The deployment is not set up the way the lab needs (a fix, not a defect)."""


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


@dataclass
class Checks:
    """Prints ``ok``/``FAIL`` lines and remembers the failures; :meth:`finish` raises on any."""

    report: Report = print
    failures: list[str] = field(default_factory=list)

    def __call__(self, label: str, ok: bool, detail: Any = "") -> bool:
        self.report(
            f"{'ok  ' if ok else 'FAIL'} {label}" + (f"  [{detail}]" if detail != "" else "")
        )
        if not ok:
            self.failures.append(label)
        return bool(ok)

    def finish(self) -> None:
        if self.failures:
            raise VerificationError(f"{len(self.failures)} check(s) failed: {self.failures}")
        self.report("\nAll documented outcomes verified.")


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def load_json(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def form_values(path: Path) -> dict[str, Any]:
    """A spec file's values as the plan's input form takes them: without its ``notes``."""
    values = load_json(path)
    values.pop("notes", None)
    return values


# ---------------------------------------------------------------------------
# Accounts and refusals
# ---------------------------------------------------------------------------


def refused(call: Callable[[], Any]) -> CaliberAPIError | None:
    """The API error a call raises, or ``None`` when it succeeded."""
    try:
        call()
    except CaliberAPIError as exc:
        return exc
    return None


def sign_in(base_url: str, user_id: str, password: str) -> str:
    """``POST /auth/login``; the session token doubles as a bearer token for scripts."""
    root = base_url.rstrip("/")
    csrf = httpx.get(f"{root}{API_PREFIX}/csrf", timeout=30)
    headers: dict[str, str] = {}
    if csrf.status_code == 200:
        body = csrf.json().get("data") or {}
        if body.get("enabled") and body.get("token"):
            headers["X-CALIBER-CSRF"] = str(body["token"])
    response = httpx.post(
        f"{root}{API_PREFIX}/auth/login",
        json={"user_id": user_id, "password": password},
        headers=headers,
        timeout=30,
    )
    if response.status_code != 200:
        raise SetupError(
            f"could not sign in as {user_id!r}: {response.status_code} {response.text[:200]}. "
            "An account left by an earlier run may have another password: reset it under "
            "Administration, or set the lab's *_PASSWORD variable"
        )
    return str(response.cookies["caliber_session"])


def ensure_accounts(caliber: CaliberClient, users: Iterable[str], password: str) -> None:
    existing = {account.user_id: account for account in caliber.auth.accounts.list()}
    for user in users:
        if user not in existing:
            caliber.auth.accounts.create(user, password)
        elif getattr(existing[user], "disabled", False):
            caliber.auth.accounts.update(user, disabled=False)


def require_admin_and_aria(caliber: CaliberClient) -> str:
    """Return the admin's user id, or say how to fix the deployment."""
    me = caliber.me.get()
    if not me.is_admin:
        raise SetupError("CALIBER_TOKEN must be an admin personal access token")
    try:
        caliber.aria.capabilities()
    except CaliberNotFoundError as exc:
        raise SetupError(
            "Aria is off: start the server with CALIBER_FEATURE_ARIA=true "
            "(scripts/cookbook-env.sh up NAME --flags cookbooks,aria)"
        ) from exc
    return str(me.user_id)


def require_scopes(
    clients: dict[str, CaliberClient], *, has: dict[str, bool], hint: str
) -> dict[str, list[str]]:
    """Check that each account holds (or lacks) ``caliber.operator``; return their scopes."""
    scopes: dict[str, list[str]] = {}
    for user, client in clients.items():
        held = sorted(client.me.get().scopes)
        scopes[user] = held
        if ("caliber.operator" in held) != has[user]:
            raise SetupError(hint)
    return scopes


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def step_keys(detail: Any) -> list[str]:
    return [str(step.capability_key) for step in detail.steps]


def step_status(detail: Any) -> dict[str, str]:
    return {str(step.capability_key): str(step.status) for step in detail.steps}


def pending(caliber: CaliberClient, plan_id: str) -> list[Any]:
    return [i for i in caliber.aria.interactions(plan_id) if i.status == "pending"]


def warnings_of(detail: Any) -> list[str]:
    return [str(item) for item in (detail.extra.get("warnings") or [])]


#: One scripted answer: ``("input", {...})`` fills a form, ``("input", "skip")`` skips the step,
#: ``("permission", "approve")`` and ``("permission", "deny")`` answer an Approve / Deny prompt.
Move = tuple[str, Any]


def walk(
    caliber: CaliberClient, plan_id: str, moves: Sequence[Move]
) -> tuple[Any, list[tuple[str, str]]]:
    """Answer a paused plan's questions in order, the way a person clicking through would.

    Each move names the kind of question it expects next, so a plan that asks something else (or
    nothing) fails loudly instead of being answered blindly. Returns the plan as it ended and the
    ``(kind, capability)`` of every question answered.
    """
    trail: list[tuple[str, str]] = []
    detail = caliber.aria.get_plan(plan_id)
    for kind, action in moves:
        waiting = pending(caliber, plan_id)
        if len(waiting) != 1 or waiting[0].kind != kind:
            raise VerificationError(
                f"expected one pending {kind!r} question, found "
                f"{[(i.kind, i.step_id) for i in waiting]} (plan {detail.plan.status})"
            )
        question = waiting[0]
        step = next(s for s in detail.steps if s.step_id == question.step_id)
        trail.append((kind, str(step.capability_key)))
        if isinstance(action, dict):
            detail = caliber.aria.answer(question.interaction_id, inputs=action)
        elif action in ("approve", "deny", "skip"):
            detail = caliber.aria.answer(question.interaction_id, approved=action == "approve")
        else:  # pragma: no cover - a typo in a lab script
            raise ValueError(f"unknown move {action!r}")
    return detail, trail


def start(caliber: CaliberClient, goal: str, *, autonomy: str) -> Any:
    """State the goal, approve the drawn plan and execute it, to its first question."""
    drawn = caliber.aria.create_plan(goal, autonomy=autonomy)
    caliber.aria.approve_plan(drawn.plan.plan_id)
    return caliber.aria.execute_plan(drawn.plan.plan_id)


def run_plan(
    caliber: CaliberClient, goal: str, *, autonomy: str, moves: Sequence[Move]
) -> tuple[Any, list[tuple[str, str]]]:
    """:func:`start` and then :func:`walk` through every question."""
    first = start(caliber, goal, autonomy=autonomy)
    return walk(caliber, first.plan.plan_id, moves)


def check_goals(
    caliber: CaliberClient, goals: list[dict[str, Any]]
) -> tuple[list[str], list[str], list[str]]:
    """Draw a plan for every goal and compare it with the dataset.

    Returns ``(mismatches, unapprovable_failures, not_cancelled)``: goals whose plan differs from
    ``expected_steps``; goals that drew no steps but could still be approved (or had no warning);
    drafts that did not cancel. Every plan is a draft and is cancelled again, so nothing runs.
    """
    mismatches: list[str] = []
    empty_problems: list[str] = []
    not_cancelled: list[str] = []
    for goal in goals:
        drawn = caliber.aria.create_plan(goal["goal"])
        keys = step_keys(drawn)
        if keys != goal["expected_steps"]:
            mismatches.append(f"{goal['id']}: drew {keys}, expected {goal['expected_steps']}")
        if not goal["expected_steps"]:
            error = refused(partial(caliber.aria.approve_plan, drawn.plan.plan_id))
            if error is None or error.status_code != 409 or not warnings_of(drawn):
                empty_problems.append(goal["id"])
        cancelled = caliber.aria.update_plan(drawn.plan.plan_id, status="cancelled")
        if cancelled.plan.status != "cancelled":
            not_cancelled.append(goal["id"])
    return mismatches, empty_problems, not_cancelled


def check_goals_report(
    caliber: CaliberClient, goals: list[dict[str, Any]], expected_count: int, check: Checks
) -> None:
    """:func:`check_goals`, reported as the three checks every Aria lab starts with."""
    mismatches, empty_problems, not_cancelled = check_goals(caliber, goals)
    no_steps = sum(1 for goal in goals if not goal["expected_steps"])
    agreed = f"{len(goals) - len(mismatches)}/{len(goals)} {goals_summary(goals)}"
    check(
        "the planner drew the documented plan for every goal",
        len(goals) == expected_count and not mismatches,
        mismatches[:3] or agreed,
    )
    check(
        "a goal that drew no steps cannot be approved (409) and says why",
        no_steps > 0 and not empty_problems,
        empty_problems or f"{no_steps} goals",
    )
    check(
        "every draft was cancelled; nothing ran",
        not not_cancelled,
        not_cancelled or f"{len(goals)} drafts",
    )


def goals_summary(goals: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for goal in goals:
        counts[goal["outcome"]] = counts.get(goal["outcome"], 0) + 1
    return dict(sorted(counts.items()))


def env_password(name: str, default: str) -> str:
    return os.environ.get(name, default)
