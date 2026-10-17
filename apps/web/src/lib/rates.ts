// Server-only. CLP conversion rates for the foreign/crypto currencies held in
// the app. Balances are scraped in native units (BTC, USDC, …); to
// total them we convert with Buda's public market tickers — the same exchange
// those balances come from, so the rates match the source of truth. Rates are
// cached in-memory for a few minutes so page loads don't refetch every request.
// These are the fallback "provider" rates: an institution's own published rate
// overrides them for its products (RateBook / ratesFor below).

const BUDA_BASE = "https://www.buda.com/api/v2";
const CACHE_TTL_MS = 5 * 60 * 1000;

// Buda quotes each of these against CLP. Any currency not listed here (e.g.
// CLP itself) is handled directly; unknown currencies stay unconvertible.
const BUDA_MARKETS: Record<string, string> = {
  BTC: "btc-clp",
  ETH: "eth-clp",
  LTC: "ltc-clp",
  BCH: "bch-clp",
  USDC: "usdc-clp",
  USDT: "usdt-clp",
};

export type ClpRates = Record<string, number>;

// Fiat FX (USD, EUR, …) comes from a free, no-auth multi-currency endpoint, so
// the set of convertible currencies grows automatically as new fiat products
// appear — unlike the crypto tickers above, which are one Buda market per coin.
// The response gives units-per-USD for ~160 currencies; we pivot each to
// CLP-per-unit via CLP-per-USD so `toClp` works uniformly. It's updated daily
// upstream, which is plenty for holdings valuation.
const FIAT_RATES_URL = "https://open.er-api.com/v6/latest/USD";

let cache: { at: number; rates: ClpRates } | null = null;

/**
 * CLP per 1 unit of each fiat currency the FX endpoint knows, derived from its
 * units-per-USD table (CLP per X = CLP-per-USD ÷ X-per-USD). Returns `{}` on any
 * failure so those currencies stay *absent* (unconvertible) rather than wrong.
 */
async function fetchFiatClpRates(): Promise<ClpRates> {
  try {
    const res = await fetch(FIAT_RATES_URL, {
      // Public endpoint, no auth. Don't let Next cache it across requests.
      cache: "no-store",
      signal: AbortSignal.timeout(6000),
    });
    if (!res.ok) return {};
    const data = await res.json();
    const rates = data?.rates;
    const clpPerUsd = Number(rates?.CLP);
    if (data?.result !== "success" || !rates || !(clpPerUsd > 0)) return {};

    const out: ClpRates = {};
    for (const [code, perUsd] of Object.entries(rates)) {
      const n = Number(perUsd);
      if (Number.isFinite(n) && n > 0) out[code.toUpperCase()] = clpPerUsd / n;
    }
    return out;
  } catch {
    return {};
  }
}

async function fetchMarketPrice(market: string): Promise<number | null> {
  try {
    const res = await fetch(`${BUDA_BASE}/markets/${market}/ticker`, {
      // Public endpoint, no auth. Don't let Next cache it across requests.
      cache: "no-store",
      signal: AbortSignal.timeout(6000),
    });
    if (!res.ok) return null;
    const data = await res.json();
    // ticker.last_price is ["<amount>", "CLP"].
    const price = Number(data?.ticker?.last_price?.[0]);
    return Number.isFinite(price) && price > 0 ? price : null;
  } catch {
    return null;
  }
}

/**
 * CLP per 1 unit of each currency. Always includes `CLP: 1`. Currencies whose
 * ticker fails (network error, unlisted market) are simply absent, so callers
 * can detect an unconvertible balance rather than silently mis-summing it.
 */
export async function getClpRates(): Promise<ClpRates> {
  if (cache && Date.now() - cache.at < CACHE_TTL_MS) return cache.rates;

  const [entries, fiat] = await Promise.all([
    Promise.all(
      Object.entries(BUDA_MARKETS).map(
        async ([code, market]) => [code, await fetchMarketPrice(market)] as const
      )
    ),
    fetchFiatClpRates(),
  ]);

  // Fiat first, then Buda crypto on top: the crypto balances come from Buda, so
  // its tickers stay authoritative for those coins (their keys don't overlap the
  // fiat ones anyway). `fetchFiatClpRates` already returns CLP: 1.
  const rates: ClpRates = { CLP: 1, ...fiat };
  for (const [code, price] of entries) if (price != null) rates[code] = price;

  // Cache a fresh result whenever we got real rates (more than just CLP), or on
  // the very first call. On a total network failure with a prior cache, keep the
  // stale rates and let the next request retry.
  if (Object.keys(rates).length > 1 || !cache) {
    cache = { at: Date.now(), rates };
  }
  return cache.rates;
}

/** Convert `amount` of `currency` to CLP. Returns null when no rate is known. */
export function toClp(
  currency: string,
  amount: number,
  rates: ClpRates
): number | null {
  const rate = rates[currency.toUpperCase()];
  return rate != null ? amount * rate : null;
}

// ---------------------------------------------------------------------------
// Institution and product rates (exchange_rates, V024)
// ---------------------------------------------------------------------------
//
// Some institutions convert with their own rate (a bank's card "Tipo de
// cambio"), sometimes one per product. Their scrapers store it in
// `exchange_rates`; converting an amount of product P (currency C) to CLP then
// resolves, most specific first:
//   1. P's own rate for C            (scope "product")
//   2. P's institution's rate for C  (scope "institution")
//   3. the provider rates above      (scope "provider")
// A stale override still wins over the provider; the UI warns instead.

/** One CLP-quoted rate an institution published, as stored. */
export interface RateOverride {
  institutionId: string;
  institutionSlug: string;
  /** Null = institution-wide. */
  productId: string | null;
  /** Base currency (upper ISO); the quote is always CLP. */
  currency: string;
  /** CLP per 1 unit of `currency`. */
  rate: number;
  /** Spanish label of where the institution shows it. */
  source: string;
  /** Validity date printed by the institution (YYYY-MM-DD), when shown. */
  reportedAsOf: string | null;
  /** Last scrape that read this value. */
  observedAt: Date;
}

/** Everything a request needs to convert any product: the provider rates and
 *  every override. Load once per request (lib/rate-book) and treat as
 *  immutable: `ratesFor` memoizes per book. */
export interface RateBook {
  provider: ClpRates;
  overrides: RateOverride[];
}

/** Which product a conversion is for. */
export interface RateTarget {
  productId: string;
  institutionSlug: string;
}

export type RateScope = "product" | "institution" | "provider";

/** The rate that converts one product, as the API returns it. */
export interface RateInfo {
  currency: string;
  /** CLP per 1 unit of `currency`. */
  rate: number;
  scope: RateScope;
  /** Null for provider rates. */
  source: string | null;
  /** ISO timestamp of the last scrape that read it; null for provider rates. */
  observedAt: string | null;
  reportedAsOf: string | null;
  /** Read on an earlier Santiago calendar day than now (see isRateStale). */
  stale: boolean;
}

const ratesMemo = new WeakMap<RateBook, Map<string, ClpRates>>();

/**
 * The CLP rates that convert `target`'s amounts: the provider rates with its
 * institution's overrides on top, then its own (product wins). Returns
 * `book.provider` itself when no override applies.
 */
export function ratesFor(book: RateBook, target: RateTarget): ClpRates {
  let memo = ratesMemo.get(book);
  if (!memo) {
    memo = new Map();
    ratesMemo.set(book, memo);
  }
  const key = `${target.institutionSlug}\u0000${target.productId}`;
  const cached = memo.get(key);
  if (cached) return cached;

  const institutionWide = book.overrides.filter(
    (o) => o.productId == null && o.institutionSlug === target.institutionSlug
  );
  const own = book.overrides.filter((o) => o.productId === target.productId);
  let rates = book.provider;
  if (institutionWide.length > 0 || own.length > 0) {
    rates = { ...book.provider };
    for (const o of [...institutionWide, ...own]) {
      rates[o.currency.toUpperCase()] = o.rate;
    }
  }
  memo.set(key, rates);
  return rates;
}

const SANTIAGO_DAY = new Intl.DateTimeFormat("en-US", {
  timeZone: "America/Santiago",
  calendar: "gregory",
  numberingSystem: "latn",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

/** YYYY-MM-DD of an instant on the America/Santiago calendar, whatever the
 *  process timezone. Built from the formatter's parts, so it does not depend
 *  on any locale's date layout. */
export function santiagoDay(at: Date): string {
  const parts: Partial<Record<Intl.DateTimeFormatPartTypes, string>> = {};
  for (const { type, value } of SANTIAGO_DAY.formatToParts(at)) {
    parts[type] = value;
  }
  return `${parts.year}-${parts.month}-${parts.day}`;
}

/** Whether a rate read at `observedAt` is from an earlier Santiago calendar
 *  day than `now`: an institution rate is only trusted for the day it was
 *  read on. */
export function isRateStale(observedAt: Date, now: Date): boolean {
  return santiagoDay(observedAt) < santiagoDay(now);
}

/**
 * The rate converting `product` to CLP and where it comes from, or null for a
 * CLP product and for a currency nothing can convert.
 */
export function rateInfoFor(
  book: RateBook,
  product: RateTarget & { currency: string },
  now: Date
): RateInfo | null {
  const currency = product.currency.toUpperCase();
  if (currency === "CLP") return null;

  const matches = (o: RateOverride) => o.currency.toUpperCase() === currency;
  const override =
    book.overrides.find((o) => o.productId === product.productId && matches(o)) ??
    book.overrides.find(
      (o) =>
        o.productId == null &&
        o.institutionSlug === product.institutionSlug &&
        matches(o)
    );
  if (override) {
    return {
      currency,
      rate: override.rate,
      scope: override.productId != null ? "product" : "institution",
      source: override.source,
      observedAt: override.observedAt.toISOString(),
      reportedAsOf: override.reportedAsOf,
      stale: isRateStale(override.observedAt, now),
    };
  }

  const rate = book.provider[currency];
  if (rate == null) return null;
  return {
    currency,
    rate,
    scope: "provider",
    source: null,
    observedAt: null,
    reportedAsOf: null,
    stale: false,
  };
}
