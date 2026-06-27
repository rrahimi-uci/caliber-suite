# SCN-02 assets — create these

Concrete, copy-pasteable artifacts for [the recipe](../README.md). Build order:

| # | Artifact | File | Create via |
| --- | --- | --- | --- |
| 1 | Skill `support-tone-and-citation` | [`skills/support-tone-and-citation.md`](skills/support-tone-and-citation.md) | `Library → Skills → New skill`. Paste **summary** (the frontmatter `summary` line — this is the trigger text the selector reads), **content** (everything below the frontmatter), `category`, `tags`. API: `POST /skills {name, summary, content, category, tags}` |
| 2 | Render-vars sample | [`dataset/render-vars.json`](dataset/render-vars.json) | `Skills → <skill> → Render Preview`. Paste the `variables` object. API: `POST /skills/{id}/test-render {variables:{...}}` |
| 3 | Trigger/selection cases | [`dataset/trigger-cases.jsonl`](dataset/trigger-cases.jsonl) | `Skills → <skill> → Trigger Tests`. Run each row's `inputs`. API: `POST /skills/{id}/test-selection {user_message, artifact_type?, session_goal?}` |

Then exercise the full recipe in [`../README.md`](../README.md): Render Preview →
Trigger Tests (positive + negative) → package export (**Download ZIP**;
`GET /skills/{id}/package.zip`) → Calibrate (queued, capture job id) → Bind.
(Re-import is **API-only** — `POST /skills/import-package` — there is no import
action in the shipped UI, so the UI recipe ends at export.)

## No judges folder here

Unlike SCN-01, there is **no `judges/` folder**. Skill trigger quality is graded
by **`SelectionPrecision`, a deterministic scorer** — `POST /skills/{id}/test-selection`
runs a deterministic selector (no LLM) and returns `is_selected`,
`selection_score`, `selection_reason`. So instead of judge instructions, the
selection expectations are documented below and encoded as the
`expectations.is_selected` field in [`dataset/trigger-cases.jsonl`](dataset/trigger-cases.jsonl).

### Selection expectations (what `SelectionPrecision` checks)

- **Positive (`is_selected: true`)** — customer-facing support phrasing: refunds,
  billing disputes, account lockout, plan changes, "how do I…/where is…" from a
  customer, "draft a reply", "respond to the customer". 6 rows (`T01`–`T06`).
- **Negative (`is_selected: false`)** — engineering/code/API/infra/schema work:
  "rotate the JWT signing key", "fix this Python stack trace", "DB index
  strategy", "SQL migration plan", "deployment latency metrics", "write a unit
  test". 6 rows (`T07`–`T12`). The `summary` is deliberately narrow so these do
  **not** trigger.
- **Gate.** Trigger accuracy ≥ `0.95`; and (at the API/verification level)
  source-vs-imported decisions identical (`package_round_trip_success = 1.0`) — see
  [`../verification.yaml`](../verification.yaml). The UI recipe itself ends at export
  (Download ZIP); the round-trip is a backend check, not a UI step.
- **If a negative selects (false positive),** sharpen the **`summary`** first
  (summary/trigger text dominates selection) before touching the long-form
  content, then re-run the cases.

## Conventions used across the pack

- **Skill files** (`skills/*.md`): SKILL.md style — YAML frontmatter (`name`
  [kebab-case, must **not** start with `claude`/`anthropic`], `summary`,
  `category`, `tags`, and the `render_variables` it expects) then the literal
  skill content. Paste the content below the frontmatter into the authoring
  textarea; render variables are `{{ snake_case }}`. The `summary` is the
  one-line trigger text the deterministic selector reads — keep it narrow.
- **Dataset files** (`dataset/*.jsonl`): one example per line,
  `{"id", "tags", "inputs", "expectations"}`. For trigger tests, `inputs` is the
  `POST /skills/{id}/test-selection` body (`user_message`, optional
  `artifact_type`, optional `session_goal`) and `expectations` is
  `{"is_selected": true|false}`.
- **Render-vars** (`dataset/render-vars.json`): a sample `variables` object for
  Render Preview plus the `expected.unresolved_variables` it should return
  (empty when every `render_variables` entry is supplied).
