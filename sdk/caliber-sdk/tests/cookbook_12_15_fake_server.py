"""A small stateful stand-in for the routes cookbooks 12 to 15 call (Aria plans and what they make).

``test_cookbook_12_15_examples.py`` replays the real example scripts against this instead of a
server, so each script's call sequence and what it sends and reads stay covered offline. It imitates
the contract the scripts depend on: Aria's deterministic planner (the same domain words), the plan
executor's states (paused on a form, paused on an approval, completed, failed), the answers it
refuses, the scope floor on the plan's owner, what each capability creates, and what the artifacts'
own routes answer (a duplicate name, a judge with an unsupported variable, a review answer that does
not fit the queue's questions). The behaviour that matters in production (the real planner,
executor, write-back to MLflow and the calibration worker) is exercised by running the scripts
against a live stack (see each lab README) and by ``caliber/tests/test_aria_*``.

The rules are restated here independently of the server's code on purpose: the tests then check
that the dataset files, the fake and the scripts agree, and that a script notices when the server
stops following the rules (see the ``bend`` switches).
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable
from typing import Any

import httpx

PREFIX = "/ajax-api/2.0/mlflow/caliber"
TEMPLATE_VARS = ("inputs", "outputs", "expectations", "conversation", "trace")

ALL_SCOPES = {"caliber.admin", "caliber.approver", "caliber.operator", "caliber.viewer"}
USERS: dict[str, set[str]] = {
    "admin": ALL_SCOPES,
    "olga": {"caliber.operator", "caliber.viewer"},
    "vera": {"caliber.viewer"},
}

QUESTION_TYPES = {"pass_fail", "categorical", "numeric", "text"}


def _envelope(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data})


def _error(status: int, detail: str, **extra: Any) -> httpx.Response:
    return httpx.Response(status, json={"detail": detail, "status_code": status, **extra})


def _cap(
    key: str,
    tier: str,
    properties: dict[str, dict[str, Any]],
    required: tuple[str, ...],
    results: tuple[str, ...],
    title: str,
    scope: str = "operator",
) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "tier": tier,
        "scope": scope,
        "properties": properties,
        "required": required,
        "results": results,
    }


_STR = {"type": "string"}
_NAME = {"type": "string", "minLength": 1}
_QUESTION = {
    "type": "object",
    "properties": {
        "key": _STR,
        "title": _STR,
        "type": {"type": "string", "enum": sorted(QUESTION_TYPES)},
        "options": {"type": "array", "items": _STR},
        "required": {"type": "boolean"},
        "target": {"type": "string", "enum": ["feedback", "expectation"]},
    },
    "required": ["key", "title"],
}

#: Registry order matters: the planner emits steps in this order.
REGISTRY: list[dict[str, Any]] = [
    _cap("judge.list", "read", {}, (), ("judges",), "List judges", "viewer"),
    _cap(
        "judge.create",
        "mutate",
        {
            "name": _NAME,
            "instructions": _NAME,
            "description": _STR,
            "model": _STR,
            "feedback_value_type": {"type": "string", "enum": ["bool", "int", "float", "str"]},
            "tags": {"type": "array", "items": _STR},
        },
        ("name", "instructions"),
        ("judge_id", "name", "status"),
        "Create judge",
    ),
    _cap("review_queue.list", "read", {}, (), ("review_queues",), "List review queues", "viewer"),
    _cap(
        "review_queue.create",
        "mutate",
        {
            "name": _NAME,
            "description": _STR,
            "questions": {"type": "array", "minItems": 1, "items": _QUESTION},
            "reviewers": {"type": "array", "items": _STR},
        },
        ("name", "questions"),
        ("queue_id", "name", "questions"),
        "Create review queue",
    ),
    _cap(
        "eval_dataset.create",
        "mutate",
        {"name": _NAME, "description": _STR, "tags": {"type": "array", "items": _STR}},
        ("name",),
        ("dataset_id", "name"),
        "Create test set",
    ),
    _cap(
        "review_queue.add_items",
        "mutate",
        {
            "queue_id": _STR,
            "trace_ids": {"type": "array", "minItems": 1, "items": _STR},
            "experiment_id": _STR,
            "assigned_to": _STR,
        },
        ("queue_id", "trace_ids"),
        ("queue_id", "added"),
        "Enqueue traces for review",
    ),
    _cap(
        "workflow.calibrate",
        "mutate",
        {"workflow_id": _NAME, "agent_id": _NAME},
        ("workflow_id", "agent_id"),
        ("job_id",),
        "Calibrate workflow",
    ),
    _cap("openapi_tool.list", "read", {}, (), ("tools",), "List OpenAPI tools", "viewer"),
    _cap(
        "openapi_tool.invoke",
        "read",
        {"tool_id": _STR, "input": {"type": "object"}},
        ("tool_id",),
        ("status_code", "json", "text"),
        "Invoke a read-only OpenAPI tool",
        "viewer",
    ),
]
BY_KEY = {cap["key"]: cap for cap in REGISTRY}
SYNONYMS = {"eval_dataset": ("test set", "test-set", "testset", "eval dataset")}


def plan_steps(goal: str) -> list[dict[str, Any]]:
    """The deterministic planner: a step for every non-read capability whose domain word appears."""
    text = goal.lower()
    steps: list[dict[str, Any]] = []
    produced: dict[str, int] = {}
    for cap in REGISTRY:
        if cap["tier"] == "read":
            continue
        domain = cap["key"].split(".", 1)[0]
        if not (
            domain.replace("_", " ") in text
            or domain in text
            or cap["key"] in text
            or any(word in text for word in SYNONYMS.get(domain, ()))
        ):
            continue
        inputs: dict[str, Any] = {}
        deps: list[int] = []
        for name in cap["required"]:
            if name in produced and name.endswith("_id"):
                inputs[name] = {"$from_step_index": produced[name], "path": name}
                deps.append(produced[name])
        steps.append({"cap": cap, "inputs": inputs, "deps": sorted(set(deps))})
        for name in cap["results"]:
            produced[name] = len(steps) - 1
    return steps


def validate_value(value: Any, schema: dict[str, Any], path: str) -> str | None:
    """The JSON-Schema subset the server's form validation understands; ``None`` when it fits."""
    if isinstance(value, dict) and "$from_step" in value:
        return None
    allowed = schema.get("enum")
    if allowed is not None and value not in allowed:
        return f"{path} must be one of {allowed!r}"
    kind = schema.get("type")
    checks = {
        "string": lambda v: isinstance(v, str),
        "boolean": lambda v: isinstance(v, bool),
        "array": lambda v: isinstance(v, list),
        "object": lambda v: isinstance(v, dict),
    }
    if kind in checks and not checks[kind](value):
        return f"{path} must be {kind}"
    if kind == "string" and len(value) < schema.get("minLength", 0):
        return f"{path} must contain at least {schema['minLength']} character(s)"
    if kind == "array":
        if len(value) < schema.get("minItems", 0):
            return f"{path} must contain at least {schema['minItems']} item(s)"
        for index, item in enumerate(value):
            problem = validate_value(item, schema.get("items") or {}, f"{path}[{index}]")
            if problem:
                return problem
    if kind == "object" and "properties" in schema:
        missing = [n for n in schema.get("required", []) if n not in value or value[n] is None]
        if missing:
            return f"{path} is missing required field(s): {', '.join(missing)}"
        for key, item in value.items():
            if key in schema["properties"]:
                problem = validate_value(item, schema["properties"][key], f"{path}.{key}")
                if problem:
                    return problem
    return None


class FakeAria:
    """Everything cookbooks 12 to 15 read and write, in memory.

    ``bend`` makes the fake deviate from the rules in one named way, to prove a script reports the
    deviation instead of passing: ``"links_name"`` (a later step takes the earlier step's name),
    ``"stuck_skip"`` (skipping a step leaves its dependants waiting forever), ``"no_scope_floor"``
    (a plan runs for an owner without the operator scope).
    """

    def __init__(
        self,
        *,
        simulated: bool = True,
        bend: str | None = None,
        traces: dict[str, str] | None = None,
    ) -> None:
        self.simulated = simulated
        self.bend = bend
        self.accounts: set[str] = {"admin"}
        self.disabled: set[str] = set()
        # Lab 15: the things a calibration remediates, and the job the plan parks on.
        self.tools: dict[tuple[str, str], dict[str, Any]] = {}
        self.agents: dict[str, dict[str, Any]] = {}
        self.workflows: dict[str, dict[str, Any]] = {}
        self.versions: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        #: ``rejected`` (a simulated deployment: no candidate beats the baseline) or
        #: ``candidate_ready`` (a stack with a real model).
        self.calibration_outcome = "rejected"
        #: How many reads of a plan (or polls) a calibration job takes to finish.
        self.job_reads = 3
        #: Whether the plan worker is running: it advances a parked plan whenever the plan is read.
        self.worker = True
        self.evaluations: dict[str, dict[str, Any]] = {}
        self.tokens = {"calpat_admin": "admin", "session-olga": "olga", "session-vera": "vera"}
        self.plans: dict[str, dict[str, Any]] = {}
        self.steps: dict[str, list[dict[str, Any]]] = {}
        self.interactions: dict[str, dict[str, Any]] = {}
        self.judges: dict[str, dict[str, Any]] = {}
        self.datasets: dict[str, dict[str, Any]] = {}
        self.examples: dict[str, list[dict[str, Any]]] = {}
        self.queues: dict[str, dict[str, Any]] = {}
        self.items: dict[str, list[dict[str, Any]]] = {}
        self.known_traces: dict[str, str] = dict(traces or {})
        self.assessments: dict[str, list[dict[str, Any]]] = {}
        self.audit: list[dict[str, Any]] = []
        self.calls: list[str] = []
        self._ids: Counter[str] = Counter()
        self.extra_routes: list[tuple[str, str, Callable[..., httpx.Response]]] = []

    # -- plumbing ------------------------------------------------------------------------
    def new(self, prefix: str) -> str:
        self._ids[prefix] += 1
        return f"{prefix}-{self._ids[prefix]:04d}"

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self, client_cls: Any, token: str = "calpat_admin") -> Any:
        return client_cls(
            "https://caliber.test",
            token=token,
            http_client=httpx.Client(transport=self.transport()),
            max_retries=0,
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix(PREFIX)
        method = request.method
        self.calls.append(f"{method} {path}")
        body = json.loads(request.content) if request.content else {}
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        user = self.tokens.get(token)
        if user is None:
            return _error(401, "authentication required (no identity in request)")
        scopes = USERS[user]
        routes: list[tuple[str, str, Callable[..., httpx.Response]]] = [
            ("GET", r"/me", self.me),
            ("GET", r"/aria/capabilities", self.capabilities),
            ("GET", r"/aria/plans", self.list_plans),
            ("POST", r"/aria/plans", self.create_plan),
            ("GET", r"/aria/plans/(?P<p>[^/]+)", self.get_plan),
            ("PATCH", r"/aria/plans/(?P<p>[^/]+)", self.update_plan),
            ("POST", r"/aria/plans/(?P<p>[^/]+)/approve", self.approve_plan),
            ("POST", r"/aria/plans/(?P<p>[^/]+)/execute", self.execute_plan),
            ("POST", r"/aria/plans/(?P<p>[^/]+)/poll", self.poll_plan),
            ("GET", r"/aria/plans/(?P<p>[^/]+)/interactions", self.list_interactions),
            ("POST", r"/aria/interactions/(?P<i>[^/]+)/answer", self.answer),
            ("GET", r"/judges", self.list_judges),
            ("POST", r"/judges", self.create_judge),
            ("GET", r"/judges/(?P<j>[^/]+)", self.get_judge),
            ("PATCH", r"/judges/(?P<j>[^/]+)", self.update_judge),
            ("GET", r"/eval-datasets", self.list_datasets),
            ("POST", r"/eval-datasets", self.create_dataset),
            ("GET", r"/eval-datasets/(?P<d>[^/]+)", self.get_dataset),
            ("PATCH", r"/eval-datasets/(?P<d>[^/]+)", self.update_dataset),
            ("GET", r"/eval-datasets/(?P<d>[^/]+)/examples", self.list_examples),
            ("POST", r"/eval-datasets/(?P<d>[^/]+)/examples", self.add_example),
            ("GET", r"/evaluations", self.list_evaluations),
            ("GET", r"/evaluations/(?P<e>[^/]+)", self.get_evaluation),
            ("POST", r"/evaluations", self.create_evaluation),
            ("GET", r"/auth/accounts", self.list_accounts),
            ("POST", r"/auth/accounts", self.create_account),
            ("PATCH", r"/auth/accounts/(?P<u>[^/]+)", self.update_account),
            ("GET", r"/review-queues", self.list_queues),
            ("POST", r"/review-queues", self.create_queue_route),
            ("GET", r"/review-queues/(?P<q>[^/]+)", self.get_queue),
            ("PATCH", r"/review-queues/(?P<q>[^/]+)", self.update_queue),
            ("POST", r"/review-queues/(?P<q>[^/]+)/items", self.enqueue_route),
            (
                "POST",
                r"/review-queues/(?P<q>[^/]+)/items/(?P<i>[^/]+)/submit",
                self.submit_item,
            ),
            (
                "GET",
                r"/review-queues/(?P<q>[^/]+)/alignment-examples",
                self.alignment_examples,
            ),
            ("GET", r"/observability/traces/(?P<t>[^/]+)", self.get_trace),
            ("GET", r"/tools", self.list_tools),
            ("POST", r"/tools", self.register_tool),
            ("POST", r"/agents", self.create_agent),
            ("GET", r"/workflows", self.list_workflows),
            ("POST", r"/workflows/import", self.import_workflow),
            ("PATCH", r"/workflows/(?P<w>[^/]+)", self.update_workflow),
            ("GET", r"/workflows/(?P<w>[^/]+)/versions", self.list_versions),
            ("GET", r"/workflow-versions/(?P<v>[^/]+)", self.get_version),
            ("POST", r"/workflow-versions/(?P<v>[^/]+)/publish", self.publish_version),
            ("GET", r"/jobs", self.list_jobs),
            ("GET", r"/jobs/(?P<j>[^/]+)", self.get_job),
            *self.extra_routes,
        ]
        for route_method, pattern, handler in routes:
            match = re.fullmatch(pattern, path)
            if route_method == method and match:
                return handler(
                    body=body, user=user, scopes=scopes, request=request, **match.groupdict()
                )
        raise AssertionError(  # pragma: no cover - guards against an unstubbed call
            f"example called an unstubbed route: {method} {path}"
        )

    def me(self, *, user: str, scopes: set[str], **_: Any) -> httpx.Response:
        return _envelope(
            {"user_id": user, "scopes": sorted(scopes), "is_admin": "caliber.admin" in scopes}
        )

    def require(self, scopes: set[str], scope: str = "caliber.operator") -> httpx.Response | None:
        if scope not in scopes:
            return _error(403, f"missing required scope: {scope}")
        return None

    # -- Aria: plans ---------------------------------------------------------------------------
    def capabilities(self, **_: Any) -> httpx.Response:
        return _envelope(
            {
                "capabilities": [
                    {
                        "key": cap["key"],
                        "title": cap["title"],
                        "tier": cap["tier"],
                        "required_scopes": [cap["scope"]],
                    }
                    for cap in sorted(REGISTRY, key=lambda c: c["key"])
                ],
                "count": len(REGISTRY),
            }
        )

    def _detail(self, plan_id: str) -> dict[str, Any]:
        plan = self.plans[plan_id]
        steps = self.steps[plan_id]
        return {
            "plan": {**plan, "step_count": len(steps)},
            "steps": [
                {k: v for k, v in step.items() if k not in {"cap"}} | {"seq": i}
                for i, step in enumerate(steps)
            ],
        }

    def _visible(self, plan_id: str, user: str, scopes: set[str]) -> httpx.Response | None:
        plan = self.plans.get(plan_id)
        if plan is None or plan["owner"] != user:
            return _error(404, f"aria plan {plan_id!r} not found")
        return None

    def list_plans(self, *, user: str, **_: Any) -> httpx.Response:
        return _envelope([p for p in self.plans.values() if p["owner"] == user])

    def create_plan(self, *, body: dict[str, Any], user: str, **_: Any) -> httpx.Response:
        plan_id = self.new("PLAN")
        planned = plan_steps(body["goal"])
        step_ids = [self.new("PSTEP") for _ in planned]
        self.plans[plan_id] = {
            "plan_id": plan_id,
            "goal": body["goal"],
            "status": "draft",
            "autonomy": body.get("autonomy", "approve_plan"),
            "owner": user,
        }
        self.steps[plan_id] = [
            {
                "step_id": sid,
                "plan_id": plan_id,
                "capability_key": item["cap"]["key"],
                "cap": item["cap"],
                "title": item["cap"]["title"],
                "inputs": self._link(item["inputs"], step_ids),
                "depends_on": [step_ids[d] for d in item["deps"]],
                "status": "pending",
                "result": {},
                "error": None,
                "job_id": None,
            }
            for sid, item in zip(step_ids, planned, strict=True)
        ]
        if self.bend == "links_name":
            for index in range(1, len(self.steps[plan_id])):
                step = self.steps[plan_id][index]
                if "name" in step["cap"]["required"]:
                    step["inputs"]["name"] = {
                        "$from_step": self.steps[plan_id][index - 1]["step_id"],
                        "path": "name",
                    }
        detail = self._detail(plan_id)
        if not planned:
            detail["warnings"] = [
                "No platform steps matched this goal, so there is nothing to approve."
            ]
        return _envelope(detail, 201)

    @staticmethod
    def _link(inputs: dict[str, Any], step_ids: list[str]) -> dict[str, Any]:
        return {
            name: {"$from_step": step_ids[ref["$from_step_index"]], "path": ref["path"]}
            for name, ref in inputs.items()
        }

    def get_plan(self, *, p: str, user: str, scopes: set[str], **_: Any) -> httpx.Response:
        if (hidden := self._visible(p, user, scopes)) is not None:
            return hidden
        if self.worker and self.plans[p]["status"] == "running":
            self._tick_jobs()
            self._advance(p)
        return _envelope(self._detail(p))

    def update_plan(
        self, *, p: str, body: dict[str, Any], user: str, scopes: set[str], **_: Any
    ) -> httpx.Response:
        if (hidden := self._visible(p, user, scopes)) is not None:
            return hidden
        if self.plans[p]["status"] != "draft":
            return _error(409, "only draft plans can be edited")
        if "autonomy" in body:
            self.plans[p]["autonomy"] = body["autonomy"]
        if body.get("status") == "cancelled":
            self.plans[p]["status"] = "cancelled"
        return _envelope(self._detail(p))

    def approve_plan(self, *, p: str, user: str, scopes: set[str], **_: Any) -> httpx.Response:
        if (hidden := self._visible(p, user, scopes)) is not None:
            return hidden
        plan = self.plans[p]
        if plan["status"] != "draft":
            return _error(409, f"plan is {plan['status']!r}, not draft")
        if not self.steps[p]:
            return _error(409, "this plan has no steps, so it cannot be approved.")
        plan["status"] = "approved"
        return _envelope(self._detail(p))

    def execute_plan(self, *, p: str, user: str, scopes: set[str], **_: Any) -> httpx.Response:
        if (hidden := self._visible(p, user, scopes)) is not None:
            return hidden
        if self.plans[p]["status"] not in {"approved", "paused", "running"}:
            return _error(409, f"plan is {self.plans[p]['status']!r}; approve it before executing")
        self._advance(p)
        return _envelope(self._detail(p))

    def poll_plan(self, *, p: str, user: str, scopes: set[str], **_: Any) -> httpx.Response:
        if (hidden := self._visible(p, user, scopes)) is not None:
            return hidden
        self._tick_jobs()
        self._advance(p)
        return _envelope(self._detail(p))

    def list_interactions(self, *, p: str, user: str, scopes: set[str], **_: Any) -> httpx.Response:
        if (hidden := self._visible(p, user, scopes)) is not None:
            return hidden
        return _envelope([i for i in self.interactions.values() if i["plan_id"] == p])

    # -- Aria: the executor -------------------------------------------------------------------
    def _owner_scopes(self, plan: dict[str, Any]) -> set[str]:
        return USERS[plan["owner"]]

    def _advance(self, plan_id: str) -> None:
        plan = self.plans[plan_id]
        steps = self.steps[plan_id]
        by_id = {s["step_id"]: s for s in steps}
        while plan["status"] in {"approved", "running", "paused"}:
            for step in steps:
                if step["status"] in {"done", "skipped"}:
                    continue
                if step["status"] == "waiting_job":
                    if self._resolve_job(plan, step):
                        if plan["status"] == "failed":
                            return
                        continue
                    plan["status"] = "running"
                    return
                if step["status"] == "waiting_input":
                    plan["status"] = "paused"
                    return
                deps = [by_id[d] for d in step["depends_on"]]
                if any(d["status"] == "failed" for d in deps):
                    step["status"] = "blocked"
                    plan["status"] = "paused"
                    return
                if any(d["status"] == "skipped" for d in deps):
                    if self.bend == "stuck_skip":
                        plan["status"] = "paused"
                        return
                    skipped = next(d for d in deps if d["status"] == "skipped")
                    step["status"] = "skipped"
                    step["error"] = (
                        f"skipped because step {steps.index(skipped) + 1} "
                        f"({skipped['capability_key']}) was skipped"
                    )
                    continue
                if not all(d["status"] == "done" for d in deps):
                    plan["status"] = "paused"
                    return
                cap = step["cap"]
                if (
                    cap["tier"] != "read"
                    and f"caliber.{cap['scope']}" not in self._owner_scopes(plan)
                    and self.bend != "no_scope_floor"
                ):
                    step["status"] = "failed"
                    step["error"] = f"plan owner lacks required scope(s): {cap['scope']}"
                    plan["status"] = "failed"
                    return
                missing = [n for n in cap["required"] if step["inputs"].get(n) is None]
                if missing:
                    self._ask(plan, step, "input", missing=missing)
                    return
                if plan["autonomy"] == "ask_each" and not step.get("approved"):
                    self._ask(plan, step, "permission")
                    return
                plan["status"] = "running"
                self._run(plan, step, by_id)
                if plan["status"] == "failed":
                    return
                if step["status"] == "waiting_job":
                    continue
            if all(s["status"] in {"done", "skipped"} for s in steps):
                plan["status"] = "completed"
            return

    def _ask(self, plan: dict[str, Any], step: dict[str, Any], kind: str, **evidence: Any) -> None:
        interaction_id = self.new("ASK")
        cap = step["cap"]
        index = self.steps[plan["plan_id"]].index(step) + 1
        self.interactions[interaction_id] = {
            "interaction_id": interaction_id,
            "plan_id": plan["plan_id"],
            "step_id": step["step_id"],
            "kind": kind,
            "prompt": (
                f"Provide the required inputs for step {index}: {step['title']}."
                if kind == "input"
                else f"Approve step {index}: {step['title']} ({cap['key']})?"
            ),
            "evidence": {
                "capability_key": cap["key"],
                "input_schema": {
                    "properties": cap["properties"],
                    "required": list(cap["required"]),
                },
                "current_inputs": dict(step["inputs"]),
                **evidence,
            },
            "status": "pending",
            "required_scope": None,
            "response": {},
        }
        step["status"] = "waiting_input"
        plan["status"] = "paused"

    def answer(
        self, *, i: str, body: dict[str, Any], user: str, scopes: set[str], **_: Any
    ) -> httpx.Response:
        interaction = self.interactions.get(i)
        if interaction is None:
            return _error(409, f"interaction {i!r} not found")
        if interaction["status"] != "pending":
            return _error(409, f"interaction {i!r} already answered")
        plan = self.plans[interaction["plan_id"]]
        if plan["owner"] != user and "caliber.admin" not in scopes:
            return _error(403, "only the plan owner may answer this interaction")
        step = next(
            s for s in self.steps[plan["plan_id"]] if s["step_id"] == interaction["step_id"]
        )
        approved = (
            bool(body["approved"])
            if body.get("approved") is not None
            else body.get("inputs") is not None
        )
        if approved and interaction["kind"] == "input":
            supplied = body.get("inputs")
            if not isinstance(supplied, dict):
                return _error(409, "an input interaction requires an 'inputs' object")
            cap = step["cap"]
            merged = {**step["inputs"], **supplied}
            unknown = sorted(set(merged) - set(cap["properties"]))
            if unknown:
                return _error(
                    409, f"{cap['key']} received unknown input field(s): {', '.join(unknown)}"
                )
            missing = [n for n in cap["required"] if merged.get(n) is None]
            if missing:
                return _error(
                    409, f"{cap['key']} is missing required input field(s): {', '.join(missing)}"
                )
            for key, value in merged.items():
                problem = validate_value(value, cap["properties"][key], key)
                if problem:
                    return _error(409, problem)
            step["inputs"] = merged
        interaction["status"] = "answered"
        interaction["response"] = {"approved": approved, **body}
        if not approved:
            step["status"] = "skipped"
        elif interaction["kind"] == "input":
            step["status"] = "pending"
        else:
            step["status"] = "pending"
            step["approved"] = True
        self._advance(plan["plan_id"])
        return _envelope(self._detail(plan["plan_id"]))

    def _run(self, plan: dict[str, Any], step: dict[str, Any], by_id: dict[str, Any]) -> None:
        inputs = {}
        for name, value in step["inputs"].items():
            if isinstance(value, dict) and "$from_step" in value:
                inputs[name] = by_id[value["$from_step"]]["result"][value["path"]]
            else:
                inputs[name] = value
        handler = getattr(self, "cap_" + step["capability_key"].replace(".", "_"))
        try:
            outcome = handler(plan, inputs)
        except Problem as problem:
            step["status"] = "failed"
            step["error"] = str(problem)
            plan["status"] = "failed"
            return
        if isinstance(outcome, AsyncJob):
            step["status"] = "waiting_job"
            step["job_id"] = outcome.job_id
            step["result"] = {"__job_kind__": "refinement_job"}
            return
        step["status"] = "done"
        step["result"] = outcome

    def _resolve_job(self, plan: dict[str, Any], step: dict[str, Any]) -> bool:
        """Resolve the job a step waits on: ``True`` when it ended (the step is settled)."""
        job = self.jobs[step["job_id"]]
        if job["status"] in {"queued", "running"}:
            return False
        if job["status"] in {"candidate_ready", "applied"}:
            step["status"] = "done"
            step["result"] = {"job_id": job["job_id"]}
            return True
        step["status"] = "failed"
        step["error"] = f"{job['status']}: {job['error_message']}"
        plan["status"] = "failed"
        return True

    def _tick_jobs(self) -> None:
        for job in self.jobs.values():
            if job["status"] in {"queued", "running"}:
                job["reads_left"] -= 1
                job["status"] = "running"
                if job["reads_left"] <= 0:
                    job["status"] = self.calibration_outcome
                    if job["status"] == "rejected":
                        job["error_message"] = (
                            "workflow calibration rejected: no candidate met calibration gates; "
                            "quality delta +0.000 < epsilon 0.02"
                        )
                        job["eval_results"]["gate"] = {
                            "passed": False,
                            "reasons": [
                                "no candidate met calibration gates",
                                "quality delta +0.000 < epsilon 0.02",
                            ],
                        }

    # -- Aria: the capabilities ----------------------------------------------------------------
    def cap_judge_create(self, plan: dict[str, Any], inputs: dict[str, Any]) -> dict[str, Any]:
        used = set(re.findall(r"\{\{\s*(\w+)\s*\}\}", inputs["instructions"]))
        if not used & set(TEMPLATE_VARS):
            raise Problem(
                "invalid input: instructions: instructions must reference at least one "
                "evaluation variable: " + ", ".join("{{ " + v + " }}" for v in TEMPLATE_VARS)
            )
        if any(j["name"] == inputs["name"] for j in self.judges.values()):
            raise Problem(f"ValueError: judge name {inputs['name']!r} is already in use")
        judge = self._store_judge(inputs, plan["owner"])
        return {"judge_id": judge["judge_id"], "name": judge["name"], "status": "active"}

    def cap_eval_dataset_create(
        self, plan: dict[str, Any], inputs: dict[str, Any]
    ) -> dict[str, Any]:
        if any(d["name"] == inputs["name"] for d in self.datasets.values()):
            raise Problem(f"ValueError: eval-dataset name {inputs['name']!r} is already in use")
        dataset = self._store_dataset(inputs, plan["owner"])
        return {"dataset_id": dataset["dataset_id"], "name": dataset["name"]}

    def cap_review_queue_create(
        self, plan: dict[str, Any], inputs: dict[str, Any]
    ) -> dict[str, Any]:
        problem = self._question_problem(inputs["questions"])
        if problem:
            raise Problem(f"invalid input: {problem}")
        if any(q["name"] == inputs["name"] for q in self.queues.values()):
            raise Problem(f"ValueError: review-queue name {inputs['name']!r} is already in use")
        queue = self._store_queue(inputs, plan["owner"])
        return {
            "queue_id": queue["queue_id"],
            "name": queue["name"],
            "questions": len(queue["questions"]),
        }

    def cap_review_queue_add_items(
        self, plan: dict[str, Any], inputs: dict[str, Any]
    ) -> dict[str, Any]:
        queue = self.queues.get(inputs["queue_id"])
        if queue is None:
            raise Problem(f"ValueError: review queue {inputs['queue_id']!r} not found")
        created = self._enqueue(queue["queue_id"], inputs["trace_ids"], inputs.get("assigned_to"))
        return {"queue_id": queue["queue_id"], "added": len(created)}

    def cap_workflow_calibrate(self, plan: dict[str, Any], inputs: dict[str, Any]) -> Any:
        workflow = self.workflows.get(inputs["workflow_id"])
        if workflow is None or workflow["owner"] != plan["owner"]:
            raise Problem(f"workflow {inputs['workflow_id']!r} not found (HTTP 404)")
        agent = self.agents.get(inputs["agent_id"])
        if agent is None:
            raise Problem(f"agent {inputs['agent_id']!r} is not registered (HTTP 400)")
        manifest = workflow["manifest"]
        gate_ref = next((g["dataset_ref"] for g in manifest.get("deploy_gates", {}).values()), None)
        dataset_name = (
            manifest["artifacts"]["eval_datasets"][gate_ref]["dataset_name"] if gate_ref else None
        )
        gate_rows = [d["dataset_id"] for d in self.datasets.values() if d["name"] == dataset_name]
        if not gate_rows or not self.examples[gate_rows[0]]:
            raise Problem(
                "Workflow calibration requires an active deploy-gate eval dataset with examples. "
                "(HTTP 400)"
            )
        job_id = self.new("RFN")
        self.jobs[job_id] = {
            "job_id": job_id,
            "status": "queued",
            "agent_id": inputs["agent_id"],
            "workflow_id": inputs["workflow_id"],
            "reads_left": self.job_reads,
            "error_message": None,
            "eval_results": {
                "workflow": True,
                "calibration": True,
                "n_examples": min(20, len(self.examples[gate_rows[0]])),
                "baseline": {"overall": 0.0},
                "gate": {"passed": True, "reasons": []},
            },
        }
        return AsyncJob(job_id)

    # -- judges, test sets, evaluations -------------------------------------------------------
    def _store_judge(self, body: dict[str, Any], owner: str) -> dict[str, Any]:
        judge_id = self.new("JDG")
        self.judges[judge_id] = {
            "judge_id": judge_id,
            "name": body["name"],
            "description": body.get("description", ""),
            "instructions": body["instructions"],
            "model": body.get("model"),
            "feedback_value_type": body.get("feedback_value_type", "bool"),
            "owner": owner,
            "tags": body.get("tags", []),
            "status": "active",
        }
        return self.judges[judge_id]

    def _store_dataset(self, body: dict[str, Any], owner: str) -> dict[str, Any]:
        dataset_id = self.new("ED")
        self.datasets[dataset_id] = {
            "dataset_id": dataset_id,
            "name": body["name"],
            "description": body.get("description", ""),
            "owner": owner,
            "tags": body.get("tags", []),
            "status": "active",
            "version": 1,
        }
        self.examples[dataset_id] = []
        return self.datasets[dataset_id]

    def list_judges(self, **_: Any) -> httpx.Response:
        return _envelope(list(self.judges.values()))

    def get_judge(self, *, j: str, **_: Any) -> httpx.Response:
        return _envelope(self.judges[j])

    def update_judge(self, *, j: str, body: dict[str, Any], **_: Any) -> httpx.Response:
        self.judges[j].update(body)
        return _envelope(self.judges[j])

    def create_judge(
        self, *, body: dict[str, Any], user: str, scopes: set[str], **_: Any
    ) -> httpx.Response:
        if (denied := self.require(scopes)) is not None:
            return denied
        used = set(re.findall(r"\{\{\s*(\w+)\s*\}\}", body["instructions"]))
        if not used & set(TEMPLATE_VARS):
            return _error(400, "request body validation failed")
        if any(j["name"] == body["name"] for j in self.judges.values()):
            return _error(409, f"judge name {body['name']!r} is already in use")
        return _envelope(self._store_judge(body, user), 201)

    def list_datasets(self, **_: Any) -> httpx.Response:
        return _envelope(list(self.datasets.values()))

    def get_dataset(self, *, d: str, **_: Any) -> httpx.Response:
        return _envelope(self.datasets[d])

    def update_dataset(self, *, d: str, body: dict[str, Any], **_: Any) -> httpx.Response:
        self.datasets[d].update(body)
        return _envelope(self.datasets[d])

    def create_dataset(
        self, *, body: dict[str, Any], user: str, scopes: set[str], **_: Any
    ) -> httpx.Response:
        if (denied := self.require(scopes)) is not None:
            return denied
        if any(d["name"] == body["name"] for d in self.datasets.values()):
            return _error(409, f"eval-dataset name {body['name']!r} is already in use")
        return _envelope(self._store_dataset(body, user), 201)

    def list_examples(self, *, d: str, **_: Any) -> httpx.Response:
        return _envelope(self.examples[d])

    def add_example(self, *, d: str, body: dict[str, Any], **_: Any) -> httpx.Response:
        assert set(body) <= {"input", "expected", "tags", "weight"}, body
        self.datasets[d]["version"] += 1  # every appended example is a new version
        example = {"example_id": self.new("EX"), "dataset_id": d, **body}
        self.examples[d].append(example)
        return _envelope(example, 201)

    def create_evaluation(self, *, body: dict[str, Any], **_: Any) -> httpx.Response:
        if self.simulated:
            return _error(
                400,
                "evaluation requires a real LLM provider; set CALIBER_LLM_PROVIDER "
                "(openai/anthropic) and the matching API key, then retry.",
            )
        run_id = self.new("EVR")
        self.evaluations[run_id] = {
            "evaluation_id": run_id,
            "run_id": run_id,
            "dataset_id": body["dataset_id"],
            "status": "completed",
            "n_examples": len(self.examples[body["dataset_id"]]),
        }
        return _envelope(self.evaluations[run_id], 201)

    def list_evaluations(self, **_: Any) -> httpx.Response:
        return _envelope(list(self.evaluations.values()))

    def get_evaluation(self, *, e: str, **_: Any) -> httpx.Response:
        return _envelope(self.evaluations[e])

    # -- accounts -----------------------------------------------------------------------------
    def list_accounts(self, **_: Any) -> httpx.Response:
        return _envelope(
            {"accounts": [{"user_id": u, "disabled": u in self.disabled} for u in self.accounts]}
        )

    def create_account(self, *, body: dict[str, Any], **_: Any) -> httpx.Response:
        self.accounts.add(body["user_id"])
        return _envelope({"user_id": body["user_id"]}, 201)

    def update_account(self, *, u: str, body: dict[str, Any], **_: Any) -> httpx.Response:
        if "disabled" in body:
            (self.disabled.add if body["disabled"] else self.disabled.discard)(u)
        return _envelope({"user_id": u})

    # -- tools, agents, workflows, jobs (what a calibration remediates) -----------------------
    def list_tools(self, **_: Any) -> httpx.Response:
        return _envelope(list(self.tools.values()))

    def register_tool(self, *, body: dict[str, Any], scopes: set[str], **_: Any) -> httpx.Response:
        if (denied := self.require(scopes)) is not None:
            return denied
        key = (body["name"], body["version"])
        if key in self.tools:
            return _error(
                409, f"tool {body['name']!r} version {body['version']!r} already registered"
            )
        self.tools[key] = {**body, "tool_id": self.new("TL"), "status": "active"}
        return _envelope(self.tools[key], 201)

    def create_agent(
        self, *, body: dict[str, Any], user: str, scopes: set[str], **_: Any
    ) -> httpx.Response:
        if (denied := self.require(scopes)) is not None:
            return denied
        if body["agent_id"] in self.agents:
            return _error(409, f"agent {body['agent_id']!r} already exists")
        self.agents[body["agent_id"]] = {**body, "enabled": True, "owner": user}
        return _envelope(self.agents[body["agent_id"]], 201)

    def list_workflows(self, *, user: str, **_: Any) -> httpx.Response:
        return _envelope([w for w in self.workflows.values() if w["owner"] == user])

    def import_workflow(
        self, *, body: dict[str, Any], user: str, scopes: set[str], **_: Any
    ) -> httpx.Response:
        if (denied := self.require(scopes)) is not None:
            return denied
        manifest = body["manifest"]
        known = {d["name"] for d in self.datasets.values()}
        for ref, artifact in manifest.get("artifacts", {}).get("eval_datasets", {}).items():
            if artifact["dataset_name"] not in known:
                return _error(
                    400,
                    "workflow import preflight failed; resolve validation and dependency errors "
                    f"first ({ref}: dataset {artifact['dataset_name']!r} is not visible)",
                )
        for tool in manifest.get("nodes", {}).get("support_agent", {}).get("tools", []):
            if not any(name == tool for name, _version in self.tools):
                return _error(400, "workflow import preflight failed: unregistered tool " + tool)
        workflow_id = self.new("WF")
        version_id = self.new("WFV")
        self.workflows[workflow_id] = {
            "workflow_id": workflow_id,
            "name": body["name"],
            "status": "active",
            "owner": user,
            "manifest": manifest,
        }
        self.versions[version_id] = {
            "version_id": version_id,
            "workflow_id": workflow_id,
            "version_number": 1,
            "status": "draft",
            "manifest": manifest,
        }
        return _envelope(
            {
                "workflow": {
                    k: v for k, v in self.workflows[workflow_id].items() if k != "manifest"
                },
                "version": self.versions[version_id],
            },
            201,
        )

    def update_workflow(self, *, w: str, body: dict[str, Any], **_: Any) -> httpx.Response:
        self.workflows[w].update(body)
        return _envelope({k: v for k, v in self.workflows[w].items() if k != "manifest"})

    def list_versions(self, *, w: str, **_: Any) -> httpx.Response:
        return _envelope([v for v in self.versions.values() if v["workflow_id"] == w])

    def get_version(self, *, v: str, **_: Any) -> httpx.Response:
        return _envelope(self.versions[v])

    def publish_version(self, *, v: str, **_: Any) -> httpx.Response:
        self.versions[v]["status"] = "published"
        return _envelope(self.versions[v])

    def list_jobs(self, **_: Any) -> httpx.Response:
        return _envelope(
            [{k: x for k, x in job.items() if k != "reads_left"} for job in self.jobs.values()]
        )

    def get_job(self, *, j: str, **_: Any) -> httpx.Response:
        self._tick_jobs()
        job = {k: x for k, x in self.jobs[j].items() if k != "reads_left"}
        return _envelope(job)

    # -- review queues ------------------------------------------------------------------------
    @staticmethod
    def _question_problem(questions: list[dict[str, Any]]) -> str | None:
        keys = [q["key"] for q in questions]
        if len(keys) != len(set(keys)):
            return "questions: question keys must be unique"
        for index, question in enumerate(questions):
            if question.get("type") == "categorical" and not question.get("options"):
                return (
                    f"questions.{index}: categorical questions require a non-empty 'options' list"
                )
        return None

    def _store_queue(self, body: dict[str, Any], owner: str) -> dict[str, Any]:
        queue_id = self.new("RVQ")
        questions = [
            {
                "options": [],
                "required": True,
                "target": "feedback",
                "type": "pass_fail",
                **question,
            }
            for question in body["questions"]
        ]
        self.queues[queue_id] = {
            "queue_id": queue_id,
            "name": body["name"],
            "description": body.get("description", ""),
            "questions": questions,
            "reviewers": body.get("reviewers", []),
            "owner": owner,
            "status": "active",
        }
        self.items[queue_id] = []
        return self.queues[queue_id]

    def _enqueue(
        self, queue_id: str, trace_ids: list[str], assigned_to: str | None
    ) -> list[dict[str, Any]]:
        present = {item["trace_id"] for item in self.items[queue_id]}
        created = []
        for trace_id in trace_ids:
            if trace_id in present:
                continue
            present.add(trace_id)
            item: dict[str, Any] = {
                "item_id": self.new("RVI"),
                "queue_id": queue_id,
                "trace_id": trace_id,
                "status": "pending",
                "assigned_to": assigned_to,
                "answers": {},
                "assessment_ids": [],
            }
            self.items[queue_id].append(item)
            created.append(item)
        return created

    def _queue_view(self, queue_id: str) -> dict[str, Any]:
        items = self.items[queue_id]
        return {
            **self.queues[queue_id],
            "item_count": len(items),
            "pending_count": sum(1 for i in items if i["status"] == "pending"),
        }

    def _hidden_queue(self, q: str, user: str) -> httpx.Response | None:
        """A personal queue belongs to its owner: anyone else gets a 404, reviewers or not."""
        queue = self.queues.get(q)
        if queue is None or queue["owner"] != user:
            return _error(404, f"review queue {q!r} not found")
        return None

    def list_queues(self, *, user: str, **_: Any) -> httpx.Response:
        return _envelope(
            [self._queue_view(q) for q, v in self.queues.items() if v["owner"] == user]
        )

    def create_queue_route(
        self, *, body: dict[str, Any], user: str, scopes: set[str], **_: Any
    ) -> httpx.Response:
        if (denied := self.require(scopes)) is not None:
            return denied
        if any(q["name"] == body["name"] for q in self.queues.values()):
            return _error(409, f"review-queue name {body['name']!r} is already in use")
        return _envelope(self._queue_view(self._store_queue(body, user)["queue_id"]), 201)

    def get_queue(self, *, q: str, user: str, **_: Any) -> httpx.Response:
        if (hidden := self._hidden_queue(q, user)) is not None:
            return hidden
        return _envelope({"queue": self._queue_view(q), "items": self.items[q]})

    def update_queue(self, *, q: str, body: dict[str, Any], user: str, **_: Any) -> httpx.Response:
        if (hidden := self._hidden_queue(q, user)) is not None:
            return hidden
        self.queues[q].update(body)
        return _envelope(self._queue_view(q))

    def enqueue_route(
        self, *, q: str, body: dict[str, Any], user: str, scopes: set[str], **_: Any
    ) -> httpx.Response:
        if (denied := self.require(scopes)) is not None:
            return denied
        if (hidden := self._hidden_queue(q, user)) is not None:
            return hidden
        return _envelope(self._enqueue(q, body["trace_ids"], body.get("assigned_to")), 201)

    def submit_item(
        self, *, q: str, i: str, body: dict[str, Any], user: str, scopes: set[str], **_: Any
    ) -> httpx.Response:
        if (denied := self.require(scopes)) is not None:
            return denied
        if (hidden := self._hidden_queue(q, user)) is not None:
            return hidden
        item = next((x for x in self.items.get(q, []) if x["item_id"] == i), None)
        if item is None:
            return _error(404, f"review item {i!r} not found")
        if item["status"] != "pending":
            return _error(409, f"review item {i!r} is already {item['status']}")
        answers = body["answers"]
        by_key = {question["key"]: question for question in self.queues[q]["questions"]}
        if set(answers) - set(by_key):
            return _error(400, f"unknown answer keys: {sorted(set(answers) - set(by_key))}")
        for key, question in by_key.items():
            if answers.get(key) in (None, ""):
                if question["required"]:
                    return _error(400, f"missing required answer {key!r}")
                continue
            value = answers[key]
            kind = question["type"]
            if kind == "pass_fail" and not isinstance(value, bool):
                return _error(400, f"answer {key!r} must be a boolean")
            if kind == "categorical" and value not in question["options"]:
                return _error(400, f"answer {key!r} must be one of {question['options']}")
            if kind == "numeric" and (
                isinstance(value, bool) or not isinstance(value, int | float)
            ):
                return _error(400, f"answer {key!r} must be numeric")
            if kind == "text" and not isinstance(value, str):
                return _error(400, f"answer {key!r} must be text")
        if item["trace_id"] not in self.known_traces:
            return _error(
                502,
                "failed writing review answers to trace: RESOURCE_DOES_NOT_EXIST: Trace with ID "
                f"{item['trace_id']!r} not found. It may have been deleted.",
            )
        ids = []
        for key, value in answers.items():
            if value in (None, ""):
                continue
            assessment_id = self.new("a")
            ids.append(assessment_id)
            self.assessments.setdefault(item["trace_id"], []).append(
                {"name": key, "value": value, "source": user, "assessment_id": assessment_id}
            )
        item.update(
            status="completed", answers=dict(answers), assessment_ids=ids, completed_by=user
        )
        return _envelope(item)

    def alignment_examples(self, *, q: str, request: httpx.Request, **_: Any) -> httpx.Response:
        key = request.url.params["question_key"]
        examples = [
            {
                "label": item["answers"][key],
                "provenance": {"trace_id": item["trace_id"], "item_id": item["item_id"]},
            }
            for item in self.items[q]
            if item["status"] == "completed" and key in item["answers"]
        ]
        return _envelope({"queue_id": q, "question_key": key, "examples": examples, "skipped": []})

    def get_trace(self, *, t: str, **_: Any) -> httpx.Response:
        if t not in self.known_traces:
            return _error(404, f"trace {t!r} not found")
        return _envelope(
            {
                "trace_id": t,
                "request": self.known_traces[t],
                "assessments": [
                    {"name": a["name"], "value": a["value"], "source": a["source"]}
                    for a in self.assessments.get(t, [])
                ],
            }
        )


class Problem(Exception):
    """A capability that fails: the message becomes the step's error."""


class AsyncJob:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
