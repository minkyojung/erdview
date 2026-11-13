"""MCP server for chat apps without a terminal (Claude Desktop, …): three tools over core.py. stdio only, local only.

  claude mcp add erdview -- uvx --with mcp --from git+https://github.com/minkyojung/erdview erdview mcp
  Claude Desktop: {"mcpServers": {"erdview": {"command": "uvx", "args": ["--with", "mcp", "--from",
                   "git+https://github.com/minkyojung/erdview", "erdview", "mcp"], "env": {"ERDVIEW_PG": "postgresql://…/postgres"}}}}
"""
from __future__ import annotations
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from . import core

SOURCE = ("a PostgreSQL URL (postgresql://user:pass@host:5432/db), a schema JSON file (erdview or tbls format), or a migration folder "
          "at a version like db/migration@V0002 (needs ERDVIEW_PG, an admin URL, in the server env). "
          "Ask the user for the URL if you do not have it; never guess one.")
mcp = MCPServer("erdview")


def _resolve(spec: str) -> dict:
    try: return core.resolve(spec)
    except Exception as e: raise ToolError(str(e)) from None   # the message is what the model needs to read


@mcp.tool(description="Read the database structure (tables, columns, keys, indexes, comments) as JSON. Read-only, catalog only, no row data. "
                      "tables: only these (names or schema.name) — use it on big databases to keep the answer small. source: " + SOURCE)
def schema(source: str, tables: list[str] | None = None) -> dict:
    d = _resolve(source)
    if tables: d = {**d, "tables": [t for t in d["tables"] if t["name"] in tables or f"{t['schema']}.{t['name']}" in tables]}
    return d


@mcp.tool(description="Draw the interactive ERD and open it in the user's browser. Returns the path of the HTML file (self-contained, shareable). source: " + SOURCE)
def open_erd(source: str) -> str:
    return str(core.open(_resolve(source)))


@mcp.tool(description="What changed from a to b, as lines: + added, - removed, ~ changed (tables, columns, foreign keys, indexes); "
                      "'no changes' when equal. a, b: each " + SOURCE)
def diff(a: str, b: str) -> str:
    return core.format_diff(core.diff_schema(_resolve(a), _resolve(b)), a, b)


def main() -> None: mcp.run(transport="stdio")


if __name__ == "__main__": main()
