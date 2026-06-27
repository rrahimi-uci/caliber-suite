# Adminer (Postgres web UI)

The database counterpart of the MinIO console — a lightweight web UI for the
Postgres container (CALIBER metadata, the MLflow tracking store, and the MCP
pgvector/AGE data all live there).

Starts with the core infra (like MinIO's console):

```bash
make infra-up          # or: docker compose -f deploy/compose.yaml up -d
```

- Console: <http://localhost:8081>
- Login (pre-filled where possible):

| Field | Value |
| --- | --- |
| System | PostgreSQL |
| Server | `postgres` (pre-filled) |
| Username | `caliber` |
| Password | `caliber` |
| Database | `caliber` (CALIBER + MCP) or `mlflow` (tracking store) — leave blank to list all |

Adminer is a single ~5 MB container with no setup. For a richer client you can
swap it for pgAdmin, but Adminer matches the MinIO-console simplicity. Override
the port with `ADMINER_PORT` in `deploy/.env`.
