# MCP backing services

> Local development only. Postgres binds to `127.0.0.1` and uses the documented
> `caliber` / `caliber` credential. Use a managed, least-privilege database for
> any network-reachable MCP deployment.

Containers that the MCP Quick Connect catalog's database servers connect to. This compose file provides
the one Postgres that backs all three Postgres-family catalog entries. The MCP Servers page is a preview
area: it is hidden until you set `CALIBER_FEATURE_INTEGRATIONS=true` and restart CALIBER (see
[Alpha scope](../../../docs/start/alpha-scope.md#feature-flags)).

## Postgres + pgvector + Apache AGE

A single Postgres 17 with both extensions installed:

| Extension | Backs catalog entry | Capability |
| --- | --- | --- |
| _(core)_ | `PostgreSQL` | tables, CRUD, arbitrary SQL |
| `vector` | `pgvector` | vector tables and similarity search |
| `age` | `Apache AGE` | graphs, vertices and edges, openCypher |

All three catalog entries are backed by CALIBER's **first-party database MCP server**
(`caliber.mcp_servers.db`, a FastMCP server in this repository), launched once per `--mode`
(`relational`, `vector` or `graph`). Each mode exposes real **write** tools as well as reads:

| Mode | Tools |
| --- | --- |
| `relational` | `list_tables`, `describe_table`, `create_table`, `insert_rows`, `update_rows`, `delete_rows`, `run_query` (read-only), `execute_sql` |
| `vector` | `create_vector_table`, `upsert_vectors`, `similarity_search`, plus the read tools |
| `graph` | `create_graph`, `drop_graph`, `create_vertex`, `create_edge`, `cypher_query` |

The read-classified tools (`run_query`, `list_tables`, `describe_table`, `similarity_search`) run inside a
database-enforced READ ONLY transaction. The `initdb` scripts also provision a read-only role,
`caliber_ro`, that they can use (see `initdb/02-readonly-role.sql`).

### Run

From `caliber/`, to start just this Postgres:

```bash
docker compose -f deploy/mcp/docker-compose.yml up -d --build
```

The full suite stack (`make start` from the repository root) already includes this Postgres and the
database MCP sidecars, so do not run both: they use the same container name and port. See
[Local bring-up](../../../docs/operate/local-bring-up.md) for the full stack.

### Connect from the MCP catalog

With `CALIBER_FEATURE_INTEGRATIONS=true`, open **MCP Servers**, then **Quick Connect**, and choose
**PostgreSQL**, **pgvector** or **Apache AGE**.

- **In the Docker suite stack** the entries already point at the database MCP sidecars on the stack's
  private network (`http://mcp-db-relational:8101/mcp`, `http://mcp-db-vector:8102/mcp` and
  `http://mcp-db-graph:8103/mcp`), and CALIBER's allowlists already include those hosts. Nothing else to set. The
  sidecars read `CALIBER_MCP_POSTGRES_URL` (default `postgresql://caliber:caliber@postgres:5432/caliber`) and
  `CALIBER_MCP_POSTGRES_READ_ONLY_URL`. The development default shares CALIBER's own privileged database; for
  anything else, point both at a separate least-privilege database and role.
- **When you run CALIBER natively**, those sidecar host names do not exist. Either start the server yourself
  and edit the entry's URI to match,

  ```bash
  POSTGRES_URL=postgresql://caliber:caliber@127.0.0.1:5432/caliber \
    python -m caliber.mcp_servers.db --mode relational --transport streamable-http --port 8101
  # then set the entry's URI to http://127.0.0.1:8101/mcp
  ```

  or configure a local stdio server (command `${PYTHON}`, args `-m caliber.mcp_servers.db --mode relational`,
  environment `POSTGRES_URL=...`), which CALIBER's default allowlists permit. The `psycopg` driver must be
  installed in CALIBER's environment: from a source checkout, `pip install -e ".[postgres]"`.

### Verify

```bash
docker exec -it caliber-mcp-postgres psql -U caliber -d caliber -c "\dx"
# expect: age, vector (and plpgsql)

# vector
docker exec -it caliber-mcp-postgres psql -U caliber -d caliber \
  -c "SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector AS distance;"

# graph (AGE is preloaded; ag_catalog is on the search_path)
docker exec -it caliber-mcp-postgres psql -U caliber -d caliber \
  -c "SELECT create_graph('demo');" \
  -c "SELECT * FROM cypher('demo', \$\$ CREATE (:Person {name:'Ada'}) RETURN 1 \$\$) AS (n agtype);"
```

## Notes

- **AGE is preloaded** for every connection via `session_preload_libraries=age` (compose `command`), and
  `ag_catalog` is on the default `search_path`, so `cypher(...)` works without a per-session `LOAD 'age'`.
  As a consequence, tables that CALIBER's migrations create in this database live in the `ag_catalog` schema.
- Data persists in the `caliber_pg_data` volume. `docker compose … down -v` wipes it.
- `migrate-pg16-to-pg17.sh` is a one-time helper for an older stack whose volume was created by Postgres 16.
  A new install does not need it.
- **Production:** do not run this container. Point the MCP server's `POSTGRES_URL` at your managed Postgres
  (with `vector` and `age` installed there), with a least-privilege role. This compose file is for local
  development and demos, so the catalog works end to end out of the box.
- **Ollama and MinIO:** add them here as additional `services` when you want those catalog entries backed
  locally too (Ollama is heavy; it is best behind a compose profile so it is opt-in).
