# SCN-07 assets

Files for [the lab](../README.md). The first group is what the lab runs and verifies on the simulated stack; the second adds what a real provider makes possible; the last is the part of the original recipe that is kept as a starting point and is **not** exercised (no knowledge base or connector on the lab machine).

## Used and verified

| File | What it is | How it is loaded |
| --- | --- | --- |
| [`dataset/ticket-cases.jsonl`](dataset/ticket-cases.jsonl) | 44 tickets: `{id, tags, input: {ticket_text, channel, customer_tier, [account_id], [order_id], [duplicate_of]}, expected: {decision, severity, sla_minutes, requires_approval, flags}}` | API: `POST /eval-datasets`, then `POST /eval-datasets/{id}/examples` per line (`input`, `expected`, `tags`). Read by the lab script and by the Playwright spec |
| [`workflows/support-triage.workflow.json`](workflows/support-triage.workflow.json) | The workflow manifest: `start -> classify -> triage_router -> {draft_reply, clarify_question, support_note, review -> action_request} -> final` | UI: `Workflows > Import > Choose file`. API: `POST /workflows/import {"manifest": ..., "name": ...}` |
| [`workflows/classify-ticket.py`](workflows/classify-ticket.py) | The body of the `classify` Python Code node, as a readable file | The script checks that the manifest's code equals this file. The node runs in the tool sandbox: no `import` (`json`, `re`, `math`, `datetime`, `statistics` are in scope), restricted builtins |

Dataset conventions: `input` is what the run receives (as JSON text); `expected` is what the lab compares (`decision`, `severity`, `sla_minutes`, `requires_approval`, sorted `flags`). To add a case, add a line and, if no rule gives the expected answer, change the rules (`classify-ticket.py`, then rebuild the node's `code` in the manifest).

## Used and verified on a real provider only

Run by the script and the spec only when `GET /readiness` says the stack has real providers (`--real-llm`); see "Run it with a real provider" in the README.

| File | What it is | How it is used |
| --- | --- | --- |
| [`workflows/support-triage-model.workflow.json`](workflows/support-triage-model.workflow.json) | The optional **model-based variant**: an agent (`classify_model`, the ticket-intake prompt condensed) classifies the ticket; a **deterministic guard** (a Data Transform decision table) decides what is acted on: an unreadable or low-confidence answer fails closed to `clarify`; every escalation waits at a human approval; a `reply` is written by a second agent (`draft_reply`, the reply prompt and tone skill condensed). No Python Code node, so Preview and Evaluations can run it. It names a test set `ticket-eval` for its (prod-only) deploy gate, which is what lets the calibration find a dataset. | `Workflows > Import` (create the test set first), or `POST /workflows/import`; script flag `--model-variant` |
| [`judges/support-triage-routing-and-reply.judge.json`](judges/support-triage-routing-and-reply.judge.json) | A boolean judge for one variant run: the printed label matches the labelled decision, a reply is safe (no promise, no invented citation, no internal names), an escalation or clarification does not obey an injected instruction | `POST /judges`, then `Judge.<id>` as a scorer of an Evaluations run (`predict_target: workflow`) |

The Evaluations rows of the variant come from the same 44-ticket file: twelve tickets (`TK01 TK07 TK26 TK10 TK16 TK44 TK18 TK20 TK40 TK29 TK33 TK37`, three per decision), with `expected.answer` set to the label the variant prints (`REPLY`, `CLARIFY`, `ESCALATE_SUPPORT`, `ESCALATE_BUG`). The label, not the reply text, is what `contains_expected` looks for.

## Reference only, not run by the lab

| File | What it is |
| --- | --- |
| `prompts/ticket-intake.md`, `prompts/support-reply.md` | Prompts for a model-based classifier and a cited reply |
| `skills/*.md` | Tone, escalation-checklist and citation skills |
| `tools/*.tool.json` | Registrations for two read tools (`get_order`, `search_knowledge_base` stand-ins) |
| `judges/grounded-support-reply.judge.json` | A grounding judge for the reply (`Evaluations` needs a real model) |

These were written for the original recipe (knowledge base, MCP issue write, judge scoring). They describe an `expectations` shape with `must_cite`; the verified dataset above does not carry it. The intake prompt, the reply prompt and the tone and escalation skills were condensed into the instructions of the variant's two agents; the knowledge-base tools and the grounding judge (which needs retrieved evidence to ground against) were not run.
