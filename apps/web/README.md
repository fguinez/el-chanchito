# @el-chanchito/web

The Next.js dashboard for [El Chanchito](../../README.md): a daily budget engine, a spending history, and a net-worth timeline built on top of the scraped product data in PostgreSQL.

## Running

The dashboard is part of the full stack and is normally started from the repo root:

```bash
make dev        # postgres + migrations + scraper control endpoint + this dashboard
```

`make dev` wires the dashboard to the scraper service (via `SCRAPER_CONTROL_URL`) so the "Actualizar todo" button can trigger on-demand scrapes. Running this app alone shows no data and the refresh button fails, so prefer `make dev` unless you are doing web-only work:

```bash
make dev-web    # dashboard only (no scraper service)
```

Either way the dashboard serves at [http://localhost:3000](http://localhost:3000).

## Layout

- App Router pages live in `app/` (**Inicio**, **Planificación**, **Historial**, **Instituciones**).
- Data access uses Drizzle ORM against the shared PostgreSQL database.
- Product types consumed here are code-generated from `packages/product-model`; run `make product-model-generate` after editing the registry.

See the root [README.md](../../README.md) and [ARCHITECTURE.md](../../ARCHITECTURE.md) for the full picture.
