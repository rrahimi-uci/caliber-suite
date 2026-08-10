---
audience:
  - system-user
  - developer
  - evaluator
doc_type: how-to
product_area: knowledge
stability: ga
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
