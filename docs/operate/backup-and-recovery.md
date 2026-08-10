---
audience:
  - operator
doc_type: runbook
product_area: operations
stability: ga
prerequisites:
  - A CALIBER deployment or disaster-recovery review
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - backup
  - recovery
  - storage
  - rollback
---

# Backup and recovery

Use this page when you need the operator view of what CALIBER state must be
preserved, what can be rebuilt, and where the disaster-recovery boundary stops
and asset-specific rollback begins.

## At a glance

| Surface | Preserve it? | Why it matters |
| --- | --- | --- |
| Metadata database | yes | control-plane records, governed state, releases, jobs, and references |
| Object store data | yes | uploads, previews, extracted artifacts, and log sink material |
| Workflow storage | yes | run workspaces and file-backed workflow evidence |
| Generated docs/UI assets | rebuildable | these come from repository sources and build outputs |

## 1. Back up the state that cannot be re-derived safely

The safest minimum backup set is:

- metadata database
- object-store content
- workflow storage

Without those together, a restore may recover only part of the product state.

## 2. Distinguish disaster recovery from feature rollback

Recovery of the deployment itself is not the same as rolling back a governed
asset release. Use the disaster-recovery plan to restore the platform, then use
asset-specific rollback or release controls where needed.

## 3. Verify restores, not just backups

The useful operator question is not “did a backup job run?” but “can this
deployment restore to a usable, trustworthy state?”

After a restore rehearsal, verify:

- readiness
- storage access
- release and review history visibility

## 4. Related docs

- [Storage and state](../operate/storage-and-state.md)
- [Health and readiness](../operate/health-and-readiness.md)
- [Operations runbook](../runbook.md)
- [The refinement loop](../refinement-loop.md)
