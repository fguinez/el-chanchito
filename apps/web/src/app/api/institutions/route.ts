import { NextResponse } from "next/server";
import { db } from "@/lib/db";
import {
  institutions,
  accounts,
  products,
  ASSET_KINDS,
  type ProductKind,
  type ProductAttributes,
  type ProductMetrics,
} from "@/lib/db/schema";
import { asc, eq } from "drizzle-orm";
import { getClpRates, toClp } from "@/lib/rates";
import { assetClp, debtClp } from "@/lib/networth";

interface ApiProduct {
  id: string;
  accountId: string;
  accountName: string;
  parentProductId: string | null;
  kind: ProductKind;
  name: string;
  currency: string;
  currentBalance: number | null;
  currentBalanceClp: number | null;
  balanceAsOf: string | null;
  externalRef: string | null;
  attributes: ProductAttributes | Record<string, never>;
  metrics: ProductMetrics | null;
  isActive: boolean;
}

interface Subtotals {
  // Holdings grouped by currency (assets only), CLP first.
  byCurrency: { currency: string; amount: number }[];
  // Net-worth contribution in CLP (assets − debts); null when no balances yet.
  clp: number | null;
  patrimonioClp: number | null;
  deudaClp: number | null;
  // false when some balance couldn't be converted to CLP (missing rate).
  convertible: boolean;
}

interface ApiInstitution {
  id: string;
  slug: string;
  name: string;
  kind: string;
  country: string | null;
  url: string | null;
  products: ApiProduct[];
  subtotals: Subtotals;
}

/** CLP first, then the rest alphabetically. */
function sortCurrencies(a: string, b: string): number {
  if (a === b) return 0;
  if (a === "CLP") return -1;
  if (b === "CLP") return 1;
  return a.localeCompare(b);
}

/**
 * GET /api/institutions — every institution the user is enrolled at, with its
 * products nested underneath and per-institution subtotals (holdings by
 * currency + a CLP-converted net-worth total). Institutions with no products
 * are omitted (the inner joins drop them). Foreign/crypto balances are
 * converted to CLP with Buda's public tickers (see lib/rates).
 */
export async function GET() {
  const [rows, rates] = await Promise.all([
    db
      .select({
        institutionId: institutions.id,
        slug: institutions.slug,
        institutionName: institutions.name,
        institutionKind: institutions.kind,
        country: institutions.country,
        url: institutions.url,
        accountId: accounts.id,
        accountName: accounts.name,
        productId: products.id,
        parentProductId: products.parentProductId,
        kind: products.kind,
        productName: products.name,
        currency: products.currency,
        currentBalance: products.currentBalance,
        balanceAsOf: products.balanceAsOf,
        externalRef: products.externalRef,
        attributes: products.attributes,
        metrics: products.metrics,
        isActive: products.isActive,
      })
      .from(institutions)
      .innerJoin(accounts, eq(accounts.institutionId, institutions.id))
      .innerJoin(products, eq(products.accountId, accounts.id))
      .orderBy(
        asc(institutions.name),
        asc(accounts.displayOrder),
        asc(products.displayOrder)
      ),
    getClpRates(),
  ]);

  const byInstitution = new Map<string, ApiInstitution>();

  for (const row of rows) {
    let institution = byInstitution.get(row.institutionId);
    if (!institution) {
      institution = {
        id: row.institutionId,
        slug: row.slug,
        name: row.institutionName,
        kind: row.institutionKind,
        country: row.country,
        url: row.url,
        products: [],
        subtotals: {
          byCurrency: [],
          clp: null,
          patrimonioClp: null,
          deudaClp: null,
          convertible: true,
        },
      };
      byInstitution.set(row.institutionId, institution);
    }

    // numeric columns come back as strings from postgres-js
    const balance =
      row.currentBalance != null ? Number(row.currentBalance) : null;

    institution.products.push({
      id: row.productId,
      accountId: row.accountId,
      accountName: row.accountName,
      parentProductId: row.parentProductId,
      kind: row.kind,
      name: row.productName,
      currency: row.currency,
      currentBalance: balance,
      currentBalanceClp:
        balance != null ? toClp(row.currency, balance, rates) : null,
      balanceAsOf: row.balanceAsOf ? row.balanceAsOf.toISOString() : null,
      externalRef: row.externalRef,
      attributes: row.attributes,
      metrics: row.metrics,
      isActive: row.isActive,
    });
  }

  let patrimonioClpTotal = 0;
  let deudaClpTotal = 0;

  for (const inst of byInstitution.values()) {
    const byCurrency = new Map<string, number>();
    let patrimonioClp = 0;
    let deudaClp = 0;
    let anyBalance = false;
    let convertible = true;

    for (const p of inst.products) {
      if (p.currentBalance == null) continue;
      anyBalance = true;

      // Per-currency holdings: assets only (a card's "balance" is available
      // cupo, not money on hand, so it isn't a holding).
      if (ASSET_KINDS.includes(p.kind)) {
        byCurrency.set(
          p.currency,
          (byCurrency.get(p.currency) ?? 0) + p.currentBalance
        );
      }

      const a = assetClp(p.kind, p.currentBalance, p.currency, rates);
      const d = debtClp(
        p.kind,
        p.currentBalance,
        p.metrics,
        p.currency,
        rates
      );
      if (a === null || d === null) convertible = false;
      patrimonioClp += a ?? 0;
      deudaClp += d ?? 0;
    }

    inst.subtotals = {
      byCurrency: [...byCurrency.entries()]
        .sort(([a], [b]) => sortCurrencies(a, b))
        .map(([currency, amount]) => ({ currency, amount })),
      clp: anyBalance ? patrimonioClp - deudaClp : null,
      patrimonioClp: anyBalance ? patrimonioClp : null,
      deudaClp: anyBalance ? deudaClp : null,
      convertible,
    };

    patrimonioClpTotal += patrimonioClp;
    deudaClpTotal += deudaClp;
  }

  return NextResponse.json({
    institutions: [...byInstitution.values()],
    totals: {
      patrimonioClp: patrimonioClpTotal,
      deudaClp: deudaClpTotal,
      netClp: patrimonioClpTotal - deudaClpTotal,
    },
  });
}
