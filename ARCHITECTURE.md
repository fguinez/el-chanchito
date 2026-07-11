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
│  │  categories | balances | month-reset                    │   │
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
│  product_balances | transactions | categories | category_rules  │
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
│  │   │   transactions + product_balances + scraper_runs  │   │  │
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
        │         ScrapedBalance             │
        └──────────────┬─────────────────────┘
                       │
                       ▼
        ┌────────────────────────────────────┐
        │  DB Writer                         │
        │  - upsert_transactions()           │
        │  - upsert_balance()                │
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
        │  - wealth: reads snapshots         │
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

The core formula replicating the Excel "Planificacion" sheet:

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
products ──────────────┘      product_balances (history)
  id PK                         id PK
  account_id FK ──────>       < product_id FK
  parent_product_id FK (self:   balance NUMERIC(20,8)
    debit->checking,            as_of (unique w/ product_id)
    línea->cta.cte.)            source
  kind (checking|savings|
    vista|wallet|term_deposit|
    credit_card|debit_card|
    prepaid_card|line_of_credit|
    loan|mortgage|investment|
    crypto|other)
  name, currency
  current_balance NUMERIC     -- denormalized latest
  balance_as_of               -- last checked (bumps even if unchanged)
  credit_limit, external_ref
  details JSONB               -- kind-specific attributes
  is_active, display_order

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
  -- from product_balances


income_sources                internal_transfers
  id PK                         id PK
  name                          description
  monthly_amount                amount
                                from_product_id FK
                                to_product_id FK
scraper_runs                    transfer_date
  id PK                         status (pending|resolved)
  method                         (email|fintself|http_api|open_banking)
  institution                    (mach|mercadopago|tenpo|banchile|...)
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

## Scraper Architecture

Scrapers are split into **agnostic backends** (how we scrape) and
**institution scrapers** (what we scrape):

```
apps/scrapers/scrapers/
  base.py                  # BaseScraper + ScrapedTransaction/Balance
  backends/
    email.py               # ImapSession (shared login + NOOP keepalive),
                           # EmailPattern, fetch_transactions_for_pattern
    fintself.py            # run_fintself_scraper(bank_key, user, password)
  institutions/
    mach.py  mercadopago.py  tenpo.py       -> consume backends/email
    banchile.py                              -> consumes backends/fintself
    buda.py  fintual.py  bci_lider.py        -> self-contained (HTTP/stub)
```

Each institution scraper implements:

```python
class BaseScraper(ABC):
    method: str                                  # "email" | "fintself" | "http_api" | "open_banking"
    institution: str                             # "mach" | "banchile" | "buda" | ...
    scrape_transactions() -> list[ScrapedTransaction]
    scrape_balances() -> list[ScrapedBalance]
```

Both `method` and `institution` are stored per `scraper_runs` row.

`ScrapedTransaction`/`ScrapedBalance` carry `institution` (slug),
`product_kind`, and `currency`. The writer resolves the chain
institution → account → product (creating missing links; single-user:
everything attaches to the oldest user), so e.g. each Buda currency
becomes its own `crypto` product. `upsert_balance` always refreshes
`products.current_balance`/`balance_as_of` but appends a
`product_balances` history row only when the value changed.

| Institution | Method | Source | Auth | Schedule |
|---|---|---|---|---|
| `fintual` | `http_api` | REST API (`/api/goals`) | Web session + e-mail 2FA (cached; `make fintual-login`) | 6h |
| `buda` | `http_api` | REST API | HMAC-SHA384 signed requests | 1h |
| `banchile` | `fintself` | Browser (fintself/Playwright) | RUT + password | 24h |
| `mach` | `email` | IMAP (Gmail) | Shared IMAP session | 30m |
| `mercadopago` | `email` | IMAP (Gmail) | Shared IMAP session | 30m |
| `tenpo` | `email` | IMAP (Gmail) | Shared IMAP session | 30m |
| `bci_lider` | `open_banking` | Stub (open-banking-chile) | RUT + password | - |

The three email-based scrapers reuse one `ImapSession`: it runs `NOOP` on
each acquire and only re-logs-in when the mailbox has been dropped.

### Deduplication

Transactions are deduplicated via `UNIQUE(product_id, external_id)`:

- Fintual: no transactions (balance-only)
- Buda: `buda_{deposit/withdrawal_id}`
- BanChile: `bch_{md5(date|description|amount|account_id)[:16]}` (fintself's account_id)
- Email: `email_{institution}_{hash(message_id)}`
- CSV: `csv_{base64url(date|description|amount)[:24]}`

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 16 (App Router) + React 19 + TypeScript |
| UI | Tailwind CSS 4 + shadcn/ui (New York) + Recharts |
| ORM | Drizzle ORM |
| Database | PostgreSQL 16 (Alpine) |
| Scrapers | Python 3.12 + httpx + fintself + APScheduler |
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
│   └── db-schema/                   # Shared SQL migrations
│       ├── migrations/              # V001 through V009
│       └── migrate.mjs             # Migration runner
│
├── docker-compose.yml
├── Makefile
├── USAGE.md
└── ARCHITECTURE.md
```
