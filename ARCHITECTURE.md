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
        │ Fintual  │ │  Buda   │ │ BanChile │
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
    banchile_web.py        # own BdC Playwright login -> fetch_balances()
    banchile_movements.py  # BdC movements + the shared session -> fetch_session()
    bci_lider_web.py       # real Chrome over CDP -> scrape_card() / save_login_session()
  institutions/
    mach.py  mercadopago.py  tenpo.py       -> consume backends/email
    banchile.py                              -> banchile_movements (one login: tx + balances)
    bci_lider.py                             -> bci_lider_web (one CDP drive: tx + balances)
    buda.py  fintual.py                      -> self-contained (HTTP APIs)
```

BanChile drives **one self-contained Playwright login per run** (issue #57,
which folded in #28). `scrape_transactions()` opens it, reads the balances
(`backends/banchile_web.py`: the "Mis Productos" dashboard plus four detail
routes) and the movements (`backends/banchile_movements.py`: the checking
cartola behind its account dialog, and the card's unbilled and billed legs),
and caches the products half for `scrape_products()` to serve. It uses the
`channel="chromium"` new-headless workaround (BdC serves a degraded page to the
default headless shell) and polls for each widget, since they load via later
XHRs. Because transactions and products are independent legs in `run_scraper`,
a session that crashes still leaves the products leg able to open a
balance-only login of its own (and a product-scrape crash is swallowed into a
run warning, never raised).

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

BanChile is also the only source that reports two dates per movement, so
`transactions` carries both (V019): `transaction_date` is when the movement
occurred (`fecha`, and what every chart is built on) and the nullable
`accounting_date` is when the bank posted it (`fechaContable`). They differ for
most of a typical window. A movement with only a posting date carries it in
both; an occurrence date is never invented. Neither is ever part of a dedup key.

Three more surfaces feed its transactions (`banchile_movements.py`, issue #57):
the checking cartola, reached by driving the account-selection dialog that
defaults to the USD account; the card's unbilled movements, which the SPA loads
when the card page opens; and the card's billed statements, the newest two,
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
| `banchile` | `web` | Browser (Playwright: `banchile_web` + `banchile_movements`) | RUT + password | 24h |
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
- BanChile: the bank's own operation id where the portal exposes one, else a
  description-free fingerprint (issue #57). Three forms, each greppable:
  - `bch_op_{transaccionId}`: a checking movement's "ID Transacción", read
    inline from `detalleGlosa` or from the `cartola/detalle-glosa` response
    (all but a small minority of the movements observed); normalised
    (uppercased, punctuation dropped) so a cosmetic drift can't re-key it, but
    not hashed, so it stays debuggable. An all-zero id is a placeholder, not a
    value.
  - `bch_ref_{numReferencia}`: a billed card row's reference ("DDMM
    NNNNNNNN"); an all-zero suffix means the bank has none, not a value.
  - `bch_fp_{md5(fingerprint)[:16]}`: the fallback: checking uses the bank's
    composite `id` plus the running `saldo` (unique and stable together, where
    the composite `id` alone collides for same-second batch credits); the
    card's unbilled leg, which has no id at all, uses posting date +
    authorisation date/time + amount + card last4 + Transbank merchant code.
  None of the forms includes a date, the description, the section, or the
  movement's order or multiplicity within a scrape. Because a movement can
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
  | unset | development | Disabled: identical to the pre-auth app, so `make dev` needs no setup |

- **Public surfaces**: `/login`, `POST /api/auth/login`, `GET /api/auth/session`,
  `POST /api/auth/logout`, `/favicon.ico`, `_next/static` and `_next/image`.
  Nothing else. Logout is public on purpose: it only deletes a cookie, and
  gating it would leave an expired token permanently stuck in the browser.
- **CSRF** (`apps/web/src/lib/auth/csrf.ts`): `SameSite=Lax` only stops
  *cross-site* requests, so a sibling subdomain or any host under a shared public
  suffix (`*.duckdns.org`, `*.nip.io`, `*.ngrok-free.app`) would otherwise be
  free to POST to every API. Mutating `/api/*` requests are therefore checked
  against two allow-lists before any handler runs, `403` on failure:
  `Sec-Fetch-Site` must be `same-origin`, `none` or absent (case-insensitive; an
  unknown value is refused), and `Origin`, when present, must match the request's
  own host. The Origin comparison uses `Host` (or
  `X-Forwarded-Host` with `DASHBOARD_TRUST_PROXY` on) rather than
  `request.nextUrl.origin`, and ignores the scheme: behind a TLS-terminating
  proxy `nextUrl.origin` is the internal `http://host:port` while the browser
  sends the external HTTPS origin, so comparing those two would reject every
  legitimate write. Comparing hosts keeps the protection (an attacker page still
  sends our host in `Host` and its own in `Origin`). `OPTIONS` counting as
  mutating is load-bearing rather than an oversight: it is what refuses the
  preflight that forging a header forces, so no browser-reachable path survives.
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

## Tech Stack

| Layer | Technology |
|---|---|
| Frontend | Next.js 16 (App Router) + React 19 + TypeScript |
| UI | Tailwind CSS 4 + shadcn/ui (New York) + Recharts |
| ORM | Drizzle ORM |
| Database | PostgreSQL 16 (Alpine) |
| Scrapers | Python 3.12 + httpx + Playwright + APScheduler |
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
