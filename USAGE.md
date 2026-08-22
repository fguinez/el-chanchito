# El Chanchito - Usage Guide

## Prerequisites

- **Node.js** >= 20 + **pnpm** >= 9
- **Python** >= 3.11
- **Docker** + **Docker Compose**

`make install` installs everything, including the Playwright Chromium browser
the BanChile scraper needs (in Docker this happens in the scrapers image).

## Quick Start

```bash
# 1. Clone and install (Node deps, Python venv, Playwright Chromium)
make install

# 2. Set up environment
cp .env.example .env
# Edit .env with your credentials (see "Configuration" below)

# 3. Start everything: postgres + scrapers (with on-demand refresh) + dashboard
make dev

# Open http://localhost:3000
```

`make dev` brings up PostgreSQL (Docker), runs migrations, starts the scraper
service on the host (control endpoint on `:8080`, logs in
`local/scrapers-dev.log`), and runs the Next.js dev server wired to it. Ctrl+C
stops the dashboard and the scraper service together. To run only the web dev
server (e.g. pure UI work), use `make dev-web`.

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
LIDER_BCI_RUT=12345678-9         # Tarjeta Lider Bci (real Chrome over CDP)
LIDER_BCI_CDP_URL=http://localhost:9222  # Tarjeta Lider Bci: the make bci-lider-login Chrome
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
| `chanchito.LIDER_BCI_PASSWORD` | Tarjeta Lider Bci clave (optional; only prefills the `make bci-lider-login` browser) |
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

### Tarjeta Lider Bci

The Tarjeta Lider Bci portal guards its login with a Cloudflare "Verifique que es
un ser humano" check that passes only for a genuine browser (a headless/automated
one gets an unsolvable interactive check). So the scraper drives a **real Chrome**
over the DevTools protocol, and by default runs it **managed**: each scrape
launches Chrome, autofills the login, reads the card, and closes Chrome. Nothing to
keep running: just set `LIDER_BCI_RUT` (+ `chanchito.LIDER_BCI_PASSWORD` in the
Keychain) and it runs unattended.

Requirements: a genuine Google Chrome installed on the machine (not the Playwright
Chromium `make install` fetches; set `LIDER_BCI_CHROME_PATH` if it's elsewhere) and
a display (Chrome must be headed: Cloudflare blocks headless), so this runs on your
Mac, not inside the headless Docker container. The window is launched off-screen so
it doesn't flash on your desktop; if that ever misbehaves the run retries once with
a normal visible window. If Cloudflare ever shows the human-verification check (rare
for a genuine Chrome), the run falls back to a visible window so you can tick it.

To instead reuse one long-running Chrome (e.g. to avoid the window flashing, or to
sign in by hand once), start it and point the scraper at it:

```bash
make bci-lider-login                      # launches a real Chrome, signs in, leaves it running
LIDER_BCI_CDP_URL=http://localhost:9222   # add to .env; scrapes then drive this Chrome
```

In reuse mode scrapes drive that Chrome (reusing the signed-in tab or re-logging-in
via autofill) and report `Could not reach the Chrome debug port…` when it isn't up.
(Override the debug port with `LIDER_BCI_CDP_PORT`.)

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
- **BCI Lider**: every 24 hours
- **Email parser**: every 30 minutes

### Checking scraper status

The home page shows scraper status with:
- Last sync time per account
- Green/red badges for success/error
- Error messages (click to expand)

### On-demand refresh (from the dashboard)

The **Instituciones** page has an **"Actualizar todo"** button plus a per-institution
refresh icon. Clicking one triggers an immediate scrape instead of waiting for the
next scheduled run; the button spins ("Sincronizando…") and the page reloads the
balances once the run finishes. The dashboard asks the scraper service which
scrapers are enabled (`GET /api/scrapers/available`, proxying the control
endpoint's `GET /scrapers`) and disables the button for institutions not in
that list; the same rule keeps buttons disabled while the list is loading and
when the service isn't configured.

This works only while the scraper service is running with its **internal control
endpoint** enabled. The endpoint is an unauthenticated trigger, so it must stay on
the private network — never publish its port (see the auth issue, #23). If the
service isn't reachable the dashboard shows *"Servicio de scrapers no disponible"*
instead of spinning forever.

- **Scraper service** — set `SCRAPER_CONTROL_PORT` (a small HTTP server binds to it):
  - `POST /refresh` — trigger every configured scraper
  - `POST /refresh/{slug}` — trigger one (`404` if the slug isn't configured)
  - `GET /scrapers`: the enabled scraper slugs (drives which refresh buttons
    are enabled)
  - `GET /health` — liveness check
- **Dashboard** — set `SCRAPER_CONTROL_URL` to reach that server; the web route
  `POST /api/institutions/refresh` (optional body `{"institution":"<slug>"}`) proxies
  to it and returns `503` when it's unavailable.

Under Docker Compose this is wired automatically (`scrapers` exposes `8080` on the
compose network, `web` points at `http://scrapers:8080`). For **host-dev**,
`make dev` wires it automatically too (scrapers on `:8080`, dashboard pointed at
it). To run the pieces by hand instead:

```bash
# terminal 1 — scrapers on a schedule, control endpoint on :8080 (the default)
make scrapers-start

# terminal 2 — dashboard only, reaching the scraper control endpoint
SCRAPER_CONTROL_URL=http://localhost:8080 make dev-web
```

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
| GET | `/api/institutions` | Institutions + nested products + CLP subtotals |
| POST | `/api/institutions/refresh` | Trigger a scrape (all, or `{institution}`) |
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
