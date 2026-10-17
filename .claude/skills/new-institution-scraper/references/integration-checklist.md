# Integration checklist

Every file a new institution touches, in the order it is usually done. Line
numbers drift, so find each spot by the anchor named here. The BCI Lider
commit `6bd9cc3` is the most complete precedent of a whole new scraper: run
`git show --stat 6bd9cc3` to see its footprint.

## Contents

- [1. Scraper code](#1-scraper-code)
- [2. Service registration](#2-service-registration)
- [3. Configuration and secrets](#3-configuration-and-secrets)
- [4. Session state and login targets](#4-session-state-and-login-targets)
- [5. Product model (only if a kind is missing)](#5-product-model-only-if-a-kind-is-missing)
- [6. Database (usually only the institution seed)](#6-database-usually-only-the-institution-seed)
- [7. Web app (usually nothing)](#7-web-app-usually-nothing)
- [8. Tests](#8-tests)
- [9. Documentation](#9-documentation)
- [10. Final gates](#10-final-gates)

## 1. Scraper code

- [ ] `apps/scrapers/scrapers/institutions/<slug>.py`: one `BaseScraper`
      subclass with class attributes `method = "..."` and
      `institution = "<slug>"`, plus a module docstring that records the
      acquisition decision (technique, why the higher rungs were rejected,
      auth flow, what each leg returns). Read the BCI Lider and Fintual
      docstrings before writing yours.
- [ ] `apps/scrapers/scrapers/backends/<slug>_<transport>.py`, only for a big
      or shared transport (see `architecture.md`).
- [ ] `apps/scrapers/scrapers/institutions/__init__.py`: import the class,
      add it to `__all__` (alphabetical), and extend the package docstring's
      one-line summary of which backend each institution uses.
- [ ] If you introduce a new `method` value, update the docstrings that
      enumerate them: `BaseScraper` in `scrapers/base.py` and
      `start_scraper_run` in `db/writer.py`.
- [ ] New runtime dependency? Add it to `apps/scrapers/requirements.txt` with
      a bounded range and a comment saying who needs it. If it needs system
      packages, update `apps/scrapers/Dockerfile` too.

## 2. Service registration

All in `apps/scrapers/main.py`:

- [ ] Import the class in the `from scrapers.institutions import (...)` block.
- [ ] `build_scrapers()`: a gate that enables the scraper only when its
      required env vars are non-empty, keyed by the slug, with a
      `logger.info("<Name> scraper enabled")`. Add a comment when the gate is
      unusual (BCI Lider needs only the RUT because auth is a cached session).
      **E-mail institutions** don't get their own gate. They join the shared
      `EMAIL_IMAP_*` block, its log line, and in the tests the `email-trio`
      row and `_ALL`.
- [ ] `_SCHEDULES`: an entry `{"hours": N, "label": "<Name> <technique> (every Nh)"}`.
      Without it the scraper runs once in the startup scrape, logs a "No
      schedule configured" warning, and is never scheduled again. It also
      disappears from `GET /scrapers`, and `POST /refresh/<slug>` returns
      404. Pick the cadence from the technique's cost and risk:
  - e-mail: 30 minutes (shared `_EMAIL_INTERVAL_MINUTES`);
  - cheap official API: 1 hour;
  - private API with a cached session: about 6 hours;
  - any browser login: daily.
  Each login is account risk; the on-demand refresh covers the "I want it
  now" case.

## 3. Configuration and secrets

- [ ] `.env.example`: add the non-secret identifiers (RUT, e-mail, CDP URL)
      with empty values and a comment. Add each secret's name to the
      comment that lists Keychain-held secrets. Never put a real value in it,
      not even as an example; use `00.000.000-0` or `user@example.com`.
- [ ] `Makefile` → `SECRET_KEYS`: add each secret (password, API secret,
      token) so `make secrets-init` / `secrets-status` know it.
- [ ] `scripts/load-secrets.sh`: add the same names to its `for _key in ...`
      list. It is a separate list, not generated from `SECRET_KEYS` (the two
      already differ on legacy and web-only names), so a secret missing here
      is never exported.
- [ ] `docker-compose.yml` → `scrapers.environment`: add `VAR: ${VAR:-}`
      passthroughs, but only if the scraper can run headless in the
      container. A rung-7 (real Chrome) scraper needs a display, so leave it
      out of Compose and say so in the docs (as BCI Lider does). Session-file
      paths point into the `scraper_state` volume (`/data/<slug>_session.json`).

## 4. Session state and login targets

Only when the scraper caches a session, a browser profile, or needs a
human for some logins:

- [ ] `.gitignore`: add `.<slug>_session.json` and/or
      `.<slug>_chrome_profile/` (next to the Fintual and BCI Lider entries)
      in the same commit that introduces them.
- [ ] Docker build context: `apps/scrapers/Dockerfile` copies
      `apps/scrapers/` wholesale, so a local session file or browser profile
      would be baked into the image. Make sure a root `.dockerignore`
      excludes them; `main` has none yet and an open PR adds one. Add the
      entries to whichever exists.
- [ ] A `_login_cli()` in the institution module, run by
      `if __name__ == "__main__":`. It loads dotenv, configures logging,
      performs the interactive login and prints where the session went. Copy
      the shape of `fintual._login_cli` or `bci_lider._login_cli`.
- [ ] `Makefile`: a `<slug>-login` target
      (`@. ./scripts/load-secrets.sh && cd apps/scrapers && ../../.venv/bin/python -m scrapers.institutions.<slug>`)
      with a `## help` comment, and add it to `.PHONY`.
- [ ] The scraper's session error message names that target.

## 5. Product model (only if a kind is missing)

Reuse one of the existing kinds whenever it fits (`PRODUCTS.md` has the field
matrix). Reusing a kind needs no codegen.

Adding a kind touches:
- [ ] `packages/product-model/product_model/kinds.py`: both `PRODUCT_KINDS` and the `ProductKind` Literal.
- [ ] `attributes.py` / `metrics.py`: one pydantic class each. Every numeric
      metric needs `json_schema_extra={"denomination": "currency"|"percent"|"count"}`
      and the metrics class needs a `headline()`.
- [ ] `registry.py`: a `KindSpec` (label_es, family, role, balance_convention)
      and both discriminated unions.
- [ ] `display.py`: only if the kind needs a new display family (then also
      `PRODUCT_FAMILIES` in `kinds.py`).
- [ ] Tests: `tests/test_models.py` (`VALID_ATTRIBUTES`, `VALID_METRICS`) and `tests/test_display.py` (`EXPECTED_KINDS`).
- [ ] `make product-model-generate`, which rewrites `generated/index.ts`,
      `generated/product-model.schema.json` and `PRODUCTS.md`. Never edit
      those by hand. `tests/test_codegen.py` fails on drift.
- [ ] A migration that widens `products_kind_check` (defined in `V009`).
- [ ] Web spots with hardcoded kind sets: grep `apps/web/src` for an existing
      kind string such as `"line_of_credit"` (product graphs, institution
      shared components, the product page).

Adding a kind is its own reviewable change. If the scraper needs one,
consider landing the kind first.

## 6. Database (usually only the institution seed)

- [ ] **Institution row.** The writer creates a missing institution on first
      write with `name = slug` and `kind = 'other'`, so the scraper works
      without a migration, but the dashboard then shows the raw slug. Add a
      migration that seeds it properly:

      ```sql
      -- V0NN__seed_<slug>_institution.sql
      INSERT INTO institutions (slug, name, kind, country, url)
      VALUES ('<slug>', '<Display Name>', '<bank|fintech|exchange|asset_manager|other>', 'CL', 'https://...')
      ON CONFLICT (slug) DO UPDATE SET name = EXCLUDED.name, kind = EXCLUDED.kind,
        country = EXCLUDED.country, url = EXCLUDED.url, updated_at = now();
      ```

      The upsert also fixes a row the writer may already have auto-created
      in someone's database.
- [ ] **Migration number.** Take the next number after the highest in
      `packages/db-schema/migrations/`, then check that no open PR already
      claims it: `gh pr list --state open --json headRefName` and list each
      branch's `packages/db-schema/migrations/`. A gap on `main` (a
      missing V0NN) usually means an open branch holds that number.
- [ ] Never seed products, accounts, balances or transactions from a
      migration. They come from scrapes.
- [ ] Changing an existing scraper's `external_id` scheme needs dedicated
      re-keying machinery (an SQL re-key like V017, or writer-side adoption
      like V018, which today only handles BanChile ids). A new scraper avoids
      this by getting ids right first (see `architecture.md`).

## 7. Web app (usually nothing)

The dashboard is generic over institutions. The status card, refresh button
and scraper list come from `GET /scrapers` and the `institutions` table.
There are no logos. The only hardcoded institutions are legacy wealth
columns (`api/wealth/route.ts`, `history/page.tsx`, `lib/db/schema.ts`
naming fintual, mercadopago and banchile), which new institutions don't join.
Check anyway:
- [ ] `grep -rn "fintual" apps/web/src`. If a hardcoded list of institutions
      shows up beyond those legacy spots, extend it and mention it in the PR.
- [ ] If you added a `method` value, grep `apps/web/src` for `http_api` to see
      whether methods are labelled anywhere.

## 8. Tests

See `testing.md` for the content. Files:
- [ ] `apps/scrapers/tests/test_<slug>.py`.
- [ ] `apps/scrapers/tests/test_build_scrapers.py`: add the env vars to
      `_VALUES` (synthetic values), the slug to `_ALL`, and `pytest.param`
      rows for "all set", and for each "one missing" case.
- [ ] E-mail institutions: `.eml` fixtures in `tests/fixtures/emails/` and rows
      in `test_email_parser.py`'s pattern tables.
- [ ] `Makefile` → `scrapers-test`: add the class to its hardcoded import list.

## 9. Documentation

- [ ] `ARCHITECTURE.md`, section "Scraper Architecture":
  - the file tree;
  - a prose paragraph for the institution (technique and why, auth, what each leg reads);
  - the Institution / Method / Source / Auth / Schedule table.
  Also add its `external_id` format to "Deduplication", a row to the
  "Migrations" table for any migration you added, and update the system
  overview diagram if the institution list appears there.
- [ ] `USAGE.md`:
  - "Configuration": the `.env` block and the Keychain secrets table;
  - "Scrapers": a subsection if sign-in is special (model it on "Fintual sign-in" or "Tarjeta Lider Bci");
  - "Running scrapers on schedule": the schedule list.
- [ ] `README.md`: the institution list in "Overview", the count in
      "Highlights", login targets in "Usage", and the env and secret table
      in "Configuration".
- [ ] Docs describe structure and behaviour only: no balances, no figures
      from a live run, and no measured session lifetimes ("expires after
      several idle minutes" is fine).

## 10. Final gates

- [ ] `make test-py` passes. Run `make test-db` too if you touched the writer
      or a migration. CI runs both suites against a real Postgres.
- [ ] If you touched the web or the product model's TS output, also run
      `make typecheck`, `make lint` and `make test-ts`.
- [ ] `python3 .claude/skills/new-institution-scraper/scripts/scan_personal_data.py`
      on the staged diff, plus `--range main...HEAD`, `--log main..HEAD`
      (commit messages) and `--paths` on the drafted PR body before opening
      the PR, with every `REVIEW` hit resolved (see SKILL.md).
- [ ] `git status` shows no session file, browser profile, `.env`, or
      `local/` dump staged.
