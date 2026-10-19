"""A small stateful stand-in for the routes cookbook 10 calls.

``test_cookbook_examples.py`` replays the real example script against this instead of a server, so
the script's call sequence and the shape of what it sends and reads stay covered offline. The fake
is a thin imitation of the server: what it reproduces faithfully is the *contract* the script
depends on (status codes, the fields it reads, the dataset-version arithmetic, the refusals), and
for the judge it applies the same mechanical rule as the lab's stand-in model
(``assets/stand-in-model/stand_in_model_server.py``), so the metrics it returns are the ones the
README documents. The real server, scorecard and alignment arithmetic are exercised by running the
script against a live stack (see the lab README) and by ``caliber/tests``.

``simulated=True`` imitates the default stack, where no model sits behind Evaluations or the judge.
"""

from __future__ import annotations

import importlib.util
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

ASSETS = (
    Path(__file__).resolve().parents[3]
    / "docs-site/cookbooks/10-judge-certification-human-review-lab/assets"
)
TEMPLATE_VARS = ("inputs", "outputs", "expectations", "conversation", "trace")


def _stand_in() -> Any:
    spec = importlib.util.spec_from_file_location(
        "cookbook10_stand_in", ASSETS / "stand-in-model" / "stand_in_model_server.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _envelope(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data})


def _error(status: int, detail: str, errors: list[dict[str, Any]] | None = None) -> httpx.Response:
    body: dict[str, Any] = {"detail": detail, "status_code": status}
    if errors:
        body["errors"] = errors
    return httpx.Response(status, json=body)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip().lower())


class FakeLabServer:
    def __init__(
        self, rows: list[dict[str, Any]], *, simulated: bool = False, rejected_key: bool = False
    ) -> None:
        self.rows = rows
        self.simulated = simulated
        self.rejected_key = rejected_key
        self.model = _stand_in()
        self.datasets: dict[str, dict[str, Any]] = {}
        self.examples: dict[str, list[dict[str, Any]]] = {}
        self.judges: dict[str, dict[str, Any]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.prompts: dict[str, str] = {}
        self.queue_items: dict[str, dict[str, Any]] = {}
        self.trace_row: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.archived: list[str] = []
        self._ids: Counter[str] = Counter()

    # -- plumbing ------------------------------------------------------------------------
    def _new(self, prefix: str) -> str:
        self._ids[prefix] += 1
        return f"{prefix}-{self._ids[prefix]}"

    def client(self, caliber_client_cls: Any) -> Any:
        return caliber_client_cls(
            "https://caliber.test",
            token="calpat_test",
            http_client=httpx.Client(transport=httpx.MockTransport(self.handle)),
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.rsplit("/caliber", 1)[-1]
        self.calls.append(f"{request.method} {path}")
        body = json.loads(request.content) if request.content else None
        for method, pattern, handler in self._routes():
            match = re.fullmatch(pattern, path)
            if request.method == method and match:
                response: httpx.Response = handler(body, request, *match.groups())
                return response
        raise AssertionError(  # pragma: no cover - guards against an unstubbed call
            f"example called an unstubbed route: {request.method} {path}"
        )

    def _routes(self) -> list[tuple[str, str, Any]]:
        return [
            ("GET", r"/cookbooks", self.cookbooks),
            ("GET", r"/me", lambda *_: _envelope({"user_id": "@alice"})),
            ("POST", r"/eval-datasets", self.create_dataset),
            ("GET", r"/eval-datasets/([^/]+)", self.get_dataset),
            ("GET", r"/eval-datasets/([^/]+)/examples", self.list_examples),
            ("POST", r"/eval-datasets/([^/]+)/examples", self.add_example),
            ("POST", r"/eval-datasets/([^/]+)/examples/from-trace", self.add_from_trace),
            ("POST", r"/judges", self.create_judge),
            ("POST", r"/judges/([^/]+)/alignment", self.alignment),
            ("POST", r"/prompts", self.create_prompt),
            ("POST", r"/evaluations", self.create_evaluation),
            ("GET", r"/evaluations/([^/]+)", self.get_evaluation),
            ("POST", r"/review-queues", self.create_queue),
            ("POST", r"/review-queues/([^/]+)/items", self.enqueue),
            ("POST", r"/review-queues/([^/]+)/items/([^/]+)/submit", self.submit),
            ("GET", r"/review-queues/([^/]+)/alignment-examples", self.alignment_examples),
            ("PATCH", r"/judges/([^/]+)", self.archive_judge),
            ("PATCH", r"/eval-datasets/([^/]+)", self.archive_dataset),
            ("PATCH", r"/review-queues/([^/]+)", self.archive_queue),
            ("DELETE", r"/prompts/([^/]+)", self.delete_prompt),
            ("POST", r"/prompts/calibration/runs", self.calibration_run),
            ("GET", r"/jobs/([^/]+)", self.get_job),
        ]

    # -- cookbooks, test sets ------------------------------------------------------------
    def cookbooks(self, body: Any, request: httpx.Request) -> httpx.Response:
        steps = [{"id": f"10.{n}", "title": f"step {n}", "route": "/judges"} for n in range(1, 7)]
        recipe = {
            "id": "10",
            "title": "Trustworthy Evaluation",
            "steps": steps,
            "readiness": {"status": "ready", "checks": []},
        }
        return _envelope({"recipes": [recipe]})

    def create_dataset(self, body: Any, request: httpx.Request) -> httpx.Response:
        if any(d["name"] == body["name"] for d in self.datasets.values()):
            return _error(409, f"eval-dataset name {body['name']!r} is already in use")
        dataset_id = self._new("ED")
        self.datasets[dataset_id] = {"dataset_id": dataset_id, "name": body["name"], "version": 1}
        self.examples[dataset_id] = []
        return _envelope(self.datasets[dataset_id], 201)

    def get_dataset(self, body: Any, request: httpx.Request, dataset_id: str) -> httpx.Response:
        return _envelope(self.datasets[dataset_id])

    def list_examples(self, body: Any, request: httpx.Request, dataset_id: str) -> httpx.Response:
        return _envelope(self.examples[dataset_id])

    def add_example(self, body: Any, request: httpx.Request, dataset_id: str) -> httpx.Response:
        assert set(body) <= {"input", "expected", "tags", "weight"}, body
        self.datasets[dataset_id]["version"] += 1  # every appended example is a new version
        example = {"example_id": self._new("EX"), "dataset_id": dataset_id, **body}
        self.examples[dataset_id].append(example)
        return _envelope(example, 201)

    def add_from_trace(self, body: Any, request: httpx.Request, dataset_id: str) -> httpx.Response:
        assert body["trace_id"] in self.trace_row, "from-trace needs a known trace"
        assert "judge-disagreement" in body["tags"]
        return self.add_example(
            {k: v for k, v in body.items() if k != "trace_id"}, request, dataset_id
        )

    # -- judges --------------------------------------------------------------------------
    def create_judge(self, body: Any, request: httpx.Request) -> httpx.Response:
        used = set(re.findall(r"\{\{\s*(\w+)\s*\}\}", body["instructions"]))
        unsupported = sorted(used - set(TEMPLATE_VARS))
        if not used or unsupported:
            names = ", ".join("{{ " + name + " }}" for name in unsupported)
            return _error(
                400,
                "request body validation failed",
                [
                    {
                        "loc": ["instructions"],
                        "msg": f"Value error, unsupported template variable(s) {names}",
                        "type": "value_error",
                    }
                ],
            )
        judge_id = self._new("JDG")
        self.judges[judge_id] = {
            "judge_id": judge_id,
            "name": body["name"],
            "instructions": body["instructions"],
        }
        return _envelope(self.judges[judge_id], 201)

    def _verdict(self, judge_id: str, outputs: str, expectations: dict[str, Any]) -> bool:
        strict = "faithful" in self.judges[judge_id]["instructions"].lower()
        rule = self.model.strict_faithfulness if strict else self.model.lenient_faithfulness
        return bool(rule(outputs, expectations)[0])

    def alignment(self, body: Any, request: httpx.Request, judge_id: str) -> httpx.Response:
        examples = body["examples"]
        assert examples, "alignment needs at least one example"
        assert all(set(e) <= {"inputs", "outputs", "expectations", "label"} for e in examples)
        if self.simulated:
            error = "judge invocation failed: OPENAI_API_KEY environment variable must be set"
            per = [
                {
                    "outputs": e["outputs"],
                    "human_label": e["label"],
                    "judge_label": None,
                    "judge_score": None,
                    "agree": False,
                    "error": error,
                }
                for e in examples
            ]
            return _envelope(_alignment_result(len(examples), [], [], per))
        judge = [
            self._verdict(judge_id, e["outputs"], e.get("expectations") or {}) for e in examples
        ]
        human = [bool(e["label"]) for e in examples]
        per = [
            {
                "outputs": e["outputs"],
                "human_label": h,
                "judge_label": j,
                "judge_score": 1.0 if j else 0.0,
                "agree": j == h,
                "error": None,
            }
            for e, j, h in zip(examples, judge, human, strict=True)
        ]
        return _envelope(_alignment_result(len(examples), judge, human, per))

    # -- prompts, evaluations ------------------------------------------------------------
    def create_prompt(self, body: Any, request: httpx.Request) -> httpx.Response:
        self.prompts[body["name"]] = body["template"]
        return _envelope({"name": body["name"], "version": 1}, 201)

    def create_evaluation(self, body: Any, request: httpx.Request) -> httpx.Response:
        dataset_id = body["dataset_id"]
        rows = self.examples[dataset_id]
        if not rows:
            return _error(400, f"eval dataset {dataset_id!r} has no examples to score")
        if self.simulated:
            return _error(400, "evaluation requires a real LLM provider; set CALIBER_LLM_PROVIDER")
        if self.rejected_key:  # a real provider that refuses the key: the run is saved as failed
            run_id = self._new("EVR")
            self.runs[run_id] = {
                "run_id": run_id,
                "dataset_id": dataset_id,
                "status": "failed",
                "n_examples": len(rows),
                "aggregate": {},
                "results": [],
                "error_message": "Error code: 401 - Incorrect API key provided: sk-REDACTED.",
            }
            return _envelope(self.runs[run_id], 201)
        by_input = {json.dumps(r["input"], sort_keys=True): r for r in self.rows}
        candidate = body.get("predict_target") == "prompt"
        results = []
        for example in rows:
            # a candidate run may carry the evidence in the input (real-model tier); the recorded
            # reply is keyed on the ticket alone
            ticket = {k: v for k, v in example["input"].items() if k != "evidence"}
            source = by_input[json.dumps(ticket, sort_keys=True)]
            text = self._reply(source, example, candidate)
            scores: dict[str, float] = {}
            for scorer in body["scorers"]:
                if scorer == "non_empty":
                    scores[scorer] = 1.0 if text.strip() else 0.0
                elif scorer == "contains_expected":
                    gold = example["expected"]["expected"]
                    scores[scorer] = 1.0 if _normalize(gold) in _normalize(text) else 0.0
                else:
                    scores[scorer] = (
                        1.0
                        if self._verdict(scorer.removeprefix("Judge."), text, example["expected"])
                        else 0.0
                    )
            row_score = round(sum(scores.values()) / len(scores), 4)
            results.append(
                {
                    "prediction": text,
                    "scores": scores,
                    "score": row_score,
                    "error": None,
                    "passed": row_score >= body.get("pass_threshold", 0.5),
                }
            )
        n = len(results)
        aggregate = {s: round(sum(r["scores"][s] for r in results) / n, 4) for s in body["scorers"]}
        run_id = self._new("EVR")
        self.runs[run_id] = {
            "run_id": run_id,
            "dataset_id": dataset_id,
            "status": "completed",
            "label": body.get("label", ""),
            "n_examples": n,
            "passed_count": sum(r["passed"] for r in results),
            "overall_score": round(sum(r["score"] for r in results) / n, 4),
            "pass_rate": round(sum(r["passed"] for r in results) / n, 4),
            "aggregate": aggregate,
            "results": results,
        }
        return _envelope(self.runs[run_id], 201)

    def _reply(self, source: dict[str, Any], example: dict[str, Any], candidate: bool) -> str:
        """The model's reply: the candidate replays the recorded one, the baseline a non-answer."""
        if not candidate:
            return str(self.model.BASELINE_REPLY)
        return str(source["outputs"].get("reply") or source["outputs"].get("summary") or "")

    def get_evaluation(self, body: Any, request: httpx.Request, run_id: str) -> httpx.Response:
        return _envelope(self.runs[run_id])

    # -- review queues -------------------------------------------------------------------
    def create_queue(self, body: Any, request: httpx.Request) -> httpx.Response:
        keys = [q["key"] for q in body["questions"]]
        assert keys == ["faithful", "evidence"], keys
        return _envelope({"queue_id": "RVQ-1", "name": body["name"]}, 201)

    def enqueue(self, body: Any, request: httpx.Request, queue_id: str) -> httpx.Response:
        items = []
        for trace_id in body["trace_ids"]:
            item_id = self._new("RVI")
            self.queue_items[item_id] = {"trace_id": trace_id, "status": "pending", "answers": {}}
            items.append({"item_id": item_id, "trace_id": trace_id, "status": "pending"})
        return _envelope(items, 201)

    def submit(
        self, body: Any, request: httpx.Request, queue_id: str, item_id: str
    ) -> httpx.Response:
        assert set(body["answers"]) == {"faithful", "evidence"}, body
        item = self.queue_items[item_id]
        item.update(status="completed", answers=body["answers"])
        return _envelope({"item_id": item_id, "status": "completed"})

    def alignment_examples(
        self, body: Any, request: httpx.Request, queue_id: str
    ) -> httpx.Response:
        assert request.url.params["question_key"] == "faithful"
        examples = []
        for item_id, item in self.queue_items.items():
            row = self.trace_row[item["trace_id"]]
            text = row["outputs"].get("reply") or row["outputs"].get("summary") or ""
            examples.append(
                {
                    "inputs": {"trace_id": item["trace_id"], "request": json.dumps(row["input"])},
                    "outputs": text,
                    "expectations": {"evidence": item["answers"]["evidence"]},
                    "label": item["answers"]["faithful"],
                    "provenance": {
                        "queue_id": queue_id,
                        "item_id": item_id,
                        "trace_id": item["trace_id"],
                    },
                }
            )
        return _envelope({"queue_id": queue_id, "examples": examples, "skipped": []})

    # -- cleanup -------------------------------------------------------------------------
    def archive_judge(self, body: Any, request: httpx.Request, judge_id: str) -> httpx.Response:
        assert body == {"status": "archived"}
        self.archived.append(judge_id)
        return _envelope(self.judges[judge_id])

    def archive_dataset(self, body: Any, request: httpx.Request, dataset_id: str) -> httpx.Response:
        assert body == {"status": "archived"}
        self.archived.append(dataset_id)
        return _envelope(self.datasets[dataset_id])

    def archive_queue(self, body: Any, request: httpx.Request, queue_id: str) -> httpx.Response:
        assert body == {"status": "archived"}
        self.archived.append(queue_id)
        return _envelope({"queue_id": queue_id, "status": "archived"})

    def delete_prompt(self, body: Any, request: httpx.Request, name: str) -> httpx.Response:
        self.archived.append(name)
        return _envelope({"deleted": name})

    # -- calibration (simulated stack) ----------------------------------------------------
    def calibration_run(self, body: Any, request: httpx.Request) -> httpx.Response:
        dataset = self.datasets[body["eval_dataset_id"]]
        assert self.examples[body["eval_dataset_id"]], "calibration needs a populated test set"
        self.job = {
            "job_id": "RFN-10",
            "status": "candidate_ready",
            "eval_results": {
                "provider": "fake",
                "simulated": True,
                "n_examples": len(self.examples[body["eval_dataset_id"]]),
                "eval_dataset_version": dataset["version"],
                "gate": {"passed": True},
            },
        }
        return _envelope({"item": {"item_id": "FB-10"}, "job": {"job_id": "RFN-10"}}, 201)

    def get_job(self, body: Any, request: httpx.Request, job_id: str) -> httpx.Response:
        return _envelope(self.job)


def _alignment_result(
    n: int, judge: list[bool], human: list[bool], per: list[dict[str, Any]]
) -> dict[str, Any]:
    pairs = Counter(zip(judge, human, strict=True))
    scored = len(judge)
    agree = sum(a == b for a, b in zip(judge, human, strict=True)) / scored if scored else 0.0
    expected = (
        sum((judge.count(x) / scored) * (human.count(x) / scored) for x in (True, False))
        if scored
        else 0.0
    )
    kappa = 0.0 if scored == 0 else (1.0 if expected >= 1 else (agree - expected) / (1 - expected))
    return {
        "n": n,
        "scored": scored,
        "agreement_rate": round(agree, 4),
        "cohen_kappa": round(kappa, 4),
        "threshold": 0.5,
        "confusion": {
            "true_pos": pairs[(True, True)],
            "false_pos": pairs[(True, False)],
            "true_neg": pairs[(False, False)],
            "false_neg": pairs[(False, True)],
        },
        "per_example": per,
    }
