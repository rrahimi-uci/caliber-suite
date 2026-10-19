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


# ---------------------------------------------------------------------------
# The real-provider tier
# ---------------------------------------------------------------------------
#
# The Aria labs run in one of two modes, told apart by ``GET /readiness``: on the default stack the
# language model and the evaluator are simulated (Evaluations are refused with a 400); on a stack
# started with ``--real-llm`` they are real. Planning is the same in both (a rules-based planner:
# no model is wired into it); what a real provider adds is Evaluations and judge runs, a working
# Ask Aria chat, and, in lab 15, a calibration job that is actually run.


def is_real(caliber: CaliberClient) -> bool:
    """True when neither the language model nor the evaluator is simulated (``GET /readiness``)."""
    readiness = caliber.raw.get("/readiness")
    return not ({"llm", "eval"} & set(readiness.get("simulated") or []))


def http_timeout() -> float:
    """Seconds one request may take. An evaluation scores every row inside one request."""
    return float(os.environ.get("COOKBOOK_ARIA_HTTP_TIMEOUT", "900"))


#: The answerer under test in the real evaluations: it is shown the policy text and told to stay
#: inside it. ``{{ evidence }}`` is filled from each test set row; the row's question is the
#: user's turn. (Evaluations with the default target send the model only the question, never the
#: evidence, so a faithfulness judge over that target would grade answers the model never had the
#: sources for.)
GROUNDED_PROMPT = (
    "You answer customer questions for a company's support team. Use ONLY the policy text "
    "below. State what it says and nothing more; if it does not cover the question, say so and "
    "refer the customer to a person. Do not give medical, legal or financial advice.\n\n"
    "Policy text:\n{{ evidence }}"
)

#: The control: it never sees the policy and is told to sound sure, so it commits to details the
#: policy does not support. A faithfulness judge that is working scores it lower than the
#: grounded one.
CARELESS_PROMPT = (
    "You are a confident, friendly support agent. Answer from your general knowledge of how "
    "online stores usually work, in two or three sentences, and always commit to a specific "
    "number of days, amounts or steps. Never say you are unsure and never mention a policy."
)


def create_prompt(caliber: CaliberClient, name: str, template: str) -> str:
    """Create a prompt and return its ``name@version`` reference (what Evaluations take)."""
    created = caliber.prompts.create(name, template)
    return f"{created['name']}@{created['version']}"


def judged_scorers(judge_id: str) -> list[str]:
    return ["non_empty", f"Judge.{judge_id}"]


def run_judged_evaluation(
    caliber: CaliberClient,
    dataset_id: str,
    judge_id: str,
    *,
    prompt_ref: str,
    label: str,
    timeout: float = 1200.0,
) -> Any:
    """Score a test set with the judge, the answers coming from a prompt; wait for the result."""
    created = caliber.evaluations.create(
        dataset_id,
        scorers=judged_scorers(judge_id),
        label=label,
        predict_target="prompt",
        subject_ref=prompt_ref,
    )
    run_id = created.run_id or created.evaluation_id
    return caliber.evaluations.wait(run_id, timeout=timeout)


def judge_rate(evaluation: Any, judge_id: str) -> float | None:
    """The fraction of rows the judge scored 1 (its aggregate), or None when it did not score."""
    value = (evaluation.aggregate or {}).get(f"Judge.{judge_id}")
    return None if value is None else float(value)


def check_judged_evaluation(
    check: Checks,
    label: str,
    evaluation: Any,
    judge_id: str,
    *,
    rows: int,
    prompt_ref: str,
) -> dict[str, Any]:
    """The invariants of a real, judge-scored run: structure and ranges, never exact scores."""
    token = f"Judge.{judge_id}"
    results = evaluation.results or []
    scored = [r for r in results if r.get("error") is None and token in (r.get("scores") or {})]
    rate = judge_rate(evaluation, judge_id)
    check(
        f"{label}: the run completed over all {rows} rows with a verdict from the judge for each",
        evaluation.status == "completed"
        and evaluation.n_examples == rows
        and len(results) == rows
        and len(scored) == rows,
        f"{evaluation.status}, {evaluation.n_examples} rows, {len(scored)} judged",
    )
    check(
        f"{label}: the judge's verdicts are 0 or 1 and its aggregate is the share of 1s",
        all(r["scores"][token] in (0, 0.0, 1, 1.0) for r in scored)
        and rate is not None
        and abs(rate - sum(float(r["scores"][token]) for r in scored) / max(len(scored), 1)) < 1e-3,
        f"judge aggregate {rate}",
    )
    resolved = (evaluation.evidence or {}).get("resolved", {})
    judge_info = (resolved.get("judges") or {}).get(token) or {}
    check(
        f"{label}: the evidence names a real provider, the answerer prompt and the judge",
        bool(resolved.get("llm_provider"))
        and resolved.get("llm_provider") != "fake"
        and bool(resolved.get("model"))
        and evaluation.subject_ref == prompt_ref
        and judge_info.get("judge_id") == judge_id,
        f"provider={resolved.get('llm_provider')} model={resolved.get('model')} "
        f"judge model={judge_info.get('model') or 'the deployment default'}",
    )
    return {
        "run_id": evaluation.run_id,
        "rows": evaluation.n_examples,
        "judge_rate": rate,
        "overall": evaluation.overall_score,
        "model": resolved.get("model"),
        "judge_model": judge_info.get("model") or "deployment default",
        "slices": sorted((evaluation.evidence or {}).get("slices") or {}),
    }


def ask_aria(
    caliber: CaliberClient, content: str, *, approval_mode: str | None = None
) -> dict[str, Any]:
    """One Ask Aria chat turn: a new session, one message, and the tools the model called.

    The default session is in ``build`` mode with ``manual`` approval: the model gets Aria's read
    tools only. ``approval_mode="auto_all"`` (or ``full_autonomy``) also projects the mutating
    capabilities (``judge_create``, ``review_queue_create``, ...) into the model's tools.
    """
    body: dict[str, Any] = {"title": "cookbook chat", "mode": "build"}
    if approval_mode is not None:
        body["approval_mode"] = approval_mode
    session = caliber.raw.post("/assistant/sessions", json=body)
    reply = caliber.raw.post(
        f"/assistant/sessions/{session['session_id']}/messages", json={"content": content}
    )
    calls = reply.get("tool_calls") or []
    return {
        "session_id": session["session_id"],
        "text": (reply.get("assistant_message") or {}).get("content", ""),
        "called": [str(c["name"]) for c in calls],
        "succeeded": [str(c["name"]) for c in calls if c.get("ok")],
        "calls": calls,
        "engine": (reply.get("run") or {}).get("engine"),
    }


def check_chat_boundary(
    caliber: CaliberClient,
    check: Checks,
    *,
    tool: str,
    ask: str,
    exists: Callable[[], bool],
    absent_ask: str,
    absent_exists: Callable[[], bool],
) -> dict[str, Any]:
    """Ask Aria's two sides, live: it acts in a ``build`` / ``auto_all`` session and only reads in
    the default one. ``exists`` reads the thing the first message asked for; ``absent_exists``
    the thing the second asked for."""
    config = caliber.raw.get("/assistant/config")
    check(
        "the assistant engine is a real one, not the fake heuristic",
        config.get("engine") not in (None, "fake") and bool(config.get("configured")),
        f"engine={config.get('engine')} model={config.get('model')}",
    )
    acting = ask_aria(caliber, ask, approval_mode="auto_all")
    check(
        f"Ask Aria in a build / auto_all session called {tool} itself and it worked",
        tool in acting["succeeded"] and exists(),
        f"tools called: {acting['called']}",
    )
    reading = ask_aria(caliber, absent_ask)
    check(
        f"in the default session (manual approval) {tool} is not offered: nothing was created",
        tool not in reading["called"] and not absent_exists(),
        f"tools called: {reading['called']}",
    )
    return {"acting": acting["called"], "reading": reading["called"], "engine": config["engine"]}
