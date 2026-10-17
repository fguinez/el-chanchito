// Server-only. Loads the RateBook every product conversion resolves against:
// the provider rates (lib/rates getClpRates) plus the institution/product
// rates scrapers stored in exchange_rates. Kept apart from lib/rates so the
// resolution helpers there stay pure and unit-testable without a database.

import { eq } from "drizzle-orm";
import { db } from "@/lib/db";
import { exchangeRates, institutions } from "@/lib/db/schema";
import { getClpRates, type RateBook, type RateOverride } from "@/lib/rates";

/** Every CLP-quoted institution/product rate. A failed read degrades to no
 *  overrides (provider rates only) rather than failing the page, the same way
 *  a failed provider fetch leaves a currency unconvertible. */
export async function loadRateOverrides(): Promise<RateOverride[]> {
  try {
    const rows = await db
      .select({
        institutionId: exchangeRates.institutionId,
        institutionSlug: institutions.slug,
        productId: exchangeRates.productId,
        currency: exchangeRates.baseCurrency,
        rate: exchangeRates.rate,
        source: exchangeRates.source,
        reportedAsOf: exchangeRates.reportedAsOf,
        observedAt: exchangeRates.observedAt,
      })
      .from(exchangeRates)
      .innerJoin(institutions, eq(exchangeRates.institutionId, institutions.id))
      .where(eq(exchangeRates.quoteCurrency, "CLP"));

    const overrides: RateOverride[] = [];
    for (const row of rows) {
      // numeric columns come back as strings from postgres-js
      const rate = Number(row.rate);
      if (!(Number.isFinite(rate) && rate > 0)) continue;
      overrides.push({ ...row, currency: row.currency.toUpperCase(), rate });
    }
    return overrides;
  } catch (error) {
    console.error("Loading exchange_rates failed:", error);
    return [];
  }
}

/** Provider rates and stored overrides, fetched in parallel. */
export async function getRateBook(): Promise<RateBook> {
  const [provider, overrides] = await Promise.all([
    getClpRates(),
    loadRateOverrides(),
  ]);
  return { provider, overrides };
}
