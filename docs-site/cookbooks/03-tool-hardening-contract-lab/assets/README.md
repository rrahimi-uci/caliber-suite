# Cookbook 03 assets

Files for [the recipe](../README.md). Every one is used by the lab's script
(`sdk/caliber-sdk/examples/cookbooks/cookbook_03_policy_safe_decision_tool.py`) and, where it
applies, by its browser spec.

| File | What it is | How the lab loads it |
| --- | --- | --- |
| [`tools/decide-refund.tool.json`](tools/decide-refund.tool.json) | The policy tool: a **read** tool, allowed in preview. The body of `POST /tools`. | API: `POST /tools` with the file as the body. UI: **Tools**, **Register Tool**; paste `input_schema` and `output_schema` through the Schema step's **Raw JSON** toggle. |
| [`tools/initiate-refund.tool.json`](tools/initiate-refund.tool.json) | The write tool, registered with `requires_approval: true`. | The same two paths. The wizard switches approval on, and locks it, when you pick **Write**. |
| [`dataset/refund-fixtures.jsonl`](dataset/refund-fixtures.jsonl) | 49 refund cases, one per line: `id`, `name`, `tags`, `inputs`, `expectations`. | API: the script turns each line into a tool fixture and saves them with `PUT /tools/{id}/test-cases`. UI: **Fixtures** › **Load cases from file** reads the whole file (rows can also be added one by one with **Add case**). |
| [`workflow/refund-decision.workflow.yaml`](workflow/refund-decision.workflow.yaml) | The workflow: decide, route, and issue the refund only after approval. | API: `POST /workflows/import`. UI: **Workflows**, **Import**, **Choose file**. |

## The two tools

Both point at the shipped module `caliber.workflows.demo_tools`:

- `decide_refund` is a pure function. The same inputs always give the same answer; it reads no
  clock, network or file. Rules run top to bottom and the first match wins. Anything it cannot
  judge fails closed to `manual_review`, never to `approve`.
- `initiate_refund` is a stub that returns `{order_id, refund_status: "initiated", amount_usd}`.
  Its `amount_usd` is the `amount` it was given. It stands in for a call to a payments system.

A registered tool has to be importable Python in the server's environment. To register your own,
install your package into that environment first, then point `module_path` and `callable_name` at
it (and consider setting `CALIBER_REGISTERED_TOOL_MODULE_ALLOWLIST`). There is no inline tool
editor.

## The policy

| # | Rule (first match wins) | Decision | `reason_code` |
| --- | --- | --- | --- |
| 1 | wrong type, non-finite or negative number, unknown tier | `manual_review` | `INVALID_INPUT` |
| 2 | `risk_flags` absent or `null` | `manual_review` | `MISSING_RISK_DATA` |
| 3 | `order_state` empty | `manual_review` | `UNKNOWN_ORDER_STATE` |
| 4 | any non-blank risk flag | `manual_review` | `RISK_FLAG_PRESENT` |
| 5 | `refunds_last_90d` is 3 or more | `manual_review` | `REFUND_VELOCITY` |
| 6 | state is not `delivered`, `shipped` or `returned` | `deny` | `NOT_REFUNDABLE_STATE` |
| 7 | older than the tier's window | `deny` | `OUTSIDE_WINDOW` |
| 8 | amount of zero or less | `deny` | `INVALID_AMOUNT` |
| 9 | amount above the tier's limit | `manual_review` | `AMOUNT_OVER_THRESHOLD` |
| 10 | otherwise | `approve` | `ELIGIBLE_AUTO_APPROVE` |

| Tier | Window | Largest amount decided without a reviewer |
| --- | --- | --- |
| `standard` | 30 days | 200 USD |
| `plus` | 45 days | 350 USD |
| `premium` | 60 days | 500 USD |

The tool returns exactly `{decision, reason_code, requires_approval}`, and `requires_approval` is
true exactly when the decision is `manual_review`. It is informational: the workflow's gate is the
write tool's own flag, and it pauses every refund that is not denied.

## The dataset

49 cases, hand-written so the expected value comes from the policy and not from running the tool
(a unit test, `caliber/tests/test_cookbook03_refund_policy.py`, checks every row against the tool).

| Tags | What they cover |
| --- | --- |
| `golden` | ordinary approve and deny cases |
| `boundary`, `window`, `amount` | day 30/31, 45/46, 60/61; 200.00/200.01, 350.00/350.01, 500.00/500.01 |
| `tier` | the same case under `standard`, `plus` and `premium` |
| `abuse`, `risk`, `velocity` | fraud and chargeback flags, a third refund in 90 days, a risk flag that outranks an expired window |
| `fail_closed` | risk data missing, order state unknown |
| `malformed` | text or boolean amount, negative or fractional age, unknown tier, flags that are not a list, a missing field |

Each fixture's assertion is `equals` on the JSON the tool returns (for example
`{"decision": "approve", "reason_code": "ELIGIBLE_AUTO_APPROVE", "requires_approval": false}`).
Key order and spacing do not matter, and `49` equals `49.0`; `false` is not `0` and a missing or extra key is a failure.
The script saves the value as sorted-key JSON text, the UI file loader as compact JSON; both pass.
A tool that raises fails every assertion, which is why malformed input has to produce a decision.

## The workflow

`start` feeds the run input (one case as JSON) to `policy` (a tool node on `decide_refund`) and to
`refund_request` (a Data Transform **mapping** that picks `order_id` and `amount`). `route` (a
router) sends a `deny` decision to the `denied` output and everything else to `refund` (a tool node
on `initiate_refund`). Because `initiate_refund` is registered with `requires_approval`, the
runtime stops that node at `waiting_approval`.

The tool names in the file are the plain ones. To import it next to an earlier run, replace
`decide_refund` and `initiate_refund` with the names you registered (the script and the browser
spec do this with a suffix).
