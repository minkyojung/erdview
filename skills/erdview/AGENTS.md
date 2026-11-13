# ERD (paste this section into your project's AGENTS.md)

To see or share the database as a diagram, use erdview (read-only, local, catalog only):

    uvx --from git+https://github.com/minkyojung/erdview erdview open <postgresql URL or tbls JSON or ->

Find the URL in `.env` / `DATABASE_URL` / `docker-compose.yml`; ask if unsure, never guess. `erdview export <src> -o erd.html` makes one shareable file; `erdview schema <src>` prints JSON you can read to answer questions about tables and foreign keys. Report the file path and table/view counts when done.
