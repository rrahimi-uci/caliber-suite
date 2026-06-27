# SCN-01 assets — create these

Concrete, copy-pasteable artifacts for [the recipe](../README.md). Build order:

| # | Artifact | File | Create via |
| --- | --- | --- | --- |
| 1 | Prompt `intake-classifier` | [`prompts/intake-classifier.md`](prompts/intake-classifier.md) | `Library → Prompts → New prompt`, paste the template body (text below the frontmatter). API: `POST /prompts {name, template, commit_message}` |
| 2 | Eval dataset `intake-classifier-golden` | [`dataset/intake-classifier.jsonl`](dataset/intake-classifier.jsonl) | `Prompts → intake-classifier → Test Sets`: Generate Test Cases, **Run Tests & Judge**, then **Save to Test Sets**. API: `POST /eval-datasets {name}` → `POST /eval-datasets/{id}/examples` per line |
| 3 | Judge `InstructionCompliance` | [`judges/instruction-compliance.judge.json`](judges/instruction-compliance.judge.json) | `Evaluate → Judges → New judge`, paste fields. API: `POST /judges` |

Then score the regression on the prompt's **Runs** stage (`Prompts → intake-classifier →
Runs`): run the golden Test Set, pin the strong run as baseline, save a weaker prompt
version, and re-run to show the regression in the **Vs. baseline** diff. (The
`InstructionCompliance` judge authors on the Judges page and is selectable in
Evaluations under **Custom LLM judges** as a `Judge.<id>` scorer; note Evaluations
scores the model's direct answer to dataset inputs, not the prompt itself, so use
the prompt's **Runs** stage for a prompt-grounded judged check.)

## Conventions used across the pack

- **Prompt files** (`prompts/*.md`): YAML frontmatter (name, model hint,
  variables) then the literal template body. Paste the body into the authoring
  textarea; variables are `{{ snake_case }}`.
- **Dataset files** (`dataset/*.jsonl`): one example per line,
  `{"inputs": {...}, "expectations": {...}}` — the exact shape the Evaluations
  scorers + judges read (`{{ inputs }}`, `{{ outputs }}`, `{{ expectations }}`).
- **Judge files** (`judges/*.judge.json`): `{name, model, instructions,
  feedback_value_type}`; instructions reference `{{ inputs }}`/`{{ outputs }}`/
  `{{ expectations }}` (the UI requires at least one). `feedback_value_type` ∈
  bool|int|float|str.
- **Tool files** (`tools/*.tool.json`): the `POST /tools` body
  (`module_path`+`callable_name` must be importable); `tools/*.py` holds inline
  `python_code` node bodies.
