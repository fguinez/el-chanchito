# El Chanchito 🐷

Personal finance tracker for Chilean banks and fintechs: automated scrapers, a daily budget dashboard, and net-worth history.

## Overview

El Chanchito (“the piggy bank”) is a self-hosted personal finance tracker that replaces a hand-maintained Excel budgeting sheet. Python scrapers pull balances and transactions from Chilean financial institutions (Banco de Chile, Fintual, Buda, MACH, MercadoPago and Tenpo) into PostgreSQL, and a Next.js dashboard turns them into a daily budget plan, a spending history, and a net-worth timeline. Everything runs locally via the Makefile or Docker Compose, and credentials stay on your machine in the macOS Keychain.

## Highlights

- **Daily budget engine**: an expected balance for every day of the month, and the drift of your real balance against it (the Excel “Planificación” sheet, automated).
- **Six institution scrapers** on independent schedules, mixing REST APIs (Fintual, Buda), Playwright browser automation (Banco de Chile), and Gmail inbox parsing (MACH, MercadoPago, Tenpo).
- **Typed product registry**: every product kind (checking, credit card, crypto, ...) is declared once in pydantic and code-generated into TypeScript types, a JSON Schema, and per-kind docs.
- **Net worth from snapshots**: derived from per-product snapshot history, with per-kind asset/liability conventions and multi-currency conversion to CLP.
- **On-demand refresh**: an internal control endpoint lets the dashboard trigger a scrape immediately instead of waiting for the next scheduled run.

## Quickstart

```bash
make install           # Node deps + Python venv + Playwright Chromium
cp .env.example .env   # then fill in your identifiers (see Configuration)
make secrets-init      # store scraper secrets in the macOS Keychain
make db-up db-migrate  # start PostgreSQL (port 5435) + run migrations
make dev               # dashboard at http://localhost:3000
```

## Usage

The dashboard runs at `http://localhost:3000`: **Inicio** shows today’s expected balance and drift, **Planificación** the day-by-day month plan, **Historial** the net-worth timeline, and **Instituciones** every scraped product with an on-demand refresh button.

Scrapers run separately from the dashboard and share its database:

```bash
make scrapers-once    # run every configured scraper once
make scrapers-start   # long-running, each scraper on its own schedule
make fintual-login    # one-time Fintual sign-in (e-mail 2FA, session cached)
```

A scraper is enabled only when all of its credentials are present. See [USAGE.md](USAGE.md) for the full guide: dashboard pages, daily and monthly workflows, CSV import, category auto-assignment, and the API reference.

## Configuration

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | yes | PostgreSQL connection string; the `.env.example` default works with `make db-up` |
| `BANCHILE_RUT` | no | Banco de Chile RUT; enables the BanChile scraper (fintself + Playwright) |
| `FINTUAL_EMAIL` | no | Fintual account e-mail; enables the Fintual scraper (run `make fintual-login` once) |
| `EMAIL_IMAP_HOST` / `EMAIL_IMAP_USER` | no | Gmail IMAP identifiers; enable the e-mail parsers (MACH, MercadoPago, Tenpo) |
| `chanchito.*` | scrapers only | Secrets (bank passwords, Buda API keys, Gmail App Password) live in the macOS Keychain: `make secrets-init` / `make secrets-status`. On non-macOS hosts, export them as env vars instead |

## Development

```bash
make test        # all tests: vitest (web) + pytest (scrapers)
make typecheck   # tsc --noEmit
make lint        # eslint
make product-model-generate  # regen TS/JSON artifacts after editing the registry
make up          # full stack in Docker (postgres + web + scrapers)
```

## Project structure

```
el-chanchito/
├── apps/
│   ├── web/            # Next.js 16 dashboard (App Router, Drizzle ORM)
│   └── scrapers/       # Python 3.12 scraper service (APScheduler)
├── packages/
│   ├── db-schema/      # SQL migrations (V001–V012) + runner
│   └── product-model/  # Product-kind registry (pydantic → TS/JSON codegen)
├── docker-compose.yml  # postgres + web + scrapers
├── Makefile            # every common task (`make help`)
├── USAGE.md
└── ARCHITECTURE.md
```

## Architecture

A Next.js dashboard and a Python scraper service share one PostgreSQL database: scrapers upsert typed products, snapshots, and transactions, and the dashboard derives budgets and net worth from them. Product kinds are defined once in `packages/product-model` and code-generated into the TypeScript the web app consumes.

For full detail, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Testing

`make test` runs both suites: vitest for the dashboard and pytest for the scrapers. `make test-py` also covers `packages/product-model`, including a codegen drift test that fails if the generated TypeScript/JSON artifacts fall behind the registry.

## Further reading

- [USAGE.md: full user guide (workflows, scraper setup, API reference)](USAGE.md)
- [packages/product-model/PRODUCTS.md: generated per-kind product field matrix](packages/product-model/PRODUCTS.md)
