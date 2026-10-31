---
name: new-institution-scraper
description: >-
  Build a new institution scraper end to end (recon, technique choice,
  BaseScraper module, tests, docs, PR), or move an existing one to a new
  technique.
---

# New institution scraper

A scraper here is a `BaseScraper` subclass in `apps/scrapers/scrapers/institutions/`
that turns one institution's data into `ScrapedProduct` / `ScrapedTransaction`
envelopes. The runner (`main.run_scraper`) persists them and records the
run, and `main.py` schedules it. The hard part is never the class: it is
choosing how to get the data. That choice decides whether the scraper runs unattended for years or
breaks every month and locks the user out of their bank.

So this skill is ordered: **understand, choose, design, then build**. Don't
write a selector before you know there is no JSON endpoint, and don't write
an endpoint client before you know there is no official API.

## The acquisition ladder (best first)

Every rung is a legitimate option. Start at rung 1 and step down only when
evidence rules the current rung out. Decide **per leg** (balances vs
transactions) and **separately for auth and data**.

| # | Technique | `method` | Why it ranks here |
|---|---|---|---|
| 1 | **Official API**: open finance, developer API, user-issued keys | `open_banking` / `http_api` | Documented contract, unattended, cheapest. Precedent: Buda. |
| 2 | **Maintained library or aggregator** already covering it | `fintself` / the library's technique | Someone else maintains it. Rank it by the library's own technique. |
| 3 | **Private web API over plain HTTP**, login reproduced in httpx | `http_api` | No browser; cached session plus IMAP 2FA keeps it unattended. Precedent: Fintual. |
| 4 | **Passive feeds**: notification or statement e-mails over IMAP | `email` | Zero login risk, but transactions only and heuristic. Precedent: MACH, Mercado Pago, Tenpo. |
| 5 | **Headless browser for login, JSON for data** | `web` | Browser only where needed; data stays structured. Precedent: BanChile movements. |
| 6 | **Headless browser DOM scraping** (incl. mobile-web device emulation) | `web` | Breaks on redesigns; heavy runtime. Precedent: BanChile balances. |
| 7 | **Real Chrome over CDP** (genuine browser for anti-bot or browser-bound sessions) | `web` | Needs a display, sometimes a human login. Precedent: BCI Lider. |
| 8 | **Mobile app private API**, reverse-engineered | `http_api` / new `mobile_api` | Cheap at runtime, costly and fragile to build: pinning, signing, attestation. |
| 9 | **Native app UI automation** on an emulator or device (Appium) | new `mobile_app` | Heaviest, slowest, flakiest. Last resort for app-only institutions. |
| - | **Escape hatch**: no scraper (manual entry, statement import) | - | A documented "won't scrape, here's why" is a valid outcome. |

Read `references/acquisition-ladder.md` before choosing. It has, per rung,
the signals, the probe that confirms it, the implementation shape, the
gotchas this repo already paid for, and when to step down.

## Hard rules (every rung, every phase)

1. **No real personal or financial data in the repo.** That covers code,
   tests, fixtures, docstrings, comments, migrations, commit messages, PRs and
   issues. Fixtures are written from scratch with the canonical synthetic
   values (`references/testing.md`). Live dumps live only in gitignored
   `local/`. Report live results as counts and shapes.
2. **Read-only.** Never call or click anything that moves money, changes
   settings, accepts terms or enrolls a device, during recon or at runtime.
3. **Don't defeat bot protection.** No CAPTCHA-solving services, no clicking
   challenges, no stealth plugins or fingerprint spoofing. A realistic
   context (locale, timezone, desktop user agent) is fine, and so is a
   genuine browser that passes on its own (rung 7). A check that wants a
   human goes to the human through a `make <slug>-login` target.
4. **Protect the account.** Cap login attempts, never retry a failed login in
   a loop, and reuse sessions. Logging in costs account risk: schedule a
   browser login daily at most.
5. **Secrets stay local.** Passwords and tokens live in the secret store
   (`make secret-set`), session caches are 0600 and gitignored. Exception
   messages never carry tokens, URLs with ids, or response bodies: they are
   stored in `scraper_runs.error_message` and shown on the dashboard.
6. **Live access needs the user present.** If they are away, do desk research
   only, build against synthetic fixtures, and list what is unverified.

## Workflow

### 0. Frame the request

- Name the institution and its **slug**: lowercase snake case, stable forever
  (`banco_estado`, not `BancoEstado`). It keys the DB row, env vars
  (`<SLUG>_*`), the schedule and the refresh endpoint.
- List the **products** you need (checking, credit card CLP/USD, investments,
  ...) and, per product, the **legs**: balance, transactions, or both.
- Check what exists:
  - `grep -rn 'institution = ' apps/scrapers/scrapers/institutions/`;
  - `gh issue list --search "<name>"` (the issue may already hold research);
  - `gh pr list` (open PRs touching `main.py`, the writer or migrations will
    conflict; note them).

### 1. Recon and choose the rung

Follow `references/reconnaissance.md`: desk research first, live recon only
with the user. Walk the ladder per leg. Write the **decision record**
(template at the end of `acquisition-ladder.md`): the chosen rung per leg,
the evidence for rejecting each higher rung, what was verified live, and what
would let it climb later. This record goes in the PR and, condensed, in the
module docstring.

If you are about to choose rung 6 or lower, stop and re-check rung 5: is
there really no JSON behind the page? If you are about to choose rung 8 or 9,
confirm with the user. Those rungs carry terms-of-service and device-binding
consequences they should decide on.

### 2. Design before code

Read `references/architecture.md`, then decide and write down:
- **Module layout.** Self-contained `institutions/<slug>.py`, or plus a
  `backends/<slug>_<transport>.py` facade (big or shared transports only).
- **Product mapping.** An existing kind and metrics class from
  `packages/product-model/PRODUCTS.md`; a new kind is a separate change.
  The **singleton rule**: transactions land on the product with no
  `external_ref` for their (kind, currency), so a (kind, currency) that gets
  transactions must be one singleton product, unless each transaction names
  its per-ref product in `product_external_ref` (looked up, never created).
  Every transaction also sets `scheduled_month`, or the Expenses page never
  shows it.
- **Identity.** The `external_id` scheme (prefix, source id proven stable
  across logins, or a hash of immutable fields only) and the `external_ref`
  for multi-instance products.
- **Session strategy.** Token, cached cookies, IMAP 2FA, `make <slug>-login`,
  or one browser drive per cycle cached across both legs.
- **Failure policy.** What raises (auth), what warns (partial coverage), what
  skips (one bad row).
- **Cadence.** From the technique's cost and risk (`integration-checklist.md` §2).

### 3. Build

- Branch `feat/<slug>-scraper` from `main`. Use a separate worktree if the
  main checkout has someone else's uncommitted work.
- Start from what fits the rung:
  - Rungs 1 and 3: copy `templates/http_api_scraper.py` and
    `templates/test_http_api_scraper.py`. Both are verified to pass as-is
    once copied in as `acme`. Graft in Fintual's session cache and 2FA
    methods for rung 3.
  - Rung 4: an `EmailPattern` in a module shaped like `institutions/mach.py`,
    plus `.eml` fixtures.
  - Rungs 5 to 7: model the backend on `backends/banchile_movements.py`
    (XHR capture, in-page fetch) or `backends/bci_lider_web.py` (CDP, managed
    and reuse modes, per-cycle cache in `institutions/bci_lider.py`).
  - Rungs 8 and 9: no precedent. Keep the same envelope boundary so the
    transport can later run elsewhere.
- Write the pure parsers and their tests first. They hold the institution's
  quirks and need no fakes.
- Match the surrounding code: `logging.getLogger(__name__)` with %-style
  args, docstrings that explain why, type hints, no new test libraries.

### 4. Register and configure

Walk `references/integration-checklist.md` top to bottom:
- `__init__` export;
- `build_scrapers()` gate (e-mail institutions join the shared IMAP block) and
  `_SCHEDULES` entry (without one the scraper runs once at startup, then never
  again, and can't be refreshed);
- `.env.example`;
- `SECRET_KEYS` in `scripts/secret_store.py` (the one key list);
- Compose passthrough (headless-capable scrapers only);
- `.gitignore` for session files and profiles;
- the login target;
- the institution seed migration;
- the logo (`apps/web/public/logos/institutions/` plus its entry in
  `apps/web/src/lib/institution-logos.ts`);
- `test_build_scrapers.py` rows;
- the `scrapers-test` import list;
- docs.

### 5. Test

Follow `references/testing.md`. Then run `make test-py` (and `make test-db` if
you touched the writer or a migration). CI runs no browser, so no test may
launch one.

### 6. Verify live (user present)

```bash
. ./scripts/load-secrets.sh && .venv/bin/python \
  .claude/skills/new-institution-scraper/scripts/dry_run.py <slug>
```

`dry_run.py` runs both legs in `run_scraper`'s order without touching the DB,
prints only shapes (counts, kinds, id prefixes, field presence) and checks the
writer's invariants:
- unique and prefixed ids;
- transactions landing on a singleton product, or on a product the run emits
  under the `product_external_ref` they name (a miss is a note: the writer
  lands the row only if that product is already stored);
- no duplicate or mixed product identities.

Prove the ids are stable with two runs on separate logins: the first with
`--dump local/<slug>-1.json` (full envelopes, stays in gitignored `local/`),
the second with `--compare local/<slug>-1.json`. The second prints only
counts of shared and new ids and product identities. If the user is away,
skip this step and say so in the PR.

### 7. Ship

- Privacy gate:
  `python3 .claude/skills/new-institution-scraper/scripts/scan_personal_data.py`.
  It checks the staged diff by default. Before the PR, also run it with
  `--range main...HEAD` (everything the branch adds), with `--log main..HEAD`
  (commit messages), and with `--paths` on the PR body drafted to a scratch
  file. Read every `REVIEW` line and replace anything that isn't synthetic,
  then glance at the `note` lines.
- Check that `git status` shows no `.env`, session file, browser profile or
  `local/` content staged.
- Commits: gitmoji, Sentence-case title of at most 72 chars, English, no em
  dashes, and no `Claude-Session:` trailer (`Co-Authored-By` is fine). Use
  separate commits for the parsers and tests, the scraper and its
  registration, and the docs. Put any refactor (for example extracting a
  shared CLP parser) in its own earlier commit or PR.
- PR: base `main`, English body, the decision record, how it was verified
  (counts only), what is unverified, and follow-ups (duplication you left,
  a rung it could climb to later). Use the repo's PR skill if one is
  configured.

## Reference map

| File | Read it when |
|---|---|
| `references/acquisition-ladder.md` | choosing or justifying a technique; per-rung gotchas |
| `references/reconnaissance.md` | investigating a new institution, desk or live |
| `references/architecture.md` | designing the module: layers, SOLID, patterns, ids, money, errors, config |
| `references/integration-checklist.md` | wiring it into the service, config, DB, docs |
| `references/testing.md` | writing the suite and the synthetic fixtures; live verification |
| `templates/` | starting a rung 1 or 3 scraper and its tests |
| `scripts/json_shape.py` | reading captured JSON or HAR files during recon without exposing values |
| `scripts/dry_run.py` | live verification without DB writes; `--compare` for id stability |
| `scripts/scan_personal_data.py` | before every commit |

## When the codebase has moved on

This skill describes `main` as of its writing. Before relying on a specific
detail, read the current `scrapers/base.py`, `main.py` (`build_scrapers`,
`_SCHEDULES`, `run_scraper`), `product_model/envelopes.py` and the newest
institution module. If they disagree with this skill, the code wins: follow
it and mention the drift. Changes in flight at the time of writing:
- a shared `scrapers/retry.py` for idempotent GETs and partial-aware run
  outcomes (scraper reliability work);
- a BanChile `storage_state` session cache with a login cooldown;
- Mercado Pago balances via its official API;
- a root `.dockerignore` (bundled with the BanChile session-cache work).

Reuse whichever of them has landed rather than re-implementing it.
