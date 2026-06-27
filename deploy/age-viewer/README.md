# `age-viewer/` — graph DB console (Apache AGE Viewer)

Interactive web console for the **Apache AGE** graph database that lives in the
suite's Postgres container. Write openCypher, see results rendered as an
interactive node/edge graph — the graph counterpart of the MinIO console
(object storage) and Adminer (relational SQL).

```text
Console: http://localhost:8082        (override with AGE_VIEWER_PORT)
```

## Connect — automatic

The console **auto-connects on load** and selects the `knowledge_graph` graph, so you
land straight on the label panel (`Entity(8)`, `Rule(5)`, …) with nothing to
type. The image bakes this suite's coordinates into the connect form and
auto-submits it (see the `ServerConnectFrame` patch in the [Dockerfile](./Dockerfile)):

| Field        | Value      |
| ------------ | ---------- |
| Connect URL  | `postgres` |
| Connect Port | `5432`     |
| Database     | `caliber`  |
| User Name    | `caliber`  |
| Password     | `caliber`  |

If you ever **Close Session**, the form reappears pre-filled with the above —
just press **Connect**.

## See the graph

- Click a **Node Label** / **Edge Label** chip (e.g. `*(13)`) — it fills the
  editor with the wrapped query and you press the ▷ (Run) button; results draw
  on the canvas. Toggle **Graph / Table** at the right.
- Or type Cypher in the editor. The top box is a **raw SQL** box, so wrap it
  yourself:

  ```sql
  SELECT * FROM cypher('knowledge_graph', $$ MATCH (n)-[r]->(m) RETURN n, r, m $$)
    AS (n agtype, r agtype, m agtype);
  ```

## Seeding a graph

AGE starts empty. Load a representative sample knowledge graph
(entities + relationships + business rules) with:

```bash
docker exec -i caliber-mcp-postgres psql -U caliber -d caliber \
  < deploy/age-viewer/seed-graph.sql
```

This (re)creates the `knowledge_graph` graph (13 nodes / 13 edges). Agents that use
the MCP graph tools write into AGE the same way, so anything they create shows
up here too.

## Why it's built from source

There is **no published AGE Viewer image**, so [`Dockerfile`](./Dockerfile)
builds it from `apache/age-viewer`, pinned to a commit. Notable deltas from
upstream's stale `Dockerfile`:

- **Base `node:16`** (not `node:14`): the backend's winston/colorspace deps now
  use `||=` (needs Node 15+) and `pg@8` wants ≥16. Node 16 is also the *ceiling*
  — Node 17+ breaks react-scripts 4's webpack via the OpenSSL provider change.
- Adds **`@babel/runtime`** — upstream enables `@babel/plugin-transform-runtime`
  but never declares its required runtime package (and ships no lockfile).
- Adds **`sql/16/`** metadata queries — upstream only ships through PG 15, so
  the graph "meta" panel 500s on our PG 16 without it.
- Disables the dev **ESLint plugin** — a stray `import/no-unresolved` on
  cytoscape's deep import fails the CRA build otherwise.

First `up` builds the image (clones + `npm install` for frontend & backend,
~1–2 min); afterwards it's reused unless you pass `--build`.
