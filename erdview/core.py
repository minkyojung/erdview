"""The one place with behaviour. cli.py and mcp_server.py only call these.

  schema(url)      PostgreSQL → schema dict (format "pg-erd/1"), read-only, catalog only — never row data
  from_tbls(d)     tbls JSON (`tbls out -t json`, 14 databases) → the same dict
  load(text)       any of: pg-erd JSON, tbls JSON, psql output with headers
  export(data)     the viewer HTML with the data baked in: one file, opens anywhere, nothing leaves the machine
  open(data)       export to a temp file and open it in the browser; returns the path
"""
from __future__ import annotations
import json, re, tempfile, webbrowser
from pathlib import Path

HERE = Path(__file__).parent
SQL = (HERE / "pg_erd.sql").read_text(encoding="utf-8")
VIEWER = (HERE / "viewer.html").read_text(encoding="utf-8")
DATA_TAG = '<script type="application/json" id="erd-data"></script>'
FORMAT = "pg-erd/1"


def schema(url: str) -> dict:
    import psycopg
    with psycopg.connect(url) as c:
        c.read_only = True
        return json.loads(c.execute(SQL).fetchone()[0])


def _split(qualified: str) -> tuple[str, str]:
    """tbls names tables 'schema.table' for PostgreSQL and plain 'table' for databases without schemas."""
    s, _, n = qualified.rpartition(".")
    return (s or "main", n)


_TBLS_KIND = {"BASE TABLE": "table", "VIEW": "view", "MATERIALIZED VIEW": "materialized view", "FOREIGN TABLE": "foreign table"}


def from_tbls(d: dict) -> dict:
    tables = []
    for t in d.get("tables", []):
        s, n = _split(t["name"])
        cols = [{"name": c["name"], "type": c.get("type", ""), "notnull": not c.get("nullable", True),
                 **({"default": c["default"]} if c.get("default") not in (None, "") else {}),
                 **({"comment": c["comment"]} if c.get("comment") else {})} for c in t.get("columns", [])]
        cons, backed = [], set()
        for k in t.get("constraints") or []:
            kind = k.get("type", "").upper()
            if kind not in ("PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "CHECK"): continue   # tbls also lists triggers here
            entry = {"name": k["name"], "type": kind.lower(), "columns": k.get("columns") or [], "definition": k.get("def", "")}
            if kind == "FOREIGN KEY":
                rs, rn = _split(k.get("referenced_table", ""))
                entry["references"] = {"schema": rs, "table": rn, "columns": k.get("referenced_columns") or []}
                m = re.search(r"ON DELETE (NO ACTION|RESTRICT|CASCADE|SET NULL|SET DEFAULT)", k.get("def", ""), re.I)
                entry["on_delete"] = (m.group(1).lower() if m else "no action")
                m = re.search(r"ON UPDATE (NO ACTION|RESTRICT|CASCADE|SET NULL|SET DEFAULT)", k.get("def", ""), re.I)
                entry["on_update"] = (m.group(1).lower() if m else "no action")
            else:
                backed.add(k["name"])
            cons.append(entry)
        idxs = [{"name": i["name"], "unique": " UNIQUE " in f" {i.get('def', '')} ".upper(), "partial": " WHERE " in f" {i.get('def', '')} ".upper(),
                 "columns": i.get("columns") or [], "definition": i.get("def", "")}
                for i in t.get("indexes") or [] if i["name"] not in backed]
        entry = {"schema": s, "name": n, "kind": _TBLS_KIND.get(t.get("type", "").upper(), t.get("type", "table").lower()),
                 "columns": cols, "constraints": cons, "indexes": idxs}
        if t.get("comment"): entry["comment"] = t["comment"]
        tables.append(entry)
    drv = d.get("driver") or {}
    ver = re.search(r"\d+(\.\d+)*", drv.get("database_version", "") or "")
    return {"format": FORMAT, "database": d.get("name", ""), "server_version": f"{drv.get('name', '')} {ver.group(0) if ver else ''}".strip(),
            "source": "tbls", "tables": tables}


def load(text: str) -> dict:
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < a: raise ValueError("no JSON found in the input")
    d = json.loads(text[a:b + 1])
    if d.get("format") == FORMAT and isinstance(d.get("tables"), list): return d
    if isinstance(d.get("tables"), list) and (d.get("driver") or d.get("relations") is not None): return from_tbls(d)
    raise ValueError('not an erdview schema (expected "format": "pg-erd/1" or tbls JSON)')


def export(data: dict) -> str:
    assert VIEWER.count(DATA_TAG) == 1
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")   # never closes the script tag early
    return VIEWER.replace(DATA_TAG, DATA_TAG.replace("></", ">" + blob + "</"), 1)


def open(data: dict, path: str | Path | None = None) -> Path:
    name = "erd_" + re.sub(r"[^\w.-]+", "_", data.get("database") or "schema") + ".html"
    p = Path(path) if path else Path(tempfile.mkdtemp(prefix="erdview_")) / name
    p.write_text(export(data), encoding="utf-8")
    webbrowser.open(p.as_uri())
    return p


# ── diff: one implementation for `erdview diff`, the live viewer's colouring, MCP and tests ──────────────────────

def _key(t: dict) -> str: return f"{t['schema']}.{t['name']}"
_COL_FIELDS = ("type", "notnull", "default", "identity", "generated", "expression")


def diff_schema(a: dict | None, b: dict) -> dict:
    """What changed from schema a to schema b. a=None means everything in b is new.
    {"added"|"removed": {"tables": [k], "columns": [[k, col]], "fks": [[k, name]], "indexes": [[k, name]]},
     "changed": {"columns": [{"table": k, "column": col, "from": {...}, "to": {...}}]}}  — k = "schema.table"."""
    A = {_key(t): t for t in (a or {}).get("tables", [])}; B = {_key(t): t for t in b.get("tables", [])}
    out = {"added": {"tables": [], "columns": [], "fks": [], "indexes": []},
           "removed": {"tables": [], "columns": [], "fks": [], "indexes": []}, "changed": {"columns": []}}
    out["added"]["tables"] = sorted(k for k in B if k not in A)
    out["removed"]["tables"] = sorted(k for k in A if k not in B)
    fks = lambda t: [[_key(t), c["name"]] for c in t.get("constraints", []) if c["type"] == "foreign key"]   # a new table's lines are new too
    for k in out["added"]["tables"]: out["added"]["fks"] += fks(B[k])
    for k in out["removed"]["tables"]: out["removed"]["fks"] += fks(A[k])
    for k in sorted(set(A) & set(B)):
        ca = {c["name"]: c for c in A[k].get("columns", [])}; cb = {c["name"]: c for c in B[k].get("columns", [])}
        out["added"]["columns"] += [[k, n] for n in cb if n not in ca]
        out["removed"]["columns"] += [[k, n] for n in ca if n not in cb]
        for n in cb:
            if n in ca:
                fa = {f: ca[n].get(f) for f in _COL_FIELDS}; fb = {f: cb[n].get(f) for f in _COL_FIELDS}
                if fa != fb: out["changed"]["columns"].append({"table": k, "column": n, "from": fa, "to": fb})
        for kind, field, pick in (("fks", "constraints", lambda x: x["type"] == "foreign key"), ("indexes", "indexes", lambda x: True)):
            na = {x["name"]: x for x in A[k].get(field, []) if pick(x)}; nb = {x["name"]: x for x in B[k].get(field, []) if pick(x)}
            out["added"][kind] += [[k, n] for n in nb if n not in na or na[n].get("definition") != nb[n].get("definition")]
            out["removed"][kind] += [[k, n] for n in na if n not in nb or na[n].get("definition") != nb[n].get("definition")]
    return out


def is_empty_diff(d: dict) -> bool:
    return not any(v for part in d.values() for v in part.values())
