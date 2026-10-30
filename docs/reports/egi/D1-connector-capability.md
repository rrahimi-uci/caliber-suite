# D1 connector capability experiment

Question: can CALIBER's OpenAPI tool path send a stable `Idempotency-Key` and an `If-Match` expected version to the document-request system, and can that system be queried by key after a lost response? The answer decides the Beta connector. Experiment #567; ADR 0001 (#565) cites the decision at the end of this report.

## Setup

The tool is built the way an imported integration builds it: the downstream's `/openapi.json` is normalized (`integrations/openapi/normalize.py`), the input schema and execution config come from `integrations/openapi/tool_drafts.py`, and the callable from `bind_openapi_http_tool`. Calls go through the real `execute_openapi_http_tool` and the real egress transport (loopback allowed by an `EgressPolicy` bound for the test only), against the downstream process of #568. No transport is mocked.

```
cd caliber
pytest tests/egi_scenarios/test_connector_capabilities.py -q
```

Result of the current D1 run: 23 passed. The file needs no database, so it runs in the "EGI scenarios (PostgreSQL)" job and in any local run. Not exercised: authentication (`auth_binding` is `null` throughout); and the downstream's OpenAPI document was written to declare `Idempotency-Key` and `If-Match` as header parameters, so the experiment shows the executor can carry a spec-declared header, not that the real document-request system declares them.

## Results

| capability | result (yes/no) | test | evidence |
| --- | --- | --- | --- |
| Stable key: the same `Idempotency-Key` twice commits once | yes | `test_stable_key_mutates_once` | first call 201, second 200 with an identical body, `downstream.mutations() == 1` |
| A different key is a different request | yes | `test_a_different_key_is_a_different_request` | two keys, two mutations |
| Precondition: a stale `If-Match` is refused and commits nothing | yes | `test_stale_precondition_returns_412_and_no_mutation` | `If-Match: 999` gives 412, `mutations() == 0`; `If-Match: 1` (current) gives 201 |
| Query by key after a lost response | yes | `test_query_by_key_after_lost_response` | `drop_response_after_commit`: the connector call fails, the downstream committed once, `GET /document-requests?idempotency_key=...` returns 200 with the stored request |
| Query by key through the connector, within 1 s | yes | `test_the_query_runs_through_the_connector_and_answers_within_a_second` | over 40 runs: POST through the connector median 5.4 ms (max 7.6), query by key through the connector median 4.1 ms (max 5.5); an unknown key answers 404 ("it did not commit") |
| The connector recovers a lost response on its own by retrying with the key | yes | `test_the_connector_can_recover_a_lost_response_by_retrying_with_the_key` | `max_attempts: 3` and `idempotency_key_header` bound: attempt 1 is lost, attempt 2 replays with 200 (carrying the original `If-Match: 1`, although the case is by then at version 2), `attempts == 2`, one mutation. Imported drafts now persist the same binding and bounded setting. |
| A late answer (timeout) after the commit is recovered the same way | yes | `test_a_timeout_after_the_commit_is_recovered_by_retrying_with_the_key` | `timeout_seconds: 0.3` against a 1 s delayed answer: attempt 1 times out after the commit, attempt 2 replays, one mutation. Draft generation/update expose this bounded setting. |
| A failure before the commit is retried safely | yes | `test_a_failure_before_the_commit_is_retried_safely` | 500 on attempt 1, 201 on attempt 2, one mutation |

Remaining exactly-once proof boundaries after #990 (each is pinned by a passing test that records current behavior):

| finding | test |
| --- | --- |
| The key must travel as the spec's own `Idempotency-Key` header parameter. The imported derived schema now promotes it to the canonical top-level `idempotency_key` field and removes it from free-form `header_params`; hand-written configs without the binding remain one-attempt by design. | `test_the_derived_input_schema_exposes_canonical_bound_inputs`, `test_an_unbound_handwritten_config_does_not_enable_retries_from_a_header_parameter` |
| The expected version must remain part of the proposal contract. The imported derived schema now promotes the declared `If-Match` parameter to `expected_resource_version` and the executor binds it to the configured header. | `test_the_derived_input_schema_exposes_canonical_bound_inputs`, `test_conflicting_canonical_and_header_values_are_refused_before_dispatch` |
| Retry safety is now fail-closed: with the header bound and *no key sent*, a POST that gets a 503 is sent only once. This is a regression guard from #711, not an open gap. | `test_a_bound_header_with_no_key_sent_does_not_retry_a_post` |
| A lost response remains ambiguous until the connector verifies a receipt, even though #711 now exposes structured `never_sent`/attempt state and #990 adds the per-attempt log and replay disposition. | `test_query_by_key_after_lost_response`, `test_attempt_log_exposes_transport_and_replay_disposition`, `test_a_refused_connection_and_an_egress_block_keep_their_own_cause` |
| Import and update must preserve the binding and retry policy. The draft builders and route now persist `idempotency_key_header`, `expected_resource_version_header`, `max_attempts`, and `timeout_seconds`; the SDK and UI request types expose the same settings. | `test_an_imported_tool_persists_bound_headers_and_retry_limits`, `test_openapi_draft_persists_retry_and_timeout_configuration` |
| Every 4xx is returned as a normal result and never retried: a stale `If-Match` is 412 and a reused key with a different request is 422 (`attempts == 1`). A dispatcher has to classify both as "did not commit" itself. | `test_key_reuse_for_another_request_is_a_result_not_an_exception`, `test_stale_precondition_returns_412_and_no_mutation` |
| The derived schema remains case-sensitive and typed: a lowercase `idempotency-key` is rejected, and `If-Match` as an ETag string (`"1"`) is rejected because the downstream's spec types it as an integer. A canonical value that conflicts with a header-map value is refused before dispatch. | `test_header_names_are_exact_in_the_derived_schema`, `test_conflicting_canonical_and_header_values_are_refused_before_dispatch` |

## Executor changes and current status

**Status after #711 (B05.3, PR #989) and #990 (B05.3b):** all six D1 plumbing changes are implemented. Changes 1 and 4 came from #711; #990 delivered changes 2, 3, 5 and 6. The remaining proof boundary is B05.4 receipt verification, not the OpenAPI import/executor contract.

1. **Retry only when a key is actually sent.** Done by #711; non-idempotent methods retry only when the outgoing request carries exactly one key.
2. **One canonical key input.** Done by #990; imported schemas expose `idempotency_key` and remove the bound header from `header_params`, while hand-written configs retain defensive conflict checks.
3. **A first-class expected version.** Done by #990; imported schemas expose `expected_resource_version` and the executor maps it to the declared header.
4. **Typed transport errors.** Done by #711; `OpenApiExecutionError` records `never_sent`, `attempts`, and the attempt log.
5. **Expose retry and key binding in the config an import produces.** Done by #990; draft generation and update persist the binding and bounded `max_attempts`/`timeout_seconds` values.
6. **Report the attempt log.** Done by #990; results and errors carry per-attempt status, emitted key, transport outcome, replay disposition, and an explicitly unverified receipt disposition.

## Decision

`keep`, with the D1 executor/import plumbing complete after #711 and #990. The OpenAPI document-request connector carries a stable key, a precondition and a query by key through the existing executor; imported tools now preserve those fields in one proposal-bound input contract, retry only when safe, and expose enough attempt evidence for the downstream receipt layer. No cooperative fixture connector is needed, provided the real document-request system declares the same headers as the experiment's downstream (to be checked against its OpenAPI document when #522 starts). A successful HTTP response is still not a verified receipt until B05.4 queries and matches the downstream record.
