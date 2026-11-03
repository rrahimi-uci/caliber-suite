# Harborlight fixture provenance

`case_000184.json` is the permitted synthetic Harborlight Mortgage case
fixture used by the Beta EGI scenarios. It is copied byte-for-byte from the
companion code in the book repository; the D1 downstream uses only the
case identity, version, and the fixed missing-document seed derived from the
C01--C03 scenario contract.

## Pinned source

- Repository: <https://github.com/rrahimi-uci/building-enterprise-general-intelligence>
- Commit: `dacedba44fa995a70ed1f0a02ae2f339a063a69e`
- Source path: `mortgage_case/fixtures/case_000184.json`
- Source URL: <https://github.com/rrahimi-uci/building-enterprise-general-intelligence/blob/dacedba44fa995a70ed1f0a02ae2f339a063a69e/mortgage_case/fixtures/case_000184.json>
- Upstream file SHA-256: `b0d08045874f8a0663114df288d26c55242784bb654f525c737aec11e2993fba`
- Upstream Git blob: `a7abc939ec20f829a826e64e3e6b33276ef12a01`

The upstream repository identifies `mortgage_case/` as companion code under
the MIT license. The applicable license text is
[`LICENSE-CODE`](https://github.com/rrahimi-uci/building-enterprise-general-intelligence/blob/dacedba44fa995a70ed1f0a02ae2f339a063a69e/LICENSE-CODE).
The fixture remains synthetic and carries the upstream notice that it is not
for credit decisions.

## CALIBER use and boundaries

`downstream/harborlight.py` validates and loads this file, then projects it
into the existing D1 downstream seed shape. It does not add a service or
change the existing routes. The downstream remains a disposable system of
record for tests only; it stores document requests and makes no mortgage,
underwriting, approval, denial, or LOS decision.
