# Testing a new scraper

The tests must prove the scraper's logic without a network, a browser, a
mailbox or a real account. Every value in them is synthetic.

## Contents

- [House style](#house-style)
- [Faking each technique](#faking-each-technique)
- [Synthetic data](#synthetic-data)
- [The minimum suite](#the-minimum-suite)
- [Running](#running)
- [Live verification (outside the test suite)](#live-verification-outside-the-test-suite)

## House style

Match the existing files; `tests/test_buda.py` is the best single template.

- One `apps/scrapers/tests/test_<slug>.py`. A module docstring says what is
  faked and that every figure is synthetic.
- Plain pytest classes `TestXxx`, one per unit (parser, id builder, each leg).
  Use `setup_method` when each test needs a fresh scraper.
- A one-line docstring per test stating the behaviour.
- Arrange / act / assert separated by single blank lines, no inline comments.
- `@pytest.mark.parametrize` with a tuple of argnames and `pytest.param(..., id=...)`
  for the table cases (skip reasons, number formats, sign conventions).
- No pytest-asyncio: call `asyncio.run(scraper.scrape_products())`.
- Mocking with `monkeypatch`, `caplog` and stdlib `unittest.mock`
  (`MagicMock`, `AsyncMock`). The repo does not use pytest-mock, respx or
  freezegun. Don't add them for one scraper.
- Env vars the constructor needs: `os.environ.setdefault("<SLUG>_X", "synthetic-...")`
  before importing the module, and `monkeypatch.setenv` / `delenv` inside tests.
- Fixtures and fakes live in the test file that uses them. `conftest.py` holds
  only the DB fixtures. Builders are file-local `_row(...)` functions with
  canonical synthetic defaults that mirror the source's payload shape.
- Never name a fixture `REAL_*`, even when its shape mirrors a real page.

## Faking each technique

| Technique | Fake | Precedent |
|---|---|---|
| Official or private HTTP API | Real `httpx.AsyncClient` over `httpx.MockTransport`, backed by a small stateful fake server class that records requests, checks auth headers, paginates and can inject 4xx/5xx; patch the module's `httpx.AsyncClient` in one `_install()` | `test_buda.py` (`_Buda`, `_install`) |
| HTTP with sequenced responses | Hand-written async-context fake clients returning `MagicMock` responses | `test_fintual.py` (`_FakeClient`, `_SequencedClient`) |
| Login and 2FA | `AsyncMock` for `login`, stubbed `_load_session`, a fake code provider callable | `test_fintual.py` |
| Browser, DOM text | Test the pure text parsers against synthetic page-text constants; for page logic, a `MagicMock` page whose `evaluate` returns the text, or a small stateful SPA simulator with failure injection | `test_bci_lider.py`, `test_banchile_web.py` (`_fake_page`, `_fake_portal_page`) |
| Browser, XHR capture | Fake page, locator and response objects that fire the registered `page.on("response")` handlers | `test_banchile_movements.py` (`_FakeCardPage`, `_FakeResponse`) |
| Real Chrome over CDP | Never launched. Monkeypatch the backend facade (`scrape_card`) with an async fake that returns the result dataclass, and count calls to prove one drive per cycle | `test_bci_lider.py` (`_install`) |
| E-mail | `.eml` fixtures loaded with `load()`, inline `build()` messages, and the in-memory `FakeMail` IMAP via the `imap` fixture | `test_email_parser.py` |
| Runner integration | `_StubScraper` plus the `finish` fixture | `test_main.py` |

CI downloads no Playwright browser, so a test that launches one fails in CI.
This is deliberate.

## Synthetic data

The canonical scheme from the repo policy:

| Thing | Use |
|---|---|
| Account numbers | `00-000-00000-01`, `00-000-00000-02`, ... |
| Card last 4 | `1234` or `0000`; masked as `XXXX XXXX XXXX 1234` or `****0000` |
| CLP amounts | `$ 2.500.000`, `$ 1.000.000`, `$ 999.999`, or obviously round values |
| USD amounts | `USD 1.234,56` |
| RUT | `00.000.000-0` / `00000000-0` |
| People and merchants | "Persona Sintetica", "COMERCIO SINTETICO", "SYNTHETIC STORE 1" |
| Ids | zero-padded (`000...001`), prefixed like the real scheme (`<tag>_000...001`) |
| Secrets | `synthetic-password`, `synthetic-key`, `not-a-secret` |
| E-mail | `user@example.com`; copy an existing `.eml`: they carry an `X-Fixture: synthetic` header, a synthetic `Message-ID` and the recipient "Usuario Sintetico" (rules in `tests/fixtures/emails/README.md`) |

Write fixtures from scratch so they mirror the structure of what you
observed: field names, nesting, text layout, label wording. Don't start from a
live dump and edit the numbers; a missed field leaks. Raw dumps stay in the
gitignored `local/` directory.

## The minimum suite

1. **Parsers**:
   - the happy path;
   - the sign convention (money out negative, refunds and deposits positive);
   - each number format met (`$ 1.234.567`, `USD 1.234,56`, negatives);
   - rounding (half-up);
   - currency handling (unexpected currency skipped with a warning);
   - parametrized skip cases with ids (missing id, failed state, zero amount);
   - `caplog` assertions on the warnings.
2. **Identity**:
   - the `external_id` prefix;
   - stable across calls;
   - distinct for distinct movements;
   - unchanged when only cosmetic fields change (description rewrite,
     installment counter), as `test_bci_lider.py` does;
   - `external_ref` stable for multi-instance products.
3. **Both legs end to end through the fake**:
   - pagination and the page cap;
   - a later page failing keeps the earlier pages;
   - partial coverage reaching `ProductScrapeResult.warnings`;
   - a fatal auth failure raising the typed, actionable error.
4. **Envelope agreement.** Products carry the right institution, kind,
   currency and `external_ref`, and the transactions' `(product_kind,
   currency)` match a product the balance leg emits; a transaction with a
   `product_external_ref` matches the `(kind, currency, external_ref)` of
   one. Movements between the user's own products set `internal_transfer`.
5. **Session reuse**, when both legs share one login: one drive per cycle,
   the products leg re-raises the cached error without driving again, and the
   cache resets between cycles.
6. **Secrets never surface.** When an auth or HTTP error is raised, its
   message contains neither the token or password nor ids from the URL (the
   template's `test_rejected_token_raises_without_leaking_it` shows the
   assertion).
7. **Dashboard fields.** Every transaction sets `scheduled_month`, since the
   Expenses page filters on it.
8. **Partial coverage** from the transactions leg reaches the products leg's
   `warnings` (template: `test_early_stop_surfaces_as_a_products_warning`).
9. **Gating.** Rows in `test_build_scrapers.py` for "everything set" and for
   each missing required variable.

## Running

```bash
make install-py     # once: .venv + requirements-dev.txt (pytest)
make test-py        # scrapers + product-model suites; DB tests skip locally
make test-db        # same, against a throwaway tmpfs Postgres (port 5436)
cd apps/scrapers && ../../.venv/bin/python -m pytest tests/test_<slug>.py -v
```

There is no ruff or mypy configuration in this repo, and CI runs only
pytest for Python. Still write type hints in the style of the existing
modules.

## Live verification (outside the test suite)

A live run is the only proof that the scraper works against the real
institution, and it uses the user's real account. So:

- Do it only with the user present and consenting: they provide the
  credentials and any 2FA code.
- Run read-only with the minimum number of logins, through
  `scripts/dry_run.py` (see SKILL.md step 6). It never writes to the DB,
  and its `--dump` / `--compare` pair proves id stability across two logins.
  Avoid `make scrapers-once`, which runs every configured scraper and
  writes to the database; run a real `run_scraper` only once the dry runs
  look right.
- Send output and dumps to `local/`. Report the result in counts and shapes
  ("4 products, 37 movements, all ids distinct across two logins"), never
  figures.
- Verify id stability with two separate logins before trusting a ledger id.
- If you can't verify live (user away, no credentials), say so plainly in the
  PR and mark the unverified assumptions in the code.
