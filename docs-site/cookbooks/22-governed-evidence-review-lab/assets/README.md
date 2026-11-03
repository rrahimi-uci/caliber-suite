# Lab 22 assets

Lab 22 intentionally does not copy the Harborlight case or create another downstream server.
The canonical synthetic case is:

- `caliber/tests/fixtures/egi/case_000184.json`
- provenance: `caliber/tests/fixtures/egi/SOURCE.md`
- projection: `caliber/tests/egi_scenarios/downstream/harborlight.py`
- downstream contract: `caliber/tests/egi_scenarios/downstream/app.py`

The fixture is pinned to the companion book repository commit recorded in `SOURCE.md`, carries
the upstream MIT license and synthetic-data notice, and is not for credit decisions. The lab's
only write is the missing-document request for `MORT-2026-000184`; it never makes a mortgage,
underwriting, approval or denial decision.

The scripted model, fake clock and downstream ledger are test fixtures, not production assets.
Installed examples must use these deterministic seams or an equivalent local fake and must never
call a live model or customer system in ordinary CI.
