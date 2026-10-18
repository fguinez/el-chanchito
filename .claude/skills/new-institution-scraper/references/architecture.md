# Scraper architecture and design rules

How to structure the code of a new institution scraper so it fits the
service, stays testable, and is cheap to change when the institution changes.

## Contents

- [Layers and the dependency rule](#layers-and-the-dependency-rule)
- [SOLID, applied to this service](#solid-applied-to-this-service)
- [Patterns already in use](#patterns-already-in-use)
- [Identity: external_id and external_ref](#identity-external_id-and-external_ref)
- [Mapping onto the product model](#mapping-onto-the-product-model)
- [Money, dates and currencies](#money-dates-and-currencies)
- [Errors, warnings and logging](#errors-warnings-and-logging)
- [Configuration, secrets and session state](#configuration-secrets-and-session-state)
- [Async and concurrency](#async-and-concurrency)
- [Shared helpers and known duplication](#shared-helpers-and-known-duplication)
- [Anti-patterns](#anti-patterns)

## Layers and the dependency rule

```
main.py                      runner: schedule, scraper_runs, DB writes, control server
  │  uses only the BaseScraper interface
  ▼
scrapers/institutions/<slug>.py   adapter: env config, the two legs, mapping, external_id
  │  calls one entry point
  ▼
scrapers/backends/<slug>_<transport>.py   facade over the transport (optional, see below)
  │  returns raw records (frozen dataclasses / dicts)
  ▼
pure parsers                 text or JSON in, typed records out: no I/O, no clock, no env
  ▼
product_model                envelopes (ScrapedProduct / ScrapedTransaction), kinds, metrics
```

- Imports point down only. A scraper never imports `db.*`: the runner
  (`main.run_scraper`) owns persistence and run bookkeeping, and the writer
  validates envelopes it receives.
- Create a `backends/` module only when the transport is big (a browser
  session with many surfaces) or shared by several institutions (the e-mail
  backend serves three). A self-contained HTTP scraper (Buda) keeps
  everything in `institutions/<slug>.py`; one whose parsing grows big may move
  just the pure parsers to a backend (`backends/fintual_stocks.py`: a query
  text and the ledger parsers, no I/O).
- Parsers are where the institution's quirks live. Keep them pure so most tests
  are a fixture in and an assertion out, with no fakes.

## SOLID, applied to this service

**Single responsibility.** Four reasons to change, four places:

| Changes when... | Lives in |
|---|---|
| the schedule or run recording changes | `main.py` |
| the institution's login or navigation changes | backend (or the scraper's transport methods) |
| the institution's payload or page text changes | pure parsers |
| our product model changes | the mapping in `institutions/<slug>.py` |

Smells: SQL in a scraper, HTTP inside a parser, `input()` inside `scrape_*`,
a parser that reads `os.environ`.

**Open/closed.** New institutions are added by adding modules, and for e-mail
institutions by adding data (an `EmailPattern`) without touching
`backends/email.py`. Two spots are not closed today: the `if` chain in
`build_scrapers()` and the `_SCHEDULES` dict in `main.py`. Add your entry in the
existing style. Turning them into a registry is a good idea but a refactor,
so it goes in its own PR (never mix a refactor into a feature PR), not in the
scraper PR.

**Liskov substitution.** `run_scraper` must be able to run any scraper
blindly. So a scraper:
- has a constructor that only reads env vars (no network, no login, no file
  writes), because `build_scrapers()` constructs every enabled scraper at
  startup;
- is fully non-interactive inside `scrape_*`, with no prompts and no
  `sys.exit`. Interactive login belongs in a module-level `_login_cli()`
  behind `if __name__ == "__main__"` and a make target;
- signals failure by raising, never by returning an error value;
- returns `[]` or `ProductScrapeResult([])` for a leg the institution can't
  provide, with a one-line comment or docstring saying why (the e-mail
  scrapers' products leg: a notification carries no balance).

**Interface segregation.** `BaseScraper` is deliberately small: `method`,
`institution`, two legs, and an optional `superseded_transactions()` hook.
Don't add an abstract method to it for one institution's needs. Optional
capabilities (a `login()` coroutine, a CLI) stay on the concrete class.

**Dependency inversion.** Inject the things that vary or that tests must
replace:
- The 2FA **code provider** is a callable passed to `login()` (Fintual): a
  sync prompt from the CLI, an async IMAP reader when unattended.
- Build the HTTP client in **one place** (one `httpx.AsyncClient(...)` call
  site or a `_client()` method) so a test can swap in `httpx.MockTransport`
  by patching a single attribute, as `tests/test_buda.py` does.
- The backend facade is the seam for browser scrapers: the institution
  module calls `backends.<x>.scrape_<thing>(...)`, and tests monkeypatch that
  one function (`tests/test_bci_lider.py`).

## Patterns already in use

| Pattern | Where | Use it when |
|---|---|---|
| Template method / strategy | `BaseScraper` + `run_scraper` | always: it is the contract |
| Adapter | `institutions/<slug>.py` mapping source records onto envelopes | always |
| Facade | `backends/bci_lider_web.scrape_card`, `backends/banchile_movements.fetch_session` | the transport has many steps |
| Data-driven strategy | `EmailPattern` consumed by `fetch_transactions_for_pattern` | institutions differ only in configuration |
| Per-cycle session cache | `BciLiderScraper._cached` / `_error`, `FintualScraper._scrape_cycle` | one login must feed both legs |
| Async generator pagination | `BudaScraper._pages`, `FintualScraper._wallet_pages` | paged APIs; earlier pages survive a later failure |
| Value objects | frozen dataclasses for raw records; pydantic envelopes at the boundary | always |
| Typed, actionable errors | `FintualSessionError("... Run make fintual-login ...")` | any human-recoverable failure |

The per-cycle cache, in detail: the transactions leg runs first (that is
`run_scraper`'s order). It resets the cache, drives the session once, and
stores either the result or the exception. The products leg consumes the
stored result once, or re-raises the stored exception without driving again.
If called alone it drives the session itself. Resetting at the start of each
cycle matters: the scraper instance lives for the whole service lifetime.

## Identity: external_id and external_ref

`external_id` is the dedup key for transactions: the writer inserts with
`ON CONFLICT (product_id, external_id) DO NOTHING`. Getting it wrong means
duplicate spend or lost rows. Fixing it later is expensive, and each past fix
needed its own machinery:
- V017 re-keyed BCI Lider rows in SQL, which only works when the new key is
  computable from stored columns;
- V018 relies on the writer re-keying stored rows as new ids arrive, a path
  hardcoded for BanChile's `bch_` ids;
- `SupersededTransaction` (BanChile, with V023) deletes rows an earlier
  version filed on the wrong product.

1. **Prefix with an institution tag** that no other scraper uses
   (`buda_`, `bch_op_`, `bcl_`, `email_<slug>_`). Grep for it first.
2. **Prefer the institution's own ledger id**, and prove it is stable: the
   same movement must yield the same id across two separate logins. BanChile
   showed that fields named like ids (`idMovimiento`, `idComprobante`) can be
   session-scoped tokens that change on every login, and that a listing's
   `id` can be a composite that collides for batch movements. The real
   operation id may sit one request deeper.
3. **Hash fallback**: hash only fields that never change for the life of the
   movement: date, amount, currency. Keep descriptions, merchant names,
   installment counters and section labels out of it. The portal rewrites them
   when a charge is billed, and hashing them imported the same charge twice
   (see the `_movement_to_transaction` docstring in `bci_lider.py`). Document
   the collision limit (two identical charges on one day collapse).
4. **Pending vs posted**: if a movement appears first as pending and later as
   posted, make sure both states produce the same id, or the writer stores it
   twice.

`external_ref` identifies a product among several of the same
`(kind, currency)` (a Fintual goal id, one entry per deposit or fund). Use
`None` for singleton products. It must be stable across runs, or each run
creates a new product and splits the history.

## Mapping onto the product model

- Pick the `kind` from `packages/product-model/product_model/kinds.py` and the
  matching `*Metrics` / `*Attributes` classes from the registry. Read
  `packages/product-model/PRODUCTS.md` (generated field matrix) before
  inventing anything. Adding a kind takes pydantic classes, `make
  product-model-generate`, and a migration that widens the
  `products_kind_check` constraint (see `integration-checklist.md` §5).
  New fields on an existing kind need no DDL.
- One real-world product, one product row. Don't sum several products into
  one (V012 and V013 retired summed products); emit one observation each,
  told apart by `external_ref`.
- **The singleton rule.** By default a `ScrapedTransaction` lands on the
  product with the same `(institution, kind, currency)` **and no
  `external_ref`**: `db.writer.write_transactions` creates that product if it
  is missing. So:
  - a (kind, currency) whose transactions carry no product ref must be one
    singleton product (`external_ref=None`), and its `product_kind` /
    `currency` must match what the balance leg emits (Buda's comment on
    `PRODUCT_KIND`);
  - never mix a singleton and per-ref products of the same (kind, currency).
    The writer treats the singleton as a summed roll-up: the first per-ref
    write retires it (`_retire_rollup_sibling` deactivates it, nulls its
    balance and **deletes its snapshots**), and later writes to it are
    skipped. Transactions still land on the retired row, because product
    resolution ignores `is_active`.
- **Transactions on a per-ref product.** A multi-instance product (per goal,
  per deposit, per stock position: each with an `external_ref`) receives
  transactions only when they name it: set `product_external_ref` to the
  exact `external_ref` the balance leg emits for it (build both from one
  helper, as Fintual's `stock_ref` does). The writer looks that product up and
  **never creates it**.
  `run_scraper` writes transactions before products, so a holding's first
  movements find no product, are counted `unresolved` (a run warning,
  `partial`) and land on the next run. Never send such a row without its
  ref: it would create a ref-less roll-up of that kind and currency, which
  the next per-ref product write retires.
- **Movements between the user's own products** (a stock purchase paid from a
  cash wallet, a deposit from the user's bank) set `internal_transfer=True`.
  The writer stores it as `is_internal_transfer` on insert only, so a user's
  change survives re-scrapes, and the row stays out of spending and income.
  `scripts/dry_run.py` flags violations of these rules.
- **Daily price series** of a product's unit (a stock's closes) go in
  `ProductScrapeResult.price_histories` as `ScrapedPriceHistory`, naming the
  product with a `ProductRef` built from the same helper as its
  `external_ref` (Fintual's `stock_ref`). `run_scraper` writes them into
  `product_prices` after the products, lookup-only like an exchange rate, so
  send them only for products the same run emits. A market's daily bar is
  dated by its UTC date, the one exception to the America/Santiago rule
  below (Fintual's bars are stamped at New York midnight).
- `name` is used only when the product is created; user renames win
  afterwards. Use a generic label, never an account number.
- `metrics` is the per-scrape observation (snapshotted on change);
  `attributes` is slow-changing identity (shallow-merged). Bank-printed
  "as of" dates go in `metrics.reported_as_of` where the class has it;
  envelopes have no `as_of`, because the writer stamps the time.

## Money, dates and currencies

- Transactions are signed: negative is money out, positive is money in.
  Check the sign convention per surface. BanChile's billed and unbilled card
  legs use opposite signs.
- CLP amounts are whole (the envelope rejects fractions). Other currencies
  are quantized to cents. Parse money with `Decimal` and
  `ROUND_HALF_UP`, never `float` and never Python's `round` (banker's
  rounding). The existing browser parsers use `round`; don't copy that.
- Chilean formats: `$ 1.234.567` (dot thousands), `USD 1.234,56` (comma
  decimals), negatives as `-$ 1.000` or `$ -1.000` or in parentheses. Write a
  parser test for each form you meet.
- A movement in an unexpected currency is skipped with a warning, not coerced
  (Buda's `_parse_movement`).
- Timestamps in UTC must be converted to `America/Santiago` before you take
  the date, or evening movements land on the next day. The e-mail backend
  already does this.
- Use `accounting_date` when the source reports a posting date separately
  from the occurrence date. It is never part of the dedup key.
- Set `scheduled_month` on every transaction: the first of the month of
  `transaction_date` (`date(d.year, d.month, 1)`), or the month an
  installment or deferred charge lands. The Expenses page filters
  `/api/transactions?month=` on it, so a row without one never shows there.
  Buda, BanChile and the e-mail backend all set it. `bci_lider.py` doesn't
  today (a gap on `main`), so don't copy its `_movement_to_transaction`
  verbatim.
- Balances from delayed sources (an e-mail or statement days old): the writer
  stamps observations with the time of the write, so an old balance would
  overwrite a newer one (a manual entry, say). Put the printed date in
  `metrics.reported_as_of` where the kind has it, and don't emit a balance
  older than the newest known observation. If the kind lacks the field,
  adding it is a separate product-model change.
- Currency conversion to CLP happens in the web app (`apps/web/src/lib/rates.ts`).
  Scrapers report the native currency.

## Errors, warnings and logging

Three levels:

1. **Leg-fatal: raise.** Auth failed, the session expired with no way to
   refresh it unattended, or the top-level payload shape is unrecognizable.
   Use a typed exception (`<Institution>SessionError(RuntimeError)`) whose
   message says what the human should do ("Run `make <slug>-login`").
   `run_scraper` records it.
2. **Partial coverage: warn.** One surface failed after its retries, a page
   cap was hit, entries without ids were skipped. Add a short string to
   `ProductScrapeResult.warnings` (the run becomes `partial`) and log a
   warning. `scrape_transactions` returns a bare list, so it has no
   warnings channel. A transactions-leg warning goes on the instance
   (`self._warnings`, reset at the start of each cycle) and
   `scrape_products` returns it with its own, as BanChile does. The template
   shows the pattern.
3. **Row-level: skip.** One malformed movement is logged and skipped; the
   rest of the leg continues. Count skips instead of logging each one when
   there can be many.

Don't swallow a whole leg's failure and return `[]`: the run would be
recorded as `success` with zero rows and nobody notices it broke.

What may go into exception messages and logs: exception messages end up in
`scraper_runs.error_message`, which the dashboard shows. Never put tokens,
passwords, cookies, RUTs, account or card numbers, URLs that carry user ids,
or response bodies into them. Log counts and status codes, not payloads or
amounts. Some older modules log balances; don't add more of that. httpx
logs every request URL at INFO, so send tokens and API keys in headers,
never in the query string, and keep user or account ids out of paths
where the API allows it.

Idiom: `logger = logging.getLogger(__name__)` with %-style arguments
(`logger.warning("Buda %s failed: %s", tx_type, e)`), as in every existing
module.

## Configuration, secrets and session state

- **Env vars** are named `<SLUG>_<THING>` (`BUDA_API_KEY`, `FINTUAL_EMAIL`;
  `LIDER_BCI_*` is a historical exception). Read required ones with
  `os.environ["..."]` in `__init__` and optional ones with `.get`.
  `build_scrapers()` enables the scraper only when the required ones are set.
- **Secrets** (passwords, API secrets, tokens) live in the macOS Keychain as
  `chanchito.<VAR>`, set with `make secret-set KEY=<VAR>` and exported by
  `scripts/load-secrets.sh`. `.env.example` documents the names with empty
  values; non-secret config may live in `.env`.
- **Session caches**: a JSON file under `apps/scrapers/` named
  `.<slug>_session.json`, written with `chmod 0o600`, path overridable by
  `<SLUG>_SESSION_FILE` (Compose points it at the `scraper_state` volume).
  Add it to `.gitignore` in the same commit that creates it.
- **Browser profiles**: `.<slug>_chrome_profile/` under `apps/scrapers/`,
  gitignored in the same commit. A profile holds live cookies.
- Resolve default paths from `__file__` (`Path(__file__).resolve().parents[2]`)
  so the cwd doesn't matter.

## Async and concurrency

- `scrape_*` are coroutines on the scheduler's event loop. Blocking work goes
  off the loop: sync Playwright via `asyncio.to_thread` (preferred; older
  modules use `loop.run_in_executor`), IMAP through the existing backend
  (which already uses a lock and `to_thread`).
- APScheduler runs each institution with `max_instances=1` and
  `coalesce=True`, and manual refreshes reuse those guards, so a scraper never
  overlaps itself. It may overlap other scrapers, so share no mutable
  module-level state except deliberate singletons (the IMAP session).
- Set explicit timeouts on every network call (httpx `timeout=30.0` is the
  local norm) and bound every loop (`MAX_PAGES`, retry counts).
- Throttle bursts against a portal (BanChile spaces its detail calls and caps
  them). Politeness also lowers the fraud-check risk.

## Shared helpers and known duplication

There is no shared parsing module yet, and the browser backends duplicate
several helpers (CLP parsing, DD/MM/YYYY dates, "first visible" locators,
popup dismissal, inner-text JS). When your scraper needs one of these:

- Don't import a private helper from another institution's backend. That
  couples two institutions' release cycles.
- Don't add a third copy without saying so. Either extract the helper into a
  shared module (for example `scrapers/parsing.py`) in a separate, earlier
  refactor commit or PR, or keep a local, tested pure function and list the
  duplication in the PR description as a follow-up.

## Anti-patterns

- Fixed `sleep()` waits instead of waiting for an element, a response or a
  text predicate.
- Deep CSS selector chains or generated class names; prefer visible labels
  and whole-text parsing.
- Clicking anything that could start a transaction. Use exact-text matches
  (BanChile uses `:text-is`) and stay out of payment routes.
- Logging in twice per cycle when one session can feed both legs.
- Retrying a failed login in a loop (lockout and fraud-check risk). Retry
  idempotent reads; surface auth failures.
- Summing products, or reading the same product's movements from two sources.
- Hashing descriptions into `external_id`.
- Floats for transaction money.
- Committing a fixture "lightly edited" from a live dump. Fixtures are written
  from scratch with synthetic values (see `testing.md`).
