# El Chanchito - Usage Guide

## Prerequisites

- **Node.js** >= 20 + **pnpm** >= 9
- **Python** >= 3.11
- **Docker** + **Docker Compose**
- **Playwright** browsers (for BanChile scraper): `playwright install chromium`

## Quick Start

```bash
# 1. Clone and install
pnpm install

# 2. Set up environment
cp .env.example .env
# Edit .env with your credentials (see "Configuration" below)

# 3. Start PostgreSQL
make db-up

# 4. Run database migrations
make db-migrate

# 5. Start the dashboard
make dev

# Open http://localhost:3000
```

## Configuration

Configuration is split in two:

- **`.env`** — non-secret config: Postgres settings and scraper identifiers.
- **macOS Keychain** — secrets (passwords, tokens, API keys). Loaded automatically
  by `make scrapers-once`, `make scrapers-start`, and `make up` via
  `scripts/load-secrets.sh`.

Copy `.env.example` to `.env` and fill in the identifiers:

```bash
# Required - PostgreSQL (defaults work out of the box)
POSTGRES_USER=finance
POSTGRES_PASSWORD=finance
POSTGRES_DB=finance
DATABASE_URL=postgres://finance:finance@localhost:5435/finance

# Scraper identifiers (enable only the ones you need)
BANCHILE_RUT=12345678-9          # Banco de Chile (via fintself)
FINTUAL_EMAIL=your@email.com     # Fintual (web session; run `make fintual-login` once)
EMAIL_IMAP_HOST=imap.gmail.com   # Email parser (MercadoPago, MACH, Tenpo)
EMAIL_IMAP_USER=your@gmail.com
```

Then store the secrets in the Keychain (prompts interactively, values never
touch disk or shell history):

```bash
make secrets-init      # prompts for each missing secret
make secrets-status    # shows which secrets are stored
make secret-set KEY=FINTUAL_PASSWORD   # overwrite a single secret
```

Secrets and their meaning:

| Keychain item | Value |
|---|---|
| `chanchito.BANCHILE_PASSWORD` | Banco de Chile web password |
| `chanchito.FINTUAL_PASSWORD` | Fintual account password (used by `make fintual-login` to open a web session) |
| `chanchito.BUDA_API_KEY` | Buda.com API key |
| `chanchito.BUDA_API_SECRET` | Buda.com API secret |
| `chanchito.EMAIL_IMAP_PASSWORD` | Gmail App Password (see below) |

A scraper is enabled only when all of its credentials are present.
On non-macOS hosts (e.g. Docker-only deploys), export the secret env vars
directly instead of using the Keychain.

### Gmail App Password

For the email parser, you need a Gmail App Password (not your regular password):

1. Go to https://myaccount.google.com/apppasswords
2. Generate a new app password for "Mail"
3. Use that 16-character password as `EMAIL_IMAP_PASSWORD`

## Dashboard Pages

| Page | URL | Description |
|---|---|---|
| **Inicio** | `/` | Today's budget status, expected balance, drift, quick actions |
| **Planificacion** | `/planning` | Day-by-day expected balance table (31 rows, today highlighted) |
| **Historial** | `/history` | Wealth timeline chart + snapshot table (patrimonio, deuda, ahorro) |
| **Gastos** | `/expenses` | Transaction list, manual entry form, CSV import |
| **Gastos Fijos** | `/fixed` | Monthly fixed expenses with shared ratio (69%) |
| **Transferencias** | `/transfers` | Internal money movements (pending/resolved) |
| **Configuracion** | `/settings` | Budget parameters, income split calculator, monthly reset |

## Daily Workflow

1. Open the dashboard at `http://localhost:3000`
2. The **Inicio** page shows your expected balance for today vs your real balance
3. If the drift is negative, you're over budget; positive means under budget
4. Add manual expenses in **Gastos** or let scrapers import them automatically
5. Check **Planificacion** to see how the rest of the month looks

## Monthly Workflow

At the start of each month:

1. Go to **Configuracion**
2. Update any budget parameters that changed (salary, credit card limit, etc.)
3. Click **"Crear proximo mes"** to initialize next month's config
4. Review **Gastos Fijos** for any changes to recurring expenses
5. Add a new wealth snapshot in **Historial** (patrimonio + deuda)

## Scrapers

Scrapers run independently from the dashboard. They share a PostgreSQL database.

### Fintual sign-in

Fintual retired its token-only API — reading your goals now requires a real web
session with e-mail 2FA. Sign in once and the session is cached to disk
(`apps/scrapers/.fintual_session.json`, gitignored):

```bash
make fintual-login   # signs in, prompts for the 6-digit code Fintual e-mails you
```

Scheduled scrapes reuse the cached session automatically. When it eventually
expires, the Fintual scraper reports `Fintual session expired. Run
`make fintual-login`…` — just run the login again. (Override the cache location
with `FINTUAL_SESSION_FILE` if needed.)

Running scrapers in Docker? The session is cached in the `scraper_state` volume
(`FINTUAL_SESSION_FILE=/data/fintual_session.json`). Sign in *inside* the
container so it writes to that volume:

```bash
docker compose run --rm scrapers python -m scrapers.institutions.fintual
```

### Running scrapers once

```bash
make scrapers-once
```

### Running scrapers on schedule

```bash
make scrapers-start
```

Schedule per scraper:
- **Fintual**: every 6 hours
- **Buda**: every 1 hour
- **BanChile**: every 24 hours
- **Email parser**: every 30 minutes

### Checking scraper status

The home page shows scraper status with:
- Last sync time per account
- Green/red badges for success/error
- Error messages (click to expand)

## CSV Import

1. Go to **Gastos**
2. Scroll to **Importar CSV**
3. Select a CSV file from any bank
4. The importer auto-detects separators (`,` or `;`) and column names
5. Map columns: Descripcion, Monto, Fecha
6. Preview the first 5 rows
7. Click **Importar**

Supported date formats: `DD/MM/YYYY`, `DD-MM-YYYY`, `YYYY-MM-DD`

Amounts: handles `1.234` (Chilean thousands separator) and `-1.234,56`

## Category Auto-Assignment

1. Categories are pre-seeded: Supermercado, Transporte, Restaurantes, etc.
2. Add keyword rules via the API:
   ```bash
   # Example: UBER -> Transporte
   curl -X POST http://localhost:3000/api/categories \
     -H "Content-Type: application/json" \
     -d '{"keyword":"uber","categoryId":"<transport-category-id>"}'
   ```
3. Run auto-assignment:
   ```bash
   curl -X PUT http://localhost:3000/api/categories
   ```

## API Reference

All API routes are under `/api/`:

| Method | Endpoint | Description |
|---|---|---|
| GET/POST | `/api/budget` | Budget config (current month) |
| GET | `/api/planning` | Planning table + today status |
| GET/POST | `/api/transactions` | Transactions (list, create) |
| POST | `/api/import` | CSV import |
| GET/POST/PUT/DELETE | `/api/fixed-expenses` | Fixed expenses CRUD |
| GET/POST/DELETE | `/api/wealth` | Wealth snapshots |
| GET/POST/DELETE | `/api/income-sources` | Income sources |
| GET/POST/PUT/DELETE | `/api/transfers` | Internal transfers |
| GET/POST/PUT | `/api/categories` | Categories + auto-assign rules |
| GET | `/api/scrapers` | Scraper run status |
| GET | `/api/balances` | Latest balance per account |
| POST | `/api/month-reset` | Create next month's config |

## Deployment

### Local (Docker Compose)

```bash
make up        # Start everything (postgres + web + scrapers)
make down      # Stop everything
make logs      # View logs
```

### Production

The project is deployment-agnostic. Options:
- **Fly.io**: `fly launch` in each app directory
- **Railway**: Connect the monorepo, set build commands per service
- **VPS**: Use `docker-compose.yml` with proper secrets management
