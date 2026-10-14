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
reviewed_on: 2026-08-10
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
| use knowledge in assistant answers | connect retrieval to the consuming surface | [Aria assistant](../use/aria-assistant.md) |
| evaluate grounded answers | score citation quality and answer faithfulness | [Evaluation and test sets](../use/evaluation-and-test-sets.md) |

## Before the first step: what a knowledge base needs

A knowledge base is the one supported area whose prerequisites live outside the base install. The
alpha install page's native path has none of them. Check each before you create one, because the
failure comes late (a build that stops, not a form that refuses) when one is missing.

| Need | Why | If it is missing |
| --- | --- | --- |
| An S3-compatible object store holding the sources (MinIO, or S3) | sources are read from a bucket (`kind: file` or `folder` in it), and the build writes its chunks and graph back | creating a knowledge base answers 503 and says the object store is unreachable. Set `CALIBER_OBJECT_STORE_ENDPOINT_URL` and credentials, or use the docker stack, which runs MinIO |
| `pip install "caliber-suite[knowledge,knowledge-local]"` | the `knowledge` extra gives chunking and entity extraction; the embedding libraries (`sentence-transformers`, `langchain-huggingface`, and torch) are in `knowledge-local` | the options list every embedding model as **unavailable**, with a reason that names the extra, and creating fails fast with the same sentence. The default docker image installs `knowledge` but **not** `knowledge-local`: add it through `CALIBER_INSTALL_EXTRAS` when you build |
| An embedding model on the host | the first build downloads the model from Hugging Face (about 90 MB for `all-MiniLM-L6-v2`, more for the larger ones) | an offline host fails at the first build. Pre-cache the model into `HF_HOME` while online, then build |
| PostgreSQL with the pgvector extension | only for ANN search, which is opt-in with `CALIBER_KNOWLEDGE_PGVECTOR_ENABLED=true`. Without it retrieval scans the stored vectors, which is fine at small scale | nothing breaks; large corpora get slower |
| Apache AGE | only for the graph retrieval mode `age_graph`. Optional | that mode is not offered, and the other modes work |

`CALIBER_ALLOW_FLAGGED_LOCAL_EMBEDDINGS=true` is a different thing: it lets embeddings run on an install whose
torch version is on the advisory list. It cannot install a missing package, so a missing package is never
reported as something an override fixes.

## What is verified, and what is not

`tests/test_knowledge_stack_journey.py` runs create, build, version, query, activate and roll back against the
docker stack's real MinIO and PostgreSQL (`scripts/verify-knowledge-stack.sh`; CI runs it weekly and on changes
to `deploy/`). **Its embeddings are simulated**: a deterministic bag-of-words double stands in for a model, so a
green run shows the storage, the build and the routes work on the real services and says nothing about
retrieval quality. Judge quality with your own model and test set.

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
