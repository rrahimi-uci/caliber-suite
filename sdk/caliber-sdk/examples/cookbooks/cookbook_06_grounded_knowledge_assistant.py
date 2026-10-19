"""Cookbook 06 (Grounded Knowledge Assistant), run end to end against a real server.

The lab's whole API path, from the policy documents to a verified outcome:

    upload the corpus -> build knowledge-base version 1 (queue + worker) -> read the chunks back
    -> query with citations and check the cited chunk holds the clause -> compare dense, hybrid
    and graph_hybrid over the 41 answerable questions -> check what a score gate can and cannot
    refuse -> calibrate (Recall@k, nDCG@k) -> build the tuned version 2, pin a baseline -> activate
    and roll back -> a build that skips an unreadable file, a build that fails outright -> install
    the answer workflow, run it and route the low-confidence runs to a review queue -> store the
    answer prompt and the faithfulness judge, and see the model-scored step refused on the
    simulated stack.

It exits 0 only when every documented outcome is verified. The assets it reads are the lab's own
files under ``docs-site/cookbooks/06-knowledge-retrieval-policy-qa-lab/assets/``.

Run it from the repository root, against a server started with the Cookbooks flag and an
S3-compatible object store (``scripts/cookbook-env.sh up lab06 --flags cookbooks --s3``)::

    source .tmp/cookbook-env/lab06/env.sh        # CALIBER_BASE_URL, CALIBER_S3_ENDPOINT, ...
    export CALIBER_TOKEN=calpat_...              # Settings > API tokens, or POST /auth/tokens
    PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python \\
        -m examples.cookbooks.cookbook_06_grounded_knowledge_assistant

``--cleanup SUFFIX`` removes what a run created (the suffix is in the first line it prints).

What is real and what is simulated on the default stack (``CALIBER_LLM_PROVIDER=fake``,
``CALIBER_EVAL_PROVIDER=fake``, ``CALIBER_ASSISTANT_ENGINE=fake``):

* Real: the object store (an S3 endpoint), the build worker, the chunking, the embeddings (the
  ``all-MiniLM-L6-v2`` model, run locally) and every retrieval ranking, so Recall@k, nDCG@k, the hit
  rates and the score gate are arithmetic on real retrieval.
* Simulated: the answer text (an extractive "most relevant evidence" digest, not a generated
  answer), the model-judged calibration metrics (Faithfulness and Answer-correctness come back
  ``null``) and scoring a prompt on the Evaluations page (refused with ``400``). A real provider
  changes those three and nothing else; the script prints what it saw.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from caliber_sdk import CaliberClient
from caliber_sdk.errors import CaliberAPIError
from examples.cookbooks._helpers import configuration_blockers, env_client, get_recipe

BUCKET = "policy-corpus"
PREFIX = "policies/"
UNREADABLE_KEY = "unreadable/scanned-handbook.pdf"
EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"

#: The two chunkings the lab compares: the markdown strategy's own defaults, then smaller chunks.
BASELINE_CHUNKING = {"chunk_size": 1400, "chunk_overlap": 120}
TUNED_CHUNKING = {"chunk_size": 500, "chunk_overlap": 60}

TOP_K = 3
#: ``verification.yaml``: the hybrid mode must put the clause's chunk in the top 3 for this share.
HYBRID_HIT_AT_3_MIN = 0.95
#: The top-1 dense similarity below which a question is treated as not covered by the corpus.
#: Chosen between the weakest answerable question (0.444 / 0.498 on the two versions) and the
#: strongest question that really is outside the corpus (0.384 / 0.377).
SCORE_GATE = 0.42
#: Questions that are unanswerable but share their vocabulary with a policy that exists: a score
#: gate cannot refuse them (``verification.yaml`` records why, and what does).
TRAP_TAG = "trap"

CONFIDENCE_REVIEW_THRESHOLD = 0.65


def assets_dir() -> Path:
    override = os.environ.get("COOKBOOK_06_ASSETS")
    if override:
        return Path(override)
    repo_root = Path(__file__).resolve().parents[4]
    return repo_root / "docs-site/cookbooks/06-knowledge-retrieval-policy-qa-lab/assets"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def load_corpus(assets: Path) -> dict[str, bytes]:
    sources = assets / "dataset/sources"
    return {
        path.name: path.read_bytes()
        for path in sorted(sources.glob("*.md"))
        if path.name != "README.md"
    }


def norm(text: str) -> str:
    """The comparison the dataset and the checks share: no stars, one space, lowercase."""
    return re.sub(r"\s+", " ", text.replace("*", "")).strip().lower()


def split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """A prompt asset: ``key: value`` frontmatter, then the literal template."""
    _, front, body = text.split("---\n", 2)
    meta: dict[str, str] = {}
    for line in front.splitlines():
        key, _, value = line.partition(":")
        if key.strip():
            meta[key.strip()] = value.strip().strip('"')
    return meta, body.strip()


class Report:
    """Collects ``ok`` lines the README quotes, and fails loudly on the first broken claim."""

    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def check(self, label: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"label": label, "ok": bool(ok), "detail": detail})
        print(
            f"{'ok  ' if ok else 'FAIL'} {label}" + (f"  [{detail}]" if detail else ""), flush=True
        )
        if not ok:
            raise AssertionError(f"{label}: {detail}")


def reason_of(exc: CaliberAPIError) -> str:
    return exc.detail or str(exc)


def expect_status(label: str, status: int, call: Callable[[], Any]) -> str:
    """Run ``call`` and require it to fail with HTTP ``status``; return the server's reason."""
    try:
        call()
    except CaliberAPIError as exc:
        assert exc.status_code == status, f"{label}: expected {status}, got {exc.status_code}"
        return f"{status} {reason_of(exc)}"
    raise AssertionError(f"{label}: expected HTTP {status}, but the call succeeded")


def wait_for_build(
    caliber: CaliberClient, version_id: str, *, timeout: float = 300.0, poll: float = 1.0
) -> dict[str, Any]:
    """Poll until the queued build has been claimed by the worker and has stopped."""
    deadline = time.monotonic() + timeout
    while True:
        version: dict[str, Any] = caliber.knowledge_bases.version(version_id)
        if version["status"] in ("completed", "failed"):
            return version
        if time.monotonic() > deadline:
            raise TimeoutError(
                f"version {version_id} is still {version['status']} after {timeout}s"
            )
        time.sleep(poll)


def ask(
    caliber: CaliberClient,
    version_id: str,
    question: str,
    *,
    mode: str = "dense",
    top_k: int = TOP_K,
) -> dict[str, Any]:
    result = caliber.knowledge_bases.query(
        version_ids=[version_id], question=question, top_k=top_k, retrieval_modes=[mode]
    )
    hits: dict[str, Any] = result["versions"][0]
    return hits


def evidence_rank(retrieved: dict[str, Any], evidence: str) -> int | None:
    """1-based rank of the first retrieved chunk whose text holds the supporting clause."""
    for rank, chunk in enumerate(retrieved["retrieved_chunks"], start=1):
        if norm(evidence) in norm(chunk["content"]):
            return rank
    return None


def sweep(
    caliber: CaliberClient, version_id: str, rows: list[dict[str, Any]], mode: str
) -> dict[str, Any]:
    """Where the supporting clause ranks, for every answerable question, in one retrieval mode."""
    ranks: dict[str, int | None] = {}
    for row in rows:
        retrieved = ask(caliber, version_id, row["input"]["question"], mode=mode)
        ranks[row["id"]] = evidence_rank(retrieved, row["expected"]["evidence"])
    total = len(ranks)
    return {
        "mode": mode,
        "questions": total,
        "hit_at_1": sum(1 for r in ranks.values() if r == 1),
        "hit_at_3": sum(1 for r in ranks.values() if r is not None),
        "misses": sorted(qid for qid, r in ranks.items() if r is None),
    }


def calibrate(
    caliber: CaliberClient, kb_id: str, version_id: str, dataset_id: str, mode: str, top_k: int
) -> dict[str, Any]:
    run = caliber.knowledge_bases.calibrate(
        kb_id, version_id=version_id, eval_dataset_id=dataset_id, retrieval_mode=mode, top_k=top_k
    )
    metrics = run["metrics"]
    return {
        "test_run_id": run["test_run_id"],
        "mode": mode,
        "top_k": top_k,
        "recall_at_k": round(metrics["recall_at_k"], 4),
        "ndcg_at_k": round(metrics["ndcg_at_k"], 4),
        "faithfulness": metrics.get("faithfulness"),
        "answer_correctness": metrics.get("answer_correctness"),
        "question_count": metrics["question_count"],
    }


def bind_knowledge_node(
    caliber: CaliberClient, version_id: str, kb_id: str
) -> tuple[Any, dict[str, Any]]:
    """Bind the installed template's knowledge node to the KB and gate confidence on the score.

    The template ships a ``confidence`` node whose signals read the citations of the query result.
    One more signal is added: the best chunk's similarity must reach :data:`SCORE_GATE`, the same
    gate the script just checked against the question set.
    """
    version = caliber.workflows.versions.get(version_id)
    manifest = version.manifest
    manifest["nodes"]["knowledge"].update(
        knowledge_base_id=kb_id, version_ids=[], retrieval_modes=["dense"], top_k=TOP_K
    )
    manifest["nodes"]["confidence"]["config"] = {
        "bias": 0.1,
        "review_threshold": CONFIDENCE_REVIEW_THRESHOLD,
        "signals": [
            {
                "name": "has-citations",
                "path": "versions.0.citations.0",
                "operator": "exists",
                "value": True,
                "weight": 0.15,
            },
            {
                "name": "top-chunk-score",
                "path": "versions.0.retrieved_chunks.0.score",
                "operator": "greater_or_equal",
                "value": SCORE_GATE,
                "weight": 0.55,
            },
        ],
    }
    manifest_hash = version.manifest_hash
    if manifest_hash is None:
        raise AssertionError("the workflow version reported no manifest_hash to edit against")
    updated = caliber.workflows.versions.update(
        version_id, manifest=manifest, manifest_hash=manifest_hash
    )
    return updated, manifest


def step_output(run: Any, node_id: str) -> dict[str, Any]:
    summary = (run.extra or {}).get("summary") or {}
    for step in summary.get("steps", []):
        if step.get("node_id") == node_id:
            return step.get("output_by_port") or {}
    return {}


def run(caliber: CaliberClient, *, suffix: str | None = None, poll: float = 1.0) -> dict[str, Any]:
    assets = assets_dir()
    suffix = suffix or secrets.token_hex(3)
    report = Report()
    check = report.check
    out: dict[str, Any] = {"suffix": suffix}
    print(f"cookbook 06 run {suffix}", flush=True)

    corpus = load_corpus(assets)
    qa_rows = load_jsonl(assets / "dataset/qa-eval.jsonl")
    calibration_rows = load_jsonl(assets / "dataset/calibration-questions.jsonl")
    answerable = [r for r in qa_rows if r["expected"]["sources"]]
    unanswerable = [r for r in qa_rows if not r["expected"]["sources"]]
    check(
        "the dataset is the one the README describes",
        len(corpus) == 11
        and len(qa_rows) == 48
        and len(answerable) == 41
        and len(unanswerable) == 7
        and len(calibration_rows) == 41,
        f"{len(corpus)} policy documents, {len(qa_rows)} questions "
        f"({len(answerable)} answerable, {len(unanswerable)} not), "
        f"{len(calibration_rows)} calibration pairs",
    )
    # every gold clause is a literal passage of its document, so the dataset cannot drift
    docs = {name: norm(body.decode("utf-8")) for name, body in corpus.items()}
    drifted = [
        r["id"]
        for r in answerable
        if not any(norm(r["expected"]["evidence"]) in docs[s] for s in r["expected"]["sources"])
    ]
    check(
        "every supporting clause is a literal passage of its document",
        not drifted,
        f"drifted: {drifted}",
    )

    # ---- 0. what the server has ------------------------------------------------------------
    recipe = get_recipe(caliber, "06")
    readiness = caliber.raw.get("/readiness")
    simulated = readiness.get("simulated") or []
    out["simulated"] = simulated
    status = caliber.object_store.status()
    check(
        "the object store is reachable",
        bool(status.get("connected")),
        f"{status.get('bucket_count')} buckets",
    )
    options = caliber.knowledge_bases.options()
    model = next(m for m in options["embedding_models"] if m["id"] == EMBEDDING_MODEL)
    check(
        "the embedding model is available",
        bool(model["available"]),
        model.get("unavailable_reason") or EMBEDDING_MODEL,
    )
    out["unmet_checks"] = configuration_blockers(recipe)

    # ---- 1. the corpus in the object store -------------------------------------------------
    caliber.object_store.create_bucket(BUCKET)
    for name, body in corpus.items():
        caliber.object_store.upload(
            BUCKET, filename=name, content=body, prefix=PREFIX, media_type="text/markdown"
        )
    caliber.object_store.upload(
        BUCKET,
        filename=Path(UNREADABLE_KEY).name,
        content=(assets / "dataset/unreadable/scanned-handbook.pdf").read_bytes(),
        prefix="unreadable/",
        media_type="application/pdf",
    )
    listed = sorted(
        o.key for o in caliber.object_store.objects(BUCKET, prefix=PREFIX, recursive=True)
    )
    check(
        "the corpus is in the bucket",
        listed == [PREFIX + name for name in sorted(corpus)],
        f"{len(listed)} objects under {BUCKET}/{PREFIX}",
    )

    # ---- 2. version 1: queue + worker ------------------------------------------------------
    kb_name = f"policy-qa-kb-{suffix}"
    created = caliber.knowledge_bases.create(
        kb_name,
        description="Policy QA lab: 11 HR, IT, finance, billing and security policies",
        source_bucket=BUCKET,
        sources=[{"kind": "folder", "path": PREFIX}],
        chunking_strategy="markdown",
        chunking_config=BASELINE_CHUNKING,
        embedding_model=EMBEDDING_MODEL,
    )
    kb_id = created.knowledge_base_id
    v1_id = created.extra["version"]["knowledge_base_version_id"]
    out.update(knowledge_base_id=kb_id, v1=v1_id)
    check(
        "the build was queued for the worker",
        created.extra["version"]["status"] == "queued"
        and created.extra["run"]["status"] == "queued",
        f"{v1_id} {created.extra['run']['status']}",
    )
    v1 = wait_for_build(caliber, v1_id, poll=poll)
    summary = v1["summary"]
    check(
        "version 1 completed",
        v1["status"] == "completed",
        f"{v1['status']} in {summary['duration_seconds']}s",
    )
    check(
        "every document was read and chunked",
        summary["processed_sources"] == 11 and summary["failed_sources"] == 0,
        f"{summary['processed_sources']} sources, {summary['chunk_count']} chunks, "
        f"{summary['total_chars']} chars",
    )
    check(
        "the embeddings are the model's real 384-dimension vectors",
        v1["embedding_dimension"] == 384 and v1["embedding_provider"] == "huggingface",
        f"{v1['embedding_provider']} {v1['embedding_model']} dim {v1['embedding_dimension']}",
    )
    kb = caliber.knowledge_bases.get(kb_id)
    check("the finished build is the active version", kb.active_version_id == v1_id, v1_id)
    out["v1_summary"] = {k: summary[k] for k in ("processed_sources", "chunk_count", "total_chars")}

    chunks = caliber.knowledge_bases.chunks(v1_id, limit=500)
    by_source: dict[str, int] = {}
    for chunk in chunks:
        by_source[chunk["source_name"]] = by_source.get(chunk["source_name"], 0) + 1
    check(
        "chunks carry their source documents",
        sorted(by_source) == sorted(corpus)
        and all(c["source_key"].startswith(PREFIX) for c in chunks),
        f"{len(chunks)} chunks over {len(by_source)} documents",
    )

    # ---- 3. query with citations -----------------------------------------------------------
    probe = next(r for r in answerable if r["id"] == "Q15")
    retrieved = ask(caliber, v1_id, probe["input"]["question"], mode="hybrid")
    rank = evidence_rank(retrieved, probe["expected"]["evidence"])
    holder = retrieved["retrieved_chunks"][rank - 1] if rank else {}
    cited = {c["chunk_id"] for c in retrieved["citations"]}
    check(
        "the answer cites the chunk that holds the clause",
        rank is not None
        and holder["chunk_id"] in cited
        and holder["source_name"] == "data-retention-policy.md",
        f"{probe['input']['question']!r} -> [{rank}] {holder.get('source_name')} "
        f"{holder.get('chunk_id')}",
    )
    check(
        "the chunk states the retention period",
        "365 days" in holder.get("content", ""),
        "365 days",
    )
    check(
        "the answer is the extractive digest, with a numbered label per citation",
        retrieved["answer"].startswith("Question:")
        and retrieved["citations"][0]["label"].startswith("[1] "),
        retrieved["citations"][0]["label"],
    )

    # ---- 4. retrieval modes over the whole answerable set ----------------------------------
    sweeps = {
        mode: sweep(caliber, v1_id, answerable, mode)
        for mode in ("dense", "hybrid", "graph_hybrid")
    }
    out["sweeps"] = sweeps
    for mode, row in sweeps.items():
        print(
            f"     {mode:<13} hit@1 {row['hit_at_1']}/{row['questions']}  "
            f"hit@3 {row['hit_at_3']}/{row['questions']}  missed {row['misses']}",
            flush=True,
        )
    check(
        "hybrid puts the clause's chunk in the top 3 for nearly every question",
        sweeps["hybrid"]["hit_at_3"] / sweeps["hybrid"]["questions"] >= HYBRID_HIT_AT_3_MIN,
        f"{sweeps['hybrid']['hit_at_3']}/{sweeps['hybrid']['questions']}",
    )
    check(
        "hybrid ranks the clause first more often than dense",
        sweeps["hybrid"]["hit_at_1"] > sweeps["dense"]["hit_at_1"],
        f"hybrid {sweeps['hybrid']['hit_at_1']} vs dense {sweeps['dense']['hit_at_1']}",
    )

    # the two refund documents a conflict-aware answerer must see together
    for qid in ("Q40", "Q41"):
        row = next(r for r in qa_rows if r["id"] == qid)
        found = {
            c["source_name"]
            for c in ask(caliber, v1_id, row["input"]["question"], mode="dense")["retrieved_chunks"]
        }
        check(
            f"{qid}: both conflicting refund documents are retrieved together",
            {"refund-policy.md", "refund-faq.md"} <= found,
            ", ".join(sorted(found)),
        )

    # ---- 5. what a score gate can refuse ---------------------------------------------------
    gate: dict[str, list[float]] = {"answerable": [], "outside": [], "trap": []}
    for row in qa_rows:
        top = ask(caliber, v1_id, row["input"]["question"], mode="dense", top_k=1)[
            "retrieved_chunks"
        ][0]["score"]
        if row["expected"]["sources"]:
            gate["answerable"].append(top)
        elif TRAP_TAG in row["tags"]:
            gate["trap"].append(top)
        else:
            gate["outside"].append(top)
    out["gate"] = {k: [min(v), max(v)] for k, v in gate.items()}
    check(
        f"a top-1 similarity gate of {SCORE_GATE} refuses every question outside the corpus",
        max(gate["outside"]) < SCORE_GATE <= min(gate["answerable"]),
        f"outside <= {max(gate['outside']):.3f}, answerable >= {min(gate['answerable']):.3f}",
    )
    check(
        "it cannot refuse the trap questions: their policy exists, only the clause is missing",
        min(gate["trap"]) >= SCORE_GATE,
        f"traps {min(gate['trap']):.3f}..{max(gate['trap']):.3f}",
    )

    # ---- 6. calibration: the test set, the baseline, and a tuned version 2 -----------------
    owner = caliber.me.get().user_id
    dataset = caliber.eval_datasets.create(
        f"policy-qa-calibration-{suffix}",
        owner=owner,
        description="41 policy questions with their gold sources",
    )
    for row in calibration_rows:
        caliber.eval_datasets.add_example(
            dataset.dataset_id, input=row["input"], expected=row["expected"], tags=row["tags"]
        )
    out["eval_dataset_id"] = dataset.dataset_id
    check(
        "the calibration test set holds every question",
        len(caliber.eval_datasets.examples(dataset.dataset_id)) == len(calibration_rows),
        f"{len(calibration_rows)} examples",
    )

    baseline = calibrate(caliber, kb_id, v1_id, dataset.dataset_id, "dense", 1)
    caliber.knowledge_bases.set_baseline(kb_id, test_run_id=baseline["test_run_id"])
    check(
        "the baseline run is pinned",
        caliber.knowledge_bases.get(kb_id).extra.get("baseline_run_id") == baseline["test_run_id"],
        f"{baseline['test_run_id']} recall@1 {baseline['recall_at_k']} "
        f"nDCG@1 {baseline['ndcg_at_k']}",
    )

    tuned = caliber.knowledge_bases.create_version(
        kb_id,
        chunking_strategy="markdown",
        chunking_config=TUNED_CHUNKING,
        embedding_model=EMBEDDING_MODEL,
    )
    v2_id = tuned["version"]["knowledge_base_version_id"]
    out["v2"] = v2_id
    v2 = wait_for_build(caliber, v2_id, poll=poll)
    check(
        "version 2 (500-character chunks) completed with more, smaller chunks",
        v2["status"] == "completed" and v2["summary"]["chunk_count"] > summary["chunk_count"],
        f"{summary['chunk_count']} -> {v2['summary']['chunk_count']} chunks",
    )
    check(
        "a finished build becomes the active version by itself",
        caliber.knowledge_bases.get(kb_id).active_version_id == v2_id,
        v2_id,
    )

    grid: list[dict[str, Any]] = []
    for label, version_id in (("v1", v1_id), ("v2", v2_id)):
        for mode in ("dense", "hybrid"):
            for k in (1, 3):
                if (label, mode, k) == ("v1", "dense", 1):
                    row = dict(baseline)
                else:
                    row = calibrate(caliber, kb_id, version_id, dataset.dataset_id, mode, k)
                grid.append({"version": label, **row})
    out["calibration"] = grid
    print("     version mode    k  recall@k  nDCG@k  faithfulness", flush=True)
    for row in grid:
        faith = "n/a" if row["faithfulness"] is None else f"{row['faithfulness']:.2f}"
        print(
            f"     {row['version']:<7} {row['mode']:<7} {row['top_k']}  {row['recall_at_k']:<8}  "
            f"{row['ndcg_at_k']:<6}  {faith}",
            flush=True,
        )
    check(
        "every calibration run scored all 41 questions",
        all(r["question_count"] == 41 for r in grid),
        f"{len(grid)} runs",
    )
    top_of_k1 = [r for r in grid if r["top_k"] == 1]
    best = max(top_of_k1, key=lambda r: (r["recall_at_k"], r["ndcg_at_k"]))
    check(
        "tuning raises Recall@1 above the baseline",
        best["recall_at_k"] > baseline["recall_at_k"],
        f"{baseline['recall_at_k']} -> {best['recall_at_k']} ({best['version']}, {best['mode']})",
    )
    check(
        "with three chunks every configuration finds the right policy for nearly every question",
        all(r["recall_at_k"] >= 0.95 for r in grid if r["top_k"] == 3),
        "recall@3 " + ", ".join(str(r["recall_at_k"]) for r in grid if r["top_k"] == 3),
    )
    judged = [r for r in grid if r["faithfulness"] is not None]
    check(
        "the model-judged metrics are n/a on a simulated judge, and real numbers otherwise",
        bool(judged) == ("eval" not in simulated),
        "faithfulness n/a (simulated judge)"
        if not judged
        else f"faithfulness {judged[0]['faithfulness']}",
    )

    # ---- 7. activate, roll back ------------------------------------------------------------
    caliber.knowledge_bases.activate_version(kb_id, v1_id)
    check(
        "activating version 1 makes it live",
        caliber.knowledge_bases.get(kb_id).active_version_id == v1_id,
        v1_id,
    )
    caliber.knowledge_bases.rollback(kb_id)
    check(
        "rollback restores the version that was live before",
        caliber.knowledge_bases.get(kb_id).active_version_id == v2_id,
        f"back to {v2_id}",
    )

    # ---- 8. builds that meet an unreadable file --------------------------------------------
    partial = caliber.knowledge_bases.create_version(
        kb_id,
        sources=[{"kind": "folder", "path": PREFIX}, {"kind": "file", "path": UNREADABLE_KEY}],
        chunking_strategy="markdown",
        chunking_config=TUNED_CHUNKING,
        embedding_model=EMBEDDING_MODEL,
    )
    v3_id = partial["version"]["knowledge_base_version_id"]
    v3 = wait_for_build(caliber, v3_id, poll=poll)
    sources = caliber.knowledge_bases.sources(v3_id)
    failed = [s for s in sources if s["status"] == "failed"]
    check(
        "an unreadable file is recorded as a failed source, and the build still completes",
        v3["status"] == "completed" and v3["summary"]["failed_sources"] == 1 and len(failed) == 1,
        f"{failed[0]['object_name']}: {failed[0]['error_summary']}" if failed else "",
    )
    check(
        "that incomplete version went live on its own: read failed_sources before trusting a build",
        caliber.knowledge_bases.get(kb_id).active_version_id == v3_id,
        f"active {v3_id}, processed {v3['summary']['processed_sources']}, "
        f"failed {v3['summary']['failed_sources']}",
    )
    caliber.knowledge_bases.activate_version(kb_id, v2_id)
    check(
        "activating the good version undoes it",
        caliber.knowledge_bases.get(kb_id).active_version_id == v2_id,
        v2_id,
    )

    broken = caliber.knowledge_bases.create_version(
        kb_id,
        sources=[{"kind": "file", "path": UNREADABLE_KEY}],
        chunking_strategy="markdown",
        chunking_config=TUNED_CHUNKING,
        embedding_model=EMBEDDING_MODEL,
    )
    v4_id = broken["version"]["knowledge_base_version_id"]
    v4 = wait_for_build(caliber, v4_id, poll=poll)
    check(
        "a build with nothing readable fails with the reason",
        v4["status"] == "failed" and "did not produce any chunks" in (v4["error_summary"] or ""),
        v4["error_summary"] or "",
    )
    check(
        "a failed build leaves the live version alone",
        caliber.knowledge_bases.get(kb_id).active_version_id == v2_id,
        f"still {v2_id}",
    )
    not_ready = ask(caliber, v4_id, "How long are audit logs retained?")
    check(
        "querying a version that is not ready returns no chunks and says why",
        not not_ready["retrieved_chunks"]
        and not_ready["answer_error"] == "Version is not ready for retrieval.",
        not_ready["answer_error"],
    )
    out["activate_failed"] = expect_status(
        "activating a failed version",
        409,
        lambda: caliber.knowledge_bases.activate_version(kb_id, v4_id),
    )
    check(
        "a failed version cannot be activated",
        out["activate_failed"].startswith("409 "),
        out["activate_failed"],
    )
    # a completed version that is not active is still queryable: "active" decides what consumers use
    other = ask(caliber, v1_id, "How long are audit logs retained?")
    check(
        "a completed version that is not active can still be queried by id",
        bool(other["retrieved_chunks"]) and other["answer_error"] is None,
        f"{v1_id} is not active, {len(other['retrieved_chunks'])} chunks",
    )

    # ---- 9. the answer workflow ------------------------------------------------------------
    installed = caliber.cookbooks.install(
        "06",
        name=f"cookbook-06-kb-assistant-{suffix}",
        acknowledge_prerequisites=bool(recipe.prerequisites),
    )
    workflow_id = installed["workflow"]["workflow_id"]
    draft_id = installed["version"]["version_id"]
    out.update(workflow_id=workflow_id, workflow_version_id=draft_id)
    check(
        "the recipe installs as a paused workflow with a draft version",
        installed["workflow"]["status"] == "paused" and installed["version"]["status"] == "draft",
        f"{workflow_id} {draft_id}",
    )
    bind_knowledge_node(caliber, draft_id, kb_id)
    check(
        "the bound draft validates",
        caliber.workflows.versions.validate(draft_id)["valid"] is True,
        draft_id,
    )
    paused = expect_status(
        "running a paused workflow",
        409,
        lambda: caliber.workflows.versions.run(draft_id, input="How long are audit logs retained?"),
    )
    check("a paused workflow refuses to run", paused.startswith("409 "), paused)
    caliber.workflows.update(workflow_id, status="active")

    def run_workflow(question: str) -> dict[str, Any]:
        started = caliber.workflows.versions.run(draft_id, input=question)
        finished = caliber.workflows.runs.wait(started["workflow_run_id"], timeout=180)
        result = step_output(finished, "knowledge").get("result") or {}
        verdict = (step_output(finished, "confidence").get("result")) or {}
        version = (result.get("versions") or [{}])[0]
        return {
            "run_id": finished.workflow_run_id,
            "trace_id": finished.trace_id,
            "status": finished.status,
            "resolved_version_ids": result.get("resolved_version_ids"),
            "top_source": (version.get("retrieved_chunks") or [{}])[0].get("source_name"),
            "top_score": (version.get("retrieved_chunks") or [{}])[0].get("score"),
            "confidence": verdict.get("confidence"),
            "needs_review": verdict.get("needs_review"),
            "matched_signals": verdict.get("matched_signals"),
        }

    runs = {
        "answerable": run_workflow("How long are security audit logs retained?"),
        "outside_the_corpus": run_workflow("What is the dress code for client meetings?"),
        "trap": run_workflow(
            "What is the first-response time for an Enterprise customer's P1 case?"
        ),
    }
    out["workflow_runs"] = runs
    for name, item in runs.items():
        print(
            f"     {name:<19} {item['status']}  top {item['top_source']} {item['top_score']}  "
            f"confidence {item['confidence']}  needs_review {item['needs_review']}",
            flush=True,
        )
    check(
        "all three runs completed through the real knowledge_query node",
        all(i["status"] == "completed" and i["top_source"] for i in runs.values()),
        ", ".join(i["run_id"] for i in runs.values()),
    )
    check(
        "the workflow resolved the knowledge base's active version",
        runs["answerable"]["resolved_version_ids"] == [v2_id],
        f"resolved {runs['answerable']['resolved_version_ids']}",
    )
    check(
        "a covered question is confident and a question outside the corpus is sent to review",
        runs["answerable"]["needs_review"] is False
        and runs["answerable"]["top_source"] == "data-retention-policy.md"
        and runs["outside_the_corpus"]["needs_review"] is True,
        f"{runs['answerable']['confidence']} vs {runs['outside_the_corpus']['confidence']}",
    )
    check(
        "the trap question passes the gate: only a model can tell the clause is missing",
        runs["trap"]["needs_review"] is False,
        f"confidence {runs['trap']['confidence']} from {runs['trap']['top_source']}",
    )

    caliber.knowledge_bases.activate_version(kb_id, v1_id)
    rerun = run_workflow("How long are security audit logs retained?")
    caliber.knowledge_bases.rollback(kb_id)
    check(
        "activating another version changes what the workflow uses; rollback changes it back",
        rerun["resolved_version_ids"] == [v1_id]
        and caliber.knowledge_bases.get(kb_id).active_version_id == v2_id,
        f"{v2_id} -> {rerun['resolved_version_ids'][0]} -> {v2_id}",
    )

    # ---- 10. route the low-confidence run to a review queue --------------------------------
    queue = caliber.review_queues.create(
        f"policy-qa-review-{suffix}",
        description="Runs the confidence gate sent to a person",
        questions=[
            {
                "key": "decision_justified",
                "title": "Is abstaining or clarifying justified by the retrieved evidence?",
                "type": "pass_fail",
                "required": True,
            },
            {
                "key": "right_source_cited",
                "title": "Did the answer cite the right policy?",
                "type": "pass_fail",
                "required": False,
            },
        ],
    )
    flagged = [i for i in runs.values() if i["needs_review"]]
    items = caliber.review_queues.enqueue(
        queue.queue_id, trace_ids=[i["trace_id"] for i in flagged]
    )
    out["review_queue_id"] = queue.queue_id
    check(
        "only the low-confidence run was enqueued, as a trace-linked item",
        len(items) == 1
        and items[0]["trace_id"] == runs["outside_the_corpus"]["trace_id"]
        and items[0]["status"] == "pending",
        f"{queue.queue_id} {items[0]['item_id']}",
    )
    done = caliber.review_queues.submit(
        queue.queue_id, items[0]["item_id"], decision_justified=True
    )
    check(
        "the reviewer's answer is saved and written back to the trace as an assessment",
        done["status"] == "completed"
        and done["answers"] == {"decision_justified": True}
        and bool(done["assessment_ids"]),
        f"assessment {done['assessment_ids'][0]}",
    )

    # ---- 11. the answer prompt and the faithfulness judge ----------------------------------
    meta, template = split_frontmatter(
        (assets / "prompts/kb-answer.md").read_text(encoding="utf-8")
    )
    prompt_name = f"kb-answer-{suffix}"
    caliber.prompts.create(prompt_name, template, commit_message=meta["commit_message"])
    stored = caliber.prompts.versions(prompt_name)
    check(
        "the answer prompt is stored with its three variables",
        all(
            f"{{{{ {var} }}}}" in template
            for var in ("question", "policy_domain", "retrieved_chunks")
        ),
        f"{prompt_name}, {len(stored) if isinstance(stored, list) else 'versioned'}",
    )
    judge_file = json.loads(
        (assets / "judges/citation-faithfulness.judge.json").read_text(encoding="utf-8")
    )
    judge = caliber.judges.create(
        f"CitationFaithfulness-{suffix}",
        instructions=judge_file["instructions"],
        feedback_value_type=judge_file["feedback_value_type"],
    )
    out["judge_id"] = judge.judge_id
    check("the faithfulness judge is registered", bool(judge.judge_id), judge.judge_id)

    qa_set = caliber.eval_datasets.create(
        f"policy-qa-eval-{suffix}",
        owner=owner,
        description="48 policy questions: answer, clarify, abstain",
    )
    for row in qa_rows:
        caliber.eval_datasets.add_example(
            qa_set.dataset_id, input=row["input"], expected=row["expected"], tags=row["tags"]
        )
    out["qa_dataset_id"] = qa_set.dataset_id
    if "llm" in simulated:
        refused = expect_status(
            "scoring a prompt on a simulated model",
            400,
            lambda: caliber.evaluations.create(
                qa_set.dataset_id,
                predict_target="prompt",
                subject_ref=f"{prompt_name}@1",
                scorers=["contains_expected"],
                label="kb-answer v1",
            ),
        )
        out["evaluation"] = f"refused: {refused}"
        check(
            "scoring the answer prompt is refused on the simulated model",
            "real LLM provider" in refused,
            refused,
        )
    else:
        evaluation = caliber.evaluations.create(
            qa_set.dataset_id,
            predict_target="prompt",
            subject_ref=f"{prompt_name}@1",
            scorers=["contains_expected"],
            label="kb-answer v1",
        )
        out["evaluation"] = {"run_id": evaluation.run_id, "status": evaluation.status}
        check(
            "the answer prompt was scored on all 48 questions",
            evaluation.n_examples == 48,
            evaluation.run_id,
        )

    out["recipe_unmet_checks"] = configuration_blockers(recipe)
    out["verified"] = all(c["ok"] for c in report.checks)
    out["checks"] = len(report.checks)
    return out


def cleanup(caliber: CaliberClient, suffix: str) -> None:
    """Remove what a run with ``suffix`` created. Idempotent; the bucket is left in place."""
    removed = []
    for kb in caliber.knowledge_bases.list():
        if kb.name == f"policy-qa-kb-{suffix}":
            caliber.knowledge_bases.delete(kb.knowledge_base_id)
            removed.append(f"knowledge base {kb.name}")
    for workflow in caliber.workflows.list():
        if workflow.name == f"cookbook-06-kb-assistant-{suffix}":
            caliber.workflows.update(workflow.workflow_id, status="archived")
            removed.append(f"workflow {workflow.name} (archived)")
    for prompt in caliber.prompts.list():
        if prompt.prompt_name == f"kb-answer-{suffix}":
            caliber.prompts.delete(prompt.prompt_name)
            removed.append(f"prompt {prompt.prompt_name}")
    for judge in caliber.judges.list():
        if judge.name == f"CitationFaithfulness-{suffix}":
            caliber.judges.update(judge.judge_id, status="archived")
            removed.append(f"judge {judge.name} (archived)")
    for queue in caliber.review_queues.list():
        if queue.name == f"policy-qa-review-{suffix}":
            caliber.review_queues.update(queue.queue_id, status="archived")
            removed.append(f"review queue {queue.name} (archived)")
    for name in (f"policy-qa-calibration-{suffix}", f"policy-qa-eval-{suffix}"):
        for dataset in caliber.eval_datasets.list():
            if dataset.name == name:
                caliber.eval_datasets.update(dataset.dataset_id, status="archived")
                removed.append(f"test set {name} (archived)")
    print("removed: " + ("; ".join(removed) if removed else "nothing"))


def main() -> None:
    with env_client() as caliber:
        if "--cleanup" in sys.argv:
            cleanup(caliber, sys.argv[sys.argv.index("--cleanup") + 1])
            return
        report = run(caliber)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
