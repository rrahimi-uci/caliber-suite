# Beta reliability evidence fragments

This file records small, deterministic reliability fixes that contribute evidence
to the D5 Beta gate. It is not the release decision; `#613` owns that decision.

## Workspace-import idempotent replay (#979)

The D2 candidate once failed
`test_import_submission_is_durable_idempotent_and_digest_pinned` on a slow runner:
the same idempotency key returned `409` instead of `202`. The endpoint was correctly
comparing upload digests. The test rebuilt a ZIP for each request, and ZIP entry
timestamps could differ when the requests crossed a timestamp boundary, turning the
replay into a different upload.

The regression now creates one immutable ZIP body and reuses it for the first request,
same-intent replay, and changed-commit conflict. The changed-bundle conflict remains
covered separately. This preserves the durable `202`/`409` contract and does not add
sleep, polling, or production behavior changes.

Validation evidence:

- `test_workspace_routes.py::test_import_submission_is_durable_idempotent_and_digest_pinned`
  passed in five consecutive clean Python 3.11 runs.
- The full workspace route/import regression suite is required before merge; its
  exact result belongs in the pull request and the D5 executed-check manifest.
