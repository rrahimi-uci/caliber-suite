# Cookbook 06 assets

Everything the lab loads, and how: [the README](../README.md) is the walk-through, [`cookbook_06_grounded_knowledge_assistant.py`](../../../../sdk/caliber-sdk/examples/cookbooks/cookbook_06_grounded_knowledge_assistant.py) runs it through the API, and [`06-knowledge-retrieval.spec.ts`](../../../../caliber/caliber-ui/e2e/cookbooks/labs/06-knowledge-retrieval.spec.ts) runs it through the browser.

| # | Artifact | File | Loaded by |
| --- | --- | --- | --- |
| 1 | Policy corpus (11 documents) | [`dataset/sources/`](dataset/sources/) (see its README) | `Object Store > Upload` into bucket `policy-corpus`, folder `policies/`; API `POST /object-store/buckets/{bucket}/objects` |
| 2 | Knowledge base versions | built from the corpus | `Knowledge Bases > New knowledge base` / `New version`; API `POST /knowledge-bases`, `POST /knowledge-bases/{id}/versions`. Version 1: Markdown Aware, 1400/120. Version 2: 500/60. Model `sentence-transformers/all-MiniLM-L6-v2`. |
| 3 | Calibration test set (41 questions) | [`dataset/calibration-questions.jsonl`](dataset/calibration-questions.jsonl) | `Prompts > a prompt > Calibration > Upload Dataset`; API `POST /eval-datasets` then `POST /eval-datasets/{id}/examples` per line. Read by `Knowledge Bases > Calibrate`. |
| 4 | QA test set (48 questions: answer, clarify, abstain, traps) | [`dataset/qa-eval.jsonl`](dataset/qa-eval.jsonl) | Same importers. The script uses it to check retrieval, the clause's chunk and the score gate. |
| 5 | Prompt `kb-answer` | [`prompts/kb-answer.md`](prompts/kb-answer.md) | `Prompts > New prompt` (paste the body); API `POST /prompts`. Needs a model to be scored. |
| 6 | Judge `CitationFaithfulness` | [`judges/citation-faithfulness.judge.json`](judges/citation-faithfulness.judge.json) | `Judges > New Judge`; API `POST /judges`. Needs a model to run. |
| 7 | Answer workflow | template `knowledge_rag` (cookbook 06) | `Cookbooks > Install`; API `POST /cookbooks/06/install`, then bind the Knowledge node. |
| 8 | Unreadable file | [`dataset/unreadable/scanned-handbook.pdf`](dataset/unreadable/scanned-handbook.pdf) | Upload under `unreadable/` and add it to a build, for the failure scenario. |

## Row shape

One JSON object per line, `{"id", "tags", "input", "expected"}`: the shape both importers read (`input` and `expected`, singular).

- `input`: `{"question", "policy_domain"}` (the calibration file keeps only `question`).
- `expected` in `qa-eval.jsonl`: `decision` (`answer`, `clarify`, `abstain`), `must_cite`, `expected` (a substring of the supporting text), `sources` (gold file names; empty when nothing in the corpus answers), `clause` (document and section), `evidence` (a literal passage that holds the clause), `answer` (the gold answer, in words).
- `expected` in `calibration-questions.jsonl`: `sources`, `answer`, `evidence`. **Calibrate reads only `sources` and `answer`.** Chunks are matched to gold by **file name** (`data-retention-policy.md`), not by the `Document ID:` printed in the document.

## What is verified where

- [`caliber/tests/test_cookbook_06_knowledge_lab.py`](../../../../caliber/tests/test_cookbook_06_knowledge_lab.py): every `evidence` is a literal passage of its document, the row shape, the question mix (39 answer, 2 clarify, 7 abstain), the calibration set is the answerable part of the QA set, and the installed template's confidence node reads a real query result.
- The script and the spec, against a live stack: everything else. See the README for what is simulated.

## Skills

The original recipe listed two optional skills (`contradiction-detector`, `citation-and-next-steps`). They are not part of the verified path (a skill runs only through an agent, and the simulated stack has no model to run one), so they are no longer in this lab; `citation-and-next-steps` remains with cookbook 07, which uses it.
