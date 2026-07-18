# Trustworthy Intake Classifier

## Demo objective

A prompt that classifies inbound text into a stable JSON shape:
`intent`, `priority`, `confidence`, `needs_review`, and `reason`.

## Feasibility & substitutions

Read [`../FEASIBILITY.md`](../FEASIBILITY.md) first. For this scenario:

- ✅ Author + version the prompt in **Prompts** (real, MLflow Prompt Registry).
- ⚠️ The **Playground** is a quick live sanity check (a real chat call, not a
  scored run). Use it to confirm the prompt emits one strict-JSON record; the
  *scored* regression runs happen on the prompt's own **Test Sets** + **Runs**
  stages. See FEASIBILITY §2.1.
- ✅ Regression is scored **on the prompt workspace**: generate cases on
  **Test Sets** and *Run Tests & Judge*, then on **Runs** pin a strong
  **baseline** and re-run a weakened version to read the **Vs. baseline** diff.
  The standalone **Evaluations** page *can* score a prompt now (set
  *What to score* = **Prompt version**, which renders the prompt as the system
  instruction), but this lab uses the prompt workspace's **Runs** baseline-diff
  as the demoable regression surface.
- ⚠️ **Calibration** queues a background optimizer job; show the queued job id,
  don't wait for an inline score (FEASIBILITY §2.4).
- The `InstructionCompliance` judge (`custom_judge`) is a real LLM judge you can
  author on the Judges page and select in Evaluations under **Custom LLM judges**
  (it runs as a `Judge.<id>` scorer); with the default *What to score* =
  **Model completion**, Evaluations scores the model's direct answer to dataset
  inputs (choose **Prompt version** to score the prompt itself), while this lab
  uses the prompt's own Test Sets / Runs judge for the per-case pass / partial /
  fail verdicts.
  The `rule_checks` (`valid_json`, `allowed_intent_values`) map to deterministic
  **scorers**, not a "deterministic judge" (which doesn't exist).

## Prerequisites & seed

- A configured chat / judge model (else the Test Sets and Runs stages error).
- 4 labeled cases already in [`test-data.yaml`](test-data.yaml) (P01–P04).

## Recipe (UI-first, with API fallbacks)

1. **Create the prompt.** `Library → Prompts → New prompt`, name
   `intake-classifier`. Paste the system template from
   [`build.yaml`](build.yaml) and append the output contract:
   `Return ONLY JSON with keys intent, priority, confidence, needs_review, reason.`
   - API: `POST /prompts {name, template, commit_message}`.
2. **Author.** Open the workspace → `Author`; edit the template and either
   `Save draft` (registers a new version without touching the live alias) or
   `Save & promote` (also rotates the alias live). Add a commit message.
   Versions resolve under alias `prod`; the Version history panel below lists
   them, with promote/roll back per version.
3. **Playground (live sanity check).** Pick a model, send a representative
   ticket; confirm the prompt emits one strict-JSON record. *(A real chat call,
   not a scored run — expected.)*
4. **Build & run the test set.** `Prompts → intake-classifier → Test Sets`:
   set a count covering billing/account/how-to plus an injection case
   (mirroring P01–P04), click **Generate Test Cases**, then **Run Tests & Judge**
   to score them inline, then **Save to Test Sets** as `intake-classifier-golden`
   so they survive a refresh.
   - API: `POST /eval-datasets` then `POST /eval-datasets/{id}/examples`.
5. **(Optional) JSON/intent judge.** `Evaluate → Judges → New judge`
   `InstructionCompliance`: instructions = *"Given `{{ inputs }}` and model
   `{{ outputs }}`, return true only if outputs is valid JSON with the required
   keys and `intent` is allowed; else false."* `feedback_value_type = bool`.
   (Authored here for reference; the prompt's own Test Sets / Runs judge does the
   scoring for this lab.)
6. **Run the baseline.** `Prompts → intake-classifier → Runs` → **Run tests**:
   the Runs stage scores the cases from your latest Test Sets run with a built-in
   pass / partial / fail judge into a scorecard. **This is your baseline** — click
   **Set as baseline** to pin it.
7. **Introduce a regression.** In `Prompts → Author`, save a *weaker* version
   (drop the "JSON only" rule).
8. **Compare.** Back on `Prompts → Runs`, click **Run tests** again on the
   weakened version; because a baseline is pinned, a **Vs. baseline** panel
   renders the per-case diff and a **regressions** list — confirm the weak variant
   drops the cases that fail the JSON contract.
9. **Calibration (queued).** `Prompts → Calibration → Start Calibration Run` →
   capture the job id (runs in background: MetaPrompt/GEPA).
10. **Observe.** `Observe → Observability` → open a failing run's trace; read the
    node tree to explain the failure (don't expect a span named `prompt_render`).

## Demo evidence to capture

- Prompt id + the two version numbers (strong + weak).
- The pinned baseline run and the comparison run, plus the **Vs. baseline** diff.
- One intentional regression visible in the per-case Runs diff.
- Calibration job id.

## Done when / gate

- Golden output JSON is valid and schema-stable (`min_overall_score ≥ 0.90`).
- The weaker variant is caught as a regression in the Runs **Vs. baseline** diff.
- Each failure is explainable from its trace.
