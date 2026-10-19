# SCN-01 assets

Concrete files for [the lab](../README.md). Every file below was used in the verified run, except the judge.

| # | Artifact | File | Use it via |
| --- | --- | --- | --- |
| 1 | Prompt `cookbook-01-intake-classifier` v1 (strong) | [`prompts/intake-classifier.md`](prompts/intake-classifier.md) | `Prompts → New prompt → Write / paste` (paste the body below the front matter). API: `POST /prompts {name, template, commit_message}` then `POST /prompts/{name}/aliases/prod {version: 1}` |
| 2 | Prompt v2 (weaker, the regression) | [`prompts/intake-classifier-weak.md`](prompts/intake-classifier-weak.md) | `Author → Save draft`. API: `POST /prompts/{name}/versions {template, commit_message}` (does not move the alias) |
| 3 | Test set, 44 labelled cases | [`dataset/intake-classifier.jsonl`](dataset/intake-classifier.jsonl) | UI: the prompt's `Calibration` stage → `Upload JSON/JSONL` → `Upload Dataset`. API: `POST /eval-datasets {name, owner}` then one `POST /eval-datasets/{id}/examples {input, expected, tags}` per line |
| 4 | Stand-in model (not a model) | [`stand-in-model/server.py`](stand-in-model/server.py) | `python3 server.py --port 8765`, then start CALIBER with `OPENAI_BASE_URL=http://127.0.0.1:8765/v1` so Evaluations has something to score without a model key. (With a real key, `up lab01h --flags cookbooks --real-llm` needs none of it.) |
| 4b | Calibration scorer config (`Guidelines`) | [`calibration/guidelines.json`](calibration/guidelines.json) | UI: Calibration stage, tick **Guidelines**, paste the file as its config. API: `"scorers":[{"name":"Guidelines","config":<the file>}]`. Used instead of `Correctness`, which needs an `expected_response` this test set does not have |
| 5 | Judge `InstructionCompliance` (reference, not exercised) | [`judges/instruction-compliance.judge.json`](judges/instruction-compliance.judge.json) | A model-graded check: needs a real model, so it is not part of the verified lab |

## Conventions

- **Dataset files** (`dataset/*.jsonl`): one example per line. The keys the importers read are `input` and `expected` (and optionally `tags`, `weight`);
  `id` is documentation only. Do not use `inputs`/`expectations`: the UI importer would not find them.
- **Prompt files** (`prompts/*.md`): YAML front matter, then the literal template body. Variables are `{{ snake_case }}` and match the dataset's `input` keys
  (`ticket_text`, `channel`).
- **Judge files** (`judges/*.judge.json`): `{name, model, instructions, feedback_value_type}`; instructions reference `{{ inputs }}`/`{{ outputs }}`/`{{ expectations }}`.
