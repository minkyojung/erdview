"""erdview — interactive ERD for any database, nothing leaves your machine.

  erdview open   [SOURCE]            draw and open in the browser
  erdview export [SOURCE] [-o FILE]  write a self-contained HTML (stdout by default) to share
  erdview schema [SOURCE]            print the schema JSON (for agents and other tools)
  erdview sql                        print the read-only PostgreSQL query the tool runs

SOURCE is a PostgreSQL URL (postgresql://user@host/db), a JSON file (erdview or tbls format), or '-' for stdin.
Omitted → $DATABASE_URL. Other databases: `tbls out -t json | erdview open -`.
"""
from __future__ import annotations
import argparse, os, sys
from pathlib import Path
from . import core, __version__


def source(arg: str | None) -> dict:
    s = arg or os.environ.get("DATABASE_URL")
    if not s: sys.exit("give a PostgreSQL URL, a JSON file, '-' for stdin, or set DATABASE_URL")
    if s == "-": return core.load(sys.stdin.read())
    if "://" in s: return core.schema(s)
    p = Path(s)
    if p.exists(): return core.load(p.read_text(encoding="utf-8"))
    sys.exit(f"not a URL, not a file: {s}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="erdview", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("open", "export", "schema"):
        p = sub.add_parser(name); p.add_argument("source", nargs="?")
        if name == "export": p.add_argument("-o", "--out")
        if name == "open": p.add_argument("-o", "--out", help="also keep the HTML at this path")
    sub.add_parser("sql")
    a = ap.parse_args(argv)
    if a.cmd == "sql": sys.stdout.write(core.SQL); return
    data = source(a.source)
    if a.cmd == "schema": import json; print(json.dumps(data, ensure_ascii=False))
    elif a.cmd == "export":
        html = core.export(data)
        if a.out: Path(a.out).write_text(html, encoding="utf-8"); print(a.out)
        else: sys.stdout.write(html)
    elif a.cmd == "open":
        n = len(data["tables"]); print(f"{core.open(data, a.out)}  ({n} relations, {data.get('database', '')})")


if __name__ == "__main__": main()
