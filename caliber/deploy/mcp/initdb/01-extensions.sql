-- Runs once, on first cluster initialization, against the POSTGRES_DB database.
-- CREATE EXTENSION only needs the extension files installed (they are) — it does
-- not require the libraries to be preloaded, so this works during init.

-- Vector similarity search (backs the `pgvector` MCP catalog entry).
CREATE EXTENSION IF NOT EXISTS vector;

-- Graph / openCypher queries (backs the `apache-age` MCP catalog entry).
CREATE EXTENSION IF NOT EXISTS age;
