# D1 connector capability experiment

Question: can CALIBER's OpenAPI tool path send a stable `Idempotency-Key` and an `If-Match` expected version to the document-request system, and can that system be queried by key after a lost response? The answer decides the Beta connector. Experiment #567; ADR 0001 (#565) cites the decision at the end of this report.

## Setup

The tool is built the way an imported integration builds it: the downstream's `/openapi.json` is normalized (`integrations/openapi/normalize.py`), the input schema and execution config come from `integrations/openapi/tool_drafts.py`, and the callable from `bind_openapi_http_tool`. Calls go through the real `execute_openapi_http_tool` and the real egress transport (loopback allowed by an `EgressPolicy` bound for the test only), against the downstream process of #568. No transport is mocked.

```
cd caliber
pytest tests/egi_scenarios/test_connector_capabilities.py -q
```

Result of the last run: 17 passed. The file needs no database, so it runs in the "EGI scenarios (PostgreSQL)" job and in any local run. Not exercised: authentication (`auth_binding` is `null` throughout); and the downstream's OpenAPI document was written to declare `Idempotency-Key` and `If-Match` as header parameters, so the experiment shows the executor can carry a spec-declared header, not that the real document-request system declares them.

## Results

| capability | result (yes/no) | test | evidence |
| --- | --- | --- | --- |
| Stable key: the same `Idempotency-Key` twice commits once | yes | `test_stable_key_mutates_once` | first call 201, second 200 with an identical body, `downstream.mutations() == 1` |
| A different key is a different request | yes | `test_a_different_key_is_a_different_request` | two keys, two mutations |
| Precondition: a stale `If-Match` is refused and commits nothing | yes | `test_stale_precondition_returns_412_and_no_mutation` | `If-Match: 999` gives 412, `mutations() == 0`; `If-Match: 1` (current) gives 201 |
| Query by key after a lost response | yes | `test_query_by_key_after_lost_response` | `drop_response_after_commit`: the connector call fails, the downstream committed once, `GET /document-requests?idempotency_key=...` returns 200 with the stored request |
| Query by key through the connector, within 1 s | yes | `test_the_query_runs_through_the_connector_and_answers_within_a_second` | over 40 runs: POST through the connector median 5.4 ms (max 7.6), query by key through the connector median 4.1 ms (max 5.5); an unknown key answers 404 ("it did not commit") |
| The connector recovers a lost response on its own by retrying with the key | yes, **with a hand-edited config** | `test_the_connector_can_recover_a_lost_response_by_retrying_with_the_key` | `max_attempts: 3` and `idempotency_key_header` bound: attempt 1 is lost, attempt 2 replays with 200 (carrying the original `If-Match: 1`, although the case is by then at version 2), `attempts == 2`, one mutation. No import path sets either setting (see finding 5). |
| A late answer (timeout) after the commit is recovered the same way | yes, same condition | `test_a_timeout_after_the_commit_is_recovered_by_retrying_with_the_key` | `timeout_seconds: 0.3` against a 1 s delayed answer: attempt 1 times out after the commit, attempt 2 replays, one mutation. The default timeout is 30 s, so loss detection is slow unless the operation sets one. |
| A failure before the commit is retried safely | yes, same condition | `test_a_failure_before_the_commit_is_retried_safely` | 500 on attempt 1, 201 on attempt 2, one mutation |

Where the tool as imported is *not* ready for exactly-once dispatch (these are findings, each pinned by a passing test that records current behavior):

| finding | test |
| --- | --- |
| The key must travel as the spec's own `Idempotency-Key` header parameter. `idempotency_key_header` reads `input_data["idempotency_key"]`, but the input schema derived from the spec is `additionalProperties: false` and has no such property, so a bound tool rejects it. It works only with no input schema. | `test_a_derived_input_schema_refuses_the_configured_idempotency_key_input`, `test_the_configured_idempotency_key_header_works_without_a_derived_schema` |
| Retries of a POST are enabled only when `idempotency_key_header` is bound in the config, not when the caller sends the key as a header parameter. Without the binding a lost response is an error after one attempt. | `test_a_key_sent_as_a_header_parameter_does_not_enable_retries_by_itself` |
| The converse is a hazard: with the header bound and *no key sent*, `_max_attempts` still allows retries, so a POST that got a 503 is sent again without any key. Against a downstream that does not deduplicate that is a duplicate create. | `test_retries_are_allowed_when_a_header_is_bound_even_if_no_key_is_sent` |
| A lost response surfaces as `OpenApiExecutionError("... failed after N attempt(s): <message>")`. The cause is kept (`__cause__` is `httpx.RemoteProtocolError` for a dropped read, `httpx.ConnectError` for a refused connection, an `EgressBlockedError` for a policy block), but the exception class and message are generic, so telling "never left" from "may have committed" means inspecting `__cause__`, which is fragile. | `test_query_by_key_after_lost_response`, `test_a_refused_connection_and_an_egress_block_keep_their_own_cause` |
| No import path produces `idempotency_key_header` or `max_attempts`: `tool_drafts.py::build_execution_config` emits neither, and nothing in the API or UI sets them. A tool built the way an import builds it gets one attempt, no retries and no bound key. | `test_an_imported_tool_has_neither_a_bound_key_header_nor_retries` |
| Every 4xx is returned as a normal result and never retried: a stale `If-Match` is 412 and a reused key with a different request is 422 (`attempts == 1`). A dispatcher has to classify both as "did not commit" itself. | `test_key_reuse_for_another_request_is_a_result_not_an_exception`, `test_stale_precondition_returns_412_and_no_mutation` |
| The derived schema is case-sensitive and typed: a lowercase `idempotency-key` is rejected, and `If-Match` as an ETag string (`"1"`) is rejected because the downstream's spec types it as an integer; `header_params` is optional to the schema though required by the system (omitting it passes validation and the downstream answers 400). A `header_params` key supplied by the caller also silently wins over a key derived into the bound header (`headers.setdefault`). | `test_header_names_are_exact_in_the_derived_schema` |

## Executor changes needed

None of these is made here (out of scope; #522 task 3 owns them). All are in `caliber/src/caliber/integrations/openapi/executor.py::execute_openapi_http_tool` unless noted.

1. **Retry only when a key is actually sent.** `_max_attempts(config, method=..., has_idempotency_key=bool(idempotency_key))` (line ~178) takes the flag from the binding. It should be true only when the outgoing request carries the idempotency header, whether it came from `input_data["idempotency_key"]` or from `header_params`.
2. **One way to supply the key.** Either add an `idempotency_key` property to the derived input schema whenever `idempotency_key_header` is bound (`integrations/openapi/tool_drafts.py::build_tool_input_schema`), or drop the top-level input and read the key from `header_params[<header>]`. The dispatcher (#520) will derive the key and should pass it in one agreed field.
3. **A first-class expected version.** `If-Match` works today only as a free-form `header_params` entry (typed by the spec; the downstream's spec says integer). The dispatcher should pass `expected_resource_version` as a dedicated input and the executor map it to the header named by the operation, so a caller cannot send a precondition that the proposal did not record.
4. **Typed transport errors.** The cause is already chained (`raise ... from last_error`); the gap is that the exception class and message are generic. Raise distinct errors, or set a structured attribute such as `never_sent: bool`, for "the request never left" (`ConnectError`, a policy block) and "the request may have been processed" (`RemoteProtocolError`, `ReadTimeout`, `ReadError`). The reconciliation work (#522, #626) branches on exactly that distinction; today it would have to inspect `__cause__`.
5. **Expose retry and key binding in the config an import produces** (`caliber/src/caliber/integrations/openapi/tool_drafts.py::build_execution_config`, line ~168, and whatever persists it). Today `idempotency_key_header` and `max_attempts` exist only in `executor.py` and in tests; an operation whose spec declares an idempotency header should get it bound, and an operator should be able to set `max_attempts` and `timeout_seconds`.
6. **Report the attempt log.** `attempts` is returned; the status of each attempt (and whether the final answer was a replay: 200 after a 201 elsewhere) is not. A receipt needs it.

## Decision

`keep`, conditional on #522 task 3 delivering changes 1, 2 and 5 (retry only when a key is sent; one way to supply the key; retry and key binding reachable from an import). The OpenAPI document-request connector carries a stable key, a precondition and a query by key through the existing executor today; what it lacks is the plumbing around them, which touches the tool-input and exception contracts and is therefore a task, not a footnote. No cooperative fixture connector is needed, provided the real document-request system declares the same headers as the experiment's downstream (to be checked against its OpenAPI document when #522 starts).
