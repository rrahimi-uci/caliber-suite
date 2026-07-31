# MCP backing services

> Local development only. Postgres binds to `127.0.0.1` and uses the documented
> `caliber` / `caliber` credential. Use a managed, least-privilege database for
> any network-reachable MCP deployment.

Containers that the MCP catalog's servers connect to. This compose provides the
database the Postgres-family catalog entries talk to.

## Postgres + pgvector + Apache AGE

A single Postgres 17 with both extensions installed:

| Extension      | Backs catalog entry | Capability                          |
| -------------- | ------------------- | ----------------------------------- |
| _(core)_       | `PostgreSQL`        | tables, CRUD, arbitrary SQL         |
| `vector`       | `pgvector`          | vector tables + similarity search   |
| `age`          | `Apache AGE`        | graphs, vertices/edges, openCypher  |

All three catalog entries are backed by CALIBER's **first-party DB MCP server**
(`caliber.mcp_servers.db`, a FastMCP server in this repo), each launched in a
different `--mode` (`relational` / `vector` / `graph`) so it exposes real
**write** tools — `create_table`, `insert_rows`, `update_rows`,
`create_vector_table`, `similarity_search`, `create_graph`, `cypher_query`, etc.
— not just the read-only `query` of the deprecated
`@modelcontextprotocol/server-postgres` adapter it replaces. They all point at
this one `POSTGRES_URL`, so this single instance backs all of them.

The catalog entries use `command = ${PYTHON}` (resolved by the MCP gateway to
the CALIBER interpreter, so the server runs under the same venv where `psycopg`
is installed — from a source checkout, `pip install -e ".[postgres]"`) and
`env POSTGRES_URL=${POSTGRES_URL}` (resolved from the CALIBER process env — set
`POSTGRES_URL` in your `.env`).

### Run

```bash
docker compose -f deploy/mcp/docker-compose.yml up -d --build
```

### Connect from the MCP catalog

Open **MCP Servers → Add → Postgres** (or pgvector / Apache AGE) and set:

```
POSTGRES_URL=postgresql://caliber:caliber@localhost:5432/caliber
```

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

- **AGE is preloaded** for every connection via `session_preload_libraries=age`
  (compose `command`), and `ag_catalog` is on the default `search_path`, so
  `cypher(...)` works without a per-session `LOAD 'age'`.
- Data persists in the `caliber_pg_data` volume. `docker compose … down -v`
  wipes it.
- **Production:** don't run this container — point `POSTGRES_URL` at your managed
  Postgres (with `vector` / `age` installed there). This compose is for local
  dev / demo so the catalog works end-to-end out of the box.
- **Ollama / MinIO:** add them here as additional `services` when you want those
  catalog entries backed locally too (Ollama is heavy — best behind a compose
  profile so it's opt-in).
