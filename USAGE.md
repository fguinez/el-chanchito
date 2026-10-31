# El Chanchito - Usage Guide

## Prerequisites

- **Node.js** >= 20 + **pnpm** 9 (`corepack enable` provides the version pinned
  in `package.json`)
- **Python** >= 3.12 as `python3` (the shared `packages/product-model` requires
  it; `make install` builds `.venv` from whatever `python3` is on your PATH)
- **Docker** + **Docker Compose**
- **make** and a POSIX shell for the make targets and `scripts/*.sh` (on Windows,
  use WSL2; see "Supported platforms")

`make install` installs everything, including the Playwright Chromium browser
the BanChile scraper needs (in Docker this happens in the scrapers image).

## Supported platforms

| Host | Status |
|---|---|
| macOS | Everything. Secrets live in the login Keychain |
| Linux (x86_64 or arm64) | Everything. Secrets live in the Secret Service (GNOME Keyring, KWallet) on a desktop, or in an owner-only file on a headless host |
| Windows | Through **WSL2**, which is a Linux host for everything in this table: `make`, `scripts/*.sh` and the secret store (the Linux keyring or the file) run there. The Python scraper code itself has no POSIX-only calls and CI runs its tests natively on Windows, but native Windows is not a documented way to run the stack |
| Raspberry Pi 4/5 | The whole stack through Docker Compose on 64-bit Raspberry Pi OS (Debian, `aarch64`); see [Raspberry Pi](#raspberry-pi) below. CI builds both images on `linux/arm64` |

The Docker images (`postgres:16-alpine`, `python:3.12-slim`, `node:20-alpine`) are
multi-arch, and Playwright ships its Chromium for Linux ARM64, so the headless
scrapers (Banco de Chile, Santander, Banco Falabella) run in the container on any
of these hosts.

**Real-Chrome scrapers need a display.** Tarjeta Lider Bci drives a genuine,
headed Chrome (see [Tarjeta Lider Bci](#tarjeta-lider-bci)), so it runs on the host
with a desktop session, never in the headless container. On a headless Pi it
needs a desktop session or a virtual display (for example Xvfb), and unattended
runs there are not promised. Google Chrome has no Linux ARM64 build, so a Pi uses
Debian's `chromium`; whether Cloudflare Turnstile passes for Debian Chromium as it
does for Google Chrome is **unverified**.

### Raspberry Pi

Target: a Raspberry Pi 4 or 5 on 64-bit Raspberry Pi OS (Bookworm or
later). A 32-bit OS will not work: the images are `arm64`.

```bash
# 1. Docker (its convenience script, or Docker's Debian guide), make and git;
#    log out and back in so the docker group applies
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker "$USER"
sudo apt install -y make git

# 2. Clone and configure
git clone https://github.com/fguinez/el-chanchito.git
cd el-chanchito
cp .env.example .env      # fill in the identifiers (see "Configuration")

# 3. Secrets: no system keyring on a headless Pi, so the store is the
#    owner-only file local/secrets.env (see "Secret store backends")
make secrets-init

# 4. Build and start postgres + web + scrapers
make up
```

`make up` works with the system `python3` (no `.venv` needed for the secret
store); `make install` is only needed to run the scrapers or tests outside Docker.
The first `make up` builds both images on the Pi, which takes a while. The
Next.js build is the memory-hungry step: on a 4 GB Pi, stop other services or add
swap if the build is killed. Then reach the dashboard from
another device as described in
[Reaching the dashboard from another device](#reaching-the-dashboard-from-another-device):
`DASHBOARD_HOST=0.0.0.0` (or the Pi's LAN or Tailscale address) in `.env`, and
`DASHBOARD_PASSWORD` exported for `make up` (see [Authentication](#authentication-read-this-first)).
Postgres and the scraper control endpoint stay on loopback.

## Quick Start

```bash
# 1. Clone and install (Node deps, Python venv, Playwright Chromium)
git clone https://github.com/fguinez/el-chanchito.git
cd el-chanchito
make install

# 2. Set up environment
cp .env.example .env
# Edit .env with your identifiers (see "Configuration" below)
make secrets-init   # store passwords and API keys in the secret store

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
- **The secret store**: secrets (passwords, tokens, API keys): the macOS
  Keychain, the system keyring or an owner-only file (see "Secret store
  backends" below). Loaded automatically by `make dev`, `make scrapers-once`,
  `make scrapers-start`, `make up`, `make fintual-login` and
  `make bci-lider-login` via `scripts/load-secrets.sh`.

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
BANCO_FALABELLA_RUT=00.000.000-0 # Banco Falabella (headless Playwright login)
FINTUAL_EMAIL=your@email.com     # Fintual (web session; run `make fintual-login` once)
EMAIL_IMAP_HOST=imap.gmail.com   # Email parser (MercadoPago, MACH, Tenpo)
EMAIL_IMAP_USER=your@gmail.com
```

`.env.example` also lists every optional setting with its default: the Postgres
host port (`POSTGRES_PORT`), the e-mail tuning (`EMAIL_LOOKBACK_DAYS`,
`EMAIL_MAX_MESSAGES`, `EMAIL_IMAP_MAILBOX`), the Fintual session cache
(`FINTUAL_SESSION_FILE`), the Tarjeta Lider Bci Chrome
(`LIDER_BCI_CDP_PORT`, `LIDER_BCI_CHROME_PATH`), the visible-browser debug switches
for Santander (`SANTANDER_HEADLESS=0`) and Banco Falabella
(`BANCO_FALABELLA_HEADLESS=0`), the scraper service mode
(`SCRAPER_MODE`), the on-demand refresh wiring and the dashboard login.

Optional email parser tuning (defaults in parentheses):

| Variable | Meaning |
|---|---|
| `EMAIL_LOOKBACK_DAYS` (7) | Days of mail searched on each run |
| `EMAIL_MAX_MESSAGES` (100) | Emails parsed per institution and run, newest first; a truncated window logs a warning |
| `EMAIL_IMAP_MAILBOX` (`INBOX`) | Mailbox searched for notifications. Set it to Gmail's All Mail (`[Gmail]/All Mail`, or `[Gmail]/Todos` with a Spanish Gmail UI) when filters archive them. Fintual 2FA codes are always read from `INBOX` |

Then store the secrets in the secret store (prompts interactively with hidden
input, values never touch shell history):

```bash
make secrets-init      # prompts for each missing secret
make secrets-status    # shows which secrets are stored
make secret-set KEY=FINTUAL_PASSWORD   # overwrite a single secret
```

Secrets and their meaning:

| Item | Value |
|---|---|
| `chanchito.BANCHILE_PASSWORD` | Banco de Chile web password |
| `chanchito.LIDER_BCI_PASSWORD` | Tarjeta Lider Bci clave (autofills the login; without it someone has to type it into the Chrome window) |
| `chanchito.SANTANDER_PASSWORD` | Banco Santander web clave |
| `chanchito.BANCO_FALABELLA_PASSWORD` | Banco Falabella internet clave (six digits) |
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
| Banco Falabella | `BANCO_FALABELLA_RUT` + `BANCO_FALABELLA_PASSWORD` |
| MACH, MercadoPago, Tenpo | `EMAIL_IMAP_HOST` + `EMAIL_IMAP_USER` + `EMAIL_IMAP_PASSWORD` |

A value already exported in your shell wins over the store, so you can also
export the secret env vars directly (e.g. in a CI or Docker-only deploy).

### Secret store backends

`scripts/secret_store.py` (behind `make secrets-init`, `make secret-set`,
`make secrets-status` and `scripts/load-secrets.sh`) keeps every secret as an
item named `chanchito.<VAR>` with your user (`$USER`) as the account, and picks
the first backend that works on the host:

| Backend | Where | Notes |
|---|---|---|
| `keychain` | macOS | The login Keychain through the `security` CLI. The same items the earlier Keychain-only helpers created, so nothing to migrate |
| `keyring` | Linux desktop (also under WSL2 when a Secret Service runs there) | The [`keyring`](https://pypi.org/project/keyring/) package (`make install-py` installs it) with the Secret Service (GNOME Keyring, KWallet). Used only when its collection is unlocked: checking never pops an unlock prompt |
| `file` | Headless Linux (Raspberry Pi, server) | `local/secrets.env` (gitignored and Docker-ignored), a `KEY=value` dotenv file written owner-only (`0600`). The helper warns when it is readable by others. `CHANCHITO_SECRETS_FILE` moves it |

`make secrets-status` and `make secrets-init` print which backend is in use, and
no command but `export` (used by `load-secrets.sh`) ever prints a value. When the
helper falls back to the file on its own (no keyring installed, no Secret
Service, or a locked collection), `load-secrets.sh` says so on stderr with the
reason, and it warns when the store holds none of the secrets. Set
`CHANCHITO_SECRETS_BACKEND=keychain`, `keyring` or `file` to force one, for
example `file` on a Linux desktop whose keyring is locked when the scrapers run.
The file backend keeps secrets in plain text on disk: rely on the file mode, a
disk you control, and keep `local/` out of backups you share.

Under WSL2, keep the clone on the Linux filesystem (e.g. `~/el-chanchito`), not
under `/mnt/c`: chmod is a no-op on the Windows drives, so `local/secrets.env`
there can't be made owner-only. If the repo must live on `/mnt/c`, point
`CHANCHITO_SECRETS_FILE` at a path under `~/` instead.

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
| **Gastos** | `/expenses` | Transaction list (searchable by original or displayed description; the search stays in the URL as `?q=`), manual entry form, CSV import |
| **Categorías** | `/categories` | Categories and keyword rules for automatic categorization |
| **Presupuesto** | `/budget` | Monthly income split into budget items (Gasto fijo, Consumo estimado, Ahorro, with an optional shared ratio) and what is left over, as a live donut; `/fixed` redirects here |
| **Transferencias** | `/transfers` | Internal money movements between two of your CLP products (pending/resolved); "Vincular" links each one to the scraped debit and credit that moved the money, and "Sugeridas" proposes rows that look like transfers for one-click confirm or dismiss |
| **Instituciones** | `/institutions` | Every institution and its products with balances; edit institutions, accounts and products from their detail pages |
| **Configuracion** | `/settings` | Budget parameters, income split calculator, monthly reset |

## Daily Workflow

1. Open the dashboard at `http://localhost:3000`
2. The **Inicio** page lists the monitors in warning or alert (or says all is in
   order) and the latest run of each scraper
3. Add manual expenses in **Gastos** or let scrapers import them automatically
4. When a one-off transaction should not count against a monitor this month (a reimbursed expense, a one-off purchase), add it as a variación on the monitor's page (see [Monitors and variaciones](#monitors-and-variaciones))
5. Check **Monitores** for every monitor and its day-by-day history

## Monthly Workflow

At the start of each month:

1. Nothing to reset: thresholds built on `DAY_OF_MONTH()` (the "Rampa mensual"
   preset) start over on their own, and every monitor starts the month with no variaciones
2. Edit any monitor whose thresholds depend on a figure that changed (salary,
   credit card limit, etc.)
3. Review **Presupuesto** for any changes to your income or recurring items
4. Check **Historial**: net worth is computed from product balances, so there
   is nothing to enter; if an institution looks stale, refresh it from
   **Instituciones** (see [Net-worth history](#net-worth-history))

## Monitors and variaciones

A monitor is one equation over product values (for example, the checking
balance minus what the cards owe) compared against an alert threshold and an
optional warning one. Thresholds can ramp through the month; the "Rampa
mensual" preset builds `base - descuento * (DAY_OF_MONTH() - 1)`. Create them
from **Monitores > Nuevo monitor**.

**Variaciones** are transactions a monitor ignores for the month, like the
"Variaciones" column of the old planning sheet: a reimbursed expense, a
one-off purchase paid from savings. On the monitor's page, the
**Variaciones** card's "Agregar variación" button searches the transactions
of the products the monitor uses, in its equation or a threshold (by
description or amount, month by month), and links the one you pick:

- From the transaction's date to the end of its month, every threshold
  shifts so the comparison reads as if the transaction never happened: an
  ignored expense on the checking account lowers a balance floor by its
  amount, an ignored card purchase does the same through `- owed`, a
  coefficient like `- owed * 0.7` scales it, and a threshold that reads the
  same product moves with it. The monitor's value stays the real one; the
  status, the margin, the history chart and the Inicio card all use the
  shifted thresholds.
- A transaction in a foreign currency (a USD card purchase) converts at the
  same rate the monitor uses for that product, on every evaluation: its
  effect follows the live rate, it is never frozen.
- The card shows each transaction with its product and amount, its current
  effect on the alert threshold in the monitor's currency, each day's total
  and the month-to-date sum. A transaction whose product the monitor no
  longer reads says "no afecta a la expresión actual"; a deleted transaction
  stops applying and is flagged so you can remove it. Clicking a description
  opens the transaction's detail panel.
- A new month starts with none; the card's arrows browse other months.

For example (synthetic figures), with a balance floor that ramps down through
the month, ignoring a $ 1.000.000 purchase on the 15th lowers the floor by
$ 1.000.000 from the 15th on, so that purchase does not trip the alert.

## Scrapers

Scrapers run independently from the dashboard. They share a PostgreSQL database.

### Fintual sign-in

Fintual retired its token-only API: reading your goals and stock positions
now requires a real web session with e-mail 2FA. Sign in once and the session
is cached to disk (`apps/scrapers/.fintual_session.json`, gitignored):

```bash
make fintual-login   # signs in, prompts for the 6-digit code Fintual e-mails you
```

Scheduled scrapes refresh the cached session the way the website does, with no
2FA, so you only need `make fintual-login` again when:

- the refresh token itself expires and `FINTUAL_EMAIL` is not the IMAP mailbox
  (`EMAIL_IMAP_USER`); the scraper reports ``Fintual session expired. Run
  `make fintual-login`…``. When the account *is* the IMAP mailbox, the scraper
  reads the e-mailed code and signs in by itself.
- an automatic sign-in failed. It is not retried (each attempt e-mails you a
  new code) until a manual login succeeds; the scraper reports `Fintual
  automatic sign-in failed earlier…`.

Don't let two scraper processes share one session cache at the same time (for
example `make scrapers-once` while `make dev` or `make scrapers-start` runs):
the refresh token is single-use, so one process would spend the token the
other still holds. (Override the cache location with `FINTUAL_SESSION_FILE` if
needed.)

Running scrapers in Docker? The session is cached in the `scraper_state` volume
(`FINTUAL_SESSION_FILE=/data/fintual_session.json`). Sign in *inside* the
container so it writes to that volume:

```bash
docker compose run --rm scrapers python -m scrapers.institutions.fintual
```

### Fintual stocks

If you use Fintual's stock brokerage ("Acciones"), each run also imports it,
in USD: one product per position, one for the cash not yet invested
("Acciones: saldo disponible"), and every movement of that cash as
transactions:

- A **deposit** from your bank lands on the cash wallet; a **buy** shows twice,
  as money out of the wallet and into the stock, and a **sell** the other way
  round. All of these are flagged "Transferencia interna", so they stay out of
  Gastos and Ingresos.
- A **dividend** is income on the stock (its wallet side is flagged
  internal); the **interest** Fintual pays on the uninvested cash is income
  on the cash wallet. Withdrawals, cash mergers, compensations, returns of
  capital, ACAT transfers and warrant exercises are imported too. Not every
  movement shape is verified against live data yet; each run checks that the
  movements add up to the wallet's balance and warns when they don't.
- Movements still pending show up once Fintual executes them; one still not
  executed a week after it was requested is taken as cancelled. Divestments
  are not imported (the run says how many it skipped).
- A stock bought for the first time gets its movements one run after its
  product appears. A stock you sold entirely stays as a product valued at 0;
  one that left without a sale (a share-only merger, a spinoff, shares moved
  out by ACAT) keeps its last values.
- Your [category](#categories) keyword rules apply to these rows like to any
  other, so a rule matching "Compra ACME" or "Intereses saldo USD" files them
  under its category; USD rows and internal transfers still stay out of the
  CLP totals.
- Each stock you hold gets its daily closing prices for the last year, kept
  from then on: its product page charts them in "Historial de precio", with
  your average cost as a dashed line.

The positions get their own **Acciones** table on Fintual's institution card,
apart from the goals' **Inversiones** one: value, shares, price, average cost,
and the gain and return on the shares still held, plus the realized result of
sales and the dividends once a position has any.

The "Transferencia interna" flag is set once, when a movement is first
imported: untick it in the transaction panel and later scrapes leave it
unticked.

On a fresh database, deploy the stock products first and let one Fintual run
finish before deploying the movements. Otherwise the movements, written
first, create the USD wallet under the generic slug `fintual-wallet-usd`: its
name corrects itself, but a product's slug never changes.

Tip: the bank debit that funded a USD deposit is spending in CLP as far as
the dashboard knows, and transfers on `/transfers` only link CLP rows. Open
that debit in the transaction panel and tick **Transferencia interna** so it
drops out of Gastos too.

### Tarjeta Lider Bci

The Tarjeta Lider Bci portal guards its login with a Cloudflare "Verifique que es
un ser humano" check that passes only for a genuine browser (a headless/automated
one gets an unsolvable interactive check). So the scraper drives a **real Chrome**
over the DevTools protocol, and by default runs it **managed**: each scrape
launches Chrome, autofills the login, reads the card, and closes Chrome. Nothing to
keep running: just set `LIDER_BCI_RUT` (+ `chanchito.LIDER_BCI_PASSWORD` in the
secret store) and it runs unattended.

Requirements: a genuine Google Chrome installed on the machine (not the Playwright
Chromium `make install` fetches) and a display (Chrome must be headed: Cloudflare
blocks headless), so this runs on a desktop session (macOS, Windows, a Linux
desktop), not inside the headless Docker container. The scraper looks in
`/Applications` on macOS; for `google-chrome`, `google-chrome-stable`,
`chromium`, `chromium-browser` and `chrome` on the `PATH`; and under
`%ProgramFiles%`, `%ProgramFiles(x86)%` and `%LocalAppData%` on Windows. Set
`LIDER_BCI_CHROME_PATH` if it's elsewhere. On a Raspberry Pi (no Google Chrome
for ARM64 Linux) it finds Debian's `chromium`, but whether Cloudflare's check
passes for it is unverified (see "Supported platforms"). The window is launched off-screen so
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

### Banco Falabella

The Banco Falabella scraper logs in with a headless Chromium once per run (RUT +
six-digit internet clave, no session kept between runs and no second factor). It
reads three CLP products:

- **CMR Mastercard** credit card: its cupo figures from the dashboard product
  card and its movements from the card-detail table (the unbilled tab and the
  current billed statement; older statements are not read yet).
- **Cuenta corriente**: its available and accounting balances from the
  dashboard and its latest movements from the account detail (older months are
  not read yet).
- **Línea de crédito**: its authorized, used and available amounts from the
  dashboard, plus the daily interest rate ("Tasa diaria") from its detail. Its
  movements are not read; draws show up on the cuenta corriente.

It runs on the host and in the Docker container alike.

The movements page is intermittently slow and can return an error with a
"Reintentar" button; the scraper retries each surface a few times and records
partial coverage (a warning) instead of failing the run when one never loads. An
idle session shows a "Tu sesion expirara" modal and logs out if it is not
answered; the scraper clicks "Continuar" to stay signed in. To watch the browser
while debugging, set `BANCO_FALABELLA_HEADLESS=0` (needs a display, so not in the
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
- **Banco Falabella**: every 24 hours
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
- **Rules**: a **Contiene** rule (the default) matches when the transaction
  description contains its keyword, ignoring upper and lower case (a plain
  substring: `%` and `_` have no special meaning). An **Exacta** rule matches
  only when the whole description equals its keyword, ignoring case and
  surrounding spaces; the rules table marks it with an "Exacta" badge. An exact
  match always wins over a contains match. Among rules of the same kind the
  highest priority wins; on equal priority the older rule wins, so a more
  specific keyword added later (say `uber eats` after `uber`) needs a higher
  priority. There can be only one exact rule per keyword. Matching folds case
  with PostgreSQL's `lower()`, so it follows the database's locale.
- **Probar** previews a keyword with its match type, before or after you save the rule: how many
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

**From a transaction**: when you change a transaction's category in its detail
panel, you can tick "Aplicar a todas las transacciones con esta descripción".
That creates an **Exacta** rule whose keyword is the transaction's original
(scraped) description, at the default priority: it wins over any contains rule
but never catches a longer description (an exact rule with that keyword already
there is reused and moved to the new category, keeping its priority). The other
transactions with exactly that description (ignoring case and surrounding
spaces) take the category right away, except the ones you categorized by hand;
this is the one case where a rule's category replaces one already set
automatically. The rule shows up on `/categories` like any other. Without the
tick, a recategorization only changes that transaction.

**Suggestions**: for transactions without a category, the dashboard suggests one
learned from the transactions you already categorized (by hand or by a rule).
Descriptions are compared without digits, accents, punctuation or filler words
(company suffixes such as "SpA" or "Ltda", "compra", "pago"); the same
description counts most, then descriptions sharing most of their words. A
suggestion only shows when one category clearly wins, with a short reason such
as "3 transacciones similares están en Supermercado". Nothing is applied on its
own:

- In the transaction detail panel, "Aplicar" categorizes that transaction (as a
  manual recategorization), "Aplicar siempre" creates the exact rule described
  above, and "Ignorar" hides the suggestion until the page reloads.
- The **Sugerencias** card on `/categories` lists the uncategorized
  transactions grouped by description. "Aplicar a estas N" categorizes those
  rows once (marked manual, no rule), "Aplicar siempre" creates the exact rule,
  and "Ignorar" stops suggesting that category for that description for good
  (a different suggested category still shows).

Transactions whose category you cleared by hand get no suggestions.

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
already flagged. The clearest pairs are linked on their own (see
"Automatic links" below); everything else waits for you.

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

**Automatic links**: loading the suggestions (the Transferencias page, or the
detail panel's hint) confirms every pair scoring at least 85 whose rows no
other suggestion uses, for example a same-day card payment with a transfer
keyword. A row with two possible partners, or a debit that also has a
one-sided suggestion, stays a manual choice, and one-sided suggestions are
never linked on their own. The transfer is flagged `auto_linked` and shows an
**Automática** badge in the transfer lists and the transaction detail panel.
An automatic link saves no pattern, so it never pushes the next one past the
bar. **Deshacer vínculo** (the undo arrow in the lists) deletes the transfer.
Deleting any transfer that linked both rows, automatic or not, dismisses that
pair so it is not linked again; editing a transfer's links by hand dismisses
the pair it had too, and makes an automatic transfer an ordinary one.

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
| GET/POST | `/api/monitors/[id]/adjustments` | Variaciones with their transaction and current effect (`?month=YYYY-MM`); link one from `{ transactionId }` (400 if the monitor does not reference its product, 409 if already linked) |
| GET | `/api/monitors/[id]/adjustments/candidates` | Transactions of the monitor's products (equation or thresholds) that can become variaciones (`?q=`, `?month=YYYY-MM`, `?limit=`) |
| DELETE | `/api/monitors/[id]/adjustments/[adjustmentId]` | Unlink one variación (the transaction stays) |
| GET | `/api/institutions` | Institutions + nested products (balances included) + CLP subtotals |
| GET/PATCH | `/api/institutions/[slug]` | One institution, same shape as a list item; edit `name`, `kind`, `country`, `url` |
| GET/PATCH | `/api/institutions/[slug]/products/[product]` | One product (with its `defaultName`) plus its balance history, recent transactions and, for a stock, its daily closes (`prices`: `{date, price}` oldest first, empty without a ticker); edit `name` (`null` resets it to `defaultName`), `slug`, `parentProductId`, `isActive`, `displayOrder` |
| PATCH | `/api/accounts/[id]` | Rename an account (`name`) |
| POST | `/api/institutions/refresh` | Trigger a scrape (all, or `{institution}`) |
| GET | `/api/scrapers` | Latest run per scraper |
| GET | `/api/scrapers/available` | Enabled scraper slugs, from the scraper service |
| GET/POST | `/api/transactions` | Transactions (list, create; create takes an optional `location`); the list filters by `month`, `from`/`to`, `productId`, `institution`, `categoryId` and `q` (every word in the original or the displayed description, ignoring case and accents) |
| GET/PATCH/DELETE | `/api/transactions/[id]` | One transaction with product, institution, category, location and notes; PATCH edits `description`/`customDescription`, `amount`, `transactionDate`, `scheduledMonth`, `categoryId`, `isInternalTransfer`, `productId`, `location` and `notes` (trimmed, max 2000 chars, blank or `null` clears); DELETE soft-deletes imported (scraped or CSV) rows and hard-deletes manual ones |
| POST | `/api/transactions/[id]/category-rule` | `{ categoryId }`: categorizes the row (manual), creates or reuses an exact rule keyed on its original description, and categorizes the other non-manual rows with that same description; returns `{ rule, ruleCreated, updatedCount }` |
| POST | `/api/import` | CSV import |
| GET | `/api/transactions/[id]/category-suggestion` | `{ suggestion }`: the suggested category (`categoryId`, `categoryName`, `confidence`, `matchCount`, `exact`, `reason`) of an uncategorized row, or `null` |
| GET/POST/PUT | `/api/categories` | Categories with their rules and transaction counts; create a category; apply the rules to uncategorized transactions (PUT) |
| PATCH/DELETE | `/api/categories/[id]` | Update or delete a category (its transactions become uncategorized, its rules are deleted) |
| POST | `/api/categories/rules` | Create a keyword rule (`matchType`: `contains`, the default, or `exact`) |
| PATCH/DELETE | `/api/categories/rules/[id]` | Update or delete a rule |
| GET | `/api/categories/suggestions` | `{ suggestions }`: uncategorized rows grouped by description with their suggested category, count, latest date and a sample transaction id; dismissed ones left out |
| POST | `/api/categories/suggestions/apply` | `{ description, categoryId }`: categorizes (manual) the uncategorized rows with that description once; returns `{ updatedCount }` |
| POST | `/api/categories/suggestions/dismiss` | `{ description, categoryId }`: stops suggesting that category for that normalised description |
| GET | `/api/categories/rules/preview` | Transactions a keyword would match (`?keyword=`, optional `&matchType=exact`) |
| GET/POST/PUT/DELETE | `/api/fixed-expenses` | Budget items CRUD (Presupuesto); `kind` is `fixed` (default), `estimated` or `savings` |
| GET/PUT | `/api/budget-settings` | `{ monthlyIncome }`: the monthly income the budget splits (0 until saved); PUT takes a whole, non-negative CLP amount |
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
# store it in the secret store alongside the scraper secrets
make secret-set KEY=DASHBOARD_PASSWORD
# ...and export it for one deploy only (load-secrets.sh skips it, see below).
# The subshell scopes the password to `make up`; a missing secret stops it.
( dashboard_password=$(make -s secret-export KEY=DASHBOARD_PASSWORD) &&
  eval "$dashboard_password" && make up )

# or export it yourself (Compose passes it through to the web service)
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
`DASHBOARD_PASSWORD`, so storing it in the secret store for deploys never turns
the login on locally; production reads it from the environment (Compose passes it
through), never from the store. To exercise the login locally, opt in per run:

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
# host-dev: both variables, scoped to this one command (a subshell); a missing
# secret stops it before `make dev` starts
( dashboard_password=$(make -s secret-export KEY=DASHBOARD_PASSWORD) &&
  eval "$dashboard_password" && DASHBOARD_HOST=0.0.0.0 make dev )

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
