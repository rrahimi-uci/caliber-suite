"""A small stateful stand-in for the routes cookbook 06 calls.

``test_cookbook_examples.py`` runs the real example script against this instead of a server, so the
script's call sequence and the shape of what it sends and reads stay covered offline. It is a
*contract* fake, not a retrieval engine: it knows the lab's question set and answers each query from
a table (the clause's chunk first or second depending on the mode, a similarity score by kind of
question). What the real server does with those questions -- the embeddings, the ranking, the
Recall@k arithmetic -- is exercised by running the script against a live stack (see the lab README).
What this fake does faithfully is what the script depends on: status codes, the build life cycle
(queued, then processing, then completed or failed; a finished build activates itself), the
activation
history rollback walks, the failed-source bookkeeping, and the response fields the script reads.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

UNREADABLE = "unreadable/scanned-handbook.pdf"
TRAP = "trap"


def _ok(data: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json={"data": data})


def _error(status: int, detail: str) -> httpx.Response:
    return httpx.Response(status, json={"detail": detail, "status_code": status})


class FakeKnowledgeServer:
    def __init__(self, corpus: dict[str, bytes], qa_rows: list[dict[str, Any]]) -> None:
        self.corpus = corpus
        self.questions = {r["input"]["question"]: r for r in qa_rows}
        self.calls: list[str] = []
        self.uploaded: dict[str, set[str]] = {}
        self.kb: dict[str, Any] = {}
        self.versions: dict[str, dict[str, Any]] = {}
        self.polls: dict[str, int] = {}
        self.activations: list[tuple[str, str | None]] = []  # (activated, previous)
        self.datasets: dict[str, list[dict[str, Any]]] = {}
        self.runs: dict[str, dict[str, Any]] = {}
        self.workflow: dict[str, Any] = {}
        self.draft: dict[str, Any] = {}
        self.queue_items: dict[str, dict[str, Any]] = {}
        self.sequence = 0

    # -- plumbing ----------------------------------------------------------------------------
    def client(self, caliber_client_cls: Any) -> Any:
        return caliber_client_cls(
            "https://caliber.test",
            token="calpat_test",
            http_client=httpx.Client(transport=httpx.MockTransport(self.handle)),
        )

    def next_id(self, prefix: str) -> str:
        self.sequence += 1
        return f"{prefix}-{self.sequence:02d}"

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.rsplit("/caliber", 1)[-1]
        self.calls.append(f"{request.method} {path}")
        body: Any = None
        if request.content and request.headers.get("content-type", "").startswith(
            "application/json"
        ):
            body = json.loads(request.content)
        for method, pattern, handler in self.routes():
            match = re.fullmatch(pattern, path)
            if request.method == method and match:
                response: httpx.Response = handler(request, body, *match.groups())
                return response
        raise AssertionError(f"example called an unstubbed route: {request.method} {path}")

    def routes(self) -> list[tuple[str, str, Any]]:
        return [
            ("GET", r"/cookbooks", self.cookbooks),
            ("POST", r"/cookbooks/06/install", self.install),
            ("GET", r"/readiness", lambda r, b: _ok({"ready": True, "simulated": ["eval", "llm"]})),
            ("GET", r"/me", lambda r, b: _ok({"user_id": "admin"})),
            (
                "GET",
                r"/object-store/status",
                lambda r, b: _ok({"connected": True, "bucket_count": 1}),
            ),
            ("POST", r"/object-store/buckets", lambda r, b: _ok({"name": b["name"]}, 201)),
            ("POST", r"/object-store/buckets/([^/]+)/objects", self.upload),
            ("GET", r"/object-store/buckets/([^/]+)/objects", self.list_objects),
            ("GET", r"/knowledge-bases/options", self.options),
            ("POST", r"/knowledge-bases", self.create_kb),
            ("GET", r"/knowledge-bases/([^/]+)", self.get_kb),
            ("POST", r"/knowledge-bases/([^/]+)/versions", self.create_version),
            ("POST", r"/knowledge-bases/([^/]+)/versions/([^/]+)/activate", self.activate),
            ("POST", r"/knowledge-bases/([^/]+)/rollback", self.rollback),
            ("POST", r"/knowledge-bases/([^/]+)/calibrate", self.calibrate),
            ("POST", r"/knowledge-bases/([^/]+)/baseline", self.baseline),
            ("GET", r"/knowledge-base-versions/([^/]+)", self.get_version),
            ("GET", r"/knowledge-base-versions/([^/]+)/chunks", self.chunks),
            ("GET", r"/knowledge-base-versions/([^/]+)/sources", self.sources),
            ("POST", r"/knowledge/query", self.query),
            ("POST", r"/eval-datasets", self.create_dataset),
            ("POST", r"/eval-datasets/([^/]+)/examples", self.add_example),
            ("GET", r"/eval-datasets/([^/]+)/examples", self.examples),
            ("GET", r"/workflow-versions/([^/]+)", lambda r, b, vid: _ok(self.draft)),
            ("PATCH", r"/workflow-versions/([^/]+)", self.patch_version),
            (
                "POST",
                r"/workflow-versions/([^/]+)/validate",
                lambda r, b, vid: _ok({"valid": True}),
            ),
            ("POST", r"/workflow-versions/([^/]+)/run", self.run_workflow),
            ("PATCH", r"/workflows/([^/]+)", self.patch_workflow),
            ("GET", r"/workflow-runs/([^/]+)", lambda r, b, rid: _ok(self.runs[rid])),
            ("POST", r"/review-queues", self.create_queue),
            ("POST", r"/review-queues/([^/]+)/items", self.enqueue),
            ("POST", r"/review-queues/([^/]+)/items/([^/]+)/submit", self.submit),
            (
                "POST",
                r"/prompts",
                lambda r, b: _ok({"agent_id": "AG-06", "name": b["name"], "version": 1}, 201),
            ),
            ("GET", r"/prompts/([^/]+)/versions", lambda r, b, name: _ok([{"version": 1}])),
            ("POST", r"/judges", lambda r, b: _ok({"judge_id": "JDG-06", "name": b["name"]}, 201)),
            ("POST", r"/evaluations", self.evaluate),
        ]

    # -- cookbook + object store ------------------------------------------------------------
    def cookbooks(self, request: httpx.Request, body: Any) -> httpx.Response:
        return _ok(
            {
                "recipes": [
                    {
                        "id": "06",
                        "title": "Grounded Knowledge Assistant",
                        "summary": "x",
                        "prerequisites": [
                            "Embedding and chat providers for live retrieval and answers"
                        ],
                        "readiness": {"status": "configuration_required", "checks": []},
                    }
                ]
            }
        )

    def install(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        assert body["acknowledge_prerequisites"] is True
        self.workflow = {"workflow_id": "WF-06", "name": body["name"], "status": "paused"}
        manifest = {
            "nodes": {
                "knowledge": {"id": "knowledge", "type": "knowledge_query"},
                "confidence": {"id": "confidence", "type": "data_transform", "config": {}},
            }
        }
        self.draft = {
            "version_id": "WFV-06",
            "workflow_id": "WF-06",
            "status": "draft",
            "manifest": manifest,
            "manifest_hash": "hash-0",
        }
        return _ok(
            {
                "recipe": {"id": "06"},
                "workflow": self.workflow,
                "version": {"version_id": "WFV-06", "status": "draft"},
            }
        )

    def upload(self, request: httpx.Request, body: Any, bucket: str) -> httpx.Response:
        raw = request.content.decode("latin-1")
        filename = re.search(r'filename="([^"]+)"', raw).group(1)  # type: ignore[union-attr]
        prefix = re.search(r'name="prefix"\r\n\r\n([^\r]*)', raw)
        key = (prefix.group(1) if prefix else "") + filename
        self.uploaded.setdefault(bucket, set()).add(key)
        return _ok({"bucket": bucket, "key": key, "size": 1}, 201)

    def list_objects(self, request: httpx.Request, body: Any, bucket: str) -> httpx.Response:
        prefix = request.url.params.get("prefix", "")
        keys = sorted(k for k in self.uploaded.get(bucket, set()) if k.startswith(prefix))
        return _ok({"objects": [{"key": k, "size": 1} for k in keys], "prefixes": []})

    # -- knowledge bases --------------------------------------------------------------------
    def options(self, request: httpx.Request, body: Any) -> httpx.Response:
        return _ok(
            {
                "chunking_strategies": [{"id": "markdown", "available": True}],
                "embedding_models": [
                    {
                        "id": "sentence-transformers/all-MiniLM-L6-v2",
                        "available": True,
                        "unavailable_reason": None,
                    }
                ],
            }
        )

    def _new_version(self, body: dict[str, Any], sources: list[dict[str, str]]) -> dict[str, Any]:
        size = int(body.get("chunking_config", {}).get("chunk_size", 1400))
        chunks_per_doc = 3 if size > 1000 else 6
        unreadable = [s for s in sources if s["path"] == UNREADABLE]
        readable = [s for s in sources if s["path"] != UNREADABLE]
        documents = len(self.corpus) if any(s["kind"] == "folder" for s in readable) else 0
        version_id = self.next_id("KBV")
        version = {
            "knowledge_base_version_id": version_id,
            "knowledge_base_id": "KB-06",
            "version_number": len(self.versions) + 1,
            "status": "queued",
            "embedding_provider": "huggingface",
            "embedding_model": body["embedding_model"],
            "embedding_dimension": 384,
            "error_summary": None,
            "final_status": "completed" if documents else "failed",
            "summary": {
                "processed_sources": documents,
                "failed_sources": len(unreadable),
                "chunk_count": documents * chunks_per_doc // 1,
                "total_chars": 29785,
                "duration_seconds": 0.4,
            },
            "failed": unreadable,
        }
        self.versions[version_id] = version
        self.polls[version_id] = 0
        return version

    def create_kb(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        version = self._new_version(body, body["sources"])
        self.kb = {
            "knowledge_base_id": "KB-06",
            "name": body["name"],
            "status": "active",
            "active_version_id": None,
            "baseline_run_id": None,
        }
        run = {"knowledge_base_run_id": "KBR-06", "status": "queued"}
        return _ok({"knowledge_base": self.kb, "version": self._public(version), "run": run}, 201)

    def create_version(
        self, request: httpx.Request, body: dict[str, Any], kb_id: str
    ) -> httpx.Response:
        sources = body.get("sources") or [{"kind": "folder", "path": "policies/"}]
        version = self._new_version(body, sources)
        return _ok({"version": self._public(version), "run": {"status": "queued"}}, 201)

    @staticmethod
    def _public(version: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in version.items() if k not in {"final_status", "failed"}}

    def get_version(self, request: httpx.Request, body: Any, version_id: str) -> httpx.Response:
        version = self.versions[version_id]
        self.polls[version_id] += 1
        if self.polls[version_id] == 1:
            version["status"] = "processing"
        elif version["status"] in {"queued", "processing"}:
            version["status"] = version["final_status"]
            if version["status"] == "completed":
                self._activate(version_id)  # a finished build activates itself, unaudited
            else:
                version["error_summary"] = (
                    "The selected files did not produce any chunks. "
                    "Check the source types or extraction settings."
                )
        return _ok(self._public(version))

    def _activate(self, version_id: str, *, audited: bool = False) -> None:
        previous = self.kb["active_version_id"]
        self.kb["active_version_id"] = version_id
        if audited:
            self.activations.append((version_id, previous))

    def get_kb(self, request: httpx.Request, body: Any, kb_id: str) -> httpx.Response:
        return _ok(self.kb)

    def activate(
        self, request: httpx.Request, body: Any, kb_id: str, version_id: str
    ) -> httpx.Response:
        if self.versions[version_id]["status"] != "completed":
            return _error(409, "only completed versions can be activated")
        self._activate(version_id, audited=True)
        return _ok(self.kb)

    def rollback(self, request: httpx.Request, body: Any, kb_id: str) -> httpx.Response:
        current = self.kb["active_version_id"]
        for activated, previous in reversed(self.activations):
            if activated == current and previous and previous != current:
                self.kb["active_version_id"] = previous
                return _ok(self.kb)
        return _error(409, "no recorded prior active version")

    def chunks(self, request: httpx.Request, body: Any, version_id: str) -> httpx.Response:
        return _ok(
            [
                {"source_name": name, "source_key": f"policies/{name}", "content": "x"}
                for name in sorted(self.corpus)
                for _ in range(2)
            ]
        )

    def sources(self, request: httpx.Request, body: Any, version_id: str) -> httpx.Response:
        rows = [{"status": "completed", "object_name": n} for n in sorted(self.corpus)]
        for failed in self.versions[version_id]["failed"]:
            rows.append(
                {
                    "status": "failed",
                    "object_name": failed["path"].rsplit("/", 1)[-1],
                    "error_summary": "failed to extract pdf: startxref not found",
                }
            )
        return _ok(rows)

    def query(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        version = self.versions[body["version_ids"][0]]
        if version["status"] != "completed":
            return _ok(
                {
                    "question": body["question"],
                    "versions": [
                        {
                            "retrieved_chunks": [],
                            "citations": [],
                            "answer": None,
                            "answer_error": "Version is not ready for retrieval.",
                        }
                    ],
                }
            )
        row = self.questions.get(body["question"])
        mode = body["retrieval_modes"][0]
        chunks = self._ranked(row, mode)
        citations = [
            {
                "chunk_id": c["chunk_id"],
                "label": f"[{i + 1}] {c['source_name']}",
                "score": c["score"],
            }
            for i, c in enumerate(chunks)
        ]
        return _ok(
            {
                "question": body["question"],
                "versions": [
                    {
                        "retrieval_mode": mode,
                        "answer": f"Question: {body['question']}\n\nMost relevant retrieved evidence:",  # noqa: E501
                        "answer_error": None,
                        "citations": citations,
                        "retrieved_chunks": chunks[: body["top_k"]],
                    }
                ],
            }
        )

    def _ranked(self, row: dict[str, Any] | None, mode: str) -> list[dict[str, Any]]:
        def chunk(n: int, source: str, text: str, score: float) -> dict[str, Any]:
            return {
                "chunk_id": f"KBC-{source}-{n}",
                "source_name": source,
                "source_key": f"policies/{source}",
                "content": text,
                "score": score,
            }

        outside = row is None or (not row["expected"]["sources"] and TRAP not in row["tags"])
        filler = chunk(9, "remote-work-policy.md", "unrelated text", 0.3 if outside else 0.5)
        if row is None:
            return [filler, filler, filler]
        expected = row["expected"]
        sources = expected["sources"]
        if not sources:  # unanswerable: a trap's policy exists, an outside question matches nothing
            score = 0.6 if TRAP in row["tags"] else 0.3
            return [chunk(1, "support-sla-policy.md", "digest", score), filler, filler]
        holder = chunk(2, sources[0], f"...{expected['evidence']}...", 0.6)
        number = int(row["id"][1:])
        # hybrid ranks the clause first for more questions than dense does
        first = (number % 4 != 0) if mode == "hybrid" else (number % 2 == 1)
        ranked = [holder, filler, filler] if first else [filler, holder, filler]
        if len(sources) > 1:  # the contradiction questions retrieve both refund documents together
            ranked = [holder, chunk(3, sources[1], "faq text", 0.57), filler]
        return ranked

    def baseline(self, request: httpx.Request, body: dict[str, Any], kb_id: str) -> httpx.Response:
        self.kb["baseline_run_id"] = body["test_run_id"]
        return _ok(self.kb)

    def calibrate(self, request: httpx.Request, body: dict[str, Any], kb_id: str) -> httpx.Response:
        assert body["eval_dataset_id"] in self.datasets
        recall = {("dense", 1): 0.878, ("hybrid", 1): 0.9268}.get(
            (body["retrieval_mode"], body["top_k"]), 1.0
        )
        return _ok(
            {
                "test_run_id": self.next_id("KBTR"),
                "metrics": {
                    "recall_at_k": recall,
                    "ndcg_at_k": 0.95,
                    "faithfulness": None,
                    "answer_correctness": None,
                    "question_count": len(self.datasets[body["eval_dataset_id"]]),
                },
            }
        )

    # -- test sets ---------------------------------------------------------------------------
    def create_dataset(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        dataset_id = self.next_id("ED")
        self.datasets[dataset_id] = []
        return _ok({"dataset_id": dataset_id, "name": body["name"]}, 201)

    def add_example(
        self, request: httpx.Request, body: dict[str, Any], dataset_id: str
    ) -> httpx.Response:
        assert set(body) <= {"input", "expected", "tags"}, (
            f"unexpected example fields: {sorted(body)}"
        )
        self.datasets[dataset_id].append(body)
        return _ok({"example_id": self.next_id("EX")}, 201)

    def examples(self, request: httpx.Request, body: Any, dataset_id: str) -> httpx.Response:
        return _ok([{"example_id": f"EX-{i}"} for i, _ in enumerate(self.datasets[dataset_id])])

    # -- workflow ----------------------------------------------------------------------------
    def patch_version(
        self, request: httpx.Request, body: dict[str, Any], version_id: str
    ) -> httpx.Response:
        assert body["manifest_hash"] == self.draft["manifest_hash"], "stale manifest_hash"
        self.draft["manifest"] = body["manifest"]
        self.draft["manifest_hash"] = "hash-1"
        return _ok(self.draft)

    def patch_workflow(
        self, request: httpx.Request, body: dict[str, Any], workflow_id: str
    ) -> httpx.Response:
        self.workflow.update(body)
        return _ok(self.workflow)

    def run_workflow(
        self, request: httpx.Request, body: dict[str, Any], version_id: str
    ) -> httpx.Response:
        if self.workflow["status"] == "paused":
            return _error(409, "workflow is paused; resume it before running")
        knowledge = self.draft["manifest"]["nodes"]["knowledge"]
        assert knowledge["knowledge_base_id"] == "KB-06" and knowledge["retrieval_modes"] == [
            "dense"
        ]
        signals = self.draft["manifest"]["nodes"]["confidence"]["config"]["signals"]
        gate = next(s["value"] for s in signals if s["name"] == "top-chunk-score")
        row = self.questions.get(body["input"])
        ranked = self._ranked(row, "dense")
        top = ranked[0]
        confident = top["score"] >= gate
        run_id = self.next_id("WR")
        resolved = [self.kb["active_version_id"]]
        self.runs[run_id] = {
            "workflow_run_id": run_id,
            "workflow_id": "WF-06",
            "status": "completed",
            "trace_id": f"tr-{run_id}",
            "summary": {
                "steps": [
                    {
                        "node_id": "knowledge",
                        "output_by_port": {
                            "result": {
                                "resolved_version_ids": resolved,
                                "versions": [{"retrieved_chunks": ranked, "citations": ranked}],
                            }
                        },
                    },
                    {
                        "node_id": "confidence",
                        "output_by_port": {
                            "result": {
                                "confidence": 0.8 if confident else 0.25,
                                "needs_review": not confident,
                                "matched_signals": ["has-citations"]
                                + (["top-chunk-score"] if confident else []),
                            }
                        },
                    },
                ]
            },
        }
        return _ok({"workflow_run_id": run_id, "status": "completed"})

    # -- review queue, evaluation ----------------------------------------------------------
    def create_queue(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        return _ok({"queue_id": "RVQ-06", "name": body["name"]}, 201)

    def enqueue(
        self, request: httpx.Request, body: dict[str, Any], queue_id: str
    ) -> httpx.Response:
        items = []
        for trace_id in body["trace_ids"]:
            item = {
                "item_id": self.next_id("RVI"),
                "queue_id": queue_id,
                "trace_id": trace_id,
                "status": "pending",
            }
            self.queue_items[item["item_id"]] = item
            items.append(item)
        return _ok(items, 201)

    def submit(
        self, request: httpx.Request, body: dict[str, Any], queue_id: str, item_id: str
    ) -> httpx.Response:
        item = self.queue_items[item_id]
        item.update(status="completed", answers=body["answers"], assessment_ids=["a-06"])
        return _ok(item)

    def evaluate(self, request: httpx.Request, body: dict[str, Any]) -> httpx.Response:
        return _error(
            400,
            "evaluation requires a real LLM provider; set CALIBER_LLM_PROVIDER (openai/anthropic) "
            "and the matching API key, then retry.",
        )
