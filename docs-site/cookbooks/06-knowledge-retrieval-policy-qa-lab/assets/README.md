# SCN-06 assets — create these

Concrete, copy-pasteable artifacts for [the recipe](../README.md): a grounded
policy-QA assistant that cites its sources, abstains when evidence is missing,
and asks to clarify when sources conflict. Build order:

| # | Artifact | File | Create via |
| --- | --- | --- | --- |
| 1 | Policy corpus (4 docs) | [`dataset/sources/`](dataset/sources/) | `Object Store → Upload` into a bucket (e.g. `policy-corpus`); upload the four `*.md` (skip the READMEs). See [`dataset/sources/README.md`](dataset/sources/README.md) for the **deliberate 30-vs-14-day contradiction**. |
| 2 | KB version | (built from the corpus) | `Knowledge → Knowledge Base → New knowledge base` → pick the bucket + the 4 docs → choose an embedding model under **Advanced configuration** → **Create**; wait for the build run to reach `completed`. API: `POST /knowledge-bases`, `POST /knowledge-bases/{id}/versions`. |
| 3 | Calibration set | [`dataset/calibration-questions.jsonl`](dataset/calibration-questions.jsonl) | Build a Test Set whose gold lives in `expectations.sources` (list) + `expectations.answer`; POST one row per line to `/eval-datasets/{id}/examples`, then point `Knowledge → Calibrate` at it. API: `POST /knowledge-bases/{id}/calibrate`. |
| 4 | Prompt `kb-answer` | [`prompts/kb-answer.md`](prompts/kb-answer.md) | `Library → Prompts → New prompt`; paste the template body (text below the frontmatter). API: `POST /prompts {name, template, commit_message}`. |
| 5 | Skills (optional, 2) | [`skills/citation-and-next-steps.md`](skills/citation-and-next-steps.md), [`skills/contradiction-detector.md`](skills/contradiction-detector.md) | `Library → Skills → New skill`; paste `summary` + `content` + `category` + `tags`. API: `POST /skills`. |
| 6 | Answer workflow | (template `knowledge_rag`) | `Compose → Workflows → New` → template **`knowledge_rag`** (or `graph_hybrid_rag`); wire the nodes below; bind `kb-answer` to the agent node. API: `POST /workflows`. |
| 7 | Eval dataset `policy-qa-eval` | [`dataset/qa-eval.jsonl`](dataset/qa-eval.jsonl) | `Evaluate → Test Sets → New dataset`, then add each row. API: `POST /eval-datasets {name}` → `POST /eval-datasets/{id}/examples` per line. |
| 8 | Judge `CitationFaithfulness` | [`judges/citation-faithfulness.judge.json`](judges/citation-faithfulness.judge.json) | `Evaluate → Judges → New judge`, paste fields. API: `POST /judges`. |

## Run the recipe

Follow [`../README.md`](../README.md) end to end; these assets slot into it as:

1. **Upload the corpus → build a KB version** (assets 1–2). After the build run
   reaches `completed`, open **Explore → Chunks** and confirm the four policy
   docs chunked with correct source lineage — chunks are attributed by their
   **filename / source_key** (`refund-policy.md`, `refund-faq.md`,
   `security-policy.md`, `data-retention-policy.md`), which is the identifier the
   calibrator matches gold `sources` against (not the in-document `Source id:`).
2. **Compare retrieval modes.** In **Explore → Query**, run the same question
   (e.g. *"How long are audit logs retained?"*) across `dense`, then `hybrid`,
   then `graph_hybrid`; note which mode surfaces the `data-retention-policy.md`
   chunk highest. (If AGE is enabled: **Explore → Graph → Sync to AGE**, then
   re-query as `age_graph`.) API: `POST /knowledge/query {knowledge_base_id,
   question, retrieval_modes}`.
3. **Run KB Calibrate** (asset 3) with `calibration-questions.jsonl`; read
   **Recall@k / nDCG@k / Faithfulness / Answer-correctness**. Tune chunking or
   switch mode, re-run, and **show recall improve** without dropping
   faithfulness. Pin a baseline (`POST /knowledge-bases/{id}/baseline`).
4. **Build the answer workflow** (asset 6) on template **`knowledge_rag`**:

   ```
   retrieve_chunks       (knowledge_query — selects the KB version, mode hybrid)
     -> score_confidence (python_code — derive confidence + a conflict flag from the retrieved set)
     -> draft_answer     (agent — bind prompt `kb-answer`; pass question, policy_domain, retrieved_chunks)
     -> abstain_or_answer (router — branch on decision: answer | abstain | clarify)
     -> output
   ```

   Add a branch off the router that **enqueues a review item** when
   `decision != "answer"` or `confidence` is low. `kb-answer` already emits the
   `{decision, answer, citations, confidence}` contract the router reads; the two
   skills in asset 5 are optional reinforcement (`contradiction-detector` ahead
   of the agent to force `clarify` on the 30-vs-14-day conflict;
   `citation-and-next-steps` after it to format citations + a next step).
5. **Run the three scenario behaviors** from the workflow run monitor:
   - *answerable* → cited answer (e.g. audit-log retention → `decision: answer`,
     cites `DATA-RETENTION-POLICY`).
   - *missing-evidence* → `decision: abstain` + `missing_evidence` (e.g. the
     enterprise-incident SLA in minutes — not in the corpus).
   - *conflicting-sources* → `decision: clarify` (the refund-window question
     retrieves both `REFUND-POLICY` (30 days) and `REFUND-FAQ` (14 days)).
6. **Evaluate faithfulness** (assets 7–8). Create the `policy-qa-eval` dataset
   from `qa-eval.jsonl`, then `Evaluate → Evaluations → Run evaluation` with the
   deterministic graders (`contains_expected`) plus the `CitationFaithfulness`
   judge ticked under **Custom LLM judges** (it runs as a `Judge.<id>` scorer for
   an automatic per-row verdict). Gate:
   `faithfulness_min ≥ 0.90`, `abstention_policy_compliance = 1.0`
   (see [`../verification.yaml`](../verification.yaml)).
7. **Route low-confidence to review.** `Observe → Review Queues → New queue`
   (citation/abstention questions); enqueue the trace ids of the `abstain` and
   `clarify` runs; answer them — answers write back onto the trace. API:
   `POST /review-queues`, `POST /review-queues/{id}/items {trace_ids}`.

   > Feed back: add any failed retrieval question to
   > `calibration-questions.jsonl` and re-calibrate (revise chunking before
   > loosening the prompt), per `verification.yaml`'s feedback loop.

## How the pieces stay consistent

The corpus, prompt, datasets, and judge are authored together:

- **`contains_expected`** reads the top-level `expectations.expected` substring
  on the answerable rows of `qa-eval.jsonl`; each expected substring (`365 days`,
  `AES-256`, `MFA`, `Billing portal`, `30 days`) is a literal fact in the
  corpus. The abstain rows (Q06/Q07) set `expected: ""`, so the substring scorer
  is meaningful only on the answerable rows.
- **`Judge.CitationFaithfulness`** reads `decision` + `must_cite` on every row:
  it passes an `answer` only if every claim is cited, an `abstain` only if it
  declined with a `missing_evidence` list, and a `clarify` only if it surfaced
  the conflict instead of picking a side.
- The **`clarify` row (Q05)** only behaves correctly because the corpus ships the
  `REFUND-POLICY` (30-day) vs `REFUND-FAQ` (14-day) contradiction. Keep both
  source docs.

## Conventions used across the pack

- **Prompt files** (`prompts/*.md`): YAML frontmatter (name, model hint,
  variables) then the literal template body. Paste the body into the authoring
  textarea; variables are `{{ snake_case }}` — here `{{ question }}`,
  `{{ policy_domain }}`, `{{ retrieved_chunks }}`.
- **Skill files** (`skills/*.md`): SKILL.md style — frontmatter (`name`
  [kebab-case, must **not** start with `claude`/`anthropic`], `summary`,
  `category`, `tags`, `render_variables`) then the literal content. The
  `summary` is the narrow one-line trigger text the deterministic selector
  reads; paste the content below the frontmatter into the authoring textarea.
- **Source corpus** (`dataset/sources/*.md`): plain Markdown ingested into the
  Object Store and then a KB version. Each doc carries a `Source id:` so answers
  and the judge can cite a concrete source.
- **Eval dataset** (`dataset/qa-eval.jsonl`): one example per line,
  `{"id", "tags", "inputs", "expectations"}` — the shape Evaluations scorers +
  judges read (`{{ inputs }}`, `{{ outputs }}`, `{{ expectations }}`). `inputs`
  is `{question, policy_domain}`; `expectations` is `{decision, must_cite,
  expected}` — `expected` is the gold substring `contains_expected` matches
  (empty string on the abstain rows).
- **Calibration set** (`dataset/calibration-questions.jsonl`): one pair per line,
  `inputs.question` → `expectations.{sources, answer}`. KB Calibrate reads gold
  ONLY from `expectations.sources` (a **list** of source ids) and
  `expectations.answer`, and scores Recall@k / nDCG@k / Faithfulness /
  Answer-correctness from these; `sources` are the gold source ids retrieval
  should surface. Populate the Test Set by POSTing each row to
  `/eval-datasets/{id}/examples` (the Observability *Add to test set* widget
  cannot capture `sources`/`answer`).
- **Judge files** (`judges/*.judge.json`): `{name, model, instructions,
  feedback_value_type}`; instructions reference `{{ inputs }}`/`{{ outputs }}`/
  `{{ expectations }}` (the UI requires at least one). `feedback_value_type` ∈
  bool|int|float|str. `CitationFaithfulness` is `bool` (pass/fail) and is the
  real `custom_judge` from `verification.yaml`.
