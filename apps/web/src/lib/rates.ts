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

let cache: { at: number; rates: ClpRates } | null = null;

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

  const entries = await Promise.all(
    Object.entries(BUDA_MARKETS).map(
      async ([code, market]) => [code, await fetchMarketPrice(market)] as const
    )
  );

  const rates: ClpRates = { CLP: 1 };
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
