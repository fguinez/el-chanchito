# El Chanchito - Usage Guide

## Prerequisites

- **Node.js** >= 20 + **pnpm** 9 (`corepack enable` provides the version pinned
  in `package.json`)
- **Python** >= 3.12 as `python3` (the shared `packages/product-model` requires
  it; `make install` builds `.venv` from whatever `python3` is on your PATH)
- **Docker** + **Docker Compose**
- **macOS** for the Keychain secret helpers (`make secrets-*`); on other hosts,
  export the secrets as env vars instead (see "Configuration")

`make install` installs everything, including the Playwright Chromium browser
the BanChile scraper needs (in Docker this happens in the scrapers image).

## Quick Start

```bash
# 1. Clone and install (Node deps, Python venv, Playwright Chromium)
git clone https://github.com/fguinez/el-chanchito.git
cd el-chanchito
make install

# 2. Set up environment
cp .env.example .env
# Edit .env with your identifiers (see "Configuration" below)
make secrets-init   # macOS: store passwords and API keys in the Keychain

# 3. Start everything: postgres + migrations + scrapers (with on-demand refresh) + dashboard
make dev

# Open http://localhost:3000
```

`make dev` brings up PostgreSQL (Docker), runs migrations, starts the scraper
service on the host (control endpoint on `127.0.0.1:8080`, logs in
`local/scrapers-dev.log`), and runs the Next.js dev server wired to it. Ctrl+C
stops the dashboard and the scraper service together. To run only the web dev
server (e.g. pure UI work), use `make dev-web`.

Everything listens on loopback only (PostgreSQL on `127.0.0.1:5435`, the
dashboard on `127.0.0.1:3000`), so other machines on your network cannot reach
it. To open the dashboard from your phone, see
[Reaching the dashboard from another device](#reaching-the-dashboard-from-another-device).

With no scraper configured the scraper service exits right away: `make dev`
says so and keeps the dashboard running, but it stays empty and on-demand
refresh is unavailable until at least one scraper is enabled.

## Configuration

Configuration is split in two:

- **`.env`** — non-secret config: Postgres settings and scraper identifiers.
- **macOS Keychain** — secrets (passwords, tokens, API keys). Loaded automatically
  by `make dev`, `make scrapers-once`, `make scrapers-start`, `make up`,
  `make fintual-login` and `make bci-lider-login` via `scripts/load-secrets.sh`.

Copy `.env.example` to `.env` and fill in the identifiers:

```bash
# Required - PostgreSQL (defaults work out of the box)
POSTGRES_USER=finance
POSTGRES_PASSWORD=finance
POSTGRES_DB=finance
DATABASE_URL=postgres://finance:finance@localhost:5435/finance

# Scraper identifiers (enable only the ones you need)
BANCHILE_RUT=12345678-9          # Banco de Chile (via Playwright)
LIDER_BCI_RUT=12345678-9         # Tarjeta Lider Bci (real Chrome over CDP)
# LIDER_BCI_CDP_URL=http://localhost:9222  # optional: reuse the make bci-lider-login Chrome
SANTANDER_RUT=00.000.000-0       # Banco Santander (headless Playwright login)
FINTUAL_EMAIL=your@email.com     # Fintual (web session; run `make fintual-login` once)
EMAIL_IMAP_HOST=imap.gmail.com   # Email parser (MercadoPago, MACH, Tenpo)
EMAIL_IMAP_USER=your@gmail.com
```

`.env.example` also lists every optional setting with its default: the Postgres
host port (`POSTGRES_PORT`), the e-mail tuning (`EMAIL_LOOKBACK_DAYS`,
`EMAIL_MAX_MESSAGES`, `EMAIL_IMAP_MAILBOX`), the Fintual session cache
(`FINTUAL_SESSION_FILE`), the Tarjeta Lider Bci Chrome
(`LIDER_BCI_CDP_PORT`, `LIDER_BCI_CHROME_PATH`), the visible-browser debug switch
for Santander (`SANTANDER_HEADLESS=0`), the scraper service mode
(`SCRAPER_MODE`), the on-demand refresh wiring and the dashboard login.

Optional email parser tuning (defaults in parentheses):

| Variable | Meaning |
|---|---|
| `EMAIL_LOOKBACK_DAYS` (7) | Days of mail searched on each run |
| `EMAIL_MAX_MESSAGES` (100) | Emails parsed per institution and run, newest first; a truncated window logs a warning |
| `EMAIL_IMAP_MAILBOX` (`INBOX`) | Mailbox searched for notifications. Set it to Gmail's All Mail (`[Gmail]/All Mail`, or `[Gmail]/Todos` with a Spanish Gmail UI) when filters archive them. Fintual 2FA codes are always read from `INBOX` |

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
| `chanchito.LIDER_BCI_PASSWORD` | Tarjeta Lider Bci clave (autofills the login; without it someone has to type it into the Chrome window) |
| `chanchito.SANTANDER_PASSWORD` | Banco Santander web clave |
| `chanchito.FINTUAL_PASSWORD` | Fintual account password (used by `make fintual-login` to open a web session) |
| `chanchito.BUDA_API_KEY` | Buda.com API key |
| `chanchito.BUDA_API_SECRET` | Buda.com API secret |
| `chanchito.SKIPO_API_KEY` | Skipo read-only API key (see "Skipo API key") |
| `chanchito.EMAIL_IMAP_PASSWORD` | Gmail App Password (see below) |
| `chanchito.DASHBOARD_PASSWORD` | Dashboard login password (see "Deployment"; required in production, not exported to `make dev`) |

A scraper is enabled only when its credentials are present:

| Scraper | Enabled when set |
|---|---|
| Fintual | `FINTUAL_EMAIL` + `FINTUAL_PASSWORD` |
| Buda | `BUDA_API_KEY` + `BUDA_API_SECRET` |
| Skipo | `SKIPO_API_KEY` |
| Banco de Chile | `BANCHILE_RUT` + `BANCHILE_PASSWORD` |
| Tarjeta Lider Bci | `LIDER_BCI_RUT` |
| Banco Santander | `SANTANDER_RUT` + `SANTANDER_PASSWORD` |
| MACH, MercadoPago, Tenpo | `EMAIL_IMAP_HOST` + `EMAIL_IMAP_USER` + `EMAIL_IMAP_PASSWORD` |

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
| **Inicio** | `/` | Monitors in warning or alert, scraper status, quick actions |
| **Monitores** | `/monitors` | Formula alerts over product values; each monitor's page (`/monitors/[id]`) charts its history and manages its monthly variaciones |
| **Historial** | `/history` | Net-worth timeline (patrimonio, deuda, ahorro) computed from product balances, plus legacy snapshots |
| **Instituciones** | `/institutions` | Every scraped product grouped by institution, with on-demand refresh; drill into `/institutions/[institution]` and `/institutions/[institution]/[product]` for balance history and movements |
| **Gastos** | `/expenses` | Transaction list, manual entry form, CSV import |
| **Categorías** | `/categories` | Categories and keyword rules for automatic categorization |
| **Gastos Fijos** | `/fixed` | Monthly fixed expenses with shared ratio (69%) |
| **Transferencias** | `/transfers` | Internal money movements between two of your CLP products (pending/resolved); "Vincular" links each one to the scraped debit and credit that moved the money, and "Sugeridas" proposes rows that look like transfers for one-click confirm or dismiss |
| **Instituciones** | `/institutions` | Every institution and its products with balances; edit institutions, accounts and products from their detail pages |
| **Configuracion** | `/settings` | Budget parameters, income split calculator, monthly reset |

## Daily Workflow

1. Open the dashboard at `http://localhost:3000`
2. The **Inicio** page lists the monitors in warning or alert (or says all is in
   order) and the latest run of each scraper
3. Add manual expenses in **Gastos** or let scrapers import them automatically
4. When something one-off changes the month's plan (a reimbursement, an extra budget), add it as a variación on the monitor's page (see [Monitors and variaciones](#monitors-and-variaciones))
5. Check **Monitores** for every monitor and its day-by-day history

## Monthly Workflow

At the start of each month:

1. Nothing to reset: thresholds built on `DAY_OF_MONTH()` (the "Rampa mensual"
   preset) start over on their own, and every monitor starts the month with no variaciones
2. Edit any monitor whose thresholds depend on a figure that changed (salary,
   credit card limit, etc.)
3. Review **Gastos Fijos** for any changes to recurring expenses
4. Check **Historial**: net worth is computed from product balances, so there
   is nothing to enter; if an institution looks stale, refresh it from
   **Instituciones** (see [Net-worth history](#net-worth-history))

## Monitors and variaciones

A monitor is one equation over product values (for example, the checking
balance minus what the cards owe) compared against an alert threshold and an
optional warning one. Thresholds can ramp through the month; the "Rampa
mensual" preset builds `base - descuento * (DAY_OF_MONTH() - 1)`. Create them
from **Monitores > Nuevo monitor**.

**Variaciones** are one-off adjustments to a monitor's plan for the month, like
the "Variaciones" column of the old planning sheet: a reimbursement, a one-off
gift budget. On the monitor's page, the **Variaciones** card adds one with a
date, an amount in the monitor's currency and an optional description:

- From that day to the end of its month, the amount is added to every
  threshold of the monitor; a negative amount lowers them. The status, the
  margin, the history chart and the Inicio card all shift from that day on.
- Several variaciones on one day add up. The card shows each day's total and
  the month-to-date sum, and every entry can still be edited or deleted.
- A new month starts with none; the card's arrows browse other months.

For example (synthetic figures), with a balance floor that ramps down through
the month, a `-50000` gift budget on the 15th lowers the floor by $ 50.000 from
the 15th on, so spending that money does not trip the alert.

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

### Skipo API key

Skipo is read through its official API with a personal key. Create one in
your Skipo account with read-only scopes only: `accounts:read`
(balances and ledger) and `market_data:read` (asset catalog and coin
prices). The scraper never writes. Leave the IP allowlist empty if your
scrapers run from a dynamic residential IP, since the key would stop working
when it changes.
Store it with:

```bash
make secret-set KEY=SKIPO_API_KEY
```

A rejected key (expired, revoked, missing a scope or blocked by IP) fails the
run with the HTTP status and Skipo's error code. Tokenized stocks and fiat
other than CLP are not imported yet; holding them marks the run `partial`.

### Banco Santander

The Santander scraper logs in with a headless Chromium once per run (RUT +
clave, no session kept between runs) and reads only the CLP cuenta vista: its
balance and its movements, from the JSON the bank's own site loads. Other
Santander products are ignored. It runs on the host and in the Docker
container alike.

Santander's login is protected by an edge firewall and behavioural telemetry.
The scraper never retries a failed login: if the bank shows its "Revisa tu
conexión a internet" block page, asks for a second factor (Santander Pass, a
clave dinámica) or rejects the credentials, the run fails with a message
saying which, and the next scheduled run tries again. To watch the browser
while debugging, set `SANTANDER_HEADLESS=0` (needs a display, so not in the
container).

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
- **Skipo**: every 1 hour
- **BanChile**: every 24 hours
- **BCI Lider**: every 24 hours
- **Santander**: every 24 hours
- **MACH, MercadoPago, Tenpo** (e-mail): every 30 minutes

`make scrapers-start` (like `make dev` and the Compose service) also runs every
enabled scraper once at startup, then follows the schedule.

### Checking scraper status

The **Estado de scrapers** card on **Inicio** shows the latest run of each
scraper (one row per method and institution):
- How long ago it ran
- A status badge: `success`, `partial` (it finished with warnings), `error`, or
  `running`
- Error messages (click a failed row to expand)

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
  - `POST /refresh`: trigger every configured scraper (`202` with the slugs it
    `triggered` and those it `skipped` for a cooldown, see below)
  - `POST /refresh/{slug}`: trigger one (`404` if the slug isn't configured,
    `429` with `Retry-After` when it can't run yet, see below)
  - `GET /scrapers`: the enabled scraper slugs (drives which refresh buttons
    are enabled)
  - `GET /health`: liveness check; `503` once the scheduler's heartbeat has
    stalled for 10 minutes (the Compose healthcheck reads it)
- **Dashboard** — set `SCRAPER_CONTROL_URL` to reach that server; the web route
  `POST /api/institutions/refresh` (optional body `{"institution":"<slug>"}`) proxies
  to it and returns `503` when it's unavailable.

Banco de Chile allows at most one full RUT + password login per
`BANCHILE_LOGIN_COOLDOWN_MINUTES` (default 10, `0` disables), since the bank
throttles logins. A refresh its cached session can serve goes through; one that
would need a second login inside that window is refused, and the dashboard
shows the minutes left instead.

The server binds `SCRAPER_CONTROL_HOST`, which defaults to `127.0.0.1`, so on
the host only local processes can trigger a scrape. Under Docker Compose this is
wired automatically (`scrapers` sets `SCRAPER_CONTROL_HOST=0.0.0.0` inside its
container and exposes `8080` on the compose network only, `web` points at
`http://scrapers:8080`). For **host-dev**, `make dev` wires it automatically too
(scrapers on `127.0.0.1:8080`, dashboard pointed at it). To run the pieces by
hand instead:

```bash
# terminal 1: scrapers on a schedule, control endpoint on 127.0.0.1:8080 (the default)
make scrapers-start

# terminal 2: dashboard only, reaching the scraper control endpoint
SCRAPER_CONTROL_URL=http://127.0.0.1:8080 make dev-web
```

## Managing institutions and products

Scrapers create institutions, accounts and products with generated names
("Banco de Chile - checking"). The **Editar** buttons on the Instituciones detail
pages rename them and fill in what no scraper writes:

- **Institution** (`/institutions/{slug}`): name, kind, country and site, plus a
  rename field per account. The slug is read-only: scrapers resolve the
  institution by it. This is also how the `csv_import` institution that CSV
  imports create gets a proper name.
- **Product** (`/institutions/{slug}/{product}`): name, the product it hangs from
  (a debit card under its checking account), display order and the active flag.
  Renaming never changes the slug. The slug has its own **Cambiar slug** action,
  since it moves the product page's URL; monitors are unaffected because they
  store product ids.

Kind and currency are read-only: together with the external reference they are
how scrapers recognize a product, so changing them would split it in two on the
next scrape. Scrapers stop updating a deactivated product's balance, though its
movements keep importing; its last balance keeps showing and counting toward net
worth, and monitors that reference it report no data. A product with no balance
cannot be deactivated, because it would disappear from the dashboard.

## CSV Import

1. Go to **Gastos**
2. Scroll to **Importar CSV**
3. Select a CSV file from any bank
4. The importer auto-detects separators (`,` or `;`) and column names
5. Map columns: Descripcion, Monto, Fecha
6. Preview the first 5 rows
7. Click **Importar**

Supported date formats: `DD/MM/YYYY`, `DD-MM-YYYY`, `YYYY-MM-DD`, `YYYY/MM/DD`
(a trailing time is ignored). Rows with any other date are skipped and counted
under **Omitidos**.

Amounts: handles `1.234` (Chilean thousands separator) and `-1.234,56`

## Categories

Transactions are categorized automatically from keyword rules, which you manage
on the **Categorías** page (`/categories`).

- **Categories**: eight come pre-seeded (Supermercado, Transporte, Restaurantes,
  etc.). Add new ones with a name, color, and icon, and edit them inline.
  Deleting a category that is in use is allowed; the page first tells you how
  many transactions and rules it has. Its transactions become uncategorized
  and its rules are deleted; "Categorizar ahora" then runs the remaining rules
  over them (new transactions are categorized on insert, these are not).
- **Rules**: a rule matches when the transaction description contains its
  keyword, ignoring upper and lower case (a plain substring: `%` and `_` have no
  special meaning). When several rules match, the highest priority wins; on
  equal priority the older rule wins, so a more specific keyword added later
  (say `uber eats` after `uber`) needs a higher priority.
- **Probar** previews a keyword, before or after you save the rule: how many
  transactions contain it, how many are still waiting for a category, and the
  20 most recent matches with their current category.

When categories are assigned:

- **On import**: every new transaction is categorized as it is inserted, whatever
  its source (scrapers, CSV import, manual entry). This happens in the database
  (see [ARCHITECTURE.md](ARCHITECTURE.md#database-schema-er-diagram)), so there
  is nothing to run.
- **Categorizar ahora**: applies new or changed rules to the transactions that
  are still uncategorized and reports how many it categorized.

Rules only fill in missing categories: they never overwrite a category that is
already set (including one chosen by hand), and editing or deleting a rule does
not undo the categories it already assigned.

## Internal transfers

A transfer on `/transfers` records money moving between two of your own CLP
products, for example paying a credit card from checking. When the scrapers
import both legs, link them with **Vincular**: pick the outgoing transaction on
the source product and the incoming one on the target. Only rows with exactly
the transfer amount (no fee tolerance), the right sign, and a date within 5
days of the transfer are offered, and a row can serve one transfer at most.

Linked rows are flagged as internal transfers: they stay in every list with a
"Transferencia interna" badge but drop out of the Gastos / Ingresos / Neto
totals on `/expenses` and the monthly Ingresos/Gastos on a product page. The
product balance chart still counts them, since the balance really moved.
Unlinking (or deleting the transfer) clears the flag.

A pending transfer turns resolved by itself once every side whose product has
transactions is linked. A side whose product has none (a fund, a term
deposit) stays unlinked; resolve that transfer by hand with the check button.
Unlinking never moves a transfer back to pending.

### Suggested transfers

The **Sugeridas** section, above Pendientes, proposes scraped rows that look
like internal transfers, so most linking is a click on **Confirmar**. It is
hidden when there is nothing to suggest. Suggestions are computed on each load
from the last 90 days of CLP rows that no transfer links and that are not
already flagged. Nothing is ever linked automatically: paying a person the
same amount on the same day is a real false positive, so every suggestion
waits for you.

Two kinds:

- **Pair**: a debit on one product and a credit of exactly the same amount on
  another, dated at most 3 days apart. Paying a credit card from checking is
  the usual case. A row with several possible partners shows once per partner
  ("una de N opciones"); confirming one drops the rest.
- **One side only**: a debit whose description names a CLP product with no
  transactions (a fund, a term deposit), by its product name, or by its
  institution's name when the debit is at another institution. Names shorter
  than 5 characters and generic kind labels never match.

Each suggestion shows a score and its evidence, from simple additive signals:

| Signal | Points | Evidence |
|---|---|---|
| Same amount (pairs, always) | 40 | "Mismo monto" |
| Date gap (pairs; a one-sided balance rise) | 30, minus 10 per day | "Mismo día", "1 día de diferencia" |
| Description names the target product | 25 (10 for an institution name shared by several such products) | "La glosa menciona ..." |
| Target's balance rose by the same amount (within 2%) within 3 days | 30 | "Depósito similar en ..." |
| A keyword (Traspaso, Transferencia, Pago tarjeta, Abono, Pago TC) in either description | 15 | "Glosa '...'" |
| A confirmed pattern: same products and description prefix | 50 | "Patrón confirmado antes" |
| Same products as a confirmed suggestion, other description | 20 | "Mismos productos que una transferencia confirmada" |

**Confirmar** creates a resolved transfer (the source row's date and
description, its amount) and links its rows exactly as Vincular does, so they
drop out of Gastos and Ingresos. A one-sided suggestion links only the source.
It also saves a pattern: the two products and the start of the source
description, lowercased without accents or digits, so account numbers and
dates do not matter. Later rows matching it rank first. Patterns live in
`confirmed_transfer_patterns`; deleting a row there removes its boost.

**Descartar** remembers the dismissal in `dismissed_transfer_suggestions`, so
the suggestion does not come back. A dismissed pair is that exact pair of
rows. Dismissing a one-sided suggestion hides every one-sided suggestion for
that debit (all target products), but not its pairs.

Known limits: one balance rise can back several debits of about the same
size; an institution-name match lists every product without transactions at
that institution (scored lower when there are several); transfers with a fee
on top have no exact pair. The transaction detail panel shows "Parece una
transferencia interna" for a row in a current suggestion.

## Net-worth history

**Historial** charts patrimonio, deuda and ahorro (patrimonio minus deuda).
Two kinds of points feed it:

- **Calculado**: one point per day with product observations, carrying each
  product's latest snapshot forward. Patrimonio is the sum of asset products
  and deuda the amount owed on liability products (`credit_card`,
  `line_of_credit`, `loan`, `mortgage`), all converted to CLP, so debt needs
  no manual entry: scraping the card or línea is enough.
- **Histórico**: legacy `wealth_snapshots` totals entered by hand before the
  migration to product balances. They are authoritative up to the last legacy
  date, which keeps the chart continuous across the boundary, and can be
  deleted from the table. There is no form to add them.

Legacy snapshots can still be posted through the API, but only to backdate
history, and a backdated snapshot must never hide a computed point. The date
must be before today and before the first real product observation after the
latest legacy date; that date or later answers `409`. Dates inside legacy
history are always accepted (a date that already has a snapshot answers
`409`). The migration's backfill rows, which decomposed the three legacy
component columns into per-product rows on legacy dates, don't count as
observations.

```bash
# Synthetic values
curl -X POST http://localhost:3000/api/wealth \
  -H "Content-Type: application/json" \
  -d '{"snapshotDate":"2023-03-01","patrimonio":2500000,"deuda":999999}'
```

`deuda` defaults to 0; `fintualBalance`, `mercadopagoBalance`,
`banchileSavings` (informational component columns) and `notes` are optional.
Amounts are rounded to whole pesos. `make seed-legacy-history` posts a set of
synthetic legacy snapshots for demos.

## API Reference

All API routes are under `/api/`:

| Method | Endpoint | Description |
|---|---|---|
| GET/POST | `/api/monitors` | Monitors with their current evaluation (line-chart monitors add a 30-day sparkline); create |
| GET/PUT/DELETE | `/api/monitors/[id]` | One monitor with its history (`?days=N` or `?from=&to=`), referencias and variaciones; update; delete |
| POST | `/api/monitors/preview` | Validate and evaluate an unsaved monitor (`monitorId` applies that monitor's variaciones) |
| GET/POST | `/api/monitors/[id]/adjustments` | Variaciones (`?month=YYYY-MM`); create from `{ adjustmentDate, amount, description? }` |
| PATCH/DELETE | `/api/monitors/[id]/adjustments/[adjustmentId]` | Edit or delete one variación |
| GET | `/api/institutions` | Institutions + nested products (balances included) + CLP subtotals |
| GET/PATCH | `/api/institutions/[slug]` | One institution, same shape as a list item; edit `name`, `kind`, `country`, `url` |
| GET/PATCH | `/api/institutions/[slug]/products/[product]` | One product (with its `defaultName`) plus its balance history and recent transactions; edit `name` (`null` resets it to `defaultName`), `slug`, `parentProductId`, `isActive`, `displayOrder` |
| PATCH | `/api/accounts/[id]` | Rename an account (`name`) |
| POST | `/api/institutions/refresh` | Trigger a scrape (all, or `{institution}`) |
| GET | `/api/scrapers` | Latest run per scraper |
| GET | `/api/scrapers/available` | Enabled scraper slugs, from the scraper service |
| GET/POST | `/api/transactions` | Transactions (list, create; create takes an optional `location`) |
| GET/PATCH | `/api/transactions/[id]` | One transaction with product, institution, category and location; PATCH sets or clears only `{ location }` |
| POST | `/api/import` | CSV import |
| GET/POST/PUT | `/api/categories` | Categories with their rules and transaction counts; create a category; apply the rules to uncategorized transactions (PUT) |
| PATCH/DELETE | `/api/categories/[id]` | Update or delete a category (its transactions become uncategorized, its rules are deleted) |
| POST | `/api/categories/rules` | Create a keyword rule |
| PATCH/DELETE | `/api/categories/rules/[id]` | Update or delete a rule |
| GET | `/api/categories/rules/preview` | Transactions a keyword would match (`?keyword=`) |
| GET/POST/PUT/DELETE | `/api/fixed-expenses` | Fixed expenses CRUD |
| GET/POST/PUT/DELETE | `/api/transfers` | Internal transfers (POST needs distinct CLP `fromProductId` + `toProductId`); GET includes the linked `fromTransaction` / `toTransaction` |
| PUT | `/api/transfers/[id]/links` | Set or clear the linked transactions: `{ fromTransactionId?, toTransactionId? }` (null clears, omitted keeps) |
| GET | `/api/transfers/[id]/candidates` | Per side, the unlinked transactions a transfer may link (same amount, right sign, within 5 days) |
| GET | `/api/transfers/suggestions` | `{ suggestions }`: likely internal transfers (pairs and one-sided), highest score first, with legs, `score` and Spanish `evidence`; `?transactionId=` keeps those using that row |
| POST | `/api/transfers/suggestions/confirm` | `{ sourceTransactionId, targetTransactionId }` (pair) or `{ sourceTransactionId, targetProductId }` (one-sided): creates and links a resolved transfer and saves the pattern; 201 with the transfer, 409 when a row is already linked or flagged |
| POST | `/api/transfers/suggestions/dismiss` | `{ sourceTransactionId, targetTransactionId? }`: hides that suggestion (no target = the debit's one-sided suggestions); idempotent |
| GET/POST/DELETE | `/api/wealth` | Net-worth series (GET); backdate a legacy snapshot before the derived series starts (POST); delete a legacy snapshot (DELETE) |
| POST | `/api/auth/login` | Exchange `DASHBOARD_PASSWORD` for a session cookie |
| POST | `/api/auth/logout` | Clear the session cookie (public) |
| GET | `/api/auth/session` | `{ enabled, authenticated }` (public) |

When `DASHBOARD_PASSWORD` is set, every route above except the three marked
public (`/api/auth/login`, `/api/auth/logout`, `/api/auth/session`) answers `401`
without a valid session. Mutating requests are rejected with `403` when they look
cross-origin, logout included, and that check runs with or without a password
(so under `make dev` too). Clients that send neither `Origin` nor
`Sec-Fetch-Site`, such as curl, are not affected. Without a password the
dashboard also answers `403` to every request whose `Host` is not
`localhost`, `127.0.0.1` or `[::1]` (any port), which blocks DNS rebinding.
Reaching `make dev` from another device (a phone on the LAN, a tunnel)
therefore needs `DASHBOARD_PASSWORD` set, and `DASHBOARD_HOST` once the dev
server binds to loopback by default.

Request bodies and query strings are validated before anything touches the
database. A JSON body must be sent as `Content-Type: application/json`
(parameters such as `; charset=utf-8` are fine), or the route answers
`415 {"error": "Content-Type must be application/json"}`; the refresh route's
body is optional, but one that is present follows the same rule.
An empty or malformed JSON body answers `400 {"error": "Invalid JSON body"}`;
a body or query string that fails its schema answers `400` with `error` (the
first problem, prefixed by its field), `field`, and `issues` (every problem as
`{ path, message }`). Dates are `YYYY-MM-DD`, ids are uuids, amounts are numbers
rounded to whole pesos (`0` included), and `kind` and transfer `status` must be
one of the values the schema defines.

## Deployment

### Authentication (read this first)

> **Never expose the dashboard publicly without `DASHBOARD_PASSWORD`.** Every
> page and every `/api/*` route serves your real financial data. In production
> (`next build` + `next start`, or the Docker image) the app **fails closed** if
> the variable is unset: pages render a "not configured" notice and the API
> answers `503`. Nothing is served, but nothing is protected either until you
> configure it.

The dashboard uses a single shared password (single-user by design; there is no
user management). Setup:

```bash
# macOS: store it in the login Keychain alongside the scraper secrets
make secret-set KEY=DASHBOARD_PASSWORD
# ...and export it when deploying (no make target exports it, see below)
DASHBOARD_PASSWORD="$(security find-generic-password -a "$USER" -s chanchito.DASHBOARD_PASSWORD -w)" make up

# anywhere else: export it (Compose passes it through to the web service)
export DASHBOARD_PASSWORD='choose-a-long-random-one'
```

Surrounding whitespace is trimmed (a trailing newline from a Keychain or
Docker-secret round trip is the usual culprit) and a warning is logged when that
happens; a whitespace-only value counts as unset.

Logging in sets an httpOnly, `SameSite=Lax` cookie holding a signed token
(HMAC-SHA256, key derived from the password with PBKDF2), so **changing
`DASHBOARD_PASSWORD` immediately logs every session out**. The cookie is marked
`Secure` whenever the request arrives over HTTPS; in that case it is also named
`__Host-chanchito_session`, which pins it to the exact host, and the unprefixed
name is no longer accepted. To have `X-Forwarded-Proto` count as HTTPS (a
TLS-terminating reverse proxy), set `DASHBOARD_TRUST_PROXY` (below).

Login attempts are serialized process-wide, so at most one password check runs at
a time and each failure holds the line for 500 ms: that is a hard global ceiling
of roughly two guesses per second no matter how many clients pile on, and a flood
is turned away with `429` instead of queueing ahead of you. On top of that, each
client address gets its own lockout: after 5 consecutive failures **from that
client** the endpoint answers `429` with `Retry-After`, backing off exponentially
up to 5 minutes. A successful login clears that client's counter, an idle
15-minute window decays it, and a client that has been locked for more than 30
minutes straight has its password checked again (a correct one gets in, a wrong
one still gets `429`).

Someone hammering the login therefore locks out the address they are hammering
from, not you, and cannot keep you out indefinitely even if they manage to look
like you. If you do lock yourself out, wait for the window in `Retry-After`, log
in from another network, or restart the web service.

### Behind a reverse proxy: `DASHBOARD_TRUST_PROXY`

`X-Forwarded-Host`, `X-Forwarded-Proto` and `X-Forwarded-For` are just request
headers: anyone can send them, and there is no untainted signal to check them
against (the Next server even derives the request URL's own scheme from
`X-Forwarded-Proto`). They are therefore **ignored by default** and only honored
when you declare that a proxy is in front:

```bash
DASHBOARD_TRUST_PROXY=1   # 1, true, yes or on; anything else (or unset) is off
```

| Setting | Forwarded host (Origin check) | Forwarded proto (`Secure` cookie) | Forwarded client address (lockout scope) |
|---|---|---|---|
| unset (default) | ignored, `Host` is used | ignored; the cookie is `Secure` only when the connection itself is HTTPS and nothing forwarded a scheme | the caller's claim, which only ever scopes the caller's own lockout, and the connection's address when it made no claim |
| `1` | first entry honored | first entry honored | first entry honored |

**Set it whenever a reverse proxy sits in front**, and only then: the proxy must
overwrite these headers rather than pass a client's through. Without it a proxy
that rewrites `Host` makes every write look cross-origin (`403`), and a
TLS-terminating proxy leaves the session cookie without the `Secure` flag,
because `next start` stamps `X-Forwarded-Proto` on every request and an untrusted
stamp is worth nothing. Enabling it asks for the password once more, since the
session then moves to the host-pinned cookie name.

> **Symptom to recognize: a login that loops with no error.** If the app answers
> `200` to the login, the browser returns to `/login` immediately and nothing is
> shown, the session cookie is being dropped. That happens when the cookie is
> marked `Secure` (so it is also `__Host-`) while the browser is talking plain
> HTTP: browsers discard such a cookie silently. The usual cause is a proxy that
> stamps `X-Forwarded-Proto: https` unconditionally while still being reachable
> over `http://` (LAN access, a half-configured tunnel) together with
> `DASHBOARD_TRUST_PROXY` on. The login page now checks the session before
> navigating and reports it instead of looping, and the server logs one warning
> the first time it sets a `Secure` cookie on a forwarded header's word alone.
> The fix is to serve the dashboard over HTTPS, or to unset
> `DASHBOARD_TRUST_PROXY`.

`DASHBOARD_SESSION_MAX_AGE` controls how often the password is asked again:

| Value | Behavior |
|---|---|
| unset | Default: re-login 12 hours after logging in |
| `30m`, `12h`, `7d`, `30d` | Absolute expiry after that duration (not sliding) |
| `3600` | Same, in seconds |
| `browser` | Session cookie: re-login on every new browser session (the token still carries a hard 30-day cap) |
| `unlimited` | 400 days (the browser's cookie ceiling); sessions effectively last until the password changes |

An unparseable value falls back to `12h` and logs one warning at startup.

Auth is skipped only in `next dev` with `DASHBOARD_PASSWORD` unset, so `make dev`
stays frictionless. `scripts/load-secrets.sh` deliberately does **not** export
`DASHBOARD_PASSWORD`, so storing it in the Keychain for deploys never turns the
login on locally; production reads it from the environment (Compose passes it
through), never from the Keychain. To exercise the login locally, opt in per run:

```bash
DASHBOARD_PASSWORD='changeme-example' make dev-web
```

Treat the password as one layer, not the whole story: prefer keeping the
dashboard off the public internet entirely (Tailscale or another VPN), or behind
a reverse proxy that terminates TLS and adds its own access control.

### Reaching the dashboard from another device

By default every port listens on `127.0.0.1` only: the dashboard (`3000`),
PostgreSQL (`5435`) and, outside Compose, the scraper control endpoint
(`8080`). Nothing is reachable from your LAN, or from the internet on a VPS.

To open the dashboard from a phone or another computer, opt in with
`DASHBOARD_HOST`, the address the dashboard listens on:

```bash
# host-dev: both variables from your shell, in the same command
DASHBOARD_PASSWORD="$(security find-generic-password -a "$USER" -s chanchito.DASHBOARD_PASSWORD -w)" \
  DASHBOARD_HOST=0.0.0.0 make dev

# make up: DASHBOARD_HOST=0.0.0.0 in .env (or your shell), DASHBOARD_PASSWORD
# exported as in "Authentication" above
```

`0.0.0.0` means every interface; a specific address (e.g. your Tailscale or LAN
IP) limits it to that network. Compose needs an IPv4 address or a bracketed IPv6
one (`[::]`).

A password is required beyond loopback. `next dev` skips the login when
`DASHBOARD_PASSWORD` is unset, so `make dev` and `make dev-web` refuse to start
with a non-loopback `DASHBOARD_HOST` until it is set. It has to come from your
shell: `next dev` runs in `apps/web` and never reads the root `.env`, and
`scripts/load-secrets.sh` deliberately skips it. The production build behind
`make up` already fails closed without it (see "Authentication" above).

`next dev` also blocks hot reload for origins other than `localhost` and the
address it listens on. When you open it through another address (e.g. your LAN
IP while listening on `0.0.0.0`), list those hostnames in
`DASHBOARD_DEV_ORIGINS` (comma-separated) in the same shell:
`DASHBOARD_DEV_ORIGINS=192.168.0.10`. Production builds ignore it.

PostgreSQL and the scraper control endpoint have no opt-in: they stay on
loopback (the database password defaults to a public value and the control
endpoint is unauthenticated).

### Local (Docker Compose)

```bash
make up        # Start everything (postgres + web + scrapers)
make down      # Stop everything
make logs      # View logs
```

The dashboard is at `http://localhost:3000` on the machine running Compose; see
above to reach it from elsewhere.

`docker compose ps` shows the `scrapers` service as `unhealthy` when its
scheduler has stopped running jobs (its `/health` endpoint answers `503`).

### Production

The project is deployment-agnostic. Options:
- **Fly.io**: `fly launch` in each app directory
- **Railway**: Connect the monorepo, set build commands per service
- **VPS**: Use `docker-compose.yml` with proper secrets management
