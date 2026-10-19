# SCN-02 assets

Concrete files for [the recipe](../README.md). All of them are read by the example script, the browser spec and
`caliber/tests/test_cookbook_02_trigger_dataset.py`, so what is written here is what was run.

| # | Artifact | File | Used by |
| --- | --- | --- | --- |
| 1 | The skill `support-tone-and-citation`: front matter (`name`, `summary`, `category`, `tags`, the six starter `should_not_trigger` phrases, `render_variables`) and the instructions | [`skills/support-tone-and-citation.md`](skills/support-tone-and-citation.md) | `Skills > Build Skill` (wizard), or `POST /skills` with the front matter as fields and the body as `content` |
| 2 | The tightened trigger phrases (3 `should_trigger`, 14 `should_not_trigger`) | [`skills/trigger-phrases.json`](skills/trigger-phrases.json) | Skill detail page > Edit > Metadata JSON, or `PATCH /skills/{id}` with `skill_metadata` |
| 3 | The 40-case trigger test set | [`dataset/trigger-cases.jsonl`](dataset/trigger-cases.jsonl) | `POST /skills/{id}/test-selection` per row (UI: Trigger Tests, or Scenario Sets then Run) |
| 4 | Render variables, and a partial set | [`dataset/render-vars.json`](dataset/render-vars.json) | `Skills > <skill> > Render Preview`, or `POST /skills/{id}/test-render` |
| 5 | Invalid skill payloads and the reason each must be refused | [`skills/invalid-skills.json`](skills/invalid-skills.json) | `POST /skills` (the failure path) |

There is **no `judges/` folder**: skill trigger quality is graded by a deterministic selector, not a judge.

## Trigger cases (`dataset/trigger-cases.jsonl`)

One case per line: `{"id", "tags", "inputs": {"user_message", "artifact_type"?, "session_goal"?}, "expectations": {"is_selected"}}`. `inputs` is the
`POST /skills/{id}/test-selection` body.

- **Positive** (`is_selected: true`, 19): customer-facing support phrasing: refunds, billing disputes, account lockouts, plan changes, declined payments,
  closing an account, hand-off notes. Three are `edge` cases (`T23` is the bare word `refund?`, `T24` and `T25` are terse or urgent).
- **Negative** (`is_selected: false`, 21): engineering and unrelated work: key rotation, stack traces, database indexes, SQL migrations, latency metrics, unit tests,
  a Kubernetes crash loop, translation, SQL joins, a colour palette, a blog post, general knowledge.
- **Near misses** (8 of the negatives: `T27`-`T32`, `T34`, `T35`): engineering or internal requests that use the same words as support work (billing, invoice, customer, refund, ticket)
  and fool a keyword selector.
- The original twelve cases keep their ids `T01`-`T12`. An earlier audit found `T10`-`T12` selecting the skill (the summary names `schema`, `code`, `API` only to exclude them,
  but the selector counts any summary word); the `should_not_trigger` phrases in the skill file fix them, verified with the real selector.

What the real selector does with them (the README's table): the starter phrases (the six in the skill file) get **30 of 40** right (false positives `T27 T28 T29 T30 T31 T32 T34 T35`,
false negatives `T14 T23`); `trigger-phrases.json` gets **40 of 40**, with and without `artifact_type`/`session_goal`.

## Conventions

- **Skill file**: SKILL.md style. Front matter `name` (kebab-case, not starting with `claude`/`anthropic`), `summary`, `category`, `tags`, `should_not_trigger`, `render_variables`; then the literal
  content. Variables are `{{ snake_case }}`. The `summary` is the one-line text the selector matches against; keep it narrow.
- **Phrases match by substring** (`refund` matches `refunds`), so never use a short phrase (`api` would match `rapid`).
