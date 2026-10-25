# Architecture decision records

One file per decision, numbered in order, each with `Status`, `Context`, `Decision` and `Consequences` sections.
A record is changed by a new record that supersedes it, not by editing the old one.

| ADR | Title | Status |
| --- | --- | --- |
| 0001 | Reference profile | planned: B01, #565 (D1) |
| 0002 | Execution ownership | planned: B01, #565 (D1) |
| 0003 | Control-record schemas | planned: B01, #566 (D1) |
| [0004](0004-autonomy-levels.md) | Autonomy levels: which roles an agent may hold, per use case | Proposed |
| 0005 | Credential leases and workload identity | planned: M01, #543 (D6) |
| 0006 | Release signing and promotion | planned: M02, #544 (D6) |
| 0007 | Sandbox trust level and egress | planned: M07, #549 (D7) |
| 0008 | Regional cells and residency | planned: M18, #599 (D12) |
| 0009 | Knowledge extraction: reuse of `policy-logic-forge` behind an `Extractor` protocol | planned: M21, #602 (D13) |

Numbers are allocated here before the record is written, so two packages never claim the same one. Take the next
free row when a new decision needs a record.
