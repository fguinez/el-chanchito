// Server-only. CLP conversion rates for the foreign/crypto currencies held in
// the app. Balances are scraped in native units (BTC, USDC, …); to
// total them we convert with Buda's public market tickers — the same exchange
// those balances come from, so the rates match the source of truth. Rates are
// cached in-memory for a few minutes so page loads don't refetch every request.

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
