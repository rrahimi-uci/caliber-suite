---
audience:
  - system-user
  - developer
  - evaluator
doc_type: how-to
product_area: knowledge
stability: beta
prerequisites:
  - A CALIBER deployment with knowledge-base access
reviewed_on: 2026-10-15
version_applicability: current main branch docs contract
tags:
  - knowledge
  - retrieval
  - ingestion
  - grounding
---

# Knowledge bases

Use this page when the question is practical: ingest source material, verify
retrieval quality, and connect grounded knowledge to assistant or workflow
behavior.

## At a glance

| Task | Start here | Deep reference |
| --- | --- | --- |
| ingest documents | confirm storage and extraction path | [Knowledge bases architecture](../08-knowledge-bases/architecture.md) |
| improve retrieval quality | inspect chunking, embeddings, and reranking | [Knowledge bases architecture](../08-knowledge-bases/architecture.md) |
| use knowledge in assistant answers | connect retrieval to the consuming surface (Aria is off by default; enable with `CALIBER_FEATURE_ARIA`) | [Aria assistant](../use/aria-assistant.md) |
| evaluate grounded answers | score citation quality and answer faithfulness | [Evaluation and test sets](../use/evaluation-and-test-sets.md) |

## Before the first step: what a knowledge base needs

A knowledge base is the one supported area whose prerequisites live outside the base install. The
alpha install page's native path has none of them. Check each before you create one, because the
failure comes late (a build that stops, not a form that refuses) when one is missing.

| Need | Why | If it is missing |
| --- | --- | --- |
| An S3-compatible object store holding the sources (MinIO, or S3) | sources are read from a bucket (`kind: file` or `folder` in it), and the build writes its chunks and graph back | creating a knowledge base answers 503 and says the object store is unreachable. Set `CALIBER_OBJECT_STORE_ENDPOINT_URL` and credentials, or use the docker stack, which runs MinIO |
| `pip install "caliber-suite[knowledge,knowledge-local]"` | the `knowledge` extra gives chunking and entity extraction; the embedding libraries (`sentence-transformers`, `langchain-huggingface`, and torch) are in `knowledge-local` | the options list every embedding model as **unavailable**, with a reason that names the extra, and creating fails fast with the same sentence. The default docker image installs `knowledge` but **not** `knowledge-local`: add it through `CALIBER_INSTALL_EXTRAS` when you build |
| An embedding model on the host | the first build downloads the model from Hugging Face (about 90 MB for `all-MiniLM-L6-v2`, more for the larger ones) | an offline host fails at the first build. Pre-cache the model into `HF_HOME` while online, then build. The docker stack already sets `HF_HOME=/data/huggingface` on a persistent volume, so `docker compose ... exec caliber python -c "from sentence_transformers import SentenceTransformer as S; S('all-MiniLM-L6-v2')"` warms it in place |
| PostgreSQL with the pgvector extension | only for ANN search, which is opt-in with `CALIBER_KNOWLEDGE_PGVECTOR_ENABLED=true`. Without it retrieval scans the stored vectors, which is fine at small scale | nothing breaks; large corpora get slower |
| Apache AGE | only for the graph retrieval mode `age_graph`. Optional | that mode is not offered, and the other modes work |

`CALIBER_ALLOW_FLAGGED_LOCAL_EMBEDDINGS=true` is a different thing: it lets embeddings run on an install whose
torch version is on the advisory list. It cannot install a missing package, so a missing package is never
reported as something an override fixes.

## What a build does when something is wrong

Found by running the policy-QA cookbook ([lab 06](../../docs-site/cookbooks/06-knowledge-retrieval-policy-qa-lab/README.md)) against a real stack:

- **A finished build activates itself.** `completed` makes the version the knowledge base's active version, the one workflows and other
  consumers resolve. Activating an earlier version (`POST .../versions/{id}/activate`) is audited; a build's own activation is not, so
  `POST .../rollback` answers `409 ... rollback needs an audited activation to restore` until you have activated a version explicitly.
- **One unreadable file does not fail a build.** It becomes a source with `status: failed` and an `error_summary`, the run log gets a
  `source_failed` event, `summary.failed_sources` counts it, and the version is `completed` and **live without that document**. Check
  `failed_sources` before trusting a build. A build in which nothing is readable fails (`The selected files did not produce any chunks`),
  leaves the active version alone, cannot be activated, and querying it returns no chunks with `answer_error: Version is not ready for retrieval.`
- **A version that is not active can still be queried** by id: querying needs the version `completed`, not active.
- **The first build resolves the embedding model against huggingface.co** even when it is cached. On a slow or blocked network it can sit
  for minutes; cache the model once and set `HF_HUB_OFFLINE=1`.
- **On the simulated stack the answer text is an extractive digest**, and the model-judged calibration metrics (Faithfulness,
  Answer-correctness) are `null`. Recall@k and nDCG@k are real.

## What is verified, and what is not

`tests/test_knowledge_stack_journey.py` runs create, build, version, query, activate and roll back against the
docker stack's real MinIO and PostgreSQL (`scripts/verify-knowledge-stack.sh`; the `stack-smoke` workflow runs it
weekly and on pull requests that touch `deploy/`, the stack scripts or `caliber/src/caliber/knowledge/`, and it is
not a required check on every pull request). **Its embeddings are simulated**: a deterministic bag-of-words double stands in for a model, so a
green run shows the storage, the build and the routes work on the real services and says nothing about
retrieval quality. Judge quality with your own model and test set.

## Who can do what

Inside a project, creating, changing or deleting a knowledge base, building a version, activating or rolling back a
version, syncing a version to Apache AGE and setting a baseline need `editor` or `owner`. Running a calibration
needs `reviewer`, `editor` or `owner`. Listing, reading and querying need only membership. A member whose role is
too weak gets 403 and a person outside the project gets 404. Calibrating a knowledge base returns a run summary
and has no candidate to apply.

## 1. What knowledge bases are for in CALIBER

Knowledge bases give CALIBER a governed retrieval surface: versioned corpora,
ingestion, chunking, embeddings, optional graph extraction, and retrieval that
can be measured rather than assumed.

## 2. Common tasks

| You want to... | Read this next |
| --- | --- |
| upload and ingest content | [Knowledge bases architecture](../08-knowledge-bases/architecture.md) |
| improve search or answer quality | [Evaluation and test sets](../use/evaluation-and-test-sets.md) |
| debug a missing source or file path | [Storage and state](../operate/storage-and-state.md) |
| reason about grounded-answer trust | [Trust and governance](../use/trust-and-governance.md) |

## 3. Common failure modes

| Symptom | First thing to check |
| --- | --- |
| Files uploaded, but answers are weak | chunking, embeddings, reranking, or evaluation coverage |
| Sources exist, but retrieval cannot find them | ingestion status, storage path, or project scope |
| Answers cite the wrong material | retrieval quality, prompt instructions, or judge coverage |

## 4. Related docs

- [Evaluation and test sets](../use/evaluation-and-test-sets.md)
- [Trust and governance](../use/trust-and-governance.md)
- [Storage and state](../operate/storage-and-state.md)
- [Knowledge bases architecture](../08-knowledge-bases/architecture.md)
