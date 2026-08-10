---
audience:
  - operator
  - developer
doc_type: how-to
product_area: storage
stability: ga
prerequisites:
  - A CALIBER deployment or storage design question
reviewed_on: 2026-08-10
version_applicability: current main branch docs contract
tags:
  - storage
  - database
  - object-store
  - retention
---

# Storage and state

Use this page when the practical question is where CALIBER keeps state, what
must be backed up, and which storage surface to inspect when something is
missing or inconsistent.

## At a glance

| State surface | What it stores | Related settings |
| --- | --- | --- |
| Metadata database | CALIBER metadata and control-plane state | `CALIBER_DATABASE_URL` |
| Object store | uploaded files, previews, extracted artifacts, log sink targets | `CALIBER_OBJECT_STORE_*` |
| Workflow storage | workflow-run workspaces and files | `CALIBER_WORKFLOW_STORAGE_*` |
| Retention policy | how long finalized run files remain | `CALIBER_WORKFLOW_STORAGE_RETENTION_*` |

## 1. Separate metadata from file storage

CALIBER does not keep every operational artifact in the same place.

That means:

- a metadata record may exist even if file storage is unhealthy
- file storage may be present even if the metadata database is unavailable

Treat those as separate failure domains during diagnosis.

## 2. Workflow storage is its own operator concern

Workflow-run file storage has its own backend, endpoint, limits, signing, and
retention controls. Do not assume the generic object-store settings cover the
full workflow storage path.

## 3. Common failure modes

| Symptom | First thing to check |
| --- | --- |
| Metadata exists but files are missing | workflow storage or object-store backend health |
| Uploads succeed locally but fail in another environment | endpoint, region, path-style, or credential-source differences |
| Old run files disappeared | retention policy and janitor expectations |

## 4. Related docs

- [Config and environment reference](../reference/config-and-environment.md)
- [Object store](../07-object-store/architecture.md)
- [Backup and recovery](../operate/backup-and-recovery.md)
- [Operator troubleshooting](../operate/troubleshooting.md)
