"""Postgres-family database MCP server (relational / vector / graph).

A single FastMCP server runnable in three modes — ``relational`` (plain SQL),
``vector`` (pgvector similarity search), and ``graph`` (Apache AGE / openCypher)
— all backed by one Postgres reached via the ``POSTGRES_URL`` environment
variable. Run it as::

    python -m caliber.mcp_servers.db --mode relational
"""
