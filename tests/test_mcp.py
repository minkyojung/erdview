"""The MCP server, driven the way a chat app drives it: spawned as a subprocess over stdio with the SDK's own client.
Skipped when the mcp package is not installed (it is an optional extra)."""
import asyncio, json, os, sys
from pathlib import Path
import pytest
mcp = pytest.importorskip("mcp")
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from test_erdview import fresh, ADMIN, FIX  # noqa: F401
from test_serve import migrations  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def call(tool, env=None, **args):
    """Run one tool call against a fresh server process; returns (is_error, text)."""
    async def go():
        params = StdioServerParameters(command=sys.executable, args=["-m", "erdview.cli", "mcp"],
                                       env={**os.environ, "PYTHONPATH": str(ROOT), "ERDVIEW_NO_BROWSER": "1", **(env or {})})
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                if tool is None: return [t.name for t in (await s.list_tools()).tools], [t.description for t in (await s.list_tools()).tools]
                res = await s.call_tool(tool, args)
                return res.is_error, "".join(c.text for c in res.content if getattr(c, "type", "") == "text")
    return asyncio.run(go())


def test_lists_three_tools_with_guidance():
    names, docs = call(None)
    assert names == ["schema", "open_erd", "diff"]
    assert all("Ask the user for the URL" in d for d in docs) and "Read-only" in docs[0]


def test_schema_tool_filters_tables():
    err, text = call("schema", source=str(FIX / "tbls_postgres_sample.json"), tables=["users", "public.posts"])
    assert not err
    d = json.loads(text); assert sorted(t["name"] for t in d["tables"]) == ["posts", "users"] and d["format"] == "pg-erd/1"
    err, text = call("schema", source=str(FIX / "xss_min.json"))
    assert not err and len(json.loads(text)["tables"]) == 2


def test_open_tool_writes_the_file(fresh):
    url = fresh("mcp", (FIX / "school.sql").read_text())
    err, path = call("open_erd", source=url)
    assert not err and path.endswith(".html") and Path(path).exists()
    html = Path(path).read_text(); assert '"name":"students"' in html and url not in html


def test_diff_tool_on_migration_versions(migrations):
    err, text = call("diff", env={"ERDVIEW_PG": ADMIN}, a=f"{migrations}@V0001", b=f"{migrations}@V0002")
    assert not err and "+ table   app.b" in text and "+ fk      app.b.b_a_id_fkey" in text
    err, text = call("diff", a=str(FIX / "xss_min.json"), b=str(FIX / "xss_min.json"))
    assert not err and text.startswith("no changes")


def test_bad_source_is_a_tool_error():
    err, text = call("schema", source="/no/such/file.json")
    assert err and "not a URL" in text
    err, text = call("diff", a="db/migration@V9", b="db/migration@V9")
    assert err
