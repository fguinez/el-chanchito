# The acquisition ladder

How to choose how a scraper gets its data. The rungs run from best (1) to
worst (9), and every rung is a valid choice for some institution. Start at the
top, gather evidence, and step down one rung only when that evidence rules the
current one out. Write down why each rung above the chosen one was rejected.

## Contents

- [How the rungs are ranked](#how-the-rungs-are-ranked)
- [Ladder at a glance](#ladder-at-a-glance)
- [Rules for walking the ladder](#rules-for-walking-the-ladder)
- [Rung 1: official API](#rung-1-official-api)
- [Rung 2: maintained library or aggregator](#rung-2-maintained-library-or-aggregator)
- [Rung 3: private web API over plain HTTP](#rung-3-private-web-api-over-plain-http)
- [Rung 4: passive feeds (e-mail)](#rung-4-passive-feeds-e-mail)
- [Rung 5: headless browser login, HTTP data](#rung-5-headless-browser-login-http-data)
- [Rung 6: headless browser DOM scraping](#rung-6-headless-browser-dom-scraping)
- [Rung 7: real Chrome over CDP](#rung-7-real-chrome-over-cdp)
- [Rung 8: mobile app private API](#rung-8-mobile-app-private-api)
- [Rung 9: native app UI automation](#rung-9-native-app-ui-automation)
- [Escape hatch: no scraper](#escape-hatch-no-scraper)
- [Decision record template](#decision-record-template)

## How the rungs are ranked

Six criteria decide the ranking. The first two count most:

| Criterion | Question | Why it matters here |
|---|---|---|
| Unattended | Can it run on its schedule with no human? | The service runs on APScheduler and on demand ("Actualizar todo"). A human-gated scraper goes stale. |
| Stability | How often will it break without warning? | A documented contract almost never breaks; DOM selectors and app screens break often. |
| Fidelity | Does it yield balances, transactions, stable ids and exact amounts? | Dedup is by `external_id`; balances feed net worth. |
| Footprint | What does it need at runtime? | httpx is free. Chromium is heavy. A headed Chrome needs a display. An emulator needs an SDK and virtualization. |
| Account risk | Can it lock the account, trip fraud checks or breach terms? | Every extra login is a risk; some portals bind sessions to a device. |
| Build cost | How long until it works, and how hard is it to verify? | Live verification costs a real banking session each attempt. |

## Ladder at a glance

| # | Rung | `method` | Unattended | Stability | Footprint | Repo precedent |
|---|---|---|---|---|---|---|
| 1 | Official API (open finance, developer API, user-issued keys) | `open_banking` / `http_api` | yes | high | httpx | Buda (`institutions/buda.py`) |
| 2 | Maintained library or aggregator | `fintself` or the library's own technique | usually | depends on the library | the library's | BanChile once ran on `fintself` (see `V008` backfill) |
| 3 | Private web API over plain HTTP, login reproduced | `http_api` | yes, or with a cached session | medium | httpx | Fintual (`institutions/fintual.py`) |
| 4 | Passive feeds: notification or statement e-mails over IMAP | `email` | yes | medium-high | IMAP | MACH, MercadoPago, Tenpo (`backends/email.py`) |
| 5 | Headless browser for login, private API for data | `web` | yes | medium | Chromium | BanChile movements (`backends/banchile_movements.py`) |
| 6 | Headless browser DOM scraping | `web` | yes | low | Chromium | BanChile balances (`backends/banchile_web.py`) |
| 7 | Real Chrome over CDP (genuine browser, anti-bot) | `web` | partly: needs a display, some logins need a human | low-medium | Chrome + display | BCI Lider (`backends/bci_lider_web.py`) |
| 8 | Mobile app private API, reverse-engineered | `http_api` (or a new `mobile_api`) | yes once built | low | httpx, plus device-bound secrets | none yet |
| 9 | Native app UI automation on an emulator or device | new `mobile_app` | fragile | very low | Android SDK, emulator, Appium | none yet |

`scraper_runs.method` has no CHECK constraint (`V008`), so a new value is
legal, but grep `apps/web/src` for the existing values before adding one: the
dashboard may label methods.

## Rules for walking the ladder

1. **Decide per leg.** Balances (`scrape_products`) and transactions
   (`scrape_transactions`) may sit on different rungs. MACH, for example, has
   transactions from e-mail and no balances at all.
2. **Decide auth and data separately.** The rung that gets you a session can be
   lower than the rung that reads the data. Rung 5 is exactly that: a browser
   only for the login, then JSON. Always ask "once logged in, is there a JSON
   endpoint?" before you write a selector.
3. **One source per product.** Two sources feeding the same product's
   transactions double-count, since their ids never match. Portals often
   expose two overlapping feeds: an "activity" stream and the ledger. During
   an unmerged Mercado Pago web experiment, the activity feed overlapped the
   ledger and lacked interest rows. Pick the ledger.
4. **Re-evaluate after stepping down.** Once a lower rung is unavoidable for one
   leg, check the other leg again. Reading both legs from that one session
   (BCI Lider caches one browser result for both legs) usually beats adding a
   second, higher-rung source with its own ids.
5. **Evidence, not vibes.** "Has no API" needs a search of the developer site,
   the app's network traffic, and the OSS libraries. "Blocks headless" needs a
   recorded attempt. Put the evidence in the decision record.
6. **Hard limits apply on every rung.**
   - Read-only: never call an endpoint that moves money, changes settings or
     accepts terms. Recon included.
   - Never wire in a CAPTCHA-solving service and never try to defeat a bot
     check. A genuine browser that passes on its own is fine (rung 7); a check
     that demands a human goes to the human through a `make <slug>-login`
     target. A realistic browser context is fine and often needed for
     correct rendering: `es-CL` locale, Santiago timezone, a current desktop
     user agent, a normal viewport. BanChile also masks
     `navigator.webdriver`; treat that as the ceiling. Stealth plugins,
     fingerprint spoofing, residential proxies or behavioural mimicry to get
     past a check that is blocking you are evasion. Step down to rung 7 or
     hand the login to the human instead.
   - Cap login attempts. A lockout costs the user a branch visit, and
     repeated failed logins can trip fraud checks.
   - Credentials stay on the user's machine. Handing them to a third party
     (rung 2 aggregators) is the user's decision, never a default.
7. **Unverified is allowed, but say so.** If you could not confirm a field live,
   build on the documented or observed shape and say so in the code (Buda's
   docstring flags its unverified "anulled" spelling).

---

## Rung 1: official API

**What.** A documented API for individuals: a developer portal with API keys
the user issues (Buda's HMAC keys), OAuth2 for personal apps, or the open
finance rails. Chile's Open Finance System (SFA), created by Ley Fintec
(Ley 21.521) and regulated by the CMF, is rolling out in phases. Check
whether the institution already exposes it to individuals, and whether access
needs a registered third-party provider (which a personal tracker usually is
not).

**Signals.** A "Developers" or "API" page, "API keys" in account settings,
public docs or an OpenAPI spec, SDKs on PyPI or npm, mentions in the
institution's help center.

**Probe.** Read the docs. Issue a read-only key if possible. Call the balance
endpoint once with httpx and compare it with the app.

**Shape.** Self-contained `institutions/<slug>.py` with httpx; signing or token
logic in a private method; pagination as an async generator; pure
`_parse_*` functions. Model it on `institutions/buda.py`.

**Watch for.** Read-only key scopes (ask for the least privilege; a
trade or withdraw scope is a liability), rate limits, nonce or clock skew in
signatures, amounts as strings or as `[value, currency]` pairs, pagination
metadata that is sometimes missing (Buda stops on a short page as a fallback).

**Step down when.** No public API exists, it is business-only, it does not
cover the products you need, or onboarding requires a regulated entity.

## Rung 2: maintained library or aggregator

**What.** Someone else already maintains the integration:
- an OSS library such as `fintself` (Chilean banks), which became the
  `fintself` method value;
- a commercial aggregator API (for example Fintoc). It holds your bank
  credentials and usually needs a contract, so check its current coverage and
  terms first.

**Adopt only if** the library's own technique ranks no lower than what you
could build yourself, it is actively maintained (recent releases, issues
answered), it can be pinned in `requirements.txt`, and it covers the legs you
need. A library that drives Playwright is a rung-6 scraper you don't control.
Classify it at rung 6, not rung 2.

**Always** read its source even if you don't adopt it: it is the cheapest
recon there is (endpoint paths, login steps, selectors, id fields). A
leftover `fintself` may already be installed in `.venv`; see
`reconnaissance.md`. Credit it
in the module docstring with a full URL when it informed the design.

**Shape.** A thin adapter in `institutions/<slug>.py` that maps the library's
records onto `ScrapedProduct` / `ScrapedTransaction`. Keep the library's types
out of everything except that adapter, so swapping it out later touches one
file.

**Step down when.** No maintained library covers the institution, or it sits
lower on the ladder than rung 3 would.

## Rung 3: private web API over plain HTTP

**What.** The institution's own web app talks to a JSON backend (BFF, REST,
GraphQL). You reproduce its login with httpx and call the same endpoints.
No browser at runtime.

**Signals.** DevTools Network shows `fetch`/XHR returning JSON for balances and
movements. The login is a JSON POST rather than a form post guarded by a
script-generated token. No bot challenge on the login page.

**Probe.** Record the login and one data call, then replay them with httpx
from a script under `local/` (gitignored). If the replay gets a 401 or 403,
compare headers (User-Agent, Origin, Referer, CSRF token) and cookies before
giving up. Fintual's session endpoints, for example, misbehave without a
desktop User-Agent.

**Auth patterns that keep it unattended:**
- **Cached session.** Persist cookies to a 0600 JSON file under
  `apps/scrapers/` (gitignored), path overridable by `<SLUG>_SESSION_FILE` so
  Docker can point it at the `scraper_state` volume. Reuse the cache; re-login
  only on 401.
- **Refresh before re-login.** When the site keeps its own session alive with
  a refresh-token cookie, take that path on a 401 instead of a new login (for
  Fintual, loading `/app/goals` re-issues the short-lived token). Refresh
  tokens often rotate on every use, so persist the new cookies immediately.
- **E-mailed 2FA codes.** When the code goes to the IMAP mailbox the service
  already reads, fetch it with `backends.email.fetch_latest_code` (Fintual
  does this when `FINTUAL_EMAIL == EMAIL_IMAP_USER`). Its subject filter
  ignores case and accents; its sender filter is a case-insensitive
  substring. Confirm the real sender and subject live before relying on
  them. Pass `received_after` (taken before the login starts, minus a small
  clock skew) so a previous login's code still in the inbox is never reused.
  Make an unattended re-login give up after one failure (until a
  manual login succeeds): an OTP loop mails the user on every run and looks
  like an attack.
- **Manual fallback.** A `_login_cli()` under `if __name__ == "__main__"` plus a
  `make <slug>-login` target prompts for the code once and caches the session.
  Scheduled runs never prompt; they raise a typed error that says which make
  target to run.
- Inject the code provider as a callable (sync prompt or async IMAP reader) so
  login logic doesn't care where the code comes from.

**Step down when.** The login needs JavaScript-computed tokens you can't
reproduce reasonably (obfuscated fingerprinting, encrypted password fields with
rotating keys, WebAuthn-only), or a bot challenge guards it.

## Rung 4: passive feeds (e-mail)

**What.** Read what the institution already sends you: per-transaction
notification e-mails (MACH, MercadoPago, Tenpo), or periodic statement e-mails
with a PDF or CSV attached. No login to the institution at all.

**Strengths.** Zero account risk, no browser, survives portal redesigns, and the
backend exists: an `EmailPattern` plus `fetch_transactions_for_pattern` is a
whole scraper (see `institutions/mach.py`).

**Limits.**
- Transactions only, almost never balances.
- Coverage depends on the user's notification settings.
- Parsing is heuristic: sender domains, subject keywords, amount anchors,
  merchant regexes, income keywords.
- Ids come from the message, not the ledger.
- A statement PDF needs a new parsing dependency. Justify it in the PR.
- A balance read from an e-mail is days old by design, and the writer stamps
  observations with the write time, so it can overwrite a newer balance (a
  manual entry, say). See "Money, dates and currencies" in `architecture.md`
  before emitting one.

**Signals.** The institution sends e-mails like "Compraste en ...",
"Recibiste una transferencia ...", or a monthly "cartola".

**Shape.** A new `EmailPattern` in `institutions/<slug>.py` with tight
`sender_domains` (the MACH module explains why Bci's own domain is excluded),
plus `.eml` fixtures under `tests/fixtures/emails/` following that directory's
README.

**Step down when.** You need balances, the institution doesn't notify every
movement, or you need ledger ids. Often you add a second leg from another rung
for balances instead of stepping down for both.

## Rung 5: headless browser login, HTTP data

**What.** Playwright (headless Chromium) performs the login a plain HTTP client
can't, then the data comes from the portal's JSON endpoints:
- `fetch(url, {credentials: "include"})` evaluated inside the logged-in page,
  which rides the session cookies;
- `page.on("response")` listeners on the XHRs the SPA fires anyway;
- `context.request`, or exporting the cookies into httpx.

**Why it beats rung 6.** JSON fields don't move when the CSS changes, carry
real ids, and paginate predictably. BanChile's movements replay the portal's
BFF calls this way (the real operation id sits one XHR deeper than the
listing).

**Signals.** Rung 3 failed only at login; the data endpoints are plain JSON once
authenticated.

**Session reuse.** Playwright `storage_state` (cookies plus localStorage) can
restore a session across runs. Validate it with one cheap navigation before
trusting it, and gate full logins behind a cooldown so an on-demand refresh
burst can't hammer the login. Don't put measured session lifetimes in the repo
(they count as banking-session timelines); say "several idle minutes".

**Shape.** A `backends/<slug>_web.py` facade exposing one coroutine (for example
`fetch_session(...)` or `scrape_<thing>(...)`) that returns a dataclass of raw
records. The institution module maps that onto envelopes.

**Step down when.** The data exists only as rendered HTML, with no usable
JSON behind it.

## Rung 6: headless browser DOM scraping

**What.** Playwright drives the portal and reads the rendered page:
`inner_text`, locators, `page.evaluate` over the DOM.

**Make it less brittle:**
- Read whole text blocks and parse them with pure functions over the text,
  rather than chaining deep CSS selectors.
- Anchor on visible Spanish labels ("Saldo disponible", "Cupo utilizado")
  over generated class names.
- Wait for a specific element or response, never a fixed sleep. Retry each
  surface a bounded number of times, then report a warning
  (`ProductScrapeResult.warnings`, which marks the run `partial`).
- Use real keystrokes (`press_sequentially`) where a masked input (RUT masks,
  Angular forms) ignores `.fill()`.
- Try the mobile-web version with a Playwright device descriptor
  (`playwright.devices[...]`). Mobile sites are sometimes simpler and call the
  same JSON API. This is still rung 6 (or 5), not phone emulation.

**Step down when.** A bot challenge (Cloudflare Turnstile, reCAPTCHA, device
fingerprinting) blocks Playwright's browser even headed, or the session dies
when the browser is relaunched.

## Rung 7: real Chrome over CDP

**What.** Drive a genuine Chrome (the user's binary, a persistent profile)
through `playwright.chromium.connect_over_cdp`. A real browser passes checks
that a Playwright-launched one fails (BCI Lider's Turnstile), and keeps a
session some portals bind to the browser instance. A relaunched browser lost
the session even with the same profile in an unmerged Mercado Pago
experiment.

**Two modes (both in `backends/bci_lider_web.py`):**
- **Managed** (unattended): launch a headed Chrome off-screen, sign in with
  autofill, scrape, then terminate the process group. Needs a display, so it
  can't run in headless Docker.
- **Reuse:** a long-lived Chrome started by `make <slug>-login`, where the human
  signs in once. The scraper attaches via `<SLUG>_CDP_URL` and reuses the
  signed-in tab.

**Rules learned the hard way:**
- Submit only once the challenge token is present; never click the check
  yourself.
- Prefer a **fetch-only contract**: read data with in-page `fetch()` from
  the parked tab and never navigate. In that same unmerged experiment,
  navigating to the portal's home route re-triggered the auth step-up and
  killed the session.
- A tab stuck loading can make `connect_over_cdp` hang for its whole timeout.
  Recover through the CDP HTTP endpoints (`/json/new`, `/json/close/<id>`).
- Sessions expire on their own. Raise a clear "re-run `make <slug>-login`"
  error instead of retrying into a lockout.
- Drive the browser once per cycle: the transactions leg caches its result
  (or its exception) and the products leg consumes it.

**Step down when.** The institution offers no usable web portal for
individuals (app-only), or the web portal lacks the data.

## Rung 8: mobile app private API

**What.** App-only institutions (MACH and Tenpo are app-only in practice)
still talk HTTP to a backend. Inspect your own app's traffic with an
intercepting proxy (mitmproxy, HTTP Toolkit) on a test device or emulator,
then call those endpoints from httpx.

**Why it ranks low despite being "just HTTP":**
- Certificate pinning has to be bypassed on your own device just to see the
  traffic.
- Request signing keys are often embedded in the binary and rotate with
  releases.
- Device attestation (Play Integrity, App Attest) and device binding or
  enrollment can make a non-app client impossible.
- Every app update can break it.
- Terms of service are usually explicit about it.

Build cost is the highest of any HTTP option, and it fails silently when the
app updates.

**Shape.** Same as rung 3 once the contract is known. Keep device-bound
secrets (device id, enrollment token) in the Keychain via `make secret-set`,
never in the repo.

**Step down when.** Attestation or device binding blocks any client that isn't
the app.

## Rung 9: native app UI automation

**What.** Run the real app on an Android emulator (or a dedicated device) and
drive its UI with Appium / UiAutomator2 over adb, reading values from the
accessibility tree (OCR on screenshots as the very last resort).

**Why it is last:**
- Heaviest footprint: Android SDK, a Google Play system image, hardware
  virtualization, minutes of boot.
- Slowest and flakiest: animations, popups, A/B-tested screens.
- Apps often refuse emulators or rooted devices.
- Hard to run inside the compose stack.
- Device binding means the emulator becomes "your phone".

**If you must:**
- Run it outside Docker, on a machine with virtualization, as its own process
  that produces the same envelopes. The envelopes are designed to become the
  REST ingest contract, so keep that boundary clean.
- Make every step idempotent and screenshot on failure (into `local/`).
- Accept transactions-light coverage: balances first.

## Escape hatch: no scraper

If no rung is acceptable (cost, risk or terms), say so and propose a
non-scraper path: manual balance entry or statement import in the dashboard,
or an e-mail-only transactions leg with balances entered by hand. A clearly
documented "won't scrape, here's why" is a valid outcome.

## Decision record template

Put this in the GitHub issue or PR description, and condense it into the
module docstring (the BCI Lider and Fintual docstrings are the model). Keep
it free of personal data.

```markdown
### Acquisition decision: <Institution>

Legs needed: balances [kinds: ...], transactions [kinds: ...]
Chosen: balances = rung N (<name>); transactions = rung M (<name>)
Auth: <cached session / IMAP 2FA / manual make target / API key>

Rejected rungs:
- 1 Official API: <evidence: searched developer site X, help article Y; none for individuals>
- 2 Library: <fintself does/doesn't cover it; technique if it does>
- 3 Private HTTP API: <what failed: e.g. login posts an encrypted blob from obfuscated JS>
- ...

Verified live: <what was confirmed, without figures> / Unverified: <what is assumed>
Climb-back trigger: <what would let this move up a rung, e.g. "open finance access for individuals">
```
