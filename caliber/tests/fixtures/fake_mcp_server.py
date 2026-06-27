"""Minimal stdio MCP server used by gateway tests."""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP

app = FastMCP("caliber-test")


@app.tool()
def web_search(query: str, limit: int = 3) -> dict[str, object]:
    hits = [f"{query}-1", f"{query}-2", f"{query}-3"]
    return {"query": query, "hits": hits[: max(1, limit)]}


@app.tool()
def read_file(path: str) -> dict[str, object]:
    return {"path": path, "content": "fake file"}


@app.tool()
def fail_tool(message: str = "failure") -> dict[str, object]:
    raise RuntimeError(message)


if __name__ == "__main__":
    app.run(transport="stdio")
