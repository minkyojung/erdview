---
name: erdview
description: Show, export, or inspect the project database as an interactive ERD with erdview. Use when asked to see/draw/share the ERD or schema diagram, or to check what the database looks like after a migration.
---

erdview draws an interactive ERD of a database. It is read-only and reads only the catalog (never row data). Everything runs locally.

## Run it

```
uvx --from git+https://github.com/minkyojung/erdview erdview open <SOURCE>
```

`<SOURCE>` is one of:
- a PostgreSQL URL (`postgresql://user:pass@host:5432/db`) — find it in `.env`, `DATABASE_URL`, `docker-compose.yml`, or the app config. **If you cannot find it, ask; do not guess.**
- a tbls JSON for other databases: `tbls out -t json | uvx --from git+https://github.com/minkyojung/erdview erdview open -`
- nothing, when `DATABASE_URL` is set in the environment.

Other commands: `export <SOURCE> -o erd.html` (one file to share), `schema <SOURCE>` (JSON you can read to answer questions about tables, columns and foreign keys), `sql` (the query), `serve --source NAME=SPEC ... [--pg ADMIN_URL]` (live viewer: changes you make to the database show up within 2 s; a migration folder gives one entry per version — use this while writing migrations).

## Rules
- Never run the query through a writable path you built yourself; use the `erdview` command, which opens a read-only connection.
- If the database runs in Docker and `psql`/the port is not reachable from the host, prefer the published port from `docker-compose.yml`; `docker exec` is a fallback.
- Report back briefly: the file path, number of tables and views, and which database you connected to.
- Do not send the schema to any external service.
