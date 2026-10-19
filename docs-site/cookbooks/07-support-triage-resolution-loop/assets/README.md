# SCN-07 assets

Files for [the lab](../README.md). The first group is what the lab runs and verifies; the second group is a model-based layer that is kept as a starting point but is **not** exercised (the lab machine has no model, knowledge base or connector).

## Used and verified

| File | What it is | How it is loaded |
| --- | --- | --- |
| [`dataset/ticket-cases.jsonl`](dataset/ticket-cases.jsonl) | 44 tickets: `{id, tags, input: {ticket_text, channel, customer_tier, [account_id], [order_id], [duplicate_of]}, expected: {decision, severity, sla_minutes, requires_approval, flags}}` | API: `POST /eval-datasets`, then `POST /eval-datasets/{id}/examples` per line (`input`, `expected`, `tags`). Read by the lab script and by the Playwright spec |
| [`workflows/support-triage.workflow.json`](workflows/support-triage.workflow.json) | The workflow manifest: `start -> classify -> triage_router -> {draft_reply, clarify_question, support_note, review -> action_request} -> final` | UI: `Workflows > Import > Choose file`. API: `POST /workflows/import {"manifest": ..., "name": ...}` |
| [`workflows/classify-ticket.py`](workflows/classify-ticket.py) | The body of the `classify` Python Code node, as a readable file | The script checks that the manifest's code equals this file. The node runs in the tool sandbox: no `import` (`json`, `re`, `math`, `datetime`, `statistics` are in scope), restricted builtins |

Dataset conventions: `input` is what the run receives (as JSON text); `expected` is what the lab compares (`decision`, `severity`, `sla_minutes`, `requires_approval`, sorted `flags`). To add a case, add a line and, if no rule gives the expected answer, change the rules (`classify-ticket.py`, then rebuild the node's `code` in the manifest).

## Reference only, not run by the lab

| File | What it is |
| --- | --- |
| `prompts/ticket-intake.md`, `prompts/support-reply.md` | Prompts for a model-based classifier and a cited reply |
| `skills/*.md` | Tone, escalation-checklist and citation skills |
| `tools/*.tool.json` | Registrations for two read tools (`get_order`, `search_knowledge_base` stand-ins) |
| `judges/grounded-support-reply.judge.json` | A grounding judge for the reply (`Evaluations` needs a real model) |

These were written for the original recipe (knowledge base, MCP issue write, judge scoring). They describe an `expectations` shape with `must_cite`; the verified dataset above does not carry it. Treat them as unverified until you run them with a real provider.
