# Reconnaissance

How to find out what an institution offers before choosing a rung, without
risking the user's account or leaking their data into the repo.

## Contents

- [Two kinds of recon](#two-kinds-of-recon)
- [Desk research (no login)](#desk-research-no-login)
- [Live recon (user present)](#live-recon-user-present)
- [What to capture per datum](#what-to-capture-per-datum)
- [Probes that settle a rung](#probes-that-settle-a-rung)
- [Stop signals](#stop-signals)
- [Where findings go](#where-findings-go)

## Two kinds of recon

| | Desk research | Live recon |
|---|---|---|
| Needs the user | no | yes: credentials, 2FA, consent |
| Touches the account | no | yes, read-only |
| Can settle | rungs 1, 2, 4 often; bot protection on the login page | rungs 3 to 9, id stability, field shapes |

Always do desk research first: it is free and often decides the rung on its own.
If the user is away, stop after desk research. Write the decision record with
an explicit "to verify live" list and build against documented or observed
shapes with synthetic fixtures. Never sign in to someone's account on your own.

## Desk research (no login)

1. **Official API.** Search the institution's site for "API", "developers",
   "desarrolladores", "integraciones", "open banking", "finanzas abiertas". Check
   account settings docs for personal API keys or tokens. Search PyPI and npm
   for an official SDK.
2. **Existing integrations.**
   - `fintself` (the OSS library for Chilean institutions): does it cover this
     institution? Which technique does it use? A copy may already sit in
     `.venv/lib/python*/site-packages/fintself/scrapers/cl/` (left over, not
     a declared dependency). Otherwise download the sdist into a fresh
     scratch directory. Either way, read it, don't run it.
   - Other OSS clients (GitHub projects in the "open banking Chile" space)
     often intercept the same SPA XHRs you would, and name the endpoints.
   - GitHub code search: `"<institution domain>" scraper`, `"<domain>/api"`.
     Other people's clients reveal endpoint paths and login steps. Credit them
     with a full URL if they shape the design.
   - Commercial aggregators: coverage only, as a data point.
3. **Channels.**
   - Is there a web portal for individuals, or is it app-only? The app-store
     listing and help center tell you.
   - Does it send per-transaction e-mails? Monthly statements by e-mail?
4. **Login page, unauthenticated, minimal touch.** Start with zero-touch
   signals. DNS often names the bot manager: `dig +short CNAME <host>`
   pointing at `edgekey.net` / `akamaiedge.net` means Akamai, and
   Cloudflare-proxied hosts resolve into Cloudflare ranges. OSS clients and
   public write-ups say the rest. Then, at most, one or two plain fetches of
   the public login page. Don't fire a battery of automated clients (curl,
   httpx, headless and headed browsers) at the institution from the user's
   network while they are away: bot managers score the source IP, and the
   user is the one who later signs in from it. Browser experiments belong in
   the live session. Read the HTML and scripts you fetched for:
   - `challenges.cloudflare.com` (Turnstile);
   - `google.com/recaptcha`, `hcaptcha.com`;
   - Akamai Bot Manager (`_abck` cookie);
   - PerimeterX / HUMAN (`_px*`), DataDome.
   Also check whether the login is a JSON POST or a form with
   script-generated hidden fields, and whether the password field is
   encrypted client-side (a public-key blob in the page).
5. **Terms.** Skim the terms of use for automated access. Note it in the
   decision record; it weighs on rungs 8 and 9 especially.

## Live recon (user present)

Ground rules:
- The user signs in, in their own browser. If you drive the browser (for
  example through the Chrome extension tools), they type the credentials and
  the 2FA code.
- Read-only. Never click anything that could move money, change settings,
  accept terms, enroll a device or start a payment flow. Note those routes
  and stay out of them.
- Minimize logins: one session, many observations. Each new login is a
  lockout and fraud-check risk. Set a login budget before starting (two or
  three for the whole recon is plenty), and state it in the runbook. Each
  browser fingerprint you try (the user's Chrome, Playwright headed,
  headless) looks like a new device to the institution, so don't stack
  several on the same day. Spread fingerprint experiments across sessions,
  most conservative first.

Procedure:
1. DevTools → Network, "Preserve log", filter Fetch/XHR. Sign in, then visit
   each page that shows a datum you need (balances, each product, movements,
   older statements).
2. For each datum, find the request that carries it (see the next section).
   With the Chrome tools, `read_network_requests` on the user's tab gives the
   same view.
3. If you export a HAR, it contains live cookies, tokens and often the
   password. Save it only under `local/`, treat it as a secret, and delete
   it when recon is done. Read it through
   `scripts/json_shape.py local/<slug>-recon/session.har [--filter <text>]`.
   It prints every JSON exchange as method, masked URL (ids replaced, query
   values dropped), request and response shapes, and format hints
   (`str:clp-display`, `str:date-dmy`, `str:account-number`, ...), never a
   value. Its output is what you paste into notes and design fixtures from.
4. Try the mobile web version too (narrow viewport or mobile user agent).
   It sometimes uses a simpler or different API.
5. For app-only institutions, live recon means inspecting the app's traffic
   with an intercepting proxy on a device the user controls. Check with the
   user before going there: it is the start of rung 8.

## What to capture per datum

Record structure, never values:

- **Request:** method, path (replace ids with `{account}` placeholders), query
  and body parameters (names only), required headers (CSRF, custom
  `X-*`, Origin/Referer quirks), auth carrier (cookie name, bearer, signed
  header).
- **Response:** the keys and nesting that hold the datum, the type of each
  amount (number, string, `[value, currency]`, cents integer), the number
  format of display strings, and the sign convention.
- **Identity:** which field could be the `external_id`. Is it present on every
  row? Is there a deeper detail request with a better id?
- **Pagination:** the parameters that actually work (some APIs ignore
  `offset`/`page` and only honor `limit`), page size, a total or has-more
  flag, and how far back history goes, as a duration ("several months"),
  not dates.
- **Dates:** format and timezone (UTC ISO timestamps need conversion to
  Santiago).
- **Login sequence:** each request in order, which tokens flow from one to the
  next, the 2FA channel (SMS, e-mail, app push, passkey), and what marks
  success (cookie name, redirect, response field).
- **Session:** what invalidates it (idle time, navigation to certain routes,
  relaunching the browser). Describe it qualitatively; measured lifetimes are
  banking-session timelines and stay out of the repo.

## Probes that settle a rung

Write probes as throwaway scripts under `local/` (gitignored). They run with
the user's session and print shapes, not values. Replay only GETs and the
query-style POSTs you watched the portal itself fire while viewing data
(listings, statements, detail lookups). Never replay a request captured
from a flow that changes state, even if it "looks like" a read.

| Question | Probe | Outcome |
|---|---|---|
| Rung 3: can plain HTTP read the data? | Copy the session cookies from the signed-in browser into httpx, call the data endpoint | 200 with JSON → the data side works over HTTP |
| Rung 3: can plain HTTP log in? | Replay the login sequence with httpx (browser User-Agent, Origin, Referer) | Session cookies issued → rung 3; challenge or opaque token → rung 5 or lower |
| Rung 5/6: does Playwright pass? | Headed Playwright Chromium to the login page; sign in once with the user | Clean login → rung 5 (JSON data) or 6 (DOM only); challenge loop → rung 7 |
| Rung 7: is the session browser-bound? | Sign in with a real Chrome over CDP, close it, relaunch with the same profile | Still signed in → reuse mode viable; signed out → managed mode or a human login per run |
| Is the id stable? | Two separate logins, compare the candidate id for the same movements | Identical → use it; different → it is session-scoped; find another or hash immutable fields |
| Is `navigate` safe? | After the data calls work, navigate to the home route once, then retry a data call | Still 200 → navigation is fine; step-up or 401 → adopt a fetch-only contract |

## Stop signals

Stop live recon and tell the user when you see:
- a "too many attempts" or lockout warning, or a CAPTCHA appearing after a
  previously clean login;
- an "unusual activity" or new-device e-mail or push the plan did not
  predict. A new-device approval the runbook announced for that exact login
  (and the user chose to approve) is expected; one out of the blue is not;
- a step-up authentication you did not expect;
- any screen that asks to confirm an operation.

None of these is a puzzle to work around. Each one is information for the
decision record (usually: step down a rung, or keep a human in the loop).

## Where findings go

- Raw notes, HARs, dumps and probe scripts: `local/<slug>-recon/`
  (gitignored), with secrets deleted when done.
- The repo receives only structural facts, condensed into the module
  docstring (decision, auth flow, endpoint map with placeholders, gotchas),
  the PR description (the full decision record) and synthetic fixtures that
  mirror the observed shapes.
- Durable lessons that aren't derivable from code (for example "navigating
  to the home route kills the session") belong in the module docstring, so
  the next person doesn't re-learn them with a live banking session.
