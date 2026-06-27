# SCN-12 assets — what you hand Aria

This is an **Aria autonomous** scenario: you give Aria the intent, and it plans
+ creates the artifacts. These files are (a) the intent and (b) the concrete
**interaction answers** you paste when Aria pauses to confirm each step's inputs.

| # | Artifact | File | Used at |
| --- | --- | --- | --- |
| 1 | The intent | [`intent.md`](intent.md) | `Plans → new goal` (or `POST /aria/plans`) |
| 2 | Expected plan | [`expected-plan.json`](expected-plan.json) | sanity-check the draft plan |
| 3 | Judge spec | [`judges/answer-faithfulness.judge.json`](judges/answer-faithfulness.judge.json) | answer to the `judge.create` interaction |
| 4 | Dataset spec | [`datasets/support-faithfulness.dataset.json`](datasets/support-faithfulness.dataset.json) | answer to the `eval_dataset.create` interaction |

Flow: `POST /aria/plans` (intent) → `…/approve` → `…/execute`. The shipped
planner leaves step inputs empty and the interaction answer can't carry a payload
(`{approved, choice, value}` only), so **create each artifact via its own route**
with the specs below: `POST /judges` (judge) and `POST /eval-datasets` (test set).
The Aria answer just gates the step: `…/interactions/{id}/answer {approved:true}`.
Then verify in Judges / Test Sets.

Each spec IS the capability's create payload. Full hands-off autonomy — where an
LLM planner fills the step inputs — is not wired; see
[`../../ARIA-AUTONOMY.md`](../../ARIA-AUTONOMY.md) §Execution status.
