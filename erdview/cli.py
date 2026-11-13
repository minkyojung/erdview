"""erdview — interactive ERD for any database, nothing leaves your machine.

  erdview open   [SOURCE]            draw and open in the browser
  erdview export [SOURCE] [-o FILE]  write a self-contained HTML (stdout by default) to share
  erdview schema [SOURCE]            print the schema JSON (for agents and other tools)
  erdview sql                        print the read-only PostgreSQL query the tool runs
  erdview diff   A B [--json] [--pg ADMIN_URL]   what changed from A to B (+ added, - removed, ~ changed); exit code 1 when different
                                     A/B = SOURCE, or a migration folder with a version: db/migration@V0002 (needs --pg)
  erdview serve  [SOURCE] [--source NAME=SPEC]... [--pg ADMIN_URL] [--port N] [--no-browser]
                                     local live viewer: pick a source in the page, see changes within 2 s.
                                     SPEC = PostgreSQL URL | JSON file | folder of migrations (one version per file; needs --pg)

SOURCE is a PostgreSQL URL (postgresql://user@host/db), a JSON file (erdview or tbls format), or '-' for stdin.
Omitted → $DATABASE_URL. Other databases: `tbls out -t json | erdview open -`.
"""
from __future__ import annotations
import argparse, os, sys
from pathlib import Path
from . import core, __version__


def source(arg: str | None, admin_url: str | None = None) -> dict:
    s = arg or os.environ.get("DATABASE_URL")
    if not s: sys.exit("give a PostgreSQL URL, a JSON file, '-' for stdin, or set DATABASE_URL")
    try: return core.resolve(s, admin_url)
    except Exception as e: sys.exit(str(e))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="erdview", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("open", "export", "schema"):
        p = sub.add_parser(name); p.add_argument("source", nargs="?")
        if name == "export": p.add_argument("-o", "--out")
        if name == "open": p.add_argument("-o", "--out", help="also keep the HTML at this path")
    sub.add_parser("sql"); sub.add_parser("mcp", help="run the MCP server on stdio (needs: pip install 'erdview[mcp]')")
    p = sub.add_parser("diff"); p.add_argument("a"); p.add_argument("b"); p.add_argument("--json", action="store_true")
    p.add_argument("--pg", default=os.environ.get("ERDVIEW_PG"))
    p = sub.add_parser("serve"); p.add_argument("source", nargs="?"); p.add_argument("--source", dest="sources", action="append", default=[], metavar="NAME=SPEC")
    p.add_argument("--pg", default=os.environ.get("ERDVIEW_PG"), help="admin PostgreSQL URL used to build migration versions in throwaway databases")
    p.add_argument("--port", type=int, default=0); p.add_argument("--no-browser", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "sql": sys.stdout.write(core.SQL); return
    if a.cmd == "mcp":
        try: from . import mcp_server
        except ImportError: sys.exit("the MCP server needs the mcp package: pip install 'erdview[mcp]'  (or: uvx --with mcp ... erdview mcp)")
        mcp_server.main(); return
    if a.cmd == "diff":
        d = core.diff_schema(source(a.a, a.pg), source(a.b, a.pg))
        import json; print(json.dumps(d, ensure_ascii=False) if a.json else core.format_diff(d, a.a, a.b))
        sys.exit(0 if core.is_empty_diff(d) else 1)
    if a.cmd == "serve":
        from . import serve as S
        specs = ([a.source] if a.source else []) + a.sources
        if not specs and os.environ.get("DATABASE_URL"): specs = [os.environ["DATABASE_URL"]]
        if not specs: sys.exit("give a source: a PostgreSQL URL, a JSON file, or --source NAME=SPEC")
        try: srcs = [S.parse_source(s, a.pg) for s in specs]
        except ValueError as e: sys.exit(str(e))
        S.serve(srcs, a.port, open_browser=not a.no_browser); return
    data = source(a.source)
    if a.cmd == "schema": import json; print(json.dumps(data, ensure_ascii=False))
    elif a.cmd == "export":
        html = core.export(data)
        if a.out: Path(a.out).write_text(html, encoding="utf-8"); print(a.out)
        else: sys.stdout.write(html)
    elif a.cmd == "open":
        n = len(data["tables"]); print(f"{core.open(data, a.out)}  ({n} relations, {data.get('database', '')})")


if __name__ == "__main__": main()
