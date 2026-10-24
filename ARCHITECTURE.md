# Architecture

## System Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                         Browser (User)                          │
│                      http://localhost:3000                      │
└────────────────────────────────┬────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Dashboard (Next.js 16)                      │
│             (+ Gastos, Gastos Fijos, Transferencias)            │
│  ┌──────────┐  ┌───────────┐  ┌───────────┐  ┌───────────────┐  │
│  │  Inicio  │  │ Monitores │  │ Historial │  │ Instituciones │  │
│  │ (alerts) │  │ (charts)  │  │  (chart)  │  │  (products)   │  │
│  └────┬─────┘  └─────┬─────┘  └─────┬─────┘  └───────┬───────┘  │
│       │              │              │                │          │
│       ▼              ▼              ▼                ▼          │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                    API Routes (/api/*)                    │  │
│  │  monitors (+ adjustments) | institutions | wealth         │  │
│  │  transactions | import | fixed-expenses | transfers       │  │
│  │  categories | accounts | scrapers | auth                  │  │
│  │  institutions/refresh (→ scraper control endpoint)        │  │
│  └─────────────────────────────┬─────────────────────────────┘  │
│                                │                                │
│  ┌─────────────────────────────┴─────────────────────────────┐  │
│  │  Drizzle ORM + lib/monitors + networth.ts + rates.ts      │  │
│  └─────────────────────────────┴─────────────────────────────┘  │
└────────────────────────────────┼────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────┐
│                     PostgreSQL 16 (Alpine)                      │
│                      127.0.0.1:5435 (host)                      │
│                                                                 │
│  users | institutions | accounts | products | product_snapshots │
│  transactions | categories | category_rules | monitors          │
│  monitor_adjustments | fixed_expenses | internal_transfers      │
│  dismissed_transfer_suggestions | confirmed_transfer_patterns   │
│  scraper_runs | product_prices | wealth_snapshots (legacy)      │
└────────────────────────────────▲────────────────────────────────┘
                                 │
                                 │ writes directly (psycopg3)
                                 │
┌────────────────────────────────┼────────────────────────────────┐
│                     Scrapers (Python 3.12)                      │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                        APScheduler                        │  │
│  │                                                           │  │
│  │  ┌─────────┐ ┌──────┐ ┌───────────┐ ┌───────────────────┐ │  │
│  │  │ Fintual │ │ Buda │ │ BanChile  │ │ MACH / MP / Tenpo │ │  │
│  │  │  (6h)   │ │Skipo │ │ BCI Lider │ │  shared IMAP 30m  │ │  │
│  │  │         │ │ (1h) │ │ Santander │ │                   │ │  │
│  │  │         │ │      │ │ Falabella │ │                   │ │  │
│  │  │         │ │      │ │   (24h)   │ │                   │ │  │
│  │  └────┬────┘ └──┬───┘ └─────┬─────┘ └─────────┬─────────┘ │  │
│  │       │         │           │                 │           │  │
│  │       ▼         ▼           ▼                 ▼           │  │
│  │  ┌─────────────────────────────────────────────────────┐  │  │
│  │  │                  DB Writer (upsert)                 │  │  │
│  │  │  transactions + product_snapshots + scraper_runs    │  │  │
│  │  └─────────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## Data Flow

```
                  ┌────────────────┐
                  │   Bank APIs,   │
                  │  websites and  │
                  │ e-mail inboxes │
                  └────────┬───────┘
                           │
             ┌─────────────┼─────────────┐
             ▼             ▼             ▼
        ┌──────────┐ ┌───────────┐ ┌───────────┐
        │ Fintual  │ │ BanChile  │ │   MACH    │
        │Buda Skipo│ │ BCI Lider │ │ MP, Tenpo │
        │  (APIs)  │ │ Santander │ │  (IMAP)   │
        │          │ │ Falabella │ │           │
        │          │ │ (browser) │ │           │
        └────┬─────┘ └─────┬─────┘ └─────┬─────┘
             │             │             │
             ▼             ▼             ▼
        ┌────────────────────────────────────┐
        │         ScrapedTransaction         │
        │         ScrapedProduct             │
        │         ScrapedExchangeRate        │
        │         ScrapedPriceHistory        │
        │   (pydantic envelopes from         │
        │    packages/product-model)         │
        └──────────────────┬─────────────────┘
                           │
                           ▼
        ┌────────────────────────────────────┐
        │  DB Writer                         │
        │  - upsert_transactions()           │
        │  - upsert_product()                │
        │  - upsert_exchange_rate()          │
        │  - upsert_price_history()          │
        │  - start/finish_scraper_run()      │
        │  - ON CONFLICT DO NOTHING (dedup)  │
        └──────────────────┬─────────────────┘
                           │
                           ▼
        ┌────────────────────────────────────┐
        │           PostgreSQL               │
        └──────────────────┬─────────────────┘
                           │
                           ▼
        ┌────────────────────────────────────┐
        │  Dashboard API (Next.js)           │
        │  - monitors: evaluate expressions  │
        │    over products (+ ajustes) +      │
        │    replay history from              │
        │    product_snapshots -> status,     │
        │    margin, history                  │
        │  - wealth: reads product_snapshots │
        │    -> computes derived metrics     │
        └──────────────────┬─────────────────┘
                           │
                           ▼
        ┌────────────────────────────────────┐
        │          Browser (React)           │
        │  - Inicio: monitors in alert +     │
        │    scraper status                  │
        │  - Monitor cards + detail,         │
        │    Variaciones (adjustments)       │
        │  - Monitor and wealth charts       │
        │    (Recharts)                      │
        │  - Institution and product tables  │
        └────────────────────────────────────┘
```

## Monitors and variaciones

Monitors replaced the old budget engine (the Planificacion and Configuracion
pages, whose tables V016 dropped). A monitor is one stored equation
(`monitors`, V015): a left expression over product values compared against an
`alert` threshold and an optional `warning` one, each a comparator plus an
expression. The expression language (`apps/web/src/lib/monitors/expr.ts`) is
arithmetic over product references plus `DAY_OF_MONTH()` and
`DAYS_IN_MONTH()`, so a threshold can ramp through the month the way the old
daily budget did (the builder's "Rampa mensual" preset):

```
left:   banchile:checking:balance - banchile:credit-card:owed   (illustrative slugs)
alert:  <  1000000 - 30000 * (DAY_OF_MONTH() - 1)
```

Nothing is precomputed. `/api/monitors` evaluates every monitor on read against
the current products (currency-denominated references convert into the
monitor's currency via `lib/rates`), and `/api/monitors/[id]` replays one
monitor's history day by day from `product_snapshots`, carrying each product's
latest observation forward, the same way `/api/wealth` builds the net-worth
series (the list only replays a 30-day sparkline for line-chart monitors). A monitor's status is `ok`,
`warning`, `breached` or `no_data`; Inicio lists the ones in `warning` or
`breached`. References are stored in uuid form (`@{product_uuid:field}`) so a
product rename never breaks them, and the API round-trips them to the display
form (`institution:product:field`).

The old sheet's "Variaciones" live on as **monitor adjustments**
(`monitor_adjustments`, V022; links to transactions since V040). A variación
names one transaction its monitor ignores. The transaction's amount is signed
in its product's currency, and `lib/monitors/undo.ts` maps each product kind
to the metric fields a transaction moves and in which direction (asset-style
fields such as `balance`, `available` or `nav` undo with `-amount`; debt-style
`owed` undoes with `+amount`; limits, rates and counts do not move). On each
day, the variaciones whose `transaction_date` falls in that day's month, on
or before it, are undone on their products' raw values before the engine
converts the references into the monitor currency (same rates as the product
itself, so a USD purchase follows the live rate). The difference becomes a
per-threshold shift, and the value itself stays real:

```
L, T_i  = the left expression and threshold i; u = refs with the day's
          variaciones undone
shift_i = -[(L(u) - T_i(u)) - (L - T_i)]
threshold_i(day) = T_i(day) + shift_i

margin = value - threshold   (for < and <=; threshold - value for > and >=)
```

Comparing the real value against the shifted threshold is the same as
comparing everything on the undone values, so a threshold that reads an
undone product moves too. The shift is an exact difference, so signs and
coefficients (`- owed * 0.7`) come out right and same-day variaciones
combine; a transaction of a product the monitor does not read shifts
nothing, and a no-data side shifts nothing. If only the undo breaks an
expression (a division by zero), the monitor is `no_data` with that reason.
The evaluation's `adjustment` reports the alert threshold's shift; each
threshold carries its own. A soft-deleted transaction (V033) keeps its link
but never applies; a hard-deleted one takes its links with it. `adjustmentOnDate` in
`evaluate.ts` applies them inside `evaluateMonitor`, so the current
evaluation, the history replay, the list sparklines and the edit preview
(against the draft expression) all include them. Days are local
calendar days, the same unit as `DAY_OF_MONTH()`. The monitor detail page
manages them ("Variaciones" card) through `/api/monitors/[id]/adjustments`,
which reports each one's current effect on the alert threshold
(`lib/monitors/effects.ts`), and picks transactions of the products the left
expression or any threshold references from
`/api/monitors/[id]/adjustments/candidates`.

`lib/budget-engine.ts` survives only as two helpers: `calcWealthMetrics`
(Historial) and `calcPersonalAmount` (Gastos Fijos).

## Exchange rates

Every CLP figure of a non-CLP product (its "≈ CLP" balance, the institution
subtotals and net-worth totals, the `/api/wealth` series, monitor references)
converts with a rate resolved **per product** in `apps/web/src/lib/rates.ts`,
most specific first:

1. **product**: an `exchange_rates` row for that product and currency;
2. **institution**: a row of the product's institution with `product_id` NULL;
3. **provider**: `getClpRates()`, open.er-api fiat FX pivoted to CLP plus
   Buda's tickers for crypto, cached in memory for 5 minutes.

Skipo's scraper fills the institution level for the coins it holds: it
reads each coin's indicative SELL price in CLP from Skipo on every run, so a
coin Buda's tickers don't cover still converts.

`exchange_rates` (V024) holds the rates institutions publish themselves (a
bank's "Tipo de cambio" for its card), read by their scrapers: one current
value per (institution, product or NULL, base currency, quote currency), with
`rate` in quote units per base unit, a Spanish `source` label, the
`reported_as_of` validity date when the institution prints one, and
`observed_at`, which every scrape that reads the value moves forward. There is
no history: past balances are valued at today's resolved rate, as they were
with provider rates alone. The web reads only CLP-quoted rows.

Each request loads one `RateBook` (`lib/rate-book.ts` `getRateBook()`: the
provider rates and every override, in parallel) and converts a product with
`ratesFor(book, { productId, institutionSlug })`, which returns plain
`ClpRates`, so `toClp` and the `lib/networth` helpers are unchanged. A monitor
reference converts both legs of its cross rate with the referenced product's
rates. A failed `exchange_rates` read is logged and degrades to provider rates.

**Staleness**: an override is stale when its `observed_at` falls on an earlier
calendar day than now, both read in `America/Santiago` explicitly
(`isRateStale`, via `Intl` with a `timeZone`, so the process timezone does not
matter). A stale override is still used, never silently swapped for the
provider rate; the UI warns instead. Provider rates are never flagged.

The institutions and product endpoints return the rate behind each non-CLP
product as `product.exchangeRate` (`RateInfo`: `currency`, `rate`, `scope`
product/institution/provider, `source`, `observedAt`, `reportedAsOf`, `stale`;
null for CLP products and unconvertible currencies). The product page shows it
in a "Tipo de cambio" card with a "Desactualizado: no es de hoy" warning, and
the family tables mark a stale "≈ CLP" with an amber icon. The card has no
refresh of its own: the control server has no per-surface scoping, so an
institution rate is reread by the institution page's "Actualizar".

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
    stock|crypto|other)
  name, currency, external_ref
  default_name                -- scrapers' name; name follows it until renamed (V027)
  slug                        -- create-only, unique per institution (V014)
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
  amount (numeric(18,2))        parent_id FK (self)
  transaction_date              color, icon
  accounting_date (V019)
  category_id FK ─────────────>
  scheduled_month             category_rules
  source                        id PK
  external_id (unique w/       keyword
    product_id)                 category_id FK ──────>
  is_internal_transfer        -- set while a transfer links it, or by a scraper on insert
  is_manually_categorized
  location_name, _city,
    _country (nullable text)
  location_latitude,
    _longitude (numeric(9,6),
    both or neither)
  location_is_manual


monitors                      monitor_adjustments
  id PK ──────────────────────< monitor_id FK (cascade)
  name, description             id PK
  currency                      transaction_id FK (cascade)
  expression (uuid refs)          -> transactions.id
  thresholds JSONB              unique (monitor_id, transaction_id)
    [{severity, comparator,     -- the transaction, undone, shifts
      expression}]              --   every threshold to month end
  display JSONB
  is_active

wealth_snapshots (legacy)
  id PK
  snapshot_date (unique)
  patrimonio
  deuda
  fintual_balance
  mercadopago_balance
  banchile_savings
  -- pre-migration totals; the series
  -- is derived from product_snapshots
  -- (lib/wealth); POST only backdates
  -- before the derived series starts


fixed_expenses                internal_transfers
  id PK                         id PK
  name                          description
  amount                        amount
  is_shared                     from_product_id FK
  shared_ratio                  to_product_id FK
  active_from/to                from_transaction_id FK (V028)
                                to_transaction_id FK (V028)
                                  -- unique, ON DELETE SET NULL;
                                  -- the scraped legs it accounts for
                                transfer_date
                                status (pending|resolved)


dismissed_transfer_suggestions    confirmed_transfer_patterns
  id PK                             id PK
  source_transaction_id FK          source_product_id FK
  target_transaction_id FK            -- ON DELETE CASCADE
    -- NULL = one-sided; unique     target_product_id FK
    -- (NULLS NOT DISTINCT) pair;   description_prefix
    -- ON DELETE CASCADE              -- normalised; unique triple
  dismissed_at                      created_at


scraper_runs
  id PK
  method       (email|web|http_api|fintself|open_banking; last two on legacy rows)
  institution  (mach|mercadopago|tenpo|banchile|bci_lider|santander|...)
  started_at
  finished_at
  status       (running|success|partial|error)
  transactions_imported
  error_message


exchange_rates (V024)
  id PK
  institution_id FK (cascade)
  product_id FK (cascade; NULL = institution-wide)
  base_currency, quote_currency ('CLP')
  rate NUMERIC(20,8), > 0     -- quote units per 1 base
  source                      -- Spanish UI label
  reported_as_of              -- date the institution prints, if any
  observed_at                 -- last scrape that read it
  UNIQUE (institution_id, COALESCE(product_id, nil uuid),
          base_currency, quote_currency)


product_prices (V038)
  id PK
  product_id FK (cascade)
  price_date                  -- trading day: the daily bar's UTC date
  price NUMERIC(20,8), > 0    -- closing price per unit
  currency                    -- the product's, when written
  created_at, updated_at      -- updated_at moves only when the price changes
  UNIQUE (product_id, price_date)
```

New transactions are categorized in the database itself (V021): a `BEFORE
INSERT` trigger on `transactions` fills a missing `category_id` from
`category_rules` through `category_for_description()`, so every writer (the
scrapers' DB writer, CSV import, manual entry) gets it without code of its own.
Rows that arrive with a category, or flagged `is_manually_categorized`, are
left alone. The dashboard's "Categorizar ahora" (`PUT /api/categories`) runs
the same function over the rows that are still uncategorized. The transaction
detail panel can also turn a row's category into a rule
(`POST /api/transactions/[id]/category-rule`): one database transaction creates
or reuses a priority-1000 rule keyed on the raw `description` and recategorizes
the visible, non-manual rows with that exact description.

`exchange_rates` semantics, resolution order and staleness: see
[Exchange rates](#exchange-rates).

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
| `V014__product_slugs.sql` | products gain a create-only `slug`, unique per institution (backfilled from names) |
| `V015__monitors.sql` | monitors |
| `V016__retire_budget_and_income_tables.sql` | drops budget_configs, budget_adjustments, income_sources (the retired Planificacion/Configuracion pages) |
| `V017__bci_lider_transaction_ids_drop_description.sql` | re-keys BCI Lider transactions on date + amount (the description changes once a charge is billed) and collapses the duplicates |
| `V018__banchile_transaction_ids_adopt_operation_id.sql` | prepares BanChile transactions for operation-id keys; the writer re-keys stored rows on the next scrape |
| `V019__transactions_accounting_date.sql` | transactions gain a nullable `accounting_date` (the posting date, where the institution reports one) |
| `V020__buda_drop_crypto_movements_stored_as_clp.sql` | deletes the crypto movements the old Buda scraper truncated and stored as pesos (CLP movements only now) |
| `V021__auto_categorize_transactions.sql` | `category_for_description()` + a BEFORE INSERT trigger on transactions that fills a missing category from category_rules |
| `V022__monitor_adjustments.sql` | monitor_adjustments: dated variaciones that shift a monitor's thresholds until month end |
| `V023__transactions_amount_numeric.sql` | `transactions.amount` integer -> NUMERIC(18, 2) so foreign-currency movements keep their cents; existing values unchanged |
| `V024__exchange_rates.sql` | exchange_rates: the latest scraped rate per institution or product and currency pair |
| `V025__transactions_location.sql` | optional transaction location: name, city, country, a range-checked lat/lng pair, and `location_is_manual`; no backfill |
| `V028__internal_transfer_transaction_links.sql` | internal_transfers gain `from_transaction_id` / `to_transaction_id`: nullable, unique FKs (ON DELETE SET NULL) to the scraped rows a transfer accounts for; no backfill |
| `V029__seed_santander_institution.sql` | seeds the Banco Santander institution row (upsert, so it also fixes a row the writer auto-created) |
| `V030__seed_skipo_institution.sql` | seeds the `skipo` institution (display name, kind, site) |
| `V031__transfer_suggestions.sql` | dismissed_transfer_suggestions (rejected suggestions, null target = one-sided) and confirmed_transfer_patterns (products + description prefix that rank matching suggestions first); new tables only |
| `V032__auto_linked_transfers.sql` | internal_transfers gain `auto_linked` (default false): set on transfers the suggestions confirmed on their own, cleared when the user edits their links |
| `V033__transactions_soft_delete.sql` | transactions gain `deleted_at`: a deleted row with an `external_id` is soft-deleted (keeps its key, so a scrape cannot re-import it), manual rows are hard-deleted (see "Deleting a transaction") |
| `V037__seed_banco_falabella_institution.sql` | seeds the Banco Falabella institution row (upsert, so it also fixes a row the writer auto-created) |
| `V038__product_prices.sql` | product_prices: one daily closing price per product and trading day (the bar's UTC date), upserted by the scrapers and never pruned |
| `V039__stock_product_kind.sql` | admits the `stock` kind in products_kind_check and re-kinds Fintual's stock positions (`external_ref` `stock:%`) from `investment` to `stock`, rewriting the `kind` key of their attributes, metrics and snapshot metrics; goals stay `investment`; `products_stock_ref_kind_check` then rejects any `investment` row with a `stock:` ref, so a scraper still on the old code fails its writes instead of adding twins (stop the scrapers, migrate, restart scrapers and web) |
| `V040__monitor_adjustments_transactions.sql` | monitor_adjustments become links to transactions: deletes the free-form rows, drops `adjustment_date`/`amount`/`description`, adds `transaction_id` (FK, ON DELETE CASCADE, unique per monitor) |

## Scraper Architecture

Adding an institution? A step-by-step guide (the acquisition technique
ladder, an integration checklist, templates and a personal-data scan) lives in
`.claude/skills/new-institution-scraper/`: start at its `SKILL.md`, which reads
as plain Markdown and also works as a Claude Code skill.

Scrapers are split into **agnostic backends** (how we scrape) and
**institution scrapers** (what we scrape):

```
apps/scrapers/scrapers/
  base.py                  # BaseScraper; re-exports the ScrapedProduct /
                           # ScrapedTransaction / ScrapedExchangeRate /
                           # ScrapedPriceHistory envelopes from product_model
  backends/
    email.py               # ImapSession (shared login + NOOP keepalive),
                           # EmailPattern, fetch_transactions_for_pattern
    banchile_web.py        # own BdC Playwright login -> fetch_balances()
    banchile_movements.py  # BdC movements + the shared session -> fetch_session()
    banchile_fx.py         # BdC USD card exchange rate (fetch-only surface)
    bci_lider_web.py       # real Chrome over CDP -> scrape_card() / save_login_session()
    santander_web.py       # headless Santander login + the SPA's JSON -> fetch_session()
    banco_falabella_web.py # headless Banco Falabella login + the rendered DOM -> fetch_session()
    fintual_stocks.py      # Fintual's WalletMovements query + pure ledger parsers (no I/O)
  institutions/
    mach.py  mercadopago.py  tenpo.py       -> consume backends/email
    banchile.py                              -> banchile_movements (one login: tx + balances)
    bci_lider.py                             -> bci_lider_web (one CDP drive: tx + balances)
    santander.py                             -> santander_web (one headless login: tx + balance)
    banco_falabella.py                       -> banco_falabella_web (one headless login: tx + balance)
    fintual.py                               -> fintual_stocks (one web session: tx + balances)
    buda.py  skipo.py                        -> self-contained (HTTP APIs)
```

BanChile drives **one self-contained Playwright session per run** (issue #57,
which folded in #28). `scrape_transactions()` opens it, reads the balances
(`backends/banchile_web.py`: the "Mis Productos" dashboard plus four detail
routes) and the movements (`backends/banchile_movements.py`: the checking
cartola behind its account dialog, and the card's unbilled and billed legs),
and caches the products half for `scrape_products()` to serve. It uses the
`channel="chromium"` new-headless workaround (BdC serves a degraded page to the
default headless shell) and polls for each widget, since they load via later
XHRs. Because transactions and products are independent legs in `run_scraper`,
a session that crashes still leaves the products leg able to open a
balance-only session of its own, through the same session cache (and a
product-scrape crash is swallowed into a run warning, never raised).

Both sessions authenticate through `banchile_web._authenticated_session`, which
caches the web session between runs (issue #28): Playwright's `storage_state` in
`BANCHILE_SESSION_FILE` (default `apps/scrapers/.banchile_session.json`,
`/data` in Compose; written 0600, gitignored). A restored session counts only
after one portal-home navigation renders the checking widget; any miss (no or
corrupt file, restore error, dead session) deletes the file and runs the full
RUT + password login. The state is saved again after the reads to keep rotated
cookies. Verified live: a restored session survives back-to-back runs (every
surface read, movements included, so it's a real authenticated hit even though
`storage_state` carries no sessionStorage) but not an idle gap of several
minutes, where validation reports a miss and the full login runs. So hits are
realistic only shortly after the previous run: a manual refresh shortly after
one, a service restart shortly after one, and the products-leg fallback within
a run.

The bank throttles full logins, so there's at most one per
`BANCHILE_LOGIN_COOLDOWN_MINUTES` window (default 10, `0` disables) whatever
triggered the run (scheduled, manual, startup or the products-leg fallback). A
cache miss inside the window deletes the file and raises `LoginCooldownError`
instead of calling `_login`, and a run that would certainly need that login (an
attempt in the window and no session file, `login_cooldown_retry_after`) raises
it before launching Chromium. A failed attempt counts too, since a quick retry
is exactly what gets throttled. `BanChileScraper` logs the skip as a warning:
the transactions leg re-raises it, so the run is still recorded as an error
(`run_scraper` logs that generically, traceback included), and the products leg
turns it into a run warning. The last attempt is claimed atomically but lives
in process memory, so a service restart resets the window, and a
`SCRAPER_MODE=once` / `make scrapers-once` run alongside the service isn't
gated against it.

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

One more surface reads the rate the bank bills the USD card's debt at
(`banchile_fx.py`, "tipo de cambio"): the "Tipo de cambio" of the international
card payment page, which is neither lib/rates' market reference nor the
observed dollar of the home's indicators. It is fetch-only and runs between the
product and movement surfaces: one in-page script GETs the product selector,
builds the body of the payment page's display call
(`tarjeta-credito-digital/pago/calcular-pago-internacional`) from it and POSTs
it, returning only the rate, its date and a status code, so the customer's
name, RUT and card id never reach Python, logs or warnings. It never routes to
the payment pages or calls a payment endpoint. The rate becomes a
product-scoped `ScrapedExchangeRate` for the USD card, carried on
`BanChileSessionResult.exchange_rates` (not merged into the products); a read
that never yields a usable rate is a failed surface like the others. The
balance-only fallback does not read it.

BanChile is also the only source that reports two dates per movement, so
`transactions` carries both (V019): `transaction_date` is when the movement
occurred (`fecha`, and what every chart is built on) and the nullable
`accounting_date` is when the bank posted it (`fechaContable`). They differ for
most of a typical window. A movement with only a posting date carries it in
both; an occurrence date is never invented. Neither is ever part of a dedup key.

Its card legs also say where a purchase happened, which lands in the optional
location columns (V025): the unbilled leg's `ciudad` and `codigoPaisComercio`
(a country NAME despite the key), the national statement's `ciudad`, and the
USD statement's `ciudadComercio` and `pais`. Values are stored as the portal
spells them (trimmed, not re-cased) and are never identity. The writer inserts
a location with its row and, on a row already stored, only fills NULL location
columns; a location the user set or cleared in the dashboard
(`location_is_manual`) is never touched by a scrape.

Three more surfaces feed its transactions (`banchile_movements.py`, issue #57):
the checking cartola, reached by driving the account-selection dialog that
defaults to the USD account (it opens by itself only until an account is
chosen; the SPA then remembers that account for the browser session and the
route shows its cartola directly, so later drives reopen the dialog with the
"SELECCIONAR OTRA CUENTA" button once that view has settled, and skip the
remembered account's automatic `getCartola`: a payload whose `moneda` or account
is not the drive's is never read); the card's unbilled movements, which the SPA
loads when the card page opens (from home it first waits, bounded, for the
dashboard's checking row: a click on a dashboard still rendering is lost); and
the card's billed statements, the newest two,
read by replaying the SPA's own statement request with a different
`fechaFacturacion`. Only calls whose body is fully known are composed (the
per-movement `cartola/detalle-glosa` that carries the operation id, spaced and
bounded); everything else is captured from the portal's own traffic, because the
card endpoints take a descriptor whose derivation was never observed. For the
same reason `getCartola` paging is not implemented: a second page cannot be
composed, so a window the bank reports as truncated (`pagina[0].masPaginas`) is
logged as a warning rather than passing unnoticed. All interpretation lives in
pure helpers over raw payload dicts, and the movement surfaces use the same
bounded-retry, non-fatal machinery as the product ones, except that an empty
reading counts as success there: a card with no unbilled charges is not a
failed surface.

The USD products get their movements the same way (issue #79): the unbilled
list mixes both currencies and `origenTransaccion == "INT"` marks the USD rows;
the USD statement (`estadocuenta/internacional/resumen-por-fecha`) is a surface
of its own, and so is the USD checking cartola (issue #82: the same dialog with
USD picked and the account radio found by its "(USD)" label). A customer with
no USD account (no "(USD)" option in the currency list) reads as an empty list
rather than a failed surface, a USD dialog that fails after offering the option
(no "(USD)" radio, Aceptar, the cartola wait) fails only that surface, and
every dialog step logs which one failed and why. The USD cartola's row shape
is pending live verification
(the account observed had no movements), so its amounts are parsed defensively
(numbers or strings, cents kept).
Every movement carries its currency, so the writer resolves the USD product,
and USD amounts keep their cents. Before #79 the unbilled USD charges were
stored on the CLP card as whole "pesos"; each run deletes those leftovers once
their USD rows are stored (`superseded_transactions` ->
`db/writer.py::delete_superseded_transactions`, gated on the keys
`write_transactions` reports as persisted, so a replacement that failed to write deletes
nothing): by their exact old key when it is reproducible, else by date and old
amount (only for a USD statement row whose statement the national leg also read
in that scrape, with both CLP card legs loaded, since the two legs pick their
statements independently), one row per USD charge, only among rows created
before V023 was applied (`_migrations.applied_at`; V023 ships with the fix),
and never a row the user categorised, annotated or paired.

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

Banco Santander has no API for individuals (Chile's Open Finance system is not
operating yet), `fintself` reads its rendered DOM without movement ids, and its
login runs behind Akamai's edge with BioCatch behavioural telemetry, so a plain
HTTP client only gets the block page. The scraper therefore logs in with a
headless Playwright Chromium (`backends/santander_web.py`), typing the RUT and
clave into the login iframe, and reads only the JSON the private SPA loads for
itself, captured by a response listener: `cruceProductosOnline` (fired by the
dashboard) for the products, and the open-banking transactions API (fired when
the vista's card is clicked) for the movements, taken only from a response
whose request named the vista's own account id. If that click yields nothing,
the transactions call is replayed once from inside the page with the headers
the SPA sent in the same session, or the run is recorded as partial. Only the
CLP cuenta vista is modelled: a singleton `vista` product (`balance` from
`MONTODISPONIBLE`, read as implied cents) that receives every movement; other
products in the list are ignored. One login feeds both legs per cycle, the way
BCI Lider caches its drive. The edge's block page, a step-up (second factor)
prompt or a login error raises a typed error that is never retried, and every
wait polls for a specific response or element (never `networkidle`, which
BioCatch's telemetry keeps from firing). The scraper does not try to evade
BioCatch's automation detection: if Santander starts blocking the headless
login, the run fails and says so.

Banco Falabella has no API for individuals, and its private site's data BFF
(`/movement-cc/*`) encrypts request bodies client-side (RSA + AES), so a plain
HTTP client cannot ask it for anything. The scraper therefore logs in with a
headless Playwright Chromium (`backends/banco_falabella_web.py`): it clicks "Mi
Cuenta", types the RUT and six-digit clave into the slide-over (no second
factor), and reads the figures from the rendered DOM. Three CLP products are
modelled, each a singleton (no `external_ref`) of its own kind, so the writer
never confuses them and the card keeps the row it had when it was the only
one: a CMR Mastercard `credit_card`, a cuenta corriente `checking` and a línea
de crédito `line_of_credit`. Card balances (`available`/`limit`/`owed`, plus
the masked `last4` and brand) come from the dashboard product card, anchored on
the "Cupo de compras / utilizado / disponible" labels so the Avance cash-advance line is
never read as a balance. Movements come from the card-detail `<credit-card-movements>`
web component (shadow DOM): the unbilled "Ultimos movimientos" tab and the
current billed "Movimientos facturados" statement. Older statements (the tab's
month select) are not iterated yet; a daily run already sees each movement while
it is unbilled and again while it is the current statement. Each purchase is
emitted once at its full "Monto total" (not one row per installment). The
gotchas handled: a post-login marketing modal whose backdrop intercepts clicks
(dismissed first), components that render a moment after the page (each read
waits for its content), an idle-timeout modal ("Tu sesion expirara") that logs
the session out if its "Continuar" button is not clicked, and a flaky movements
BFF that often 500s, read with bounded retries (a "Reintentar" click between
tries) and reported as partial coverage when a surface never loads. The
component draws the same header-only table while a load runs, after it failed
and when the tab is truly empty, so the scraper watches the `/movement-cc/*`
calls on the wire: a tab only counts as empty after its `/movements` call
answered 2xx, with no "Reintentar" showing, and the header-only table or "no
movements" message held for a few polls. Every read stays inside the movements
component and its visible tab. The checking account and the línea come off
their own dashboard cards (`app-accounts` / `app-agreements`): "Saldo
disponible" / "Saldo contable" for the account, "Cupo autorizado" / "Saldo
utilizado" / "Saldo disponible" for the línea. After the card, the scraper
reloads the dashboard and opens each detail, a legacy server-rendered view in
the light DOM: the account's default "Ultimos movimientos" table (Cargo is money
out, Abono money in) and the línea's "Tasa diaria", kept as quoted in
`daily_interest_rate_pct`. The línea's own movements are not read (their view
failed live, and its draws reach the checking account anyway). A detail that
does not load is a partial-coverage warning that never costs the card's
surfaces. One login feeds both legs per cycle, the way
Santander and BCI Lider cache their drive, and every wait is bounded (never
`networkidle`).

Buda reads two signed REST surfaces (issue #3). Balances cover every wallet:
one `crypto` product per currency, `units` kept fractional. Transactions cover
**CLP deposits and withdrawals only**, minus annulled or rejected ones, paged
through `meta.total_pages` (capped at 20 pages of 50 per direction, with a
warning past that). Crypto movements
are deliberately not imported: `transactions.amount` keeps two decimals (too
few for crypto units), and a crypto movement has no CLP amount unless it is
priced at its date, which would
need a historical price source. Crypto value history comes from the balance
snapshots instead, converted at lib/rates' current tickers like every other
crypto figure. A non-CLP amount that still reaches the parser is skipped with a
warning, never truncated. Every Buda `ScrapedTransaction` carries
`currency="CLP"` and the same `crypto` kind as the balances, so CLP movements
attach to the CLP balance product. That product stays `crypto` rather than
`wallet`: re-kinding it would change its identity and metrics shape (`units` to
`balance`) under its snapshot history and any monitor bound to it, and a CLP
`units` figure already converts 1:1. V020 deleted the crypto movements the old
scraper had truncated and stored on it as pesos.

Skipo reads its official REST API (`api.skipo.com/v2`) with a personal
read-only key sent as a bearer token; every call is a GET. The products leg
classifies symbols through the asset catalog and reads the balances: CLP
becomes the singleton `wallet` product (reported even at zero), each
crypto or stablecoin row one `crypto` product per symbol, like Buda, also at
zero units so a sold-out coin stops counting (a coin the response leaves out
can't be zeroed; an "absent means zero" writer rule is a follow-up shared
with Buda). Fiat other than CLP, tokenized stocks and unclassified symbols
are left out and counted in a run warning. Each held coin is priced with
`/v2/markets/{SYMBOL}-CLP/price?side=SELL` (the price a holding would
liquidate at), which becomes an institution-wide `ScrapedExchangeRate` dated
by its `pricedAt`; a coin without a CLP market or a readable price only gets
a counted warning. The transactions leg reads only the CLP ledger, which
without a date range returns the whole history newest first, paged (capped
at 50 pages of 100): DEPOSIT and BUY (a coin sold for pesos) are
money in, WITHDRAWAL and SELL (pesos spent on a coin) money out. Coin ledgers
are skipped for Buda's reason. A 401 or 403 fails the leg with the HTTP status
and the API's problem code, never the key.

Fintual reads the website's own backend with a cached web session (see "Fintual
sign-in" in USAGE.md). `GET /api/goals` gives one CLP `investment` product per
goal (`external_ref` = goal id). The stock brokerage ("Acciones") comes from
`POST /gql/`: `StocksPortfolio` lists the USD cash wallet and the positions,
and one `StocksDetails` query batches every position's totals and profile under
index aliases. Prices come from `GET /stocks-pricing-cl/current-prices`, which
checks a per-service JWT cookie: on a 401 the scraper mints it with
`POST /auth/jwt` and retries once, as the web app does. Each position becomes a
USD `stock` product (`external_ref` `stock:<asset id>`; ticker, description
and tags as attributes; nav, net invested, profit, units, price, average cost,
unrealized and realized profit and dividends as metrics, by the web app's
formulas), shown in the institution's own "Acciones" table, and the cash a USD
`wallet`. A sold-out position drops to nav 0 and
stops counting, for as long as Fintual keeps listing zero-share positions; a
held one without a price or totals is skipped with a counted warning. The
stocks are an isolated part of the same run: a failure there keeps the goals
and records the run as `partial` (a failure on the positions' own requests
keeps the wallet too). An account that never opened the brokerage (nothing
resolves and GraphQL reports no error) gets no stock products, no movements
and no warning; nothing resolving next to a GraphQL error fails the leg (the
movements are still read).

Fintual's transactions are the USD wallet's ledger, from one `WalletMovements`
query on `POST /gql/` (`backends/fintual_stocks.py` holds the query text and
the pure parsers), paged 50 at a time until a page where every list is empty
(the offset moves by the longest list returned, never more than the page size;
40 pages at most, then a warning). Every movement is a row on the USD `wallet`;
buys, sells, dividends and cash mergers also get a mirror row on the stock's
`stock` product, named by `product_external_ref` `stock:<asset id>`. A buy
is money out of the wallet and into the stock, and both rows are flagged
internal transfers, as are deposits from a bank; a dividend is income once, on
the stock; cash interest and compensations are income and fees expenses. Dates
are the day the wallet changed, in America/Santiago. Pending movements wait
until executed (the query has no state field, so a deposit, withdrawal or order
declared over a week ago and never executed is taken as cancelled and left out
without blocking the balance check); divestments, rows in another currency and
unreadable rows are skipped with counted warnings (the row shapes come from
Fintual's query text, and not all of them are verified against live data
yet). The full ledger is read every run and the writer skips what it has; a
new stock's rows wait one run for its product. Both legs share
one session drive per run: the transactions leg runs it first and caches it for
the products leg. A movements failure is a warning that costs the movements
only. When the ledger is complete and nothing was pending or skipped, it must
add up to the wallet's balance to the cent, or the run warns (no amounts). A
stock the positions list no longer has gets a zero-valued product when the
ledger shows it left through a sale or a cash merger (deposited = buys - sells,
profit and realized profit = sells - buys). One that left through a non-cash event (a share-only
merger, a spinoff, shares moved out by ACAT) has no cash leg, so it is skipped
rather than booked as a loss, and its product keeps its last values; an empty
positions list next to stocks bought and never sold zeroes nothing that run and
warns. On a fresh database, deploy the stock products and let one Fintual run
finish before deploying the movements: the transactions leg writes first, so a
wallet row would create the USD wallet under the generic slug
`fintual-wallet-usd` (the products write corrects its name; slugs are
create-only).

Every run also reads, after the movements, for each stock still held that got
a product, the last 365 days of daily closes from
`GET /stocks-pricing-cl/historical-prices` (`timeframe=1Day`, `limit=400`:
the limit counts bars forward from `start`; one symbol per call, at most 30
stocks, in turn), through the same 401 -> `POST /auth/jwt` -> retry-once path
as the current prices (the service cookie lives 180 s, so a stale one is just
minted again). Each becomes a `ScrapedPriceHistory` on the stock's product
(named with `stock_ref`), dated by the bar's UTC date. The window is a
stateless trailing year: a cursor would save no request, and the upsert skips
unchanged days. A bar without a positive price or a readable time is dropped;
a stock whose history fails is counted into one warning (no tickers). A
failure no other stock would escape (a transport error or timeout, a refused
or failed token mint, a 429/5xx after the retries) stops the loop, and the
stocks not read yet count as failed. The whole step has a 120 s budget, since
it holds the session lock; running over it keeps the series already read and
counts the rest as not read, and any other failure of the step is one warning
that costs nothing else.

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

**Wealth series** (`web/src/lib/wealth.ts`, served by `/api/wealth`): legacy
`wealth_snapshots` totals are authoritative up to their last date; after it,
every local day with product observations becomes a computed point, carrying
each product's latest snapshot forward. `POST /api/wealth` only backdates, and
a backdated snapshot must never hide a computed point: it must be dated before
today and before the derived series starts, i.e. the first real observation
after the latest legacy date (`409` otherwise). Dates inside legacy history
are always accepted (`409` if already taken). Observations on or before the
latest legacy date (e.g. pre-migration scraper rows) are hidden by legacy
totals anyway, and the V009 backfill rows (`source = 'wealth_snapshot'`, the
three legacy component columns decomposed into per-product rows on legacy
dates) never count as observations, so deleting the last legacy row and
posting it again still works. The informational Fintual column sums the CLP
goals only; Fintual's USD stock products count in patrimonio alone.

Each institution scraper implements:

```python
class BaseScraper(ABC):
    method: str                                  # "email" | "web" | "http_api"
    institution: str                             # "mach" | "banchile" | "buda" | ...
    scrape_transactions() -> list[ScrapedTransaction]
    scrape_products() -> ProductScrapeResult     # products + non-fatal warnings
                                                 # + exchange rates read
```

Both `method` and `institution` are stored per `scraper_runs` row, and
`run_scraper` finishes the row as `success`, `partial`, or `error`, with every
failure and warning joined into `error_message`:

- `partial`: something went wrong but part of the data landed (one leg raised
  while the other wrote, or some products or transactions failed to write
  while the rest were written), or a scraper reported warnings, e.g. a
  BanChile surface that failed all its retries, Fintual goals skipped for
  lacking an id or Fintual stock movements (or a stock's price history) that
  failed to load. Transactions that name a product (`product_external_ref`)
  the database doesn't have yet are a warning too, and don't count as landed:
  the next run writes them. So is a price history that can't be written.
- `error`: something failed and nothing landed.

Writes are isolated per row: `run_scraper` writes each product separately, and
`write_transactions` runs each transaction (and each product resolution) in
its own `conn.transaction()` block, so a row that fails is rolled back alone
instead of aborting the transaction for every row after it. The dashboard's
`ScraperStatus` widget shows `partial` runs with an amber badge and banner,
and failed and partial rows expand to their `error_message`.

**Retries.** `scrapers/retry.py` retries idempotent work only, three attempts
with 1 s / 2 s backoff: Buda's and Fintual's GETs, Fintual's read-only `/gql/`
query POSTs and its `/auth/jwt` token POST on transport errors and 429/5xx
(re-signing each Buda attempt, since a nonce can't be reused; the last
response is returned as-is, so Fintual's 401 re-login is unchanged), and
the DB writers (`with_db_retry`) on `psycopg.OperationalError`, which covers a
dropped connection, a pool timeout and serialization/deadlock rollbacks
(inside `write_transactions` a per-row error, those included, counts as
failed instead; only a dropped connection replays its batch). Every writer is
idempotent, so a replay can't double-write; `start_scraper_run` mints its id
before the retry so a replay can't leave a second run stuck in `running`.
Once a product write exhausts its retries on such an error, `run_scraper`
stops writing that run's remaining products, since each would block the event
loop through its own retries. Logins are never retried (a repeated Fintual
`initiate_login` e-mails another 2FA code), and neither are whole legs. The
pool pings each connection before handing it out
(`check=ConnectionPool.check_connection`), so the ones a Postgres restart
killed are replaced rather than failing a write, recycles connections after
30 min, and gives up waiting for one after 10 s so a retry can move on.

`ScrapedTransaction`/`ScrapedProduct` are pydantic envelopes defined in
`packages/product-model`; both carry `institution` (slug), a kind, and
`currency` (plus optional `external_ref`, e.g. one product per Fintual
goal). The writer resolves the chain institution → account → product
(creating missing links via the `uq_products_identity` upsert;
single-user: everything attaches to the oldest user), so e.g. each Buda
currency becomes its own `crypto` product. A transaction lands on the product
of its kind and currency with no `external_ref`, unless it names one in
`product_external_ref` (a Fintual stock position): that product is looked up,
never created, and a miss is counted `unresolved` for a later run.
`internal_transfer` is written to `is_internal_transfer` on insert only, so a
user's change survives re-scrapes. `upsert_product`
shallow-merges `attributes`, always refreshes
`products.current_balance`/`metrics`/`balance_as_of`, and appends a
`product_snapshots` history row only when the metrics payload changed.

`ScrapedExchangeRate` is the third envelope: a rate an institution applies
(`base_currency` priced in `quote_currency`, CLP by default), scoped to one of
its products through a `ProductRef` (kind, currency, external_ref) or
institution-wide (`product` = None), with a Spanish `source` label and the
`reported_as_of` date the institution prints. `run_scraper` writes a leg's
`exchange_rates` after its products with `upsert_exchange_rate`, which looks the
product up by the same identity rule but never creates anything: a rate naming
an unknown institution or product raises `UnresolvedRateScope` and becomes a
run warning (`partial`), as does a failed write. A re-scrape updates the row in
place (see [Exchange rates](#exchange-rates)).

`ScrapedPriceHistory` is the fourth: the daily closing prices of one
product's unit (a stock position), as `points` of (`date`, `price`), each day
once, naming the product through a `ProductRef` built from the same helper as
the product's `external_ref`. `run_scraper` writes a leg's `price_histories`
after its products and rates with `upsert_price_history`, which looks the
product up the same lookup-only way (`UnresolvedPriceScope` for an unknown
institution or product, a run warning) and upserts every point in one
transaction into `product_prices` (V038), in the product's currency: a new day
is inserted, a day whose price changed is updated in place, and an unchanged
day is left alone. Nothing is pruned. Each series is written on its own, a
failed one counts into a single warning (by exception type), and none counts
as landed; a transient database error (any `OperationalError`, already
retried) stops the remaining series rather than retrying each. A daily bar's
trading day is its UTC date: Fintual stamps bars at New York midnight
(`T04:00:00Z`), which America/Santiago would move to the day before, so this
is the one place a scraped timestamp is not dated in Chile.

| Institution | Method | Source | Auth | Schedule |
|---|---|---|---|---|
| `fintual` | `http_api` | REST API (`/api/goals`), GraphQL (`/gql/`) and the stocks pricing service | Web session + e-mail 2FA (cached; `make fintual-login`) | 6h |
| `buda` | `http_api` | REST API | HMAC-SHA384 signed requests | 1h |
| `skipo` | `http_api` | REST API (`/v2`) | Read-only API key (bearer) | 1h |
| `banchile` | `web` | Browser (Playwright: `banchile_web` + `banchile_movements`) | RUT + password | 24h |
| `mach` | `email` | IMAP (Gmail) | Shared IMAP session | 30m |
| `mercadopago` | `email` | IMAP (Gmail) | Shared IMAP session | 30m |
| `tenpo` | `email` | IMAP (Gmail) | Shared IMAP session | 30m |
| `bci_lider` | `web` | Real Chrome over CDP (`bci_lider_web`) | Autofill in a real Chrome (managed by default; reuse via `LIDER_BCI_CDP_URL` + `make bci-lider-login`; Cloudflare Turnstile) | 24h |
| `santander` | `web` | Headless Playwright login, the SPA's own JSON (`santander_web`) | RUT + password, one login per run | 24h |
| `banco_falabella` | `web` | Headless Playwright login, the rendered DOM (`banco_falabella_web`) | RUT + 6-digit clave, one login per run | 24h |

The three email-based scrapers reuse one `ImapSession`: it runs `NOOP` on
each acquire and only re-logs-in when the mailbox has been dropped. imaplib is
synchronous, so every IMAP round trip runs in `asyncio.to_thread` while the
session lock is held: the email scrapers take turns on the connection without
blocking the event loop for the other scrapers.

Each institution declares an `EmailPattern`, and `parse_transaction` applies
it to one email:

- **Sender**: `sender_domains` entries must appear as whole labels of the
  From address's domain (`somosmach` matches `somosmach.com`, never `bci.cl`,
  a display name, or a local part), so MACH no longer claims Banco Bci mail.
  Subject filters ignore case and accents.
- **Amount**: `amount_rules` anchor the figure to its context
  (`anchored("pagaste|compraste")` claims the first `$` figure within 60
  characters after the phrase, never across a sentence end); the anchor that
  appears earliest in the body wins, and a figure no anchor claims (a promo
  banner, a balance line) is never read. An email skipped with such a figure
  is logged at info level with its subject, so new wording shows up.
- **Direction**: an email is income when its subject carries one of the
  pattern's `income_keywords` ("recibiste una transferencia", "devolución") or
  its figure was anchored by an `income=True` rule ("recibiste", "te
  devolvimos"); everything else is an expense.
- **Date**: the `Date` header in Chile's calendar, else the IMAP
  INTERNALDATE, else today with a warning.
- **Coverage**: `EMAIL_IMAP_MAILBOX` (default `INBOX`, opened read-only) and
  `EMAIL_MAX_MESSAGES` (default 100 emails per institution and run, newest
  first; a truncated window logs a warning) apply to every pattern unless it
  sets its own `mailbox` / `max_messages`. Zero amounts are skipped with a
  debug line naming the subject, and an email that fails to parse is logged
  and skipped without costing the rest of the run.

The synthetic corpus in `apps/scrapers/tests/fixtures/emails/` drives
table-driven tests of that behaviour per institution.

### Product model

`packages/product-model` is the single source of truth for product kinds: a
pydantic v2 registry declaring, per kind, the attribute/metric classes plus
`role`, `balance_convention`, `label_es` and a display `family`. `display.py`
holds one column spec per family (source, field, format, flags), with
formats derived from the payload classes, so the dashboard never keeps a
parallel per-kind column map. `make product-model-generate`
emits the derived artifacts — `generated/index.ts` (per-kind TS types,
`KIND_INFO`, `ASSET_KINDS`/`LIABILITY_KINDS`, consumed by `db/schema.ts`,
`networth.ts` and the institutions page, plus `PRODUCT_FAMILIES` and
`FAMILY_INFO`, which `components/institutions/shared.tsx` renders as one
products table per family), `generated/product-model.schema.json`
(the JSON Schema wire contract) and `PRODUCTS.md` (the per-kind field matrix
with placement rationale). A codegen drift test under `make test-py`
regenerates to a temp dir and diffs against the committed output, so the
artifacts can't silently fall behind the registry. The `ScrapedProduct` /
`ScrapedTransaction` / `ScrapedExchangeRate` / `ScrapedPriceHistory` envelopes
are that wire contract in motion: today they
travel in-process from scraper to `db/writer.py`, and the plan is to put the
same validation behind a REST ingest API so non-Python scrapers can submit
data without direct DB access.

### Product management

Scrapers own a product's identity: `(account, kind, currency, external_ref)`
is the `uq_products_identity` key they upsert on, and `attributes`/`metrics`
are typed by kind. The dashboard owns its display metadata, which the writer
never overwrites: `slug` is insert-only there, and `name` moves only while it
still equals `default_name` (V027), the scrapers' name for the product, which
the writer refreshes on every scrape. The management routes follow that
split, with their rules in `lib/management.ts`:

- `PATCH /api/institutions/{slug}/products/{product}` edits `name`, `slug`,
  `parent_product_id`, `is_active` and `display_order`; `name: null` copies
  `default_name` back, so the name follows the scrapers again. Kind,
  currency and external_ref are refused, since changing them would fork the product on its
  next scrape. A new slug is canonicalized with the generated-slug rules and
  must be free across the institution (retired products included); the
  `(account_id, slug)` index backstops a scraper minting the same slug
  mid-edit. Parents stay inside the institution and can't form a loop. The
  institution and product rows are locked (`FOR NO KEY UPDATE`) while the
  rules run, so concurrent edits serialize per institution.
- Deactivating a product with no balance is refused: inactive without a
  balance is the retired-ghost shape (`lib/retired-products.ts`) the UI hides,
  so the product could never be reactivated from the dashboard. The writer
  skips an inactive product's balance (`skip_inactive`) but still imports its
  transactions; net worth keeps its last balance, and monitors that reference
  it evaluate as inactive.
- `PATCH /api/institutions/{slug}` edits name, kind, country and URL; the slug
  is the scrapers' key and stays read-only. `PATCH /api/accounts/{id}` renames
  an account, which is safe because resolvers pick an institution's account by
  display order, not by name.

The old `GET /api/balances` is gone: `/api/institutions` already returns every
product with its denormalized balance.

### Deduplication

Transactions are deduplicated via `UNIQUE(product_id, external_id)`:

- Fintual: `fintual_wallet_{type}_{id}` on the USD wallet and
  `fintual_stock_{type}_{id}` on the stock position (`type` is `dep`, `buy`,
  `sell`, `div`, `int`, ...; the id is Fintual's own, the asset order's for buys
  and sells), each leg its own key since keys are unique across a batch
- Buda: `buda_{deposit/withdrawal_id}` (CLP movements only)
- Skipo: `skipo_{ledger_entry_id}` (CLP ledger only)
- BanChile: the bank's own operation id where the portal exposes one, else a
  description-free fingerprint (issue #57). Three forms, each greppable:
  - `bch_op_{transaccionId}`: a checking movement's "ID Transacción", read
    inline from `detalleGlosa` or from the `cartola/detalle-glosa` response
    (all but a small minority of the movements observed); normalised
    (uppercased, punctuation dropped) so a cosmetic drift can't re-key it, but
    not hashed, so it stays debuggable. An all-zero id is a placeholder, not a
    value.
  - `bch_ref_{numReferencia}`: a billed card row's reference ("DDMM
    NNNNNNNN"; a USD row's `numeroDocumento` is "DDMM" + 23 digits); an
    all-zero suffix or a bare "DDMM" means the bank has none, not a value.
  - `bch_fp_{md5(fingerprint)[:16]}`: the fallback: checking uses the bank's
    composite `id` plus the running `saldo` (unique and stable together, where
    the composite `id` alone collides for same-second batch credits); the
    card's unbilled leg, which has no id at all, uses posting date +
    authorisation date/time + amount + card last4 + Transbank merchant code;
    a billed row without a usable reference uses date + amount + card + the
    statement date, plus (USD only) the raw `numeroDocumento` and the statement
    section, since a USD payment and credit can share everything else.
    A USD fingerprint is hashed with the currency in front and its amount as a
    two-decimal string, so it can never equal a CLP one.
  None of the forms includes the description or the movement's order or
  multiplicity within a scrape, and only a closed statement's rows (which
  never move) carry a date or a section. Because a movement can
  *change* form
  (a checking one acquiring its operation id on a later run, a card charge
  moving from the unbilled leg to the billed one), `upsert_transactions` adopts:
  a key that matches nothing re-keys the stored row it could be under an older
  key instead of inserting a duplicate, correcting its dates at the same time.
  Candidates are scoped to the same product, amount and source, an `external_id`
  in the `bch_` namespace, and a `transaction_date` equal to either of the
  incoming dates (the stored rows hold the posting date, incoming movements the
  occurrence date: see the two-dates note below); claiming is oldest-first and
  one-to-one, preferring an exact occurrence-date match. See V018 and V019.
- BCI Lider: `bcl_{md5(date|amount|CLP)[:16]}` (no per-movement id in the DOM;
  the description is left out because the portal rewrites it once a charge is
  billed, see V017)
- Santander: `san_mov_{accountingDate}_{movementNumber}` from the bank's own
  movement number (zero padding stripped), stable across separate logins and
  scoped by the posting date, since the number may be a per-posting-day
  sequence; a movement without a number or without a posting date (the
  occurrence date is never substituted) falls back to
  `san_fp_{md5(currency|transactionDate|
  accountingDate|movementAmount|newBalance|chargePaymentFlag)[:16]}`, raw
  fields only and never the description
- Banco Falabella: `bfal_{sha1(date|amount|CLP|persona)[:16]}` over the
  occurrence date, the signed full "Monto total" as an integer, the currency and
  the persona (Titular / Adicional); no per-movement id is exposed in the DOM.
  Description, the installment counter, the section label and the billing month
  are left out because the portal rewrites them once a purchase is billed, so a
  multi-installment purchase (repeated across months) and a movement seen in both
  the unbilled and billed tabs dedup to one row. Two distinct charges on one day
  for the same amount and persona collapse (the scheme's collision limit). Its
  checking movements use `bfal_cc_{sha1(date|amount|CLP|saldo)[:16]}`, adding
  the running balance after the movement (the table has no id and no time of
  day), which tells apart same-day twins unless the balance returns to the same
  figure between them. The balance is also the scheme's re-key risk: if the
  bank re-sorts same-day rows (an earlier-booked movement listed later) or a
  hold or reversal changes a row's printed Saldo, that movement gets a new key
  and is stored a second time
- Email: `email_{institution}_{sha1(Message-ID)[:8]}` (`hashlib`, not Python's
  per-process salted `hash()`); an email without a Message-ID hashes
  `Date|From|Subject|body` instead
- CSV: `csv_{base64url(date|description|amount)[:24]}`

### Deleting a transaction (soft vs hard, V033)

Transactions can be edited, recategorized and deleted from the dashboard
(the detail panel edits the row in place; PATCH/DELETE `/api/transactions/[id]`).
Deletion is split by origin, because a hard delete would let the next scrape
bring an imported movement straight back:

- A row with an `external_id` (scraper or CSV) is soft-deleted: `deleted_at` is
  set and the row stays, still holding its `(product_id, external_id)` key, so
  `upsert_transactions` keeps matching it and skips the movement (no writer
  change), and BanChile's adoption path can still re-key a deleted legacy row.
- A row without an `external_id` (manual entry) is hard-deleted, since nothing
  can re-import it.

Every dashboard-facing read filters `deleted_at IS NULL` (the transactions
list and detail, the product movement list, category counts and rule previews,
transfer link candidates and suggestions). The writer's lookups deliberately
do not, so the re-key path above still sees the tombstoned row. A non-null
`categoryId` set by hand also sets `is_manually_categorized`, so the choice
survives the auto-categorization backfill.

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
  `coalesce=True` / `max_instances=1` guards: a manual trigger can't overlap a
  scheduled or manual run of the same institution. The startup run
  (`run_all_once`) isn't a job, so a trigger during it isn't guarded (known gap;
  for BanChile the login cooldown still stops a second login). The HTTP call
  returns `202` immediately; the scrape runs asynchronously on the scheduler's
  event loop.
  Jobs also set `misfire_grace_time=None`: APScheduler's default of 1 s would
  drop a run, or a manual trigger, that comes due while sync work (IMAP scans,
  DB writes) holds the event loop.
- Those guards stop overlap, not frequency, so a scraper can also refuse manual
  triggers (`_REFRESH_COOLDOWNS` in `main.py`; only BanChile, issue #28).
  `banchile_web.login_cooldown_retry_after` refuses only a run that would
  certainly need a full login inside the login cooldown: an attempt in the
  window and no cached session file. With a file the trigger goes through (the
  burst the cache serves); if that session turns out dead, the backend skips
  the login and deletes the file, so the next press is refused. Refused,
  `POST /refresh/{slug}` answers `429` with `Retry-After` and
  `skipped: [{slug, retry_after_seconds}]`; `POST /refresh` triggers the rest
  and lists it under `skipped` in its `202`. The dashboard shows either as a
  "consulted recently, try again in N minutes" notice.
- `GET /health` reports the scheduler, not just the server thread: a heartbeat
  job runs on the event loop every 30 s, and the endpoint answers `503
  {"status": "stale"}` once it has gone 10 minutes without one (a wedged loop).
  The Compose `scrapers` service polls it as its `healthcheck`, so a wedged
  scheduler shows as `unhealthy` in `docker compose ps`. Compose only marks
  the container; restarting it on that is up to whatever runs the stack.
- The server runs on a daemon thread and binds `SCRAPER_CONTROL_HOST`, which
  defaults to `127.0.0.1` (host-dev); Compose sets `0.0.0.0` inside the
  container so `web` can reach it. It's an **unauthenticated** trigger: keep it
  internal (Compose `expose`s port `8080` on the private network; never publish
  it, see #23). The web proxy
  returns `503` when it's unreachable so the UI can explain the outage.

| Var | Service | Meaning |
|---|---|---|
| `SCRAPER_CONTROL_PORT` | scrapers | Port the control server binds (unset ⇒ disabled) |
| `SCRAPER_CONTROL_HOST` | scrapers | Address the control server binds (default `127.0.0.1`; Compose sets `0.0.0.0`) |
| `SCRAPER_CONTROL_URL` | web | Base URL the refresh proxy calls (e.g. `http://scrapers:8080`) |
| `BANCHILE_LOGIN_COOLDOWN_MINUTES` | scrapers | Minimum minutes between full BanChile logins, which also gates manual refreshes (default `10`; `0` disables) |

## Dashboard authentication

The dashboard is single-user by design, so auth is a **shared secret**, not a
user system: `DASHBOARD_PASSWORD` plus a signed session cookie. There is no auth
table, no session table and no third-party dependency.

- **Enforcement point**: `apps/web/src/proxy.ts` (Next 16 renamed the
  `middleware` convention to `proxy`; it runs on the Node.js runtime). It gates
  every path except Next's static output, so a new page or API route is
  protected the moment it exists. Route handlers hold no auth logic of their own.
- **Session**: `apps/web/src/lib/auth/session.ts` mints
  `{expiresAtMs}.{HMAC-SHA256}` with Web Crypto. The key is
  `PBKDF2-SHA256(context ‖ DASHBOARD_PASSWORD, fixed salt, 210k iterations)`, so
  there is exactly one secret and rotating the password invalidates every
  outstanding session. PBKDF2 (rather than a bare digest) is what keeps a leaked
  cookie from yielding the password offline: the token is known plaintext. The
  derived key is memoized per password, keyed on the password itself so a
  rotation can never be served a stale key. Expiry is absolute, never sliding.
  Cookie: httpOnly, `SameSite=Lax`, `Secure` when the request is HTTPS
  (`X-Forwarded-Proto` counts only with `DASHBOARD_TRUST_PROXY`, below), and
  named `__Host-chanchito_session` whenever it is `Secure` so a sibling subdomain
  cannot toss a cookie over it. On a secure request only the pinned name is read,
  so a tossed plain cookie is not even looked at; plain HTTP, the one channel
  where browsers reject the prefix, still accepts the unprefixed name. Logout
  clears both.
- **Re-login policy**: `DASHBOARD_SESSION_MAX_AGE` (duration, seconds,
  `browser`, or `unlimited`; default `12h`), parsed in
  `apps/web/src/lib/auth/config.ts`. Invalid values fail closed to the default,
  and durations are clamped to the 400-day browser cookie ceiling.
- **Brute-force protection** (`apps/web/src/lib/auth/throttle.ts`): three layers,
  ordered by how much a remote party can influence them. There is exactly one
  account and one way in, so availability is part of the threat model: no remote
  party may be able to keep the owner from logging in.
  1. A **global** promise mutex serializing every attempt, so the 500 ms delay on
     failure is a real ceiling (~2 attempts/s) rather than a per-request pause
     that concurrency erases. Nothing can be spoofed to escape it and it covers
     distributed attacks, which is why it, and not a global lockout, is the
     backstop.
  2. A **global** queue bound (8 attempts): past that, callers get `429`
     immediately instead of parking the owner's request behind a long chain of
     500 ms failures, and memory stays bounded.
  3. A **per-client** progressive lockout: after 5 consecutive failures from one
     client, `429` with `Retry-After`, backing off exponentially from 5 s to a
     5 min cap. Scoped per client so a lockout an attacker triggers lands on the
     identity it used. A successful login clears that client's state, 15 idle
     minutes decay the counter (so the backoff cannot ratchet up forever), and
     after 30 continuous minutes of lockout the password is checked again for
     that client (a correct one gets in, a wrong one still gets `429`), which
     bounds a targeted denial of service.

  Client identity comes from `X-Forwarded-For`: its first entry when
  `DASHBOARD_TRUST_PROXY` is set, otherwise the header as a whole, which
  `next start` fills from the connection's remote address when the caller sent
  none (`NextRequest.ip` is gone since Next 15). A forged value only buys the
  forger its own bucket, which is exactly why the un-spoofable ceiling is layer 1
  and the lockout is layer 3. There is deliberately no global lockout: any global
  counter can be driven by an unauthenticated attacker, and a single failure needs
  no knowledge of the password (an empty body counts).
- **Trusting a proxy** (`DASHBOARD_TRUST_PROXY`, parsed in
  `lib/auth/config.ts`): `X-Forwarded-Host`, `X-Forwarded-Proto` and
  `X-Forwarded-For` are client-writable, and `next start` stamps the first two
  from the connection when absent, so they are ignored unless this one flag says
  a reverse proxy is in front. Off: the Origin check compares `Host`, and
  `Secure` needs an HTTPS connection with no forwarded scheme in play at all
  (`request.url`'s scheme is itself built from `X-Forwarded-Proto`, so a present
  but untrusted header means "not secure" rather than "ask the URL"). On: the
  first entry of each is honored. The flag exists because trusting the
  forwarded host let any caller send a matching forged host/`Origin` pair, and
  trusting the forwarded proto let a phantom `https` produce a `Secure`
  `__Host-` cookie that the browser silently drops (the login loop documented in
  USAGE.md). The login route logs one warning when `Secure` rests on the
  forwarded header alone while the connection is plain HTTP, and the login page
  re-checks `/api/auth/session` before navigating so a dropped cookie surfaces as
  an error instead of a loop.
- **Fail-closed posture** (`decideAuthMode`, a pure function so it is unit
  tested):

  | `DASHBOARD_PASSWORD` | `NODE_ENV` | Behavior |
  |---|---|---|
  | set | any | Enforced: pages redirect to `/login`, `/api/*` answers `401` |
  | unset | production | Misconfigured: pages show a "not configured" notice, `/api/*` answers `503` |
  | unset | development | Disabled: no login, so `make dev` needs no setup; only a loopback `Host` is served and cross-site writes are still refused |

- **Public surfaces**: `/login`, `POST /api/auth/login`, `GET /api/auth/session`,
  `POST /api/auth/logout`, `/favicon.ico`, `_next/static` and `_next/image`.
  Nothing else. Logout is public on purpose: it only deletes a cookie, and
  gating it would leave an expired token permanently stuck in the browser.
- **CSRF** (`apps/web/src/lib/auth/csrf.ts`): `SameSite=Lax` only stops
  *cross-site* requests, so a sibling subdomain or any host under a shared public
  suffix (`*.duckdns.org`, `*.nip.io`, `*.ngrok-free.app`) would otherwise be
  free to POST to every API. Mutating `/api/*` requests are therefore checked
  against two allow-lists before any handler runs, `403` on failure, in every
  auth mode: with no password (`make dev`) this is the only thing keeping any
  page the browser visits from writing transactions or starting bank logins.
  `Sec-Fetch-Site` must be `same-origin`, `none` or absent (case-insensitive; an
  unknown value is refused), and `Origin`, when present, must match the request's
  own host. The Origin comparison uses `Host` (or
  `X-Forwarded-Host` with `DASHBOARD_TRUST_PROXY` on) rather than
  `request.nextUrl.origin`, and ignores the scheme: behind a TLS-terminating
  proxy `nextUrl.origin` is the internal `http://host:port` while the browser
  sends the external HTTPS origin, so comparing those two would reject every
  legitimate write. Comparing hosts stops a page that addresses us by our own
  name (it sends our host in `Host` and its own in `Origin`), but not DNS
  rebinding: a page on `attacker.example` re-pointed at our address sends
  `attacker.example` in both and is genuinely same-origin, free to read as well
  as write. With auth enforced the session cookie, bound to our name, keeps it
  out. With auth disabled the proxy first refuses every request, pages and
  reads included, whose raw `Host` is not `localhost`, `127.0.0.1` or `[::1]`
  (any port; `X-Forwarded-Host` is ignored), with a `403`. `OPTIONS` counting as
  mutating is load-bearing rather than an oversight: it is what refuses the
  preflight that forging a header forces, so no browser-reachable path survives.
  An absent `Origin` is allowed because browsers always send one on a
  cross-origin write; only non-browser clients (curl, scripts) omit it.
  JSON routes also require `Content-Type: application/json` (`415` otherwise,
  `readJsonBody` in `lib/api/validation.ts`), which is the label that forces a
  browser preflight: a form post or `text/plain` fetch never gets that far.
- **Caching**: authenticated pages are served `Cache-Control: private, no-store`
  and the login redirect `no-store`. Protected pages build as static and would
  otherwise carry a year-long `s-maxage`, which a CDN could hand to an anonymous
  visitor. Redirect-back targets (`?next=`) are validated as same-origin
  relative paths.
- The scraper control endpoint stays unauthenticated and internal (above); the
  dashboard password protects the `/api/institutions/refresh` proxy in front of
  it, which is what #23 asked for.

**Multi-user: DEFERRED.** Real per-user sessions (Auth.js or Lucia against the
existing `users` table, hashed credentials, per-user data scoping) are only worth
their complexity once a second person actually uses an instance. Until then the
shared secret is the whole model, and the `users` table stays unused by the web
app.

## Testing and CI

The web app is tested with vitest (`make test-ts`); each Python package runs
its own pytest suite (`make test-py`: `apps/scrapers/tests`, then
`packages/product-model/tests`), with the test tooling in
`apps/scrapers/requirements-dev.txt`. Most scraper tests drive parsers and
the writer's decisions through fakes. `test_writer_db.py` runs the real
writer SQL instead: `apps/scrapers/tests/conftest.py` builds a
`chanchito_test_tpl_*` template database once per session from every
migration file (each in its own transaction, recorded in `_migrations` like
`migrate.mjs` does), and each test clones it into its own `chanchito_test_*`
database, points `DATABASE_URL` at it, resets the writer's pool, and drops it
at teardown. The server comes from `TEST_DATABASE_URL`, which must name a
disposable one: unset, the DB tests skip; set but unreachable, they fail.
`make test-db` provides it as the `postgres-test` compose service (profile
`test`, so `make up` never starts it; data in tmpfs; port 5436 or
`POSTGRES_TEST_PORT`).

CI (`.github/workflows/ci.yml`, GitHub Actions) runs on every pull request and
push to `main` in two jobs: `web` (`make typecheck`, `make lint`,
`make test-ts`) and `python` (`make install-py`, then `make test-py` against a
`postgres:16-alpine` service, so the DB tests run rather than skip). Neither
downloads a Playwright browser; no test launches one.

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 16 (App Router) + React 19 + TypeScript |
| UI | Tailwind CSS 4 + shadcn/ui (New York) + Recharts |
| ORM | Drizzle ORM |
| API validation | zod (request bodies and query strings, `lib/api`) |
| Database | PostgreSQL 16 (Alpine) |
| Scrapers | Python 3.12 + httpx + Playwright + APScheduler |
| Product model | pydantic v2 registry, code-generated into TypeScript + JSON Schema |
| DB Driver (Python) | psycopg3 + psycopg-pool |
| Containerization | Docker + Docker Compose |
| Package Manager | pnpm (workspaces) |
| Testing | vitest (web) + pytest (scrapers, product-model; DB tests on a throwaway PostgreSQL) |
| CI | GitHub Actions |

## Monorepo Structure

```
el-chanchito/
├── .github/workflows/ci.yml          # CI: typecheck, lint, vitest, pytest
├── apps/
│   ├── web/                          # Next.js dashboard
│   │   ├── src/
│   │   │   ├── app/
│   │   │   │   ├── (auth)/           # Login + "not configured" notice
│   │   │   │   ├── (dashboard)/      # All pages with sidebar layout
│   │   │   │   │   ├── page.tsx      # Inicio
│   │   │   │   │   ├── monitors/     # Monitores (list, new, [id], [id]/edit + variaciones)
│   │   │   │   │   ├── history/      # Historial
│   │   │   │   │   ├── institutions/ # Instituciones, [institution], [product]
│   │   │   │   │   ├── expenses/     # Gastos + CSV import
│   │   │   │   │   ├── categories/   # Categorías + keyword rules
│   │   │   │   │   ├── fixed/        # Gastos fijos
│   │   │   │   │   └── transfers/    # Movimientos internos
│   │   │   │   └── api/              # 11 API route groups
│   │   │   ├── components/
│   │   │   │   ├── charts/           # Interactive chart + axis ranges
│   │   │   │   ├── dashboard/        # ScraperStatus, CsvImport
│   │   │   │   ├── institutions/     # Per-family product tables, refresh, logo tile
│   │   │   │   ├── monitors/         # MonitorCard, MonitorForm
│   │   │   │   ├── layout/           # Sidebar
│   │   │   │   └── ui/               # shadcn components
│   │   │   ├── lib/
│   │   │   │   ├── api/              # zod request schemas + 400 helpers
│   │   │   │   ├── monitors/         # Expression language, evaluation, history + variaciones
│   │   │   │   ├── auth/             # Session, throttle, CSRF, config
│   │   │   │   ├── db/               # Drizzle schema + connection + resolver
│   │   │   │   ├── institution-logos.ts
│   │   │   │   │                     # Slug -> logo file in public/logos/
│   │   │   │   ├── networth.ts       # Asset/debt derivation per kind
│   │   │   │   ├── rates.ts          # CLP conversion (Buda public tickers)
│   │   │   │   ├── budget-engine.ts  # Wealth + fixed-expense helpers
│   │   │   │   └── utils.ts          # cn(), formatCLP()
│   │   │   └── proxy.ts              # Auth enforcement (Next 16 proxy)
│   │   ├── public/
│   │   │   └── logos/institutions/   # One logo per institution slug
│   │   └── Dockerfile
│   │
│   └── scrapers/                     # Python scraper service
│       ├── scrapers/
│       │   ├── backends/             # email, banchile_web, banchile_movements, bci_lider_web,
│       │   │                         #   santander_web, banco_falabella_web, fintual_stocks
│       │   └── institutions/         # 10 scrapers: banchile, banco_falabella, bci_lider, buda,
│       │                             #   fintual, mach, mercadopago, santander, skipo, tenpo
│       ├── db/                       # Connection pool + writer + slug
│       ├── tests/                    # pytest (conftest.py: per-test DB fixtures)
│       ├── main.py                   # Entry point + scheduler + control endpoint
│       ├── requirements.txt
│       ├── requirements-dev.txt     # + pytest
│       ├── pyproject.toml           # pytest config only
│       └── Dockerfile
│
├── packages/
│   ├── db-schema/                    # Shared SQL migrations
│   │   ├── migrations/               # V001 through V038 (V026 reserved by an open branch)
│   │   └── migrate.mjs               # Migration runner
│   └── product-model/                # Product-kind registry (pydantic v2)
│       ├── product_model/            # kinds, attributes, metrics, display, envelopes
│       ├── scripts/generate.py       # emits the derived artifacts
│       ├── generated/                # index.ts + product-model.schema.json
│       └── PRODUCTS.md               # generated per-kind field matrix
│
├── scripts/                          # dev.sh (make dev), load-secrets.sh (Keychain)
├── docs/screenshots/                 # README screenshot (synthetic data)
├── docker-compose.yml               # + postgres-test (profile `test`) for DB tests
├── Makefile
├── README.md
├── USAGE.md
└── ARCHITECTURE.md
```
