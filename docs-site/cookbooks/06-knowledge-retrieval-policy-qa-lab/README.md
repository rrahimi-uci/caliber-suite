# Grounded Knowledge Assistant: a policy assistant you can measure

> **Prerequisite flag:** `CALIBER_FEATURE_COOKBOOKS=true`, and nothing else. No Aria, no integrations.
> Every step below was run against a real server (a fresh stack from `scripts/cookbook-env.sh up lab06 --flags cookbooks --s3`)
> through the API script and through the browser, on the alpha's default **simulated** language model and evaluator.
> Storage, chunking, embeddings and retrieval ranking are **real**; answer text and model-judged scores are **simulated**
> ([what is simulated](#what-is-simulated-and-what-a-real-provider-changes)).

## What this lab demonstrates

A retrieval assistant over eleven realistic company policies (billing, security, privacy, HR, IT, finance, support). You put
the documents in the **object store**, build a **knowledge base version** (a queued job a worker runs), ask questions and check
that the answer **cites the chunk that holds the clause**, compare retrieval modes, **calibrate** retrieval against 41
questions with known supporting documents (Recall@k, nDCG@k), build a better-tuned version, **activate and roll back**
versions, meet the failures a knowledge base really has (no object store, no embedding libraries, an unreadable file, a version
that is not ready), then wire the answer **workflow**, run it, and send its low-confidence run to a **review queue**.

**You are done when:**

- the bucket `policy-corpus` holds 11 documents under `policies/`, and knowledge base version 1 is `completed` with 11 processed
  sources, 27 chunks of 384-dimension vectors, and is the active version;
- the answer to "How long are security audit logs retained?" cites a chunk of `data-retention-policy.md` that contains `365 days`;
- over the 41 answerable questions the clause's chunk is in the top 3 for 41/41 questions in `hybrid` mode (40/41 `dense`, 35/41
  `graph_hybrid`), and first for 32 (hybrid) against 24 (dense);
- a top-1 similarity gate of `0.42` refuses all 5 questions that really are outside the corpus and, honestly, **cannot** refuse the
  2 trap questions whose policy exists but whose clause does not;
- calibration at `top_k = 1` raises Recall@1 from `0.878` (dense, version 1, the pinned baseline) to `0.9268` (hybrid), and nDCG@1 from
  `0.9024` to `0.9512`; Faithfulness and Answer-correctness are blank on the simulated judge;
- version 2 (500-character chunks) completes with 69 chunks and becomes active by itself; activating version 1 and rolling back
  returns to version 2;
- a build that meets one unreadable file **completes** with `failed_sources: 1` and goes live; a build with nothing readable
  **fails**, leaves the live version alone, and cannot be activated; querying it returns no chunks and `Version is not ready for retrieval.`;
- the installed workflow runs three questions through the real `knowledge_query` node; the covered one is confident (`0.8`), the one
  outside the corpus is `needs_review: true` (`0.25`) and is the only item enqueued in the review queue, where a reviewer's answer is
  written back to the trace.

### What is simulated, and what a real provider changes

| Part | On the simulated stack (what you see) | With a real provider |
| --- | --- | --- |
| Object store, queue and worker, chunking, embedding vectors, retrieval ranking, Recall@k, nDCG@k, the score gate, activation and rollback | **Real.** Storage is an S3 endpoint (`moto_server` here, MinIO or S3 in production); embeddings come from the local `all-MiniLM-L6-v2` model; every number in this lab is arithmetic on real retrieval. | Same. |
| Answer text (`CALIBER_ASSISTANT_ENGINE=fake`) | An **extractive digest**: `Question: ...` then `Most relevant retrieved evidence:` and a preview of each retrieved chunk. It is not a generated answer and it never abstains or asks to clarify. | A model writes a cited answer from the chunks. |
| Faithfulness and Answer-correctness (calibration) | `null` (shown as `—`). The judges need a model. | Scores in `0..1`. |
| Scoring the answer prompt on **Evaluations** | Refused: `400 evaluation requires a real LLM provider`. CALIBER never invents a scorecard. | A scorecard, including the `CitationFaithfulness` judge. |
| `decision: answer / abstain / clarify` | **Not produced.** The prompt `kb-answer` describes the contract; nothing on a simulated stack follows it. What the lab verifies instead is the evidence a model would need (both conflicting refund documents are retrieved together) and what retrieval alone can refuse (the score gate). | The model decides; the review queue catches what it gets wrong. |

## Prerequisites and setup

1. A running CALIBER built per [`docs/start/alpha-install.md`](../../../docs/start/alpha-install.md), with `CALIBER_FEATURE_COOKBOOKS=true`.
2. **Knowledge needs more than the base install** ([`docs/use/knowledge-bases.md`](../../../docs/use/knowledge-bases.md)): an S3-compatible
   object store with credentials, `pip install "caliber-suite[knowledge,knowledge-local]"`, and the embedding model on the host
   (about 90 MB for `all-MiniLM-L6-v2`; it is downloaded from Hugging Face on first use). PostgreSQL/pgvector and Apache AGE are optional and
   off by default; this lab uses neither.
3. From a checkout, one command starts a disposable stack with all of that (this is what the lab was verified on):

   ```bash
   HF_HUB_OFFLINE=1 scripts/cookbook-env.sh up lab06 --flags cookbooks --s3   # ports are picked for you; never touches 5001
   source .tmp/cookbook-env/lab06/env.sh        # CALIBER_URL, CALIBER_BASE_URL, CALIBER_API, CALIBER_S3_ENDPOINT, ...
   curl -s $CALIBER_API/readiness | jq -c '.data|{ready,simulated}'
   ```

   Expected: `{"ready":true,"simulated":["eval","llm"]}`. `--s3` starts a private in-memory S3 endpoint (`moto_server`, admin credentials
   `minioadmin` / `minioadmin`) and points the Object Store at it; its objects do not survive `restart`.
   **`HF_HUB_OFFLINE=1`** makes the build use the cached model instead of asking huggingface.co whether it is current. Without it, every server
   start contacts the hub, and on a slow or blocked network the **first build sits in `queued`/`running` for minutes** (we hit this). Cache the
   model once while online (`python -c "from sentence_transformers import SentenceTransformer as S; S('sentence-transformers/all-MiniLM-L6-v2')"`), then run offline.
4. Sign in as `admin` / `admin`, or create an access token (Settings > API tokens) and `export CALIBER_TOKEN=calpat_...`. The script needs the
   `caliber.admin` scope (the object store console is admin-only).

## Dataset and sample data

Everything is under [`assets/`](assets/); nothing is generated at run time.

| File | What it holds | Why |
| --- | --- | --- |
| [`assets/dataset/sources/*.md`](assets/dataset/sources/) (11) | Refund and Cancellation Policy v3.2, Billing FAQ (stale), Information Security, Data Retention (a table), Privacy Requests, Leave and Time Off, Remote Work, Travel and Expense v3.0, Acceptable Use and Device, Support Service Levels, Vendor and Procurement. 320 to 660 words each, 5,100 words in all. | Real-length policies with effective dates, tables, superseded clauses (a 14-day refund window replaced by 30, a $180 hotel cap by $220, 12 weeks of parental leave by 16), overlapping clauses (a lost laptop is 4 hours, a suspected incident 24) and one deliberate contradiction. See [`assets/dataset/sources/README.md`](assets/dataset/sources/README.md). |
| [`assets/dataset/qa-eval.jsonl`](assets/dataset/qa-eval.jsonl) | 48 questions: 39 answerable, 2 that must clarify, 7 that must abstain (5 outside the corpus, **2 traps**). Each row: `input` (question, policy domain), `expected` (decision, gold `sources`, the supporting `evidence` text and clause, the gold `answer`, the `expected` substring). | The test set for the whole lab. `evidence` is a literal passage of its document, which is how "the answer cites the right chunk" is checked. |
| [`assets/dataset/calibration-questions.jsonl`](assets/dataset/calibration-questions.jsonl) | The 41 answerable and clarify rows: `input.question`, `expected.sources` (the gold documents), `expected.answer`. | What **Calibrate** reads: gold lives only in `expected.sources` (file names) and `expected.answer`. |
| [`assets/dataset/unreadable/scanned-handbook.pdf`](assets/dataset/unreadable/scanned-handbook.pdf) | A truncated "PDF" no parser can read. | The build-failure scenario. |
| [`assets/prompts/kb-answer.md`](assets/prompts/kb-answer.md), [`assets/judges/citation-faithfulness.judge.json`](assets/judges/citation-faithfulness.judge.json) | The answer prompt and the faithfulness judge. | Stored for real; **scoring** them needs a model (above). |

The question traps are on purpose: Q16 asks for *application* logs next to a table row for *audit* logs; Q19 and Q22 share a number ("30 days") with the refund window the contradiction questions (Q40, Q41) are about; Q10 and Q36 are two different deadlines for two
overlapping events; Q42 and Q43 ask about **Enterprise** plans, whose terms the policies defer to an order form that is not in the corpus.

Both JSONL files use the `{"input": ..., "expected": ...}` shape that **both** importers read: `POST /eval-datasets/{id}/examples` and the
Prompts page's **Upload Dataset**. (The previous version of this lab used `inputs`/`expectations`, which neither accepts.)

## Configuration and environment

| Variable | Value used | Meaning |
| --- | --- | --- |
| `CALIBER_FEATURE_COOKBOOKS` | `true` | Opens Cookbooks. Off: `GET /cookbooks` answers `404 feature_disabled`. |
| `CALIBER_OBJECT_STORE_ENDPOINT_URL` | `http://127.0.0.1:<port>` (set by `--s3`) | The S3 endpoint knowledge bases read from and write to. |
| `CALIBER_OBJECT_STORE_ACCESS_KEY_SOURCE`, `..._SECRET_KEY_SOURCE` | `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Names of the variables holding the credentials (`minioadmin` / `minioadmin`). |
| `CALIBER_OBJECT_STORE_FORCE_PATH_STYLE` | `true` | Path-style addressing, needed by `moto` and MinIO. |
| `CALIBER_BACKGROUND_TASKS_ENABLED` | `true` | Runs the knowledge build worker. With it off, a create builds inline instead of queueing. |
| `CALIBER_LLM_PROVIDER`, `CALIBER_EVAL_PROVIDER`, `CALIBER_ASSISTANT_ENGINE` | `fake` | The simulated model, evaluator and answer engine. |
| `HF_HUB_OFFLINE` | `1` | Use the cached embedding model; never contact huggingface.co. |
| `CALIBER_KNOWLEDGE_PGVECTOR_ENABLED`, Apache AGE | unset | Optional ANN search and the `age_graph` mode; not used. |
| `CALIBER_BASE_URL`, `CALIBER_TOKEN` | `http://127.0.0.1:<port>`, a `calpat_...` token | What the script reads (`CALIBER_BASE_URL` is the server root; the SDK adds the API prefix). `env.sh` exports the first. |
| `COOKBOOK_06_ASSETS` | the lab's `assets/` | Optional: point the script and the spec at another copy of the assets. |

## Run it through the API

One script runs the whole API path from the dataset files to the verification, and exits `0` only when every outcome above is verified:

```bash
source .tmp/cookbook-env/lab06/env.sh
export CALIBER_TOKEN=$(curl -s -c /tmp/lab06.jar -X POST $CALIBER_API/auth/login -H 'content-type: application/json' \
  -d '{"user_id":"admin","password":"admin"}' >/dev/null && curl -s -b /tmp/lab06.jar -X POST $CALIBER_API/auth/tokens \
  -H 'content-type: application/json' -d '{"name":"lab06"}' | jq -r .data.token)
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_06_grounded_knowledge_assistant
```

It took about a minute (the first build loads the model: `version 1 completed in 20s` here, under a second afterwards). Expected output, trimmed; **ids and
durations vary, every count and score does not**:

```text
cookbook 06 run 2bcd3d
ok   the dataset is the one the README describes  [11 policy documents, 48 questions (41 answerable, 7 not), 41 calibration pairs]
ok   every supporting clause is a literal passage of its document  [drifted: []]
ok   the object store is reachable  [1 buckets]
ok   the corpus is in the bucket  [11 objects under policy-corpus/policies/]
ok   the build was queued for the worker  [KBV-d7d22f65 queued]
ok   every document was read and chunked  [11 sources, 27 chunks, 29785 chars]
ok   the embeddings are the model's real 384-dimension vectors  [huggingface sentence-transformers/all-MiniLM-L6-v2 dim 384]
ok   the answer cites the chunk that holds the clause  ['How long are security audit logs retained?' -> [2] data-retention-policy.md KBC-e495fabab3664c8e]
     dense         hit@1 24/41  hit@3 40/41  missed ['Q14']
     hybrid        hit@1 32/41  hit@3 41/41  missed []
     graph_hybrid  hit@1 22/41  hit@3 35/41  missed ['Q01', 'Q06', 'Q14', 'Q20', 'Q29', 'Q41']
ok   Q40: both conflicting refund documents are retrieved together  [refund-faq.md, refund-policy.md]
ok   a top-1 similarity gate of 0.42 refuses every question outside the corpus  [outside <= 0.384, answerable >= 0.444]
ok   it cannot refuse the trap questions: their policy exists, only the clause is missing  [traps 0.560..0.661]
ok   the baseline run is pinned  [KBTR-9fd7fd40 recall@1 0.878 nDCG@1 0.9024]
ok   version 2 (500-character chunks) completed with more, smaller chunks  [27 -> 69 chunks]
     version mode    k  recall@k  nDCG@k  faithfulness
     v1      dense   1  0.878     0.9024  n/a
     v1      dense   3  1.0       0.9568  n/a
     v1      hybrid  1  0.9268    0.9512  n/a
     v1      hybrid  3  1.0       0.9753  n/a
     v2      dense   1  0.9268    0.9512  n/a
     v2      dense   3  0.9878    0.9757  n/a
     v2      hybrid  1  0.878     0.9024  n/a
     v2      hybrid  3  1.0       0.9548  n/a
ok   tuning raises Recall@1 above the baseline  [0.878 -> 0.9268 (v1, hybrid)]
ok   rollback restores the version that was live before  [back to KBV-582d9d03]
ok   an unreadable file is recorded as a failed source, and the build still completes  [scanned-handbook.pdf: failed to extract pdf '...': startxref not found]
ok   that incomplete version went live on its own: read failed_sources before trusting a build  [active KBV-77db72a0, processed 11, failed 1]
ok   a build with nothing readable fails with the reason  [The selected files did not produce any chunks. Check the source types or extraction settings.]
ok   querying a version that is not ready returns no chunks and says why  [Version is not ready for retrieval.]
ok   a failed version cannot be activated  [409 only completed versions can be activated]
ok   a completed version that is not active can still be queried by id  [KBV-d7d22f65 is not active, 3 chunks]
ok   a paused workflow refuses to run  [409 workflow is paused; resume it before running]
     answerable          completed  top data-retention-policy.md 0.703735  confidence 0.8  needs_review False
     outside_the_corpus  completed  top travel-and-expense-policy.md 0.27889  confidence 0.25  needs_review True
     trap                completed  top support-sla-policy.md 0.709443  confidence 0.8  needs_review False
ok   activating another version changes what the workflow uses; rollback changes it back  [KBV-582d9d03 -> KBV-d7d22f65 -> KBV-582d9d03]
ok   only the low-confidence run was enqueued, as a trace-linked item  [RVQ-5b5d8c71 RVI-c0230403]
ok   the reviewer's answer is saved and written back to the trace as an assessment  [assessment a-302d9b6a...]
ok   scoring the answer prompt is refused on the simulated model  [400 evaluation requires a real LLM provider; ...]
```

(51 `ok` lines in all; the file ends with a JSON summary with `"verified": true`.) What the numbers say, so you read your own run correctly:

- **Chunk-level ranking matters more than document-level.** Almost every question finds the right *document* in the top 3 (Recall@3 is `1.0` for three of the four
  configurations), but the right *chunk* is first only 24 times in 41 for dense. `hybrid` (BM25 plus vectors) fixes most of those and is the only mode with 41/41 at 3.
- **`graph_hybrid` is worse here** (35/41 at 3). Its entity graph is built by a heuristic extractor; on policy text the generic entities (`Effective`, `Policy`, `ID`) link everything to everything. Judge a mode on your corpus, not on its name.
- **No setting wins everywhere:** hybrid helps the 1,400-character chunks (`0.878 -> 0.9268` Recall@1) and hurts the 500-character chunks (`0.9268 -> 0.878`). That is why calibration exists.
- **A score gate refuses what is missing from the corpus, not what is missing from a policy.** Questions about a dress code or generative AI score `0.25 to 0.38` against the weakest answerable question's `0.44`, so a gate at `0.42` refuses them. Q42 and Q43 (Enterprise terms) score `0.56` and `0.66`: the SLA and refund policies exist, and only the clause is missing. Telling those apart is the model's job (the `kb-answer` prompt and the `CitationFaithfulness` judge), and the review queue is the net under it.
- **The gate is tuned on this question set.** Treat `0.42` as a worked example, not a constant; re-derive it from your own `outside` and `answerable` scores (the script prints both ranges).
- **A finished build activates itself**, even a partial one. Activation history (`POST .../activate`) is audited; a build's self-activation is not, which is why the script activates version 1 *explicitly* before demonstrating rollback.

The script's `--cleanup SUFFIX` option removes what a run created (the suffix is in its first line): `... cookbook_06_grounded_knowledge_assistant --cleanup 2bcd3d`.

## Recipe: run it through the UI

Sign in at `$CALIBER_URL/caliber/` as `admin` / `admin`. Sidebar groups: **Prompt lifecycle**, **Agent resources**, **Operate**, **Integrations**, **Admin**. The browser spec
[`caliber/caliber-ui/e2e/cookbooks/labs/06-knowledge-retrieval.spec.ts`](../../../caliber/caliber-ui/e2e/cookbooks/labs/06-knowledge-retrieval.spec.ts) performs exactly these steps with real clicks and no API calls (it
appends a timestamp to every name so it can be repeated); it passed twice in a row, in 22 seconds each on a warm stack.

1. **Upload the policy documents.** **Agent resources > Object Store**. In **Bucket browser** type `policy-corpus` in the new-bucket field and click the **+** (*Create bucket*), open the bucket, click **New folder**, name it `policies`, **Create**, open the folder, click **Upload** and choose the 11 files in `assets/dataset/sources/` (not the README).
   - Expected: 11 rows `policies/<name>.md`; the preview (**View preview**) of `data-retention-policy.md` shows the retention table with `Security audit logs`.
   - API: `caliber.object_store.create_bucket / upload` (`POST /object-store/buckets`, `POST /object-store/buckets/{bucket}/objects`).
2. **Build knowledge base version 1.** **Agent resources > Knowledge Bases > New knowledge base > Create new**. Bucket `policy-corpus`, tick the **`policies` folder** in the tree (`1 selected`), name `policy-qa-kb`, then open **Advanced configuration**: Chunking strategy **Markdown Aware**, Embedding model **MiniLM L6 v2**, Chunk size `1400`, Overlap `120`. Click **Create knowledge base**.
   - Expected: **Pipeline executions** lists a `KBR-...` run that goes `queued`, then `completed` (seconds); the header switcher reads `v1 (active)`.
   - API: `POST /knowledge-bases` answers `201` with `{knowledge_base, version, run}` and `status: queued`; poll `GET /knowledge-base-versions/{id}`.
3. **Explore the chunks.** **Explore > Chunks**. Searching `365 days` lists `data-retention-policy.md · chunk ...`; the source filter lists all 11 documents; **Version Summary** says `Chunk count 27`.
4. **Ask the same question in three modes.** **Explore > Query**. Type `How long are security audit logs retained?`, pick **Dense**, **Ask**; repeat with **Hybrid** and **GraphRAG**.
   - Expected, each time: a **Citations** list whose entries read `[1] <file>.md ...`, and under **Retrieved chunks** a chunk containing `365 days`. Also ask `What is the dress code for client meetings?`: chunks come back anyway, with nothing in them about dress codes. Retrieval always returns the nearest chunks; refusing is a decision on top.
   - API: `POST /knowledge/query {version_ids, question, top_k, retrieval_modes}` (1 to 3 versions, at most two modes per call).
5. **Store the answer prompt and import the calibration questions.** **Prompt lifecycle > Prompts > New prompt > Write / paste**: paste the body of [`assets/prompts/kb-answer.md`](assets/prompts/kb-answer.md) (below the frontmatter), name `kb-answer`, **Create and Open**. Open the **Calibration** stage, choose `assets/dataset/calibration-questions.jsonl` under **Upload calibration dataset**, type the dataset name `policy-qa-calibration`, click **Upload Dataset**.
   - Expected: `Uploaded 41 examples to policy-qa-calibration.` and **Prompt lifecycle > Test Sets** lists it.
   - This is the product's file importer for test sets; the Test Sets page itself only adds one row at a time.
6. **Calibrate: a baseline, then a better configuration.** **Knowledge Bases > policy-qa-kb > Calibrate**. Test set `policy-qa-calibration`, Retrieval mode **Dense**, Top-k `1`, **Run calibration**.
   - Expected: **Recall@k 88%**, **nDCG@k 90%**, **Faithfulness —**, **Answer correctness —** (the judges need a model). Click **Set baseline**. Switch to **Hybrid**, run again: **Recall@k 93%**, and **Vs. baseline** shows **+5%** for Recall@k and nDCG@k.
7. **Build the tuned version 2.** **Build > New version**, tick the `policies` folder again, **Advanced configuration**: Markdown Aware, MiniLM, Chunk size `500`, Overlap `60`, **Create version of policy-qa-kb**.
   - Expected: a second run completes; the switcher shows `v2 (active)`: a finished build activates itself.
8. **Set the active version.** In the header switcher choose `v1`, click **Set active**: `v1 (active)`. Choose `v2`, **Set active** again. (The rollback route has no button; **Set active** on the earlier version is the UI's rollback, and `POST /knowledge-bases/{id}/rollback` the API's.)
9. **Install the answer workflow.** **Integrations > Cookbooks**, **Install** on *Grounded Knowledge Assistant*, name it, acknowledge the prerequisite, confirm.
   - Expected: the Workflow Studio opens on a paused draft with a **Knowledge Query** node, a **Confidence** node and an **Output**.
10. **Bind it to the knowledge base and run it.** Click the **knowledge** node in the outline, set **Knowledge base** to `policy-qa-kb` and Top-K to `3`, **Save**, **Validate** (`Valid.`). The workflow is paused, so **Run** is disabled (`Resume this workflow before running it`): open the workflow from **Agent resources > Workflows**, click **Resume**, **Open editor**, **Run Monitor**, type `How long are security audit logs retained?`, **Run**.
    - Expected: status `completed`; the **Output** holds `Question: ...`, then `[1] data-retention-policy.md: ...` and the text `365 days`.
    - On this path the template's own confidence signals apply: they look only at citations, so every retrieval is "confident". The score gate in the API path is what separates the dress-code question; add it as an extra signal (see the script's `bind_knowledge_node`).
11. **Send the run's trace to a review queue.** **Operate > Observability**: click the newest trace and copy the `trace=tr-...` id from the address bar. **Operate > Review Queues > + New Queue**: name `policy-qa-review`, reviewer `@admin`, question key `decision_justified` ("Is abstaining or clarifying justified by the retrieved evidence?"), **Create queue**. Open the queue, paste the trace id in **Trace ids**, **Enqueue**, select the item, **Pass**, **Submit review**.
    - Expected: the item turns `completed` and reads `Reviewed by admin · wrote 1 assessment(s) back to the trace.`

## Verify it worked

- **Version and chunks** (`JAR` is the cookie jar from the login above, `/tmp/lab06.jar`): `curl -s -b $JAR $CALIBER_API/knowledge-bases | jq '.data[]|{name,active_version_id,v:.active_version_summary|{version_number,status,chunk_count}}'` shows `status: completed`, `chunk_count: 27` for version 1 (69 for version 2).
- **Cited chunk:** `POST $CALIBER_API/knowledge/query` with `{"version_ids":["<id>"],"question":"How long are security audit logs retained?","top_k":3,"retrieval_modes":["hybrid"]}`; `jq '.data.versions[0]|{citations:[.citations[].label],holder:[.retrieved_chunks[]|select(.content|contains("365 days"))|.chunk_id]}'`: the holder's `chunk_id` is one of the citations'.
- **Calibration history:** `GET $CALIBER_API/knowledge-bases/{id}/test-runs` lists each run with `metrics.recall_at_k` and `ndcg_at_k`; the knowledge base's `baseline_run_id` is the pinned one.
- **Audit:** `curl -s -b $JAR "$CALIBER_API/audit-log?limit=100" | jq -c '[.data.entries[]|select(.action|test("knowledge_base_version"))|.action]|unique'` lists `activate_knowledge_base_version` (and `rollback_knowledge_base_version` once you have rolled back); a build's self-activation writes no activation row.
- **Failed sources:** `GET $CALIBER_API/knowledge-base-versions/{id}/sources` shows `status: failed` and `error_summary: failed to extract pdf ...` for `scanned-handbook.pdf`; the version's `summary.failed_sources` is `1`.

## Common errors and troubleshooting

Each of these was provoked on a real stack.

| You see | Cause and fix |
| --- | --- |
| `404 feature_disabled`: `Cookbooks is disabled in this deployment. Set CALIBER_FEATURE_COOKBOOKS=true` | The flag is off (the shipped default). Set it and restart. The knowledge base, object store and workflow pages need no flag; only the Cookbooks gallery and `install` do. |
| Create answers `503`: `The object store is not configured or unreachable: no object-store credentials are configured. Set CALIBER_OBJECT_STORE_ACCESS_KEY_SOURCE and CALIBER_OBJECT_STORE_SECRET_KEY_SOURCE. Knowledge bases read their sources from an S3-compatible bucket, so nothing was created.` (and `GET /object-store/status` says `connected: false`, `Unable to locate credentials`) | No object store configured. Nothing is persisted: the request fails fast. Start with `--s3`, or set the variables above. |
| Same `503`, ending `the object-store endpoint cannot be reached. Check CALIBER_OBJECT_STORE_ENDPOINT_URL.` | Credentials are set, the endpoint is down or wrong. The message never includes the URL (it can carry credentials). |
| Every embedding model shows `available: false`, and create answers `503`: `Local embedding models need the optional packages 'sentence-transformers' and 'langchain-huggingface', which are not installed. Install caliber-suite[knowledge,knowledge-local], pre-cache the model if the host is offline, and restart CALIBER.` | The `knowledge-local` extra is missing. To reproduce without uninstalling torch, we started a stack whose Python hid those two packages from the product's own `find_spec` probe (a `sitecustomize.py` on `PYTHONPATH`); the options and the refusal above are the product's, the hidden packages are our simulation. |
| The first build sits in `queued` or `running` for minutes | The embedding model is being resolved against huggingface.co. Cache it once, then set `HF_HUB_OFFLINE=1`. An offline host that never cached it fails its first build. |
| A build is `completed` but a document is missing from answers | An unreadable file is **skipped, not fatal**: the version has `summary.failed_sources > 0`, the source row says why, the run log has a `source_failed` event, and **the version is live anyway**. Fix the file, build again, and activate the good version (`POST .../versions/{id}/activate`). |
| `status: failed`, `The selected files did not produce any chunks. Check the source types or extraction settings.` | Nothing readable in the selection. The live version is untouched; the failed one cannot be activated (`409 only completed versions can be activated`) and querying it returns no chunks and `answer_error: Version is not ready for retrieval.` |
| `POST /knowledge-bases/{id}/rollback` answers `409 no recorded prior active version ... rollback needs an audited activation to restore` | Rollback walks the activation audit trail, and a build's own activation is not on it. Activate the earlier version explicitly first (the script does), then rollback. |
| A version that is not active still answers queries | Intended. `active` decides what consumers that reference the knowledge base (the workflow) resolve; a query by `version_ids` only needs the version to be `completed`. |
| Faithfulness and Answer-correctness are `null` / `—` | The simulated judge. Calibration degrades to its deterministic metrics (Recall@k, nDCG@k) instead of failing. |
| `400 evaluation requires a real LLM provider; set CALIBER_LLM_PROVIDER (openai/anthropic) ...` on Evaluations | Scoring a prompt needs a model. See the simulated-vs-real table. |
| `403 missing required scope; expected one of ['caliber.operator'], have ['caliber.viewer']` creating a knowledge base, `['caliber.admin']` uploading to the object store | A viewer account (tested with a fresh `lab06-viewer`). In a project, creating or building needs `editor` or `owner`; calibrating needs `reviewer` or above ([Who can do what](../../../docs/use/knowledge-bases.md)). |
| `409 workflow is paused; resume it before running` | An installed cookbook is a paused workflow with a draft version, on purpose: review its bindings, then **Resume** (or `PATCH /workflows/{id} {"status":"active"}`). |
| The Chunks view or its source filter is blank right after a build | Fixed in this lab: the page cached the empty lists it fetched while the version was still queued. Reload if you run an older bundle. |
| `UNIQUE constraint failed: caliber_knowledge_base_relationships` and a build stuck `queued` | Fixed in this lab: chunk, entity and relationship ids were 32-bit and collided. Upgrade; existing ids keep working. |

## Cleanup and reset

```bash
# what the API script created (suffix is printed on its first line)
PYTHONPATH=sdk/caliber-sdk caliber/.venv/bin/python -m examples.cookbooks.cookbook_06_grounded_knowledge_assistant --cleanup <suffix>
# the bucket and its documents
KEYS=$(curl -s -b $JAR "$CALIBER_API/object-store/buckets/policy-corpus/objects?recursive=true" | jq -c '{keys:[.data.objects[].key]}')
curl -s -b $JAR -X POST $CALIBER_API/object-store/buckets/policy-corpus/objects/delete -H 'content-type: application/json' -d "$KEYS"
curl -s -b $JAR -X DELETE $CALIBER_API/object-store/buckets/policy-corpus      # 204; a non-empty bucket answers 409 "bucket is not empty"
# a knowledge base made in the UI: open it > Delete knowledge base (removes its chunks and its artifacts under .caliber/)
# the whole disposable stack
scripts/cookbook-env.sh down lab06 && rm -rf .tmp/cookbook-env/lab06
```

The browser spec leaves its uniquely named knowledge bases, prompts and test sets in place; delete them from their pages or reset the stack.
