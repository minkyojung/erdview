"""erdview: pg_erd.sql reads a PostgreSQL schema into one JSON line and viewer.html draws it; core.py converts, exports, opens.
SQL tests build each fixture in a fresh empty database on the server at ERDVIEW_TEST_PG (an admin URL; default: the local
docker Postgres on 5433) and compare the JSON with information_schema and with expectations written by hand from fixtures/edge.sql.
Browser tests open the page in headless Chrome with the data baked in, as `erdview export` does; with `#selftest` the page writes
raw facts (boxes, lines, errors, wheel results, its saved copy) into <pre id="selftest-report"> and the asserts live here.
Each part skips when Postgres or Chrome is missing (CHROME=/path/to/chrome overrides the lookup)."""
import html, json, os, re, shutil, subprocess, sys, time
from pathlib import Path
import pytest
from erdview import core

ROOT = Path(__file__).resolve().parents[1]; PKG = ROOT / "erdview"; FIX = Path(__file__).parent / "fixtures"
SQL, PAGE = core.SQL, core.VIEWER
ADMIN = os.environ.get("ERDVIEW_TEST_PG", "postgresql://postgres:x@localhost:5433/postgres")

def _reachable() -> bool:
    try:
        import psycopg
        with psycopg.connect(ADMIN, connect_timeout=2): return True
    except Exception:
        return False

@pytest.fixture(scope="module")
def fresh():
    """make(label, *ddl) → URL of a new empty database with the DDL applied. All are dropped at module end."""
    if not _reachable(): pytest.skip(f"Postgres not reachable at ERDVIEW_TEST_PG={ADMIN}")
    import psycopg
    made = []
    def admin(): return psycopg.connect(ADMIN, autocommit=True)
    def make(label, *ddl):
        name = f"erdview_{label}_{int(time.time())}_{os.getpid()}"
        with admin() as a: a.execute(f'create database "{name}" template template0')
        made.append(name); url = re.sub(r"/[^/?]*(\?|$)", f"/{name}\\1", ADMIN, count=1)
        with psycopg.connect(url, autocommit=True) as c:
            for d in ddl: c.execute(d)
        return url
    yield make
    with admin() as a:
        for n in made:
            a.execute("select pg_terminate_backend(pid) from pg_stat_activity where datname=%s and pid<>pg_backend_pid()", [n])
            a.execute(f'drop database if exists "{n}"')

def run(url, sql=SQL) -> str:
    import psycopg
    with psycopg.connect(url) as c: return c.execute(sql).fetchone()[0]

def rows(url, sql):
    import psycopg
    with psycopg.connect(url) as c: return c.execute(sql).fetchall()

@pytest.fixture(scope="module")
def school(fresh): return fresh("school", (FIX / "school.sql").read_text())

@pytest.fixture(scope="module")
def edge(fresh):
    url = fresh("edge", (FIX / "edge.sql").read_text()); return url, json.loads(run(url))

def by_name(d): return {(t["schema"], t["name"]): t for t in d["tables"]}
def cons(t, kind): return {c["name"]: c for c in t["constraints"] if c["type"] == kind}

def test_sql_is_one_statement_with_no_client_commands():
    # GUI clients run it as one statement and the HTML embeds it inside <script>, so these must hold.
    body = "\n".join(l for l in SQL.splitlines() if not l.lstrip().startswith("--"))
    assert SQL.count(";") == 1 and SQL.rstrip().endswith(";")
    assert "</script" not in SQL.lower() and "<!--" not in SQL
    assert not any(l.startswith("\\") for l in body.splitlines())

def test_sql_output_is_one_json_line(school):
    out = run(school)
    assert "\n" not in out
    d = json.loads(out)
    assert d["format"] == "pg-erd/1" and d["server_version"] and d["database"].startswith("erdview_school_")

def test_sql_on_an_empty_database(fresh):
    assert json.loads(run(fresh("empty")))["tables"] == []

# ── cross-check against information_schema (an independent catalog) ─────────────────────────────────────────────

ORACLE_TABLES = """
  select table_schema, table_name from information_schema.tables t
  where table_schema not in ('pg_catalog', 'information_schema')
    and not exists (select 1 from pg_class c join pg_namespace n on n.oid = c.relnamespace
                    where n.nspname = t.table_schema and c.relname = t.table_name and c.relispartition)
  union all select schemaname, matviewname from pg_matviews"""
ORACLE_COLUMNS = """
  select table_schema, table_name, column_name, is_nullable = 'NO', column_default,
         lower(identity_generation), case when is_generated = 'ALWAYS' then 'stored' end, generation_expression
  from information_schema.columns where table_schema not in ('pg_catalog', 'information_schema')
  order by table_schema, table_name, ordinal_position"""
ORACLE_KEYS = """
  select tc.table_schema, tc.table_name, tc.constraint_type, tc.constraint_name, array_agg(k.column_name::text order by k.ordinal_position)
  from information_schema.table_constraints tc
  join information_schema.key_column_usage k on k.constraint_schema = tc.constraint_schema and k.constraint_name = tc.constraint_name
                                             and k.table_name = tc.table_name
  where tc.constraint_type in ('PRIMARY KEY', 'UNIQUE') and tc.table_schema not in ('pg_catalog', 'information_schema')
  group by 1, 2, 3, 4"""
ORACLE_FKS = """
  select k.table_schema, k.table_name, k.constraint_name, array_agg(k.column_name::text order by k.ordinal_position),
         r.table_schema, r.table_name, array_agg(r.column_name::text order by k.ordinal_position),
         lower(rc.delete_rule), lower(rc.update_rule)
  from information_schema.referential_constraints rc
  join information_schema.key_column_usage k on k.constraint_schema = rc.constraint_schema and k.constraint_name = rc.constraint_name
  join information_schema.key_column_usage r on r.constraint_schema = rc.unique_constraint_schema
                                            and r.constraint_name = rc.unique_constraint_name
                                            and r.ordinal_position = k.position_in_unique_constraint
  group by 1, 2, 3, 5, 6, 8, 9"""
DUPLICATE_CONSTRAINT_NAMES = """
  select constraint_schema, constraint_name from information_schema.table_constraints
  where constraint_schema not in ('pg_catalog', 'information_schema') and constraint_type <> 'CHECK' group by 1, 2 having count(*) > 1"""

@pytest.mark.parametrize("db", ["school"])
def test_sql_matches_information_schema(db, request):
    url = request.getfixturevalue(db); d = json.loads(run(url)); T = by_name(d)
    assert set(T) == set(rows(url, ORACLE_TABLES))
    got = [(t["schema"], t["name"], c["name"], c["notnull"], c.get("default"), c.get("identity"), c.get("generated"), c.get("expression"))
           for t in d["tables"] if t["kind"] != "materialized view" for c in t["columns"]]   # information_schema has no matviews
    want = [r for r in rows(url, ORACLE_COLUMNS) if (r[0], r[1]) in T]
    assert sorted(got, key=lambda r: r[:2]) == sorted(want, key=lambda r: r[:2])   # stable sort keeps column order
    keys = {(t["schema"], t["name"], c["type"].upper(), c["name"], tuple(c["columns"]))
            for t in d["tables"] for c in t["constraints"] if c["type"] in ("primary key", "unique")}
    assert keys == {(s, n, ty, cn, tuple(cols)) for s, n, ty, cn, cols in rows(url, ORACLE_KEYS)}
    # information_schema joins an FK to its target by constraint name, which Postgres only keeps unique per table
    assert rows(url, DUPLICATE_CONSTRAINT_NAMES) == [], "the information_schema oracle needs constraint names unique per schema"
    fks = {(t["schema"], t["name"], c["name"], tuple(c["columns"]), c["references"]["schema"], c["references"]["table"],
            tuple(c["references"]["columns"]), c["on_delete"], c["on_update"])
           for t in d["tables"] for c in t["constraints"] if c["type"] == "foreign key"}
    assert fks == {(a, b, c, tuple(e), f, g, tuple(h), i, j) for a, b, c, e, f, g, h, i, j in rows(url, ORACLE_FKS)}
    assert fks, "fixture has foreign keys"

# ── edge cases, expectations written by hand from edge.sql ───────────────────────────────────────────────────────

def test_sql_lists_relations_once_without_partitions(edge):
    _, d = edge; T = by_name(d)
    assert set(T) == {("edge", n) for n in ("accounts", "orders", "teams", "members", "audit_log", "events", "event_notes",
                                             "open_orders", "order_counts", 'weird "name"', "dup")} | {("edge2", "dup")}
    ev = T["edge", "events"]
    assert (ev["kind"], ev["partitions"], ev["partition_key"]) == ("partitioned table", 2, "RANGE (happened_on)")
    assert list(cons(T["edge", "event_notes"], "foreign key")) == ["event_notes_event_fkey"]   # not one per partition
    assert (T["edge", "open_orders"]["kind"], T["edge", "order_counts"]["kind"]) == ("view", "materialized view")
    assert sorted(u["table"] for u in T["edge", "open_orders"]["uses"]) == ["accounts", "orders"]
    assert T["edge", "order_counts"]["uses"] == [{"schema": "edge", "table": "orders"}]
    assert "uses" not in T["edge", "orders"]

def test_sql_columns(edge):
    T = by_name(edge[1]); acc = {c["name"]: c for c in T["edge", "accounts"]["columns"]}
    o = {c["name"]: c for c in T["edge", "orders"]["columns"]}
    assert list(o) == ["tenant_id", "order_id", "account_id", "parent_order_id", "qty", "price", "total", "status"]
    assert acc["account_id"]["identity"] == "always" and "default" not in acc["account_id"]
    assert o["order_id"]["serial"] is True and o["order_id"]["default"].startswith("nextval(")
    assert "serial" not in o["tenant_id"] and "identity" not in o["order_id"]
    assert (o["total"]["generated"], o["total"]["expression"], o["total"]["type"]) == ("stored", "((qty)::numeric * price)", "numeric(12,2)")
    assert "default" not in o["total"]
    assert (o["price"]["type"], o["price"]["default"], o["price"]["notnull"]) == ("numeric(10,2)", "0", True)
    assert o["parent_order_id"]["notnull"] is False
    log = {c["name"]: c for c in T["edge", "audit_log"]["columns"]}
    assert log["at"] == {"name": "at", "type": "timestamp with time zone", "notnull": True, "default": "now()"}
    assert T["edge", "accounts"]["comment"] == "[reference] 계정" and acc["name"]["comment"] == "이름 <b>굵게</b>"

def test_sql_constraints_keep_column_order(edge):
    T = by_name(edge[1]); acc, o = T["edge", "accounts"], T["edge", "orders"]
    assert cons(acc, "primary key")["accounts_pkey"]["columns"] == ["tenant_id", "account_id"]
    assert cons(acc, "unique")["accounts_code_key"]["columns"] == ["code", "tenant_id"]   # declared order, not table order
    fk = cons(o, "foreign key")
    assert fk["orders_account_fkey"]["columns"] == ["tenant_id", "account_id"]
    assert fk["orders_account_fkey"]["references"] == {"schema": "edge", "table": "accounts", "columns": ["tenant_id", "account_id"]}
    assert (fk["orders_account_fkey"]["on_delete"], fk["orders_account_fkey"]["on_update"]) == ("cascade", "no action")
    assert fk["orders_parent_fkey"]["references"] == {"schema": "edge", "table": "orders", "columns": ["tenant_id", "order_id"]}
    chk = cons(o, "check")
    assert chk["orders_qty_check"]["columns"] == ["qty"] and chk["orders_price_check"]["columns"] == ["price", "status"]
    assert chk["orders_qty_check"]["definition"] == "CHECK ((qty > 0))"
    ev = cons(T["edge", "events"], "foreign key")["events_account_fkey"]
    assert (ev["columns"], ev["references"]["columns"]) == (["account_tenant", "account_id"], ["tenant_id", "account_id"])
    assert cons(T["edge", "teams"], "foreign key")["teams_lead_fkey"]["references"]["table"] == "members"
    assert cons(T["edge", "members"], "foreign key")["members_team_fkey"]["references"]["table"] == "teams"
    assert T["edge", "audit_log"]["constraints"] == []
    assert cons(T["edge2", "dup"], "foreign key")["dup_edge_fkey"]["references"] == {"schema": "edge", "table": "dup", "columns": ["id"]}

def test_sql_indexes_skip_the_ones_behind_constraints(edge):
    T = by_name(edge[1])
    assert T["edge", "accounts"]["indexes"] == []   # pkey and the unique constraint's index are constraints already
    ix = {i["name"]: i for i in T["edge", "orders"]["indexes"]}
    assert set(ix) == {"orders_open_one", "orders_lower_status"}
    assert (ix["orders_open_one"]["unique"], ix["orders_open_one"]["partial"], ix["orders_open_one"]["columns"]) == (True, True, ["tenant_id", "account_id"])
    assert ix["orders_lower_status"]["columns"] == [None]   # an expression has no column name
    assert ix["orders_lower_status"]["definition"].endswith("(lower(status))")

def test_sql_returns_names_and_comments_byte_for_byte(edge):
    t = by_name(edge[1])["edge", 'weird "name"']
    assert [c["name"] for c in t["columns"]] == ["<img src=x onerror=alert(1)>", "id"]
    assert t["comment"] == '<img src=x onerror="document.body.dataset.pwned=1"> </script><script>document.body.dataset.pwned=2</script>'

def test_sql_schema_filter(edge):
    d = json.loads(run(edge[0], SQL.replace("'{}'::text[] as only_schemas", "'{edge2}'::text[] as only_schemas", 1)))
    assert [(t["schema"], t["name"]) for t in d["tables"]] == [("edge2", "dup")]

def test_sql_leaves_out_extension_objects(fresh):
    url = fresh("ext")
    have = {r[0] for r in rows(url, "select name from pg_available_extensions where name in ('postgres_fdw', 'pg_buffercache')")}
    if have != {"postgres_fdw", "pg_buffercache"}: pytest.skip("contrib extensions postgres_fdw and pg_buffercache not installed")
    import psycopg
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("""create extension pg_buffercache; create extension postgres_fdw;
                     create server remote foreign data wrapper postgres_fdw;
                     create foreign table remote_items (id int) server remote;
                     create table plain (id int); create table owned_by_ext (id int);
                     alter extension postgres_fdw add table owned_by_ext;""")
    T = by_name(json.loads(run(url)))
    assert set(T) == {("public", "plain"), ("public", "remote_items")}   # no pg_buffercache view, no extension member table
    assert T["public", "remote_items"]["kind"] == "foreign table"

@pytest.mark.skipif(shutil.which("psql") is None, reason="psql not installed")
def test_sql_through_psql(school):
    out = subprocess.run(["psql", school, "-XAtf", str(PKG / "pg_erd.sql")], capture_output=True, text=True, check=True).stdout
    assert out.count("\n") == 1 and json.loads(out)["format"] == "pg-erd/1"

# ── the page ─────────────────────────────────────────────────────────────────────────────────────────────────────

DATA_TAG = '<script type="application/json" id="erd-data"></script>'
CHROME = os.environ.get("CHROME") or next((p for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", shutil.which("google-chrome"),
                                                        shutil.which("chromium"), shutil.which("chromium-browser")) if p and Path(p).exists()), None)

def test_page_embeds_the_sql_unchanged():
    m = re.search(r'<script type="text/plain" id="erd-sql">(.*?)</script>', PAGE, re.S)
    assert m and m.group(1) == SQL, "copy erdview/pg_erd.sql into the #erd-sql block of viewer.html"
    assert PAGE.count(DATA_TAG) == 1   # ships empty: the page asks for data
    # every "</script" closes one of the three blocks; one inside a script or a comment would end that script early
    assert len(re.findall(r"<script\b", PAGE)) == 3 and PAGE.lower().count("</script") == 3 and "<!--" not in PAGE

bake = core.export   # what the page's "save with data" produces, done in Python

@pytest.fixture(scope="module")
def browse(tmp_path_factory):
    """browse(page_text) → (dumped DOM, self-test report) from headless Chrome at 1600×1000."""
    if CHROME is None: pytest.skip("Chrome not found (set CHROME=/path/to/chrome)")
    def go(text):
        d = tmp_path_factory.mktemp("erd"); f = d / "erd.html"; f.write_text(text)
        r = subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={d / 'profile'}", "--window-size=1600,1000",
                            "--virtual-time-budget=10000", "--dump-dom", f.as_uri() + "#selftest"], capture_output=True, text=True, timeout=180)
        m = re.search(r'<pre id="selftest-report"[^>]*>(.*?)</pre>', r.stdout, re.S)
        assert m, f"the page wrote no self-test report (a script error before it?)\n{r.stderr[-1500:]}"
        return r.stdout, json.loads(html.unescape(m.group(1)))
    return go

@pytest.fixture(scope="module")
def drawn(browse, request):
    """name → (data, dom, report) for school, edge (need Postgres) and xss_min (does not)."""
    cache = {}
    def get(name):
        if name not in cache:
            if name == "xss_min": data = json.loads((FIX / "xss_min.json").read_text())
            elif name == "edge": data = request.getfixturevalue("edge")[1]
            else: data = json.loads(run(request.getfixturevalue(name)))
            cache[name] = (data, *browse(bake(data)))
        return cache[name]
    return get

def sccs(nodes, arcs):
    """Strongly connected components (Tarjan) → node → component id. Lines inside one may legitimately run either way."""
    index, low, comp, stack, on, out, n = {}, {}, {}, [], set(), {v: [] for v in nodes}, [0]
    for a, b in arcs: out[a].append(b)
    def visit(v):
        index[v] = low[v] = n[0]; n[0] += 1; stack.append(v); on.add(v)
        for w in out[v]:
            if w not in index: visit(w); low[v] = min(low[v], low[w])
            elif w in on: low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            while True:
                w = stack.pop(); on.discard(w); comp[w] = v
                if w == v: break
    for v in nodes:
        if v not in index: visit(v)
    return comp

@pytest.mark.parametrize("name", ["school", "edge", "xss_min"])
def test_page_draws_every_table_and_foreign_key(name, drawn):
    data, dom, rep = drawn(name)
    assert rep["errors"] == []
    keys = [(t["schema"], t["name"]) for t in data["tables"]]
    assert [(t["schema"], t["name"]) for t in rep["tables"]] == keys and dom.count('class="tbl"') == len(keys)
    fks = sorted((t["schema"], t["name"], c["name"]) for t in data["tables"] for c in t["constraints"]
                 if c["type"] == "foreign key" and (c["references"]["schema"], c["references"]["table"]) in keys)
    assert sorted((*e["from"][:2], e["fk"]) for e in rep["edges"]) == fks and dom.count('class="edge"') == len(fks)

@pytest.mark.parametrize("name", ["school", "edge"])
def test_page_layout(name, drawn):
    data, _, rep = drawn(name)
    box = {(t["schema"], t["name"]): t for t in rep["tables"]}
    rects = list(box.values())
    for i, a in enumerate(rects):   # no two tables overlap
        for b in rects[i + 1:]:
            assert a["x"] + a["w"] <= b["x"] or b["x"] + b["w"] <= a["x"] or a["y"] + a["h"] <= b["y"] or b["y"] + b["h"] <= a["y"], (a["name"], b["name"])
    arcs = [(tuple(e["from"][:2]), tuple(e["to"][:2])) for e in rep["edges"]]
    comp = sccs(list(box), arcs)
    for s, t in arcs:   # the referenced table sits left of the referencing one, unless both are on a cycle
        if comp[s] != comp[t]: assert box[t]["x"] + box[t]["w"] <= box[s]["x"], (s, t)
    v = rep["viewport"]   # fitted: everything between the header and the legend
    assert min(t["screen"][0] for t in rects) >= 0 and max(t["screen"][2] for t in rects) <= v["w"]
    assert min(t["screen"][1] for t in rects) >= v["top"] and max(t["screen"][3] for t in rects) <= v["h"] - v["bottom"]
    segs = []   # vertical runs of different lines never share an x over the same heights
    for e in rep["edges"]:
        ax, ay, lx, by, bx = map(float, re.fullmatch(r"M([-\d.]+),([-\d.]+) H([-\d.]+) V([-\d.]+) H([-\d.]+)", e["d"]).groups())
        segs.append((lx, min(ay, by), max(ay, by), e["fk"]))
    for i, (x1, lo1, hi1, f1) in enumerate(segs):
        for x2, lo2, hi2, f2 in segs[i + 1:]:
            assert abs(x1 - x2) > .5 or hi1 <= lo2 or hi2 <= lo1, (f1, f2)

def test_page_wheel_pans_and_ctrl_wheel_zooms(drawn):
    w = drawn("school")[2]["wheel"]
    assert w["pan"]["scale"] == w["before"]["scale"] and w["pan"]["tx"] == w["before"]["tx"] and w["pan"]["ty"] == w["before"]["ty"] - 120
    assert w["zoom"]["scale"] > w["pan"]["scale"]

def test_page_anchors_a_composite_key_line_on_its_specific_column(drawn):
    e = {x["fk"]: x for x in drawn("edge")[2]["edges"]}
    assert (e["orders_account_fkey"]["from"][2], e["orders_account_fkey"]["to"][2]) == ("account_id", "account_id")   # not tenant_id
    assert (e["orders_parent_fkey"]["from"][2], e["orders_parent_fkey"]["to"][2]) == ("parent_order_id", "order_id")
    assert (e["events_account_fkey"]["from"][2], e["events_account_fkey"]["to"][2]) == ("account_id", "account_id")

@pytest.mark.parametrize("name", ["xss_min", "edge"])
def test_page_shows_markup_in_data_as_text(name, drawn):
    _, dom, rep = drawn(name)
    assert rep["errors"] == [] and "data-pwned" not in dom and "<title>pg_erd — PostgreSQL ERD</title>" in dom
    assert not re.search(r"<(img|svg onload|b>|i>)", dom.split('<pre id="selftest-report"')[0].split('<script type="application/json"')[0])
    assert len(re.findall(r"<script\b", dom)) == 3
    assert "&lt;img src=x onerror" in dom   # shown, as text

@pytest.mark.parametrize("name", ["xss_min", "edge"])
def test_page_saved_copy_reopens_the_same(name, drawn, browse):
    data, _, rep = drawn(name)
    saved = rep["saved"]
    m = re.search(r'<script type="application/json" id="erd-data">(.*?)</script>', saved, re.S)
    assert m and "<" not in m.group(1) and json.loads(m.group(1)) == data
    _, again = browse(saved)
    assert again["errors"] == [] and again["tables"] == rep["tables"] and again["edges"] == rep["edges"]
    assert json.loads(re.search(r'id="erd-data">(.*?)</script>', again["saved"], re.S).group(1)) == data   # save of a save

def test_page_without_data_takes_pasted_psql_output(browse):
    _, rep = browse(PAGE)
    assert rep["errors"] == []
    assert rep["bad_input"].startswith("JSON이 안 보여요")
    assert rep["pasted"] == {"tables": 1, "panel_hidden": True}

def test_page_click_tooltip_and_drag(drawn):
    data, _, rep = drawn("school")
    assert rep["click"] == {"selected": True, "lines": 1, "target": True}   # the first line only, both ends lit
    first = data["tables"][0]["columns"][0]
    assert rep["tip"]["shown"] and first["type"] in rep["tip"]["text"] and "primary key" in rep["tip"]["text"]
    assert abs(rep["drag"]["moved"] - rep["drag"]["expected"]) < 1e-6

# ── core: tbls input, load(), export(), cli ──────────────────────────────────────────────────────────────────────

def test_from_tbls_converts_the_sample_schema():
    """tbls' own PostgreSQL sample (sample/postgres/schema.json in github.com/k1LoW/tbls)."""
    d = core.from_tbls(json.loads((FIX / "tbls_postgres_sample.json").read_text()))
    assert d["format"] == "pg-erd/1" and d["source"] == "tbls" and d["server_version"].startswith("postgres 15")
    T = by_name(d)
    assert ("public", "users") in T and T["public", "users"]["kind"] == "table"
    assert {t["kind"] for t in d["tables"]} == {"table", "view", "materialized view"}
    fk = cons(T["public", "user_options"], "foreign key")["user_options_user_id_fk"]
    assert fk["columns"] == ["user_id"] and fk["references"] == {"schema": "public", "table": "users", "columns": ["id"]} and fk["on_delete"] == "cascade"
    u = {c["name"]: c for c in T["public", "users"]["columns"]}
    assert u["id"]["notnull"] is True and u["id"]["default"].startswith("nextval(")
    assert "users_pkey" not in {i["name"] for i in T["public", "users"]["indexes"]}   # backs the PK constraint
    assert all(k["type"] != "trigger" for t in d["tables"] for k in t["constraints"])

def test_load_accepts_erdview_tbls_and_psql_output():
    tbls = (FIX / "tbls_postgres_sample.json").read_text()
    assert core.load(tbls)["source"] == "tbls"
    own = json.loads((FIX / "xss_min.json").read_text())
    assert core.load(" erd_json\n----\n " + json.dumps(own) + "\n(1 row)\n") == own
    with pytest.raises(ValueError): core.load('{"tables": "nope"}')
    with pytest.raises(ValueError): core.load("not json")

def test_export_embeds_the_same_data_as_the_page_save_button(drawn):
    data, _, rep = drawn("xss_min")
    blob = lambda s: re.search(r'<script type="application/json" id="erd-data">(.*?)</script>', s, re.S).group(1)
    assert blob(core.export(data)) == blob(rep["saved"]) and "<" not in blob(core.export(data))

def test_cli_export_schema_sql(tmp_path):
    env = dict(os.environ, PYTHONPATH=str(ROOT))
    run_cli = lambda *a, **k: subprocess.run([sys.executable, "-m", "erdview.cli", *a], capture_output=True, text=True, **{"env": env, **k})
    assert run_cli("sql").stdout == SQL
    out = run_cli("schema", str(FIX / "tbls_postgres_sample.json"))
    assert out.returncode == 0 and json.loads(out.stdout)["source"] == "tbls"
    out = run_cli("export", "-", input=(FIX / "xss_min.json").read_text())
    assert out.returncode == 0 and '<script type="application/json" id="erd-data">{' in out.stdout
    f = tmp_path / "e.html"; assert run_cli("export", str(FIX / "xss_min.json"), "-o", str(f)).returncode == 0 and f.exists()
    assert run_cli("export", "/no/such/file").returncode != 0
    assert run_cli("export", env={**env, "DATABASE_URL": ""}).returncode != 0
