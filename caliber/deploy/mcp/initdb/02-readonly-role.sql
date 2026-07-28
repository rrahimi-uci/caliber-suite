-- Least-privilege role for the read-classified DB MCP tools.
--
-- CALIBER marks `run_query`, `list_tables`, `describe_table`, and
-- `similarity_search` as read / no-approval. Those tools always run inside a
-- database-enforced READ ONLY transaction, which is what makes the
-- classification true rather than parser-asserted. This role adds the second
-- layer: even a read that the transaction would permit is constrained by GRANT,
-- so a compromised or confused agent cannot SELECT anything it was not granted.
--
-- Development credential only. Production must provision its own role with a
-- real password and grant it only the tables the agent is meant to see; point
-- CALIBER_MCP_POSTGRES_READ_ONLY_URL at it.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'caliber_ro') THEN
    CREATE ROLE caliber_ro LOGIN PASSWORD 'caliber_ro';
  END IF;
END
$$;

-- Connect + read the public schema, and nothing else. No CREATE, so the role
-- cannot add objects even in a read-write transaction.
GRANT CONNECT ON DATABASE caliber TO caliber_ro;
GRANT USAGE ON SCHEMA public TO caliber_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO caliber_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO caliber_ro;

-- AGE keeps its graph catalogs in ag_catalog; without USAGE the role cannot
-- read graph metadata at all, which would break describe/list on the graph
-- sidecar. Read-only there too.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'ag_catalog') THEN
    GRANT USAGE ON SCHEMA ag_catalog TO caliber_ro;
    GRANT SELECT ON ALL TABLES IN SCHEMA ag_catalog TO caliber_ro;
  END IF;
END
$$;

-- Belt and braces: make every session this role opens default to read-only, so
-- a future code path that forgets connect_read_only() still cannot write.
ALTER ROLE caliber_ro SET default_transaction_read_only = on;
