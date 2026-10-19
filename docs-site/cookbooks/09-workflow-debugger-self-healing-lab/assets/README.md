# SCN-09 assets

Everything [the lab](../README.md) uses. The workflow is imported from a file; nothing is registered.

| # | Artifact | File | Used by |
| --- | --- | --- | --- |
| 1 | Workflow `refund-intake`, version 1 (the one with the bugs) | [`workflow/ticket-intake.v1.workflow.yaml`](workflow/ticket-intake.v1.workflow.yaml) | `Agent resources > Workflows > Import`; API `POST /workflows/import {manifest_yaml, name}` |
| 2 | Workflow `refund-intake`, version 2 (the patch) | [`workflow/ticket-intake.v2.workflow.yaml`](workflow/ticket-intake.v2.workflow.yaml) | the reference for the patch; the script builds version 2 from version 1 plus `patch/` |
| 3 | The patch: the three new node bodies | [`workflow/patch/validate.py`](workflow/patch/validate.py), [`check_reference.py`](workflow/patch/check_reference.py), [`decide.py`](workflow/patch/decide.py) | pasted into the node **Code** field in the editor |
| 4 | The 35 tickets, with what each version does | [`dataset/failing-inputs.jsonl`](dataset/failing-inputs.jsonl) | the script, the spec, the offline test |
| 5 | The stand-in account service (**not a real system**) | [`upstream/account_service.py`](upstream/account_service.py) | the workflow's `lookup` node |

**Dataset rows** are `{"id", "tags", "input": {"payload": "<the run input, exactly>"}, "expected": {...}}`. `expected` has `v1_status`, `v1_failing_node`, `v1_error_contains` (what version 1 does) and `decision`, `reason_code`, and sometimes `upstream_status` or `v2_errors_contain` (what version 2 decides). It is also loadable as a test set (`POST /eval-datasets/{id}/examples` with `input`, `expected`, `tags`), but a test set does not help here: Evaluations score a model's answer, and a deploy gate replays in Preview, which cannot run Python-code or API-request nodes.

**Node bodies** run as `run_python_node(input=None, context=None, inputs=None, run_input='')`: upstream ports arrive in the `inputs` dict, the returned dict is the node's `result` port (and each key is also offered as a port), and `json`, `re` and `math` are already in scope (an `import` statement fails, and so does any `__dunder__` attribute access, e.g. `type(x).__name__`). A node that exceeds `timeout_seconds` is killed: that is how the poison record fails.

**The three faults, and the lines that cause them.** `validate` (v1) indexes and type-checks strictly and raises on the first violation. `check_reference` (v1) uses `^(?:[A-Z0-9]+-?)+$`, nested quantifiers that backtrack exponentially on a long reference with one bad character. `decide` (v1) reads `inputs["account"]["json"]["tier"]` without checking the status code the `lookup` node returned. Version 2 normalizes what it can, uses an equivalent regular expression with no ambiguity (`^[A-Z0-9]+(?:-[A-Z0-9]+)*-?$`, and a 64-character cap), and returns a decision (`rejected_input`, or `manual_review` with `ACCOUNT_LOOKUP_FAILED`) instead of raising.

Running the account service: `python3 upstream/account_service.py 8780` (it listens on 127.0.0.1 only), and start CALIBER with `CALIBER_EGRESS_ALLOWED_HOSTS=127.0.0.1`.
