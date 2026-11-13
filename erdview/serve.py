"""`erdview serve`: a local server (127.0.0.1 only, stdlib) that feeds the viewer from several named sources and tells it what changed.

Sources: a live PostgreSQL URL, a JSON file, or a folder of ordered migration files (Flyway V<ver>__x.sql or NNNN_x.sql);
a migration source has one version per file and draws version N by applying files 1..N to a throwaway database.
The page polls GET /data every 2 s with the seq it holds; the server re-reads the source at most every 1.5 s and answers
{"unchanged": true} or the new schema plus a diff against the snapshot that seq refers to. DB URLs and paths never reach the page.
"""
from __future__ import annotations
import hashlib, json, os, re, threading, time, uuid, webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs
from . import core


class MigrationError(Exception): pass


class Source:
    kind = ""
    def __init__(self, name: str): self.name = name
    def versions(self) -> list[str] | None: return None
    def fingerprint(self, version: str | None) -> str | None: return None   # None = cannot tell cheaply, re-read and compare
    def schema(self, version: str | None) -> dict: raise NotImplementedError
    def describe(self) -> dict: return {"name": self.name, "kind": self.kind, "versions": self.versions()}


class DbSource(Source):
    kind = "db"
    def __init__(self, name, url): super().__init__(name); self._url = url
    def schema(self, version=None): return core.schema(self._url)


class FileSource(Source):
    kind = "file"
    def __init__(self, name, path): super().__init__(name); self._path = Path(path)
    def fingerprint(self, version=None): s = self._path.stat(); return f"{s.st_mtime_ns}:{s.st_size}"
    def schema(self, version=None): return core.load(self._path.read_text(encoding="utf-8"))


_FLYWAY = re.compile(r"^V(\d+(?:[._]\d+)*)__.*\.sql$"); _PLAIN = re.compile(r"^(\d+)[_-].*\.sql$")


def _apply_migrations(admin_url: str, files: list[Path]) -> dict:
    """Apply files in order to a fresh database and return its schema. The database is dropped afterwards, whatever happens."""
    import psycopg
    for f in files:
        bad = next((l for l in f.read_text(encoding="utf-8").splitlines() if l.startswith("\\")), None)
        if bad: raise MigrationError(f"{f.name}: psql meta-commands are not supported ({bad.strip()[:40]})")
    name = f"erdview_tmp_{os.getpid()}_{uuid.uuid4().hex[:8]}"
    url = re.sub(r"/[^/?]*(\?|$)", f"/{name}\\1", admin_url, count=1)
    with psycopg.connect(admin_url, autocommit=True) as a: a.execute(f'create database "{name}" template template0')
    try:
        with psycopg.connect(url, autocommit=True) as c:   # one session, so `set search_path` in a file carries over like in Flyway
            for f in files:
                try: c.execute("begin"); c.execute(f.read_text(encoding="utf-8")); c.execute("commit")
                except psycopg.Error as e:
                    c.execute("rollback"); raise MigrationError(f"{f.name}: {getattr(e.diag, 'message_primary', None) or e}") from None
        return core.schema(url)
    finally:
        with psycopg.connect(admin_url, autocommit=True) as a: a.execute(f'drop database if exists "{name}" with (force)')


def drop_stale_tmp(admin_url: str) -> list[str]:
    import psycopg
    gone = []
    with psycopg.connect(admin_url, autocommit=True) as a:
        for (n,) in a.execute("select datname from pg_database where datname like 'erdview\\_tmp\\_%'").fetchall():
            a.execute(f'drop database if exists "{n}" with (force)'); gone.append(n)
    return gone


class MigrationSource(Source):
    kind = "migration"
    def __init__(self, name, folder, admin_url):
        super().__init__(name); self._dir = Path(folder); self._admin = admin_url; self._cache = {}; self.applies = 0
        if not admin_url: raise ValueError(f"source {name}: a migration folder needs --pg <admin postgres url> (or ERDVIEW_PG)")
    def _files(self) -> list[tuple[tuple[int, ...], str, Path]]:
        out = []
        for p in self._dir.iterdir():
            m = _FLYWAY.match(p.name) or _PLAIN.match(p.name)
            if m: out.append((tuple(int(x) for x in re.split(r"[._]", m.group(1))), "V" + m.group(1), p))
        return sorted(out)
    def skipped(self) -> list[str]: return sorted(p.name for p in self._dir.glob("*.sql") if not (_FLYWAY.match(p.name) or _PLAIN.match(p.name)))
    def versions(self): return [label for _, label, _ in self._files()]
    def _upto(self, version):
        files = [p for _, label, p in self._files()]; labels = self.versions()
        if version is None: return files
        if version not in labels: raise MigrationError(f"unknown version {version}")
        return files[: labels.index(version) + 1]
    def fingerprint(self, version=None):
        h = hashlib.sha256()
        for p in self._upto(version): h.update(p.name.encode()); h.update(p.read_bytes())
        return h.hexdigest()
    def schema(self, version=None):
        fp = self.fingerprint(version)
        if fp not in self._cache:
            sch = _apply_migrations(self._admin, self._upto(version)); sch["database"] = f"{self.name} @ {version or self.versions()[-1]}"   # not the throwaway db's name
            self._cache[fp] = sch; self.applies += 1
        return self._cache[fp]
    def describe(self): return {**super().describe(), "skipped": self.skipped()}


def parse_source(spec: str, admin_url: str | None) -> Source:
    name, _, s = spec.partition("=") if "=" in spec and "://" not in spec.split("=", 1)[0] else ("", "", spec)
    s = s or spec
    if "://" in s: return DbSource(name or urlparse(s).path.strip("/") or "db", s)
    p = Path(s)
    if p.is_dir(): return MigrationSource(name or p.name, p, admin_url)
    if p.is_file(): return FileSource(name or p.stem, p)
    raise ValueError(f"not a URL, file or folder: {s}")


class State:
    def __init__(self): self.lock = threading.Lock(); self.seq = 0; self.snapshots = deque(maxlen=8); self.fp = None; self.at = 0.0; self.error = None; self.schema = None
    def snapshot(self, seq): return next((s for q, s in self.snapshots if q == seq), None)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self, sources: list[Source], port=0):
        super().__init__(("127.0.0.1", port), Handler)
        self.sources = {s.name: s for s in sources}; self.states: dict[tuple, State] = {}
        self.page = core.export({"format": "erdview-serve/1", "sources": [s.describe() for s in sources]})
    @property
    def url(self): return f"http://127.0.0.1:{self.server_address[1]}/"

    def refresh(self, src: Source, version: str | None, min_interval=1.5) -> State:
        st = self.states.setdefault((src.name, version), State())
        with st.lock:
            now = time.time()
            if st.seq and now - st.at < min_interval: return st
            st.at = now
            try:
                fp = src.fingerprint(version)
                if st.seq and fp is not None and fp == st.fp and st.error is None: return st
                sch = src.schema(version)
                changed = st.schema is None or not core.is_empty_diff(core.diff_schema(st.schema, sch)) or st.error is not None
                st.fp, st.error = fp, None
                if changed: st.seq += 1; st.schema = sch; st.snapshots.append((st.seq, sch))
            except Exception as e:   # report to the page, keep the last good schema
                msg = str(e)
                if msg != st.error: st.error = msg; st.seq += 1
            return st


class Handler(BaseHTTPRequestHandler):
    server: Server
    def log_message(self, *a): pass
    def _send(self, code, body: bytes, ctype):
        self.send_response(code); self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def _json(self, obj, code=200): self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")
    def do_GET(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"): return self._json({"error": "local only"}, 403)
        u = urlparse(self.path); q = parse_qs(u.query)
        if u.path == "/": return self._send(200, self.server.page.encode(), "text/html; charset=utf-8")
        if u.path == "/sources": return self._json({"sources": [s.describe() for s in self.server.sources.values()]})
        if u.path != "/data": return self._json({"error": "not found"}, 404)
        src = self.server.sources.get(q.get("source", [""])[0])
        if not src: return self._json({"error": "unknown source"}, 400)
        version = q.get("version", [None])[0] or None
        if version and version not in (src.versions() or []): return self._json({"error": "unknown version"}, 400)
        try: seq = int(q.get("seq", ["0"])[0])
        except ValueError: seq = 0
        st = self.server.refresh(src, version)
        at = q.get("at", [None])[0]   # a past snapshot as it was (tests and "show me what seq N looked like")
        if at is not None:
            snap = st.snapshot(int(at)) if at.isdigit() else None
            if snap is None: return self._json({"error": "unknown snapshot"}, 400)
            return self._json({"seq": int(at), "source": src.name, "version": version, "updated": time.strftime("%H:%M:%S"), "schema": snap, "diff": core.diff_schema(None, snap), "error": None})
        if seq == st.seq: return self._json({"seq": st.seq, "unchanged": True})
        base = st.snapshot(seq) if seq else None
        self._json({"seq": st.seq, "source": src.name, "version": version, "updated": time.strftime("%H:%M:%S"), "schema": st.schema,
                    "diff": core.diff_schema(base, st.schema) if st.schema is not None and (base is not None or seq == 0) else None, "error": st.error})


def serve(sources: list[Source], port=0, open_browser=True) -> None:
    for s in sources:
        if isinstance(s, MigrationSource): drop_stale_tmp(s._admin); break
    srv = Server(sources, port)
    print(srv.url + "   sources: " + ", ".join(s.name for s in sources), flush=True)
    if open_browser: webbrowser.open(srv.url)
    try: srv.serve_forever()
    except KeyboardInterrupt: pass
    finally: srv.server_close()
