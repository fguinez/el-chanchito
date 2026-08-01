// Server-only. Shared query + serialization for the /api/institutions routes
// (list and single-institution) so both return identical product shapes and
// per-institution subtotals. The list route builds everything in one query;
// the single-institution route reuses the same builder with a slug filter.

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
import { asc, eq, type SQL } from "drizzle-orm";
import { getClpRates, toClp, type ClpRates } from "@/lib/rates";
import { assetClp, debtClp } from "@/lib/networth";

export interface ApiProduct {
  id: string;
  accountId: string;
  accountName: string;
  parentProductId: string | null;
  kind: ProductKind;
  name: string;
  slug: string;
  currency: string;
  currentBalance: number | null;
  currentBalanceClp: number | null;
  balanceAsOf: string | null;
  externalRef: string | null;
  attributes: ProductAttributes | Record<string, never>;
  metrics: ProductMetrics | null;
  isActive: boolean;
}

export interface InstitutionSubtotals {
  // Holdings grouped by currency (assets only), CLP first.
  byCurrency: { currency: string; amount: number }[];
  // Net-worth contribution in CLP (assets − debts); null when no balances yet.
  clp: number | null;
  patrimonioClp: number | null;
  deudaClp: number | null;
  // false when some balance couldn't be converted to CLP (missing rate).
  convertible: boolean;
}

export interface ApiInstitution {
  id: string;
  slug: string;
  name: string;
  kind: string;
  country: string | null;
  url: string | null;
  products: ApiProduct[];
  subtotals: InstitutionSubtotals;
}

export interface InstitutionTotals {
  patrimonioClp: number;
  deudaClp: number;
  netClp: number;
}

/** CLP first, then the rest alphabetically. */
function sortCurrencies(a: string, b: string): number {
  if (a === b) return 0;
  if (a === "CLP") return -1;
  if (b === "CLP") return 1;
  return a.localeCompare(b);
}

/** The join that lists every institution with its products, ordered for
 *  display. Pass a `where` clause (e.g. an institution slug) to narrow it. */
const institutionSelect = {
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
  productSlug: products.slug,
  currency: products.currency,
  currentBalance: products.currentBalance,
  balanceAsOf: products.balanceAsOf,
  externalRef: products.externalRef,
  attributes: products.attributes,
  metrics: products.metrics,
  isActive: products.isActive,
};

async function loadInstitutionRows(where?: SQL) {
  const query = db
    .select(institutionSelect)
    .from(institutions)
    .innerJoin(accounts, eq(accounts.institutionId, institutions.id))
    .innerJoin(products, eq(products.accountId, accounts.id))
    .orderBy(
      asc(institutions.name),
      asc(accounts.displayOrder),
      asc(products.displayOrder)
    );
  return where ? query.where(where) : query;
}

type InstitutionRow = Awaited<ReturnType<typeof loadInstitutionRows>>[number];

function buildInstitutions(
  rows: InstitutionRow[],
  rates: ClpRates
): Map<string, ApiInstitution> {
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
      slug: row.productSlug,
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

  return byInstitution;
}

/** Per-institution subtotals: holdings by currency + CLP net-worth total. */
export function computeSubtotals(
  products: ApiProduct[],
  rates: ClpRates
): InstitutionSubtotals {
  const byCurrency = new Map<string, number>();
  let patrimonioClp = 0;
  let deudaClp = 0;
  let anyBalance = false;
  let convertible = true;

  for (const p of products) {
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

  return {
    byCurrency: [...byCurrency.entries()]
      .sort(([a], [b]) => sortCurrencies(a, b))
      .map(([currency, amount]) => ({ currency, amount })),
    clp: anyBalance ? patrimonioClp - deudaClp : null,
    patrimonioClp: anyBalance ? patrimonioClp : null,
    deudaClp: anyBalance ? deudaClp : null,
    convertible,
  };
}

/**
 * Every institution the user is enrolled at, with its products nested
 * underneath and per-institution subtotals (holdings by currency + a
 * CLP-converted net-worth total). Institutions with no products are omitted
 * (the inner joins drop them). Foreign/crypto balances are converted to CLP
 * with Buda's public tickers (see lib/rates).
 */
export async function queryInstitutions(): Promise<{
  institutions: ApiInstitution[];
  totals: InstitutionTotals;
}> {
  const [rows, rates] = await Promise.all([
    loadInstitutionRows(),
    getClpRates(),
  ]);

  let patrimonioClpTotal = 0;
  let deudaClpTotal = 0;

  const institutions = [...buildInstitutions(rows, rates).values()].map(
    (inst) => {
      inst.subtotals = computeSubtotals(inst.products, rates);
      patrimonioClpTotal += inst.subtotals.patrimonioClp ?? 0;
      deudaClpTotal += inst.subtotals.deudaClp ?? 0;
      return inst;
    }
  );

  return {
    institutions,
    totals: {
      patrimonioClp: patrimonioClpTotal,
      deudaClp: deudaClpTotal,
      netClp: patrimonioClpTotal - deudaClpTotal,
    },
  };
}

/** A single institution by slug (with products + subtotals), or null. */
export async function queryInstitutionBySlug(
  slug: string
): Promise<ApiInstitution | null> {
  const [rows, rates] = await Promise.all([
    loadInstitutionRows(eq(institutions.slug, slug)),
    getClpRates(),
  ]);
  const institution = buildInstitutions(rows, rates).values().next().value;
  if (!institution) return null;
  institution.subtotals = computeSubtotals(institution.products, rates);
  return institution;
}
