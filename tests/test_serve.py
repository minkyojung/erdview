"""`erdview serve`: core.diff_schema, the sources, and the local HTTP API. The server runs in a thread on a free port;
GET is the only clock (no timers), so every assertion is deterministic. Postgres-backed parts reuse `fresh` from test_erdview."""
import json, threading, time, urllib.request
from pathlib import Path
import pytest
from erdview import core, serve as S
from test_erdview import fresh, ADMIN, FIX, by_name   # noqa: F401  (fixture re-export)


def T(name, cols=("id",), schema="public", **extra):
    return {"schema": schema, "name": name, "kind": "table", "columns": [{"name": c, "type": "integer", "notnull": True} for c in cols],
            "constraints": extra.get("constraints", []), "indexes": extra.get("indexes", [])}
def D(*tables): return {"format": "pg-erd/1", "tables": list(tables)}


def test_diff_schema_added_removed_changed():
    a = D(T("a", ("id", "x")), T("b"), T("c", constraints=[{"name": "c_fk", "type": "foreign key", "columns": ["id"], "definition": "FK1"}],
                                         indexes=[{"name": "c_idx", "definition": "I1"}]))
    b = D(T("a", ("id", "y")), T("d"), T("c", constraints=[{"name": "c_fk", "type": "foreign key", "columns": ["id"], "definition": "FK2"}],
                                         indexes=[{"name": "c_idx", "definition": "I1"}, {"name": "c_idx2", "definition": "I2"}]))
    b["tables"][0]["columns"][0]["type"] = "bigint"
    d = core.diff_schema(a, b)
    assert d["added"] == {"tables": ["public.d"], "columns": [["public.a", "y"]], "fks": [["public.c", "c_fk"]], "indexes": [["public.c", "c_idx2"]]}
    assert d["removed"] == {"tables": ["public.b"], "columns": [["public.a", "x"]], "fks": [["public.c", "c_fk"]], "indexes": []}
    assert d["changed"]["columns"] == [{"table": "public.a", "column": "id", "from": {"type": "integer", "notnull": True, "default": None, "identity": None, "generated": None, "expression": None},
                                                                        "to": {"type": "bigint", "notnull": True, "default": None, "identity": None, "generated": None, "expression": None}}]
    assert not core.is_empty_diff(d) and core.is_empty_diff(core.diff_schema(a, a))
    assert core.diff_schema(None, b)["added"]["tables"] == ["public.a", "public.c", "public.d"]


@pytest.fixture
def server():
    """start(sources) → base URL; stopped at test end."""
    made = []
    def start(sources, min_interval=0.0):
        srv = S.Server(sources); srv.refresh.__func__   # noqa: B018
        orig = srv.refresh; srv.refresh = lambda src, v, mi=min_interval: orig(src, v, mi)   # tests must not wait 1.5 s between polls
        threading.Thread(target=srv.serve_forever, daemon=True).start(); made.append(srv); return srv.url
    yield start
    for s in made: s.shutdown(); s.server_close()

def get(url, code=200):
    try:
        with urllib.request.urlopen(url, timeout=10) as r: assert r.status == code; return json.loads(r.read())
    except urllib.error.HTTPError as e:
        assert e.code == code; return json.loads(e.read())


def test_api_sources_page_and_security(server, tmp_path):
    f = tmp_path / "s.json"; f.write_text((FIX / "xss_min.json").read_text())
    base = server([S.FileSource("dump", f)])
    assert get(base + "sources") == {"sources": [{"name": "dump", "kind": "file", "versions": None}]}
    page = urllib.request.urlopen(base, timeout=10).read().decode()
    assert '"format":"erdview-serve/1"' in page and "s.json" not in page and str(tmp_path) not in page
    assert get(base + "data?source=nope", 400)["error"] == "unknown source" and get(base + "nope", 404)
    req = urllib.request.Request(base + "sources", headers={"Host": "evil.example"})
    with pytest.raises(urllib.error.HTTPError) as e: urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 403


def test_api_file_source_seq_and_diff(server, tmp_path):
    f = tmp_path / "s.json"; f.write_text(json.dumps(D(T("a"))))
    base = server([S.FileSource("dump", f)])
    j = get(base + "data?source=dump&seq=0")
    assert j["seq"] == 1 and j["source"] == "dump" and j["error"] is None and j["diff"]["added"]["tables"] == ["public.a"]
    assert get(base + "data?source=dump&seq=1") == {"seq": 1, "unchanged": True}
    time.sleep(0.02); f.write_text(json.dumps(D(T("a"), T("b"))))   # mtime changes → re-read
    j = get(base + "data?source=dump&seq=1")
    assert j["seq"] == 2 and j["diff"]["added"]["tables"] == ["public.b"] and j["diff"]["removed"]["tables"] == []
    assert get(base + "data?source=dump&seq=0")["diff"]["added"]["tables"] == ["public.a", "public.b"]   # seq 0 = everything is new
    assert get(base + "data?source=dump&seq=99")["diff"] is None   # unknown seq: data yes, colouring no
    time.sleep(0.02); f.write_text("not json")
    j = get(base + "data?source=dump&seq=2")
    assert j["seq"] == 3 and j["error"] and [t["name"] for t in j["schema"]["tables"]] == ["a", "b"]   # last good schema kept


def test_api_db_source_sees_alter_table(server, fresh):
    import psycopg
    url = fresh("live", "create table t1 (id int primary key)")
    base = server([S.DbSource("live", url)])
    assert get(base + "data?source=live&seq=0")["seq"] == 1
    with psycopg.connect(url, autocommit=True) as c: c.execute("create table t2 (id int primary key, t1_id int references t1); alter table t1 add col text")
    j = get(base + "data?source=live&seq=1")
    assert j["seq"] == 2 and j["diff"]["added"]["tables"] == ["public.t2"] and j["diff"]["added"]["columns"] == [["public.t1", "col"]]
    assert j["diff"]["added"]["fks"] == [["public.t2", "t2_t1_id_fkey"]]
    assert get(base + "data?source=live&seq=2")["unchanged"] is True
    assert "test_serve" not in urllib.request.urlopen(base, timeout=10).read().decode() and url not in urllib.request.urlopen(base, timeout=10).read().decode()


@pytest.fixture
def migrations(tmp_path):
    d = tmp_path / "mig"; d.mkdir()
    (d / "V0001__init.sql").write_text("create schema app; set search_path to app; create table a (id int primary key);")
    (d / "V0002__b.sql").write_text("create table b (id int primary key, a_id int references a);")   # relies on search_path from V0001
    (d / "R__view.sql").write_text("create view v as select 1;")   # repeatable: skipped
    return d


def test_migration_source_versions_cache_and_errors(fresh, migrations):
    pg_admin = ADMIN
    src = S.MigrationSource("spring", migrations, pg_admin)
    assert src.versions() == ["V0001", "V0002"] and src.skipped() == ["R__view.sql"]
    s1, s2 = src.schema("V0001"), src.schema("V0002")
    assert [t["name"] for t in s1["tables"]] == ["a"] and [t["name"] for t in s2["tables"]] == ["a", "b"] and s2["tables"][0]["schema"] == "app"
    assert src.applies == 2; src.schema("V0002"); assert src.applies == 2   # cached
    (migrations / "V0002__b.sql").write_text("create table b (id int primary key); create table c (id int);")
    assert [t["name"] for t in src.schema("V0002")["tables"]] == ["a", "b", "c"] and src.applies == 3   # file changed → re-applied
    (migrations / "V0003__bad.sql").write_text("create table oops (id int, ;")
    with pytest.raises(S.MigrationError, match=r"V0003__bad\.sql: syntax error"): src.schema("V0003")
    (migrations / "V0003__bad.sql").write_text("\\i other.sql\nselect 1;")
    with pytest.raises(S.MigrationError, match="psql meta-commands"): src.schema("V0003")
    with pytest.raises(S.MigrationError, match="unknown version"): src.schema("V9")
    import psycopg
    with psycopg.connect(pg_admin) as a: assert a.execute("select count(*) from pg_database where datname like 'erdview\\_tmp\\_%'").fetchone()[0] == 0
    with pytest.raises(ValueError, match="--pg"): S.MigrationSource("x", migrations, None)


def test_api_migration_source_versions_and_error_to_page(server, fresh, migrations):
    base = server([S.MigrationSource("spring", migrations, ADMIN)])
    assert get(base + "sources")["sources"][0] == {"name": "spring", "kind": "migration", "versions": ["V0001", "V0002"], "skipped": ["R__view.sql"]}
    assert [t["name"] for t in get(base + "data?source=spring&version=V0001&seq=0")["schema"]["tables"]] == ["a"]
    j = get(base + "data?source=spring&version=V0002&seq=0")
    assert [t["name"] for t in j["schema"]["tables"]] == ["a", "b"] and j["version"] == "V0002"
    assert get(base + "data?source=spring&version=V7&seq=0", 400)["error"] == "unknown version"
    (migrations / "V0002__b.sql").write_text("create table b (id int primary key); create table c (id int)")
    j = get(base + "data?source=spring&version=V0002&seq=1")
    assert j["seq"] == 2 and j["diff"]["added"]["tables"] == ["app.c"]
    (migrations / "V0002__b.sql").write_text("create table b (id int primary key, ;")
    j = get(base + "data?source=spring&version=V0002&seq=2")
    assert j["seq"] == 3 and "V0002__b.sql" in j["error"] and [t["name"] for t in j["schema"]["tables"]] == ["a", "b", "c"]


def test_parse_source(tmp_path, migrations):
    f = tmp_path / "x.json"; f.write_text("{}")
    assert isinstance(S.parse_source("postgresql://u@h/db", None), S.DbSource) and S.parse_source("postgresql://u@h/db", None).name == "db"
    assert S.parse_source("toy=postgresql://u@h/db", None).name == "toy"
    assert isinstance(S.parse_source(f"dump={f}", None), S.FileSource) and S.parse_source(str(f), None).name == "x"
    assert isinstance(S.parse_source(f"spring={migrations}", ADMIN), S.MigrationSource) and S.parse_source(str(migrations), ADMIN).name == "mig"
    with pytest.raises(ValueError): S.parse_source("/no/such/thing", None)


# ── the page in live mode (headless Chrome against the running server) ─────────────────────────────────────────

from test_erdview import CHROME  # noqa: E402
import html as _html, re as _re, subprocess as _sp  # noqa: E402

def chrome_report(url, tmp):
    if CHROME is None: pytest.skip("Chrome not found")
    r = _sp.run([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={tmp / 'profile'}", "--window-size=1600,1000",
                 "--virtual-time-budget=15000", "--dump-dom", url], capture_output=True, text=True, timeout=180)
    m = _re.search(r'<pre id="selftest-report"[^>]*>(.*?)</pre>', r.stdout, _re.S)
    assert m, f"no self-test report\n{r.stdout[-800:]}\n{r.stderr[-800:]}"
    return r.stdout, json.loads(_html.unescape(m.group(1)))


def test_page_live_dropdown_positions_and_colours(server, fresh, tmp_path, migrations):
    import psycopg
    url = fresh("page", "create table t1 (id int primary key, a int); create table t2 (id int primary key)")
    base = server([S.DbSource("live", url), S.MigrationSource("spring", migrations, ADMIN)])
    dom, rep = chrome_report(base + "#selftest", tmp_path / "a")
    assert rep["errors"] == [] and rep["live"]["source"] == "live" and rep["live"]["seq"] == 1
    assert rep["live"]["options"] == ["live", "spring @ V0001", "spring @ V0002"] and rep["live"]["upd"].startswith("updated ")
    assert sorted(t["key"] for t in rep["tables"]) == ["public.t1", "public.t2"] and url not in dom
    before = {t["key"]: (t["x"], t["y"]) for t in rep["tables"]}
    # change the database, then open the page again asking for the diff since seq 1
    with psycopg.connect(url, autocommit=True) as c:
        c.execute("create table t3 (id int primary key, t1_id int references t1); alter table t1 add b text; alter table t1 alter a type bigint; alter table t2 drop id; drop table if exists nothing")
    get(base + "data?source=live&seq=1")   # server re-reads → seq 2
    _, rep2 = chrome_report(base + "#selftest&since=1", tmp_path / "b")
    assert rep2["errors"] == [] and rep2["live"]["seq"] == 2
    assert rep2["highlighted"] == {"added_tables": 1, "added_cols": 1, "changed_cols": 1, "removed_tables": 0, "removed_cols": 1, "added_edges": 1}
    after = {t["key"]: (t["x"], t["y"]) for t in rep2["tables"]}
    assert after["public.t1"] == before["public.t1"] and after["public.t2"] == before["public.t2"]   # kept their places
    assert after["public.t3"][1] > max(y for _, y in before.values())                             # new one placed below
    # another source and version through the hash, plus focus
    _, rep3 = chrome_report(base + "#selftest&source=spring&version=V0002&focus=b", tmp_path / "c")
    assert rep3["live"]["version"] == "V0002" and {t["key"] for t in rep3["tables"] if not t["hidden"]} == {"app.a", "app.b"}
    _, rep4 = chrome_report(base + "#selftest&source=spring&version=V0001", tmp_path / "d")
    assert [t["key"] for t in rep4["tables"]] == ["app.a"]


def test_page_live_shows_source_error(server, tmp_path, migrations):
    (migrations / "V0002__b.sql").write_text("create table b (id int primary key, ;")
    base = server([S.MigrationSource("spring", migrations, ADMIN)])
    _, rep = chrome_report(base + "#selftest&source=spring&version=V0002", tmp_path / "e")
    assert rep["errors"] == [] and rep["live"]["err"] and "V0002__b.sql" in rep["live"]["upd"]
    _, rep = chrome_report(base + "#selftest&source=spring&version=V0001", tmp_path / "f")
    assert not rep["live"]["err"] and [t["key"] for t in rep["tables"]] == ["app.a"]


# ── step 5: diff as text (CLI), /diff endpoint, compare mode in the page ─────────────────────────────────────────

def test_format_diff_lines():
    a = D(T("a", ("id", "x")), T("b")); b = D(T("a", ("id", "y")), T("c")); b["tables"][0]["columns"][0]["notnull"] = False
    txt = core.format_diff(core.diff_schema(a, b), "before", "after")
    assert txt.splitlines() == ["before → after", "+ table   public.c", "- table   public.b", "+ column  public.a.y", "- column  public.a.x",
                                "~ column  public.a.id  notnull: True → False"]
    assert core.format_diff(core.diff_schema(a, a)) == "no changes (a → b)"


def test_cli_diff(tmp_path, migrations, fresh):
    import subprocess, sys, os
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    run = lambda *a: subprocess.run([sys.executable, "-m", "erdview.cli", "diff", *a], capture_output=True, text=True, env=env)
    fa, fb = tmp_path / "a.json", tmp_path / "b.json"
    fa.write_text(json.dumps(D(T("a")))); fb.write_text(json.dumps(D(T("a"), T("b"))))
    r = run(str(fa), str(fb)); assert r.returncode == 1 and r.stdout.splitlines()[1] == "+ table   public.b"
    r = run(str(fa), str(fa)); assert r.returncode == 0 and r.stdout.startswith("no changes")
    r = run(str(fa), str(fb), "--json"); assert json.loads(r.stdout)["added"]["tables"] == ["public.b"]
    r = run(f"{migrations}@V0001", f"{migrations}@V0002", "--pg", ADMIN)
    assert r.returncode == 1 and "+ table   app.b" in r.stdout and "+ fk      app.b.b_a_id_fkey" in r.stdout
    r = run(f"{migrations}@V0001", f"{migrations}@V0009", "--pg", ADMIN); assert r.returncode != 0 and "unknown version" in r.stderr
    r = run(f"{migrations}@V0001", str(fa)); assert r.returncode != 0 and "--pg" in r.stderr


def test_api_diff_endpoint(server, migrations, fresh):
    base = server([S.MigrationSource("spring", migrations, ADMIN)])
    j = get(base + "diff?a=spring&av=V0001&b=spring&bv=V0002")
    assert j["diff"]["added"]["tables"] == ["app.b"] and j["text"].startswith("spring@V0001 → spring@V0002") and [t["name"] for t in j["schema_a"]["tables"]] == ["a"]
    assert get(base + "diff?a=spring&av=V0001&b=nope", 400)["error"] == "unknown source"
    assert get(base + "diff?a=spring&av=V0001&b=spring&bv=V0009", 400)["error"] == "unknown version"


def test_page_compare_mode_keeps_colours_and_shows_text(server, tmp_path, migrations, fresh):
    (migrations / "V0003__c.sql").write_text("alter table a add name text; drop table b; create table c (id int primary key, a_id int references a);")
    base = server([S.MigrationSource("spring", migrations, ADMIN)])
    _, rep = chrome_report(base + "#selftest&source=spring&version=V0003&compare=spring@V0002", tmp_path / "g")
    assert rep["errors"] == [] and rep["compare"]["with"] == ["spring", "V0002"] and rep["compare"]["panel"]
    assert rep["compare"]["options"][:2] == ["비교 안 함", "vs spring @ V0001"]
    assert sorted(t["key"] for t in rep["tables"]) == ["app.a", "app.b", "app.c"]   # b is drawn too, as removed
    assert rep["highlighted"] == {"added_tables": 1, "added_cols": 1, "changed_cols": 0, "removed_tables": 1, "removed_cols": 0, "added_edges": 1}
    assert rep["removed_edges"] == 1 and "+ table   app.c" in rep["compare"]["text"] and "- table   app.b" in rep["compare"]["text"]
    assert rep["live"]["upd"].startswith("vs spring@V0002: +1 −1 tables · +1 −0 ~0 columns")
    _, rep2 = chrome_report(base + "#selftest&source=spring&version=V0003", tmp_path / "h")   # no compare: no colours, no panel
    assert rep2["compare"] is None and rep2["highlighted"]["added_tables"] == 0 and [t["key"] for t in rep2["tables"]] == ["app.a", "app.c"]
