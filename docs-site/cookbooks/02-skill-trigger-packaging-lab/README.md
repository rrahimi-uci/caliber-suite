# Precision Skills

## Demo objective

A narrow skill that activates only on intended queries and applies response
policy (tone, citation, escalation hints) without tool coupling.

## Feasibility & substitutions

Read [`../FEASIBILITY.md`](../FEASIBILITY.md). For this scenario:

- ✅ Author + version, **Render Preview**, **Trigger Tests**, package
  export/import, and **Bind** are all real.
- ✅ **Trigger/selection testing is deterministic** (`POST /skills/{id}/test-selection`,
  no LLM) — this is exactly the `SelectionPrecision` "deterministic" check in
  [`verification.yaml`](verification.yaml). Returns `is_selected`,
  `selection_score`, `selection_reason`.
- ✅ Package round-trip is UI-native: export **Download ZIP**, then **Import
  package ZIP** with an explicit reject, rename, or admin-only merge strategy.
- ✅ Positive and negative trigger phrases are stored as skill metadata; the
  selection result shows exact positive and exclusion signals.
- ⚠️ **Scenario Sets** is scaffolded — drive positive/negative cases through
  **Trigger Tests** + **Runs** instead (FEASIBILITY §1, Skills).
- ⚠️ **Calibrate** queues a background job (capture the job id, no inline score).
- ❗ Skill names must be kebab-case and must **not** start with `claude`/`anthropic`.

## Prerequisites & seed

- Skill workspace access; positive + negative trigger queries from
  [`test-data.yaml`](test-data.yaml).

## Recipe (UI-first, with API fallbacks)

1. **Create the skill.** `Library → Skills → New skill`, name
   `support-tone-and-citation`. Fill **summary** (1 line — this is what the
   selector reads) with tight boundaries, e.g. *"Customer-facing support tone +
   citation hints. Use for support replies; NOT for engineering/code/API
   questions."* Set category to `customer_support`. Add **content** (the full
   policy) + tags `support`, `tone`.
   - API: `POST /skills {name, summary, content, category, tags}`.
2. **Render Preview.** Provide the `render_contract` variables from
   [`build.yaml`](build.yaml) (`user_message`, `audience`, `policy_context`).
   Confirm `unresolved_variables` is empty and the layout is correct.
   - API: `POST /skills/{id}/test-render {variables:{...}}`.
3. **Trigger Tests (positive).** Run each positive query (e.g. *"How do I get a
   refund?"*) → expect `is_selected=true`. **(negative).** Run engineering
   queries (e.g. *"How do I rotate a JWT signing key?"*) → expect
   `is_selected=false`.
   - API: `POST /skills/{id}/test-selection {user_message, artifact_type?, session_goal?}`.
4. **Tighten + baseline.** If a negative query selects, sharpen the **summary**
   (summary/trigger text dominates selection — edit it before the long-form
   content). Save a run as baseline in **Runs**.
5. **Export package.** Open skill detail → **Download ZIP**
   (`GET /skills/{id}/package.zip`). This is your portability artifact
   (`SKILL.md` + `agents/openai.yaml` + resources).
6. **Import round-trip.** On the standalone Skill Detail page, choose
   **Rename import**, enter `support-tone-citation-copy` in **Renamed skill
   name**, and choose **Import ZIP**. The UI sends the exported archive as
   multipart `file` to `/ajax-api/2.0/mlflow/caliber/skills/import-package.zip`
   with `conflict_strategy=rename` and `rename_to=support-tone-citation-copy`.
   Re-run the same trigger queries against the imported skill; decisions and
   matched signals must agree.
7. **Calibrate (queued).** `Skills → Calibrate` → capture job id.
8. **Bind.** `Skills → Bind` to an agent (or workflow node) for later use
   (`POST /skills/{id}/bind`).

## Demo evidence to capture

- Trigger accuracy: false-positive and false-negative counts on the case set.
- Package ZIP + the side-by-side source-vs-copy trigger decisions (equivalence).
- Calibration job id.

## Done when / gate

- Positive/negative trigger behavior is stable (`trigger_accuracy_min ≥ 0.95`).
- Imported package reproduces source trigger decisions exactly
  (`package_round_trip_success = 1.0`).
