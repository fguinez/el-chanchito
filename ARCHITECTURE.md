# Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                        Browser (User)                           │
│                     http://localhost:3000                        │
└─────────────────────┬───────────────────────────────────────────┘
                      │
                      ▼
┌─────────────────────────────────────────────────────────────────┐
│                    Dashboard (Next.js 16)                        │
│                                                                 │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐   │
│  │  Inicio  │  │Planning  │  │ History  │  │   Settings   │   │
│  │  (Home)  │  │ (Table)  │  │ (Chart)  │  │  (Config)    │   │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └──────┬───────┘   │
│       │              │              │               │           │
│       ▼              ▼              ▼               ▼           │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │                   API Routes (/api/*)                    │   │
│  │  budget | planning | transactions | wealth | scrapers   │   │
│  │  fixed-expenses | income-sources | transfers | import   │   │
│  │  categories | balances | month-reset | institutions     │   │
│  │  institutions/refresh (→ scraper control endpoint)      │   │
│  └────────────────────────┬────────────────────────────────┘   │
│                           │                                     │
│  ┌────────────────────────┴────────────────────────────────┐   │
│  │              Drizzle ORM + budget-engine.ts              │   │
│  └────────────────────────┬────────────────────────────────┘   │
└───────────────────────────┼─────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                    PostgreSQL 16 (Alpine)                        │
│                     port 5435 (host)                             │
│                                                                 │
│  users | institutions | accounts | products                     │
│  product_snapshots | transactions | categories | category_rules │
│  budget_configs | budget_adjustments | wealth_snapshots          │
│  fixed_expenses | income_sources | internal_transfers           │
│  scraper_runs                                                   │
└───────────────────────────▲─────────────────────────────────────┘
                            │
                            │ writes directly (psycopg3)
                            │
┌───────────────────────────┼─────────────────────────────────────┐
│                  Scrapers (Python 3.12)                          │
│                                                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │                   APScheduler                             │  │
│  │                                                           │  │
│  │   ┌─────────┐ ┌──────┐ ┌────────┐ ┌─────────────────┐ │  │
│  │   │ Fintual │ │ Buda │ │BanChile│ │ MACH / MP / Tenpo│ │  │
│  │   │  (6h)   │ │ (1h) │ │ (24h)  │ │ shared IMAP 30m  │ │  │
│  │   └────┬────┘ └──┬───┘ └────┬───┘ └────────┬─────────┘ │  │
│  │        │            │            │               │        │  │
│  │        ▼            ▼            ▼               ▼        │  │
│  │   ┌──────────────────────────────────────────────────┐   │  │
│  │   │              DB Writer (upsert)                   │   │  │
│  │   │   transactions + product_snapshots + scraper_runs │   │  │
│  │   └──────────────────────────────────────────────────┘   │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## Data Flow

```
                    ┌──────────────┐
                    │  Bank APIs   │
                    │  & Websites  │
                    └──────┬───────┘
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        ┌──────────┐ ┌─────────┐ ┌──────────┐
        │ Fintual  │ │  Buda   │ │ fintself │
        │   API    │ │  API    │ │(browser) │
        └────┬─────┘ └────┬────┘ └────┬─────┘
             │             │           │
             ▼             ▼           ▼
        ┌────────────────────────────────────┐
        │         ScrapedTransaction         │
        │         ScrapedProduct             │
        │   (pydantic envelopes from         │
        │    packages/product-model)         │
        └──────────────┬─────────────────────┘
                       │
                       ▼
        ┌────────────────────────────────────┐
        │  DB Writer                         │
        │  - upsert_transactions()           │
        │  - upsert_product()                │
        │  - start/finish_scraper_run()      │
        │  - ON CONFLICT DO NOTHING (dedup)  │
        └──────────────┬─────────────────────┘
                       │
                       ▼
        ┌────────────────────────────────────┐
        │           PostgreSQL               │
        └──────────────┬─────────────────────┘
                       │
                       ▼
        ┌────────────────────────────────────┐
        │  Dashboard API (Next.js)           │
        │  - planning: reads budget_configs  │
        │    + transactions + balances       │
        │    -> computes expected vs real    │
        │  - wealth: reads product_snapshots │
        │    -> computes derived metrics     │
        └──────────────┬─────────────────────┘
                       │
                       ▼
        ┌────────────────────────────────────┐
        │          Browser (React)           │
        │  - TodayStatus widget              │
        │  - Planning table                  │
        │  - Wealth chart (Recharts)         │
        └────────────────────────────────────┘
```

## Budget Engine

The core formula behind the "Planificacion" table:

```
presup_diario = variable_budget / days_in_month
cupo_tc_mes = credit_card_limit - future_debts

expected_balance(day) =
    cupo_tc_mes
  - presup_diario * day
  + checking_initial_balance
  + sum(adjustments[1..day])

drift = real_balance - expected_balance
```

```
drift > 0  =>  under budget (good)
drift < 0  =>  over budget (overspending)
drift = 0  =>  exactly on track
```

## Database Schema (ER Diagram)

Core hierarchy (since V009): `users -> accounts -> products`. "Account" always
means the user's enrollment at one institution (what you log into); the
money-holding elements are **products** (schema.org/FinancialProduct).
Per-kind product data (since V011) lives in two JSONB payloads typed by the
shared registry (`packages/product-model`): `attributes` (slow-changing
identity/config, shallow-merged on write) and `metrics` (per-scrape
observation; history in `product_snapshots`, latest denormalized on the
product).

```
users ────────────────┐
  id PK               │
  name, email          │
                       │
institutions ─────────┤       accounts
  id PK               ├──────<  id PK
  slug (unique)        │        user_id FK
  name                 │        institution_id FK
  kind (bank|fintech|  │        name ('Personal')
    exchange|          │        is_active, display_order
    asset_manager|     │
    other)             │
                       │
products ──────────────┘      product_snapshots (history)
  id PK                         id PK
  account_id FK ──────>       < product_id FK
  parent_product_id FK (self:   balance NUMERIC(20,8) -- headline
    debit->checking,            metrics JSONB ('{}' pre-V011)
    línea->cta.cte.)            as_of (unique w/ product_id)
  kind (checking|savings|       source
    vista|wallet|term_deposit|
    credit_card|debit_card|
    prepaid_card|line_of_credit|
    loan|mortgage|investment|
    crypto|other)
  name, currency, external_ref
  attributes JSONB            -- typed identity/config, shallow-merged
  metrics JSONB               -- latest typed observation
  current_balance NUMERIC     -- denormalized headline (metrics.headline())
  balance_as_of               -- last checked (bumps even if unchanged)
  is_active, display_order
  UNIQUE uq_products_identity
    (account_id, kind, currency,
     COALESCE(external_ref, ''))

transactions ──────────┐
  id PK                │      categories
  product_id FK ───────┘        id PK
  description                   name
  amount (int, CLP)             parent_id FK (self)
  transaction_date              color, icon
  category_id FK ─────────────>
  scheduled_month             category_rules
  source                        id PK
  external_id (unique w/       keyword
    product_id)                 category_id FK ──────>
  is_internal_transfer
  is_manually_categorized


budget_configs                budget_adjustments
  id PK                         id PK
  month (unique) ─────────────< budget_config_id FK
  variable_budget               adjustment_date
  fixed_budget                  amount
  credit_card_limit             description
  checking_initial_balance
  salary
  shared_expenses_ratio
  day_start


wealth_snapshots (legacy)     fixed_expenses
  id PK                         id PK
  snapshot_date (unique)        name
  patrimonio                    amount
  deuda                         is_shared
  fintual_balance               shared_ratio
  mercadopago_balance           active_from/to
  banchile_savings
  -- pre-V009 totals + manual entries;
  -- /api/wealth now derives the series
  -- from product_snapshots


income_sources                internal_transfers
  id PK                         id PK
  name                          description
  monthly_amount                amount
                                from_product_id FK
                                to_product_id FK
scraper_runs                    transfer_date
  id PK                         status (pending|resolved)
  method                         (email|fintself|web|http_api|open_banking)
  institution                    (mach|mercadopago|tenpo|banchile|bci_lider|...)
  started_at
  finished_at
  status
  transactions_imported
  error_message
```

## Migrations

| File | Tables |
|---|---|
| `V001__accounts_transactions_categories.sql` | accounts, categories, transactions |
| `V002__budget_configs.sql` | budget_configs, budget_adjustments |
| `V003__wealth_snapshots.sql` | wealth_snapshots |
| `V004__fixed_expenses_income.sql` | fixed_expenses, income_sources, internal_transfers |
| `V005__scraper_runs.sql` | scraper_runs |
| `V006__account_balances.sql` | account_balances |
| `V007__category_rules.sql` | category_rules + seed default categories |
| `V008__scraper_runs_method_institution.sql` | splits `scraper_runs.scraper_name` into `method` + `institution` |
| `V009__users_institutions_products.sql` | users, institutions, accounts (enrollments); old accounts renamed to products (UUIDs preserved); account_balances -> product_balances (history); wealth backfill |
| `V010__retire_legacy_banchile_savings_product.sql` | deletes the legacy BdC savings product V009 created for the wealth backfill (double-counted net worth) |
| `V011__typed_product_attributes_and_snapshots.sql` | products gain attributes/metrics JSONB (details + credit_limit dropped, revolving metrics seeded); uq_products_identity; product_balances -> product_snapshots (adds metrics) |
| `V012__retire_fintual_aggregate_product.sql` | deactivates the summed Fintual product + drops its snapshots (replaced by per-goal products) |
| `V013__retire_banchile_summed_inversiones_products.sql` | deactivates the summed BanChile term_deposit + investment products + drops their snapshots (replaced by per-holding products) |

## Scraper Architecture

Scrapers are split into **agnostic backends** (how we scrape) and
**institution scrapers** (what we scrape):

```
apps/scrapers/scrapers/
  base.py                  # BaseScraper; re-exports the ScrapedProduct /
                           # ScrapedTransaction envelopes from product_model
  backends/
    email.py               # ImapSession (shared login + NOOP keepalive),
                           # EmailPattern, fetch_transactions_for_pattern
    fintself.py            # run_fintself_scraper(bank_key, user, password)
    banchile_web.py        # own BdC Playwright login -> fetch_balances()
    bci_lider_web.py       # real Chrome over CDP -> scrape_card() / save_login_session()
  institutions/
    mach.py  mercadopago.py  tenpo.py       -> consume backends/email
    banchile.py                              -> fintself (tx) + banchile_web (balances)
    bci_lider.py                             -> bci_lider_web (one CDP drive: tx + balances)
    buda.py  fintual.py                      -> self-contained (HTTP APIs)
```

BanChile is a hybrid: transactions come from `fintself`, but `fintself` never
exposes a balance, so `scrape_products()` runs a **second, self-contained
Playwright login** (`backends/banchile_web.py`) and reads the "Mis Productos"
dashboard plus three detail routes. It replicates fintself's
`channel="chromium"` new-headless workaround (BdC serves a degraded page to the
default headless shell) and polls for the balance widget (it loads via a later
XHR). Because transactions and products are independent legs in `run_scraper`,
a fintself timeout never blocks the products leg (and a product-scrape crash
is swallowed into a run warning, never raised).

Five surfaces feed BanChile's typed products: the dashboard (CLP + USD
`checking` — the card row there is a static placeholder, so it's skipped), the
card detail page (CLP "Nacional" + USD "Internacional" `credit_card` metrics:
`available`/`limit`/`owed` from Disponible / Cupo total / Utilizado, plus the
masked `last4` attribute), the línea detail page (`line_of_credit` metrics from
Monto autorizado / Saldo disponible / Monto utilizado), the depósitos a plazo
listing (one `term_deposit` per deposit, identity and figures read from each
card's "VER DETALLE" aside), and the fondos mutuos listing (one `investment`
per fund from the cards, with the "Acerca del fondo" aside enriching the
variation percentages best-effort). Every figure is anchored on a literal
`$`/`USD` label, so a missing/changed layout records nothing rather than a
wrong number. The portal intermittently serves slow pages, so each surface is
read with bounded retries (three attempts with escalating render budgets,
pausing and recovering to the portal home in between); a surface that still
yields nothing becomes a run warning instead of a silent gap. If per-holding
parsing stays unusable on the final attempt, the depósitos/fondos surfaces
fall back to a single summed roll-up per kind (the listing header total),
shaped like the products issue #36 retired; the DB writer keeps the roll-up
and per-holding representations mutually exclusive so neither double-counts.

BCI Lider (Tarjeta Lider Bci, the retailcard.cl card co-branded by BCI) has no
open-banking API for individuals and isn't covered by `fintself`, and its login
sits behind a Cloudflare Turnstile that passes invisibly only for a *genuine*
browser: a Playwright-launched Chromium (headless or headed) gets an unsolvable
interactive "Verifique que es un ser humano" check, and a captured session doesn't
survive headless reuse (the auth token lives in tab-scoped sessionStorage and
Cloudflare rebinds on a fresh browser). So both sign-in and scraping drive a *real*
Chrome over CDP: an ordinary Chrome on a debug port (`_launch_real_chrome`, not a
Playwright browser), driven to autofill the RUT + clave and submit once Turnstile
clears (invisibly, or after the human ticks the check, which we never do). By
default `scrape_card` runs *managed*: it launches a headed Chrome off-screen (with
a visible-window fallback so it doesn't flash on the desktop), signs in, scrapes,
and closes it, so scheduled runs are fully unattended (needs a machine with a
display: Cloudflare blocks headless). Setting `LIDER_BCI_CDP_URL` switches to *reuse*
mode, driving a long-running Chrome from `make bci-lider-login` instead.
Either way it reuses an already-signed-in tab or re-logs-in via autofill, and raises
a "run make bci-lider-login" error when Chrome is unreachable. Both legs share one
drive per cycle: `scrape_transactions` opens it and caches the result for
`scrape_products` (a session error is re-raised, never retried). The "Mi Tarjeta
-> Saldos" page yields the CLP "Nacional" and USD "Internacional" `credit_card`
metrics (`available`/`limit`/`owed` from Disponible / Autorizado / Utilizado,
plus the masked `last4` and card-name attributes), and the "Movimientos" page
yields the Nacionales (CLP) charges, paged. Figures are anchored on their
`$`/`US$` labels (a drift records nothing rather than a wrong number), and USD
balances convert to CLP via lib/rates' multi-currency FX.

**Balance conventions & net worth** are registry-driven: each kind's `role`
(asset/liability/none) and `balance_convention` (value/available/owed/units)
live in `packages/product-model` — see `packages/product-model/PRODUCTS.md`
for the per-kind field matrix — and `web/src/lib/networth.ts` just reads the
generated `KIND_INFO`. `products.current_balance` holds the kind's headline
metric (asset value/nav, *available* cupo for cards and líneas, amount *owed*
for loans, crypto *units*). Debt derivation prefers the institution-reported
`metrics.owed` (BanChile's Utilizado), falls back to `limit − available` when
the same observation carries both, uses `abs(balance)` only for kinds whose
convention *is* owed (loan/mortgage), and otherwise contributes zero — a card
with no metrics never guesses debt. `debit_card` counts nowhere — its money
lives in the parent `checking`. Everything is converted to CLP before summing
patrimonio/deuda.

Each institution scraper implements:

```python
class BaseScraper(ABC):
    method: str                                  # "email" | "fintself" | "web" | "http_api" | "open_banking"
    institution: str                             # "mach" | "banchile" | "buda" | ...
    scrape_transactions() -> list[ScrapedTransaction]
    scrape_products() -> ProductScrapeResult     # products + non-fatal warnings
```

Both `method` and `institution` are stored per `scraper_runs` row, and
`run_scraper` finishes the row as `success`, `partial` (both legs ran but a
scraper reported warnings, e.g. a BanChile surface that failed all its retries
or Fintual goals skipped for lacking an id; the warnings land in
`error_message`), or `error` (a leg raised).

`ScrapedTransaction`/`ScrapedProduct` are pydantic envelopes defined in
`packages/product-model`; both carry `institution` (slug), a kind, and
`currency` (plus optional `external_ref`, e.g. one product per Fintual
goal). The writer resolves the chain institution → account → product
(creating missing links via the `uq_products_identity` upsert;
single-user: everything attaches to the oldest user), so e.g. each Buda
currency becomes its own `crypto` product. `upsert_product`
shallow-merges `attributes`, always refreshes
`products.current_balance`/`metrics`/`balance_as_of`, and appends a
`product_snapshots` history row only when the metrics payload changed.

| Institution | Method | Source | Auth | Schedule |
|---|---|---|---|---|
| `fintual` | `http_api` | REST API (`/api/goals`) | Web session + e-mail 2FA (cached; `make fintual-login`) | 6h |
| `buda` | `http_api` | REST API | HMAC-SHA384 signed requests | 1h |
| `banchile` | `fintself` | Browser (fintself/Playwright) | RUT + password | 24h |
| `mach` | `email` | IMAP (Gmail) | Shared IMAP session | 30m |
| `mercadopago` | `email` | IMAP (Gmail) | Shared IMAP session | 30m |
| `tenpo` | `email` | IMAP (Gmail) | Shared IMAP session | 30m |
| `bci_lider` | `web` | Real Chrome over CDP (`bci_lider_web`) | Autofill in a real Chrome (managed by default; reuse via `LIDER_BCI_CDP_URL` + `make bci-lider-login`; Cloudflare Turnstile) | 24h |

The three email-based scrapers reuse one `ImapSession`: it runs `NOOP` on
each acquire and only re-logs-in when the mailbox has been dropped.

### Product model

`packages/product-model` is the single source of truth for product kinds: a
pydantic v2 registry declaring, per kind, the attribute/metric classes plus
`role`, `balance_convention` and `label_es`. `make product-model-generate`
emits the derived artifacts — `generated/index.ts` (per-kind TS types,
`KIND_INFO`, `ASSET_KINDS`/`LIABILITY_KINDS`, consumed by `db/schema.ts`,
`networth.ts` and the institutions page), `generated/product-model.schema.json`
(the JSON Schema wire contract) and `PRODUCTS.md` (the per-kind field matrix
with placement rationale). A codegen drift test under `make test-py`
regenerates to a temp dir and diffs against the committed output, so the
artifacts can't silently fall behind the registry. The `ScrapedProduct` /
`ScrapedTransaction` envelopes are that wire contract in motion: today they
travel in-process from scraper to `db/writer.py`, and the plan is to put the
same validation behind a REST ingest API so non-Python scrapers can submit
data without direct DB access.

### Deduplication

Transactions are deduplicated via `UNIQUE(product_id, external_id)`:

- Fintual: no transactions (balance-only)
- Buda: `buda_{deposit/withdrawal_id}`
- BanChile: `bch_{md5(date|description|amount|account_id)[:16]}` (fintself's account_id)
- BCI Lider: `bcl_{md5(date|description|amount|CLP)[:16]}` (no per-movement id in the DOM)
- Email: `email_{institution}_{hash(message_id)}`
- CSV: `csv_{base64url(date|description|amount)[:24]}`

### On-demand refresh (control endpoint)

Scrapers normally run only on their APScheduler intervals. The scheduled service
(`SCRAPER_MODE=scheduled`) also starts a tiny stdlib `http.server` **control
endpoint** when `SCRAPER_CONTROL_PORT` is set, so the dashboard can force an
immediate scrape:

```
Browser → web POST /api/institutions/refresh {institution?}
        → (SCRAPER_CONTROL_URL) scrapers POST /refresh[/{slug}]
        → scheduler.get_job(slug).modify(next_run_time=now)  ⇒ 202 Accepted
```

- Endpoints: `POST /refresh` (all), `POST /refresh/{slug}` (one, `404` if not a
  configured scraper), `GET /scrapers` (the enabled scraper slugs; the dashboard
  uses it to decide which refresh buttons to enable), `GET /health`.
- Triggering just moves a job's next run time to now, so it reuses each job's
  `coalesce=True` / `max_instances=1` guards — a manual trigger can't overlap a
  scheduled or in-flight run of the same institution. The HTTP call returns `202`
  immediately; the scrape runs asynchronously on the scheduler's event loop.
- The server runs on a daemon thread and binds `0.0.0.0` inside the container.
  It's an **unauthenticated** trigger — keep it internal (Compose `expose`s port
  `8080` on the private network; never publish it — see #23). The web proxy
  returns `503` when it's unreachable so the UI can explain the outage.

| Var | Service | Meaning |
|---|---|---|
| `SCRAPER_CONTROL_PORT` | scrapers | Port the control server binds (unset ⇒ disabled) |
| `SCRAPER_CONTROL_URL` | web | Base URL the refresh proxy calls (e.g. `http://scrapers:8080`) |

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 16 (App Router) + React 19 + TypeScript |
| UI | Tailwind CSS 4 + shadcn/ui (New York) + Recharts |
| ORM | Drizzle ORM |
| Database | PostgreSQL 16 (Alpine) |
| Scrapers | Python 3.12 + httpx + fintself + Playwright + APScheduler |
| DB Driver (Python) | psycopg3 + psycopg-pool |
| Containerization | Docker + Docker Compose |
| Package Manager | pnpm (workspaces) |

## Monorepo Structure

```
el-chanchito/
├── apps/
│   ├── web/                          # Next.js dashboard
│   │   ├── src/
│   │   │   ├── app/
│   │   │   │   ├── (dashboard)/      # All pages with sidebar layout
│   │   │   │   │   ├── page.tsx      # Home
│   │   │   │   │   ├── planning/     # Planificacion
│   │   │   │   │   ├── history/      # Historial
│   │   │   │   │   ├── institutions/ # Instituciones + productos
│   │   │   │   │   ├── expenses/     # Gastos + CSV import
│   │   │   │   │   ├── fixed/        # Gastos fijos
│   │   │   │   │   ├── transfers/    # Movimientos internos
│   │   │   │   │   └── settings/     # Config + split calculator
│   │   │   │   └── api/              # 13 API route groups
│   │   │   ├── components/
│   │   │   │   ├── dashboard/        # ScraperStatus, CsvImport
│   │   │   │   ├── layout/           # Sidebar
│   │   │   │   └── ui/              # shadcn components
│   │   │   └── lib/
│   │   │       ├── budget-engine.ts  # Core formulas
│   │   │       ├── db/              # Drizzle schema + connection
│   │   │       └── utils.ts         # cn(), formatCLP()
│   │   └── Dockerfile
│   │
│   └── scrapers/                     # Python scraper service
│       ├── scrapers/                 # 5 scraper implementations
│       ├── db/                      # Connection pool + writer
│       ├── main.py                  # Entry point + scheduler
│       ├── requirements.txt
│       └── Dockerfile
│
├── packages/
│   ├── db-schema/                   # Shared SQL migrations
│   │   ├── migrations/              # V001 through V012
│   │   └── migrate.mjs             # Migration runner
│   └── product-model/               # Product-kind registry (pydantic v2)
│       ├── product_model/           # kinds, attributes, metrics, envelopes
│       ├── scripts/generate.py      # emits the derived artifacts
│       ├── generated/               # index.ts + product-model.schema.json
│       └── PRODUCTS.md              # generated per-kind field matrix
│
├── docker-compose.yml
├── Makefile
├── USAGE.md
└── ARCHITECTURE.md
```
