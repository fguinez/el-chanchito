"use client";

// Shared pieces of the Instituciones pages: typed mirrors of the
// /api/institutions response shapes plus the label/format helpers and the
// products table used by the list, institution detail, and product detail
// views.

import Link from "next/link";
import { useCallback } from "react";
import {
  Table,
  TableBody,
  TableCell,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { Badge } from "@/components/ui/badge";
import { useSortableData } from "@/lib/use-sortable-data";
import { formatCLP, cn } from "@/lib/utils";
import { KIND_INFO } from "@chanchito/product-model";
import type {
  ProductAttributes,
  ProductKind,
  ProductMetrics,
} from "@chanchito/product-model";

export interface InstitutionProduct {
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
  byCurrency: { currency: string; amount: number }[];
  clp: number | null;
  patrimonioClp: number | null;
  deudaClp: number | null;
  convertible: boolean;
}

export interface ApiInstitution {
  id: string;
  slug: string;
  name: string;
  kind: string;
  country: string | null;
  url: string | null;
  products: InstitutionProduct[];
  subtotals: InstitutionSubtotals;
}

export interface InstitutionTotals {
  patrimonioClp: number;
  deudaClp: number;
  netClp: number;
}

export interface InstitutionsResponse {
  institutions: ApiInstitution[];
  totals: InstitutionTotals;
}

// Institution kinds (bank/fintech/...) are a page-local vocabulary; product
// kinds come from the shared registry (KIND_INFO: labels + asset/liability roles).
export const INSTITUTION_KIND_LABELS: Record<string, string> = {
  bank: "Banco",
  fintech: "Fintech",
  exchange: "Exchange",
  asset_manager: "Gestora",
  other: "Otro",
};

// Latest scraper run per institution, from GET /api/scrapers (used for polling).
export interface ScraperRun {
  institution: string;
  status: string;
  started_at: string;
}

export const POLL_INTERVAL_MS = 3000;
export const POLL_TIMEOUT_MS = 60000;

export const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

/** Latest run per institution (slug → status + started_at). `/api/scrapers`
 *  already returns the latest per (method, institution); collapse methods by
 *  keeping the most recent started_at so an institution maps to one entry. */
export async function fetchRunMap(): Promise<Map<string, ScraperRun>> {
  const map = new Map<string, ScraperRun>();
  try {
    const res = await fetch("/api/scrapers");
    const runs: ScraperRun[] = await res.json();
    for (const r of runs) {
      const prev = map.get(r.institution);
      if (!prev || r.started_at > prev.started_at) map.set(r.institution, r);
    }
  } catch {
    /* treat as no data */
  }
  return map;
}

export function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return "recién";
  if (minutes < 60) return `hace ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `hace ${hours}h`;
  const days = Math.floor(hours / 24);
  return `hace ${days}d`;
}

/** Format a balance in its own currency: CLP as pesos, anything else (crypto,
 *  foreign) as a trimmed decimal followed by the currency code. */
export function formatBalance(currency: string, value: number | null): string | null {
  if (value == null) return null;
  if (currency === "CLP") return formatCLP(value);
  const formatted = new Intl.NumberFormat("en-US", {
    maximumFractionDigits: 8,
  }).format(value);
  return `${formatted} ${currency}`;
}

/**
 * A meaningful label for the Producto column. Scraped products are auto-named
 * "Institution - kind" (e.g. "Buda - crypto (ETH)"), so a raw name carries no
 * more information than the Tipo badge. When that's the case we fall back to
 * the currency for crypto (CLP / ETH / BTC …) and to the friendly kind label
 * otherwise; anything a human named stays untouched.
 */
export function displayProductName(
  product: InstitutionProduct,
  institutionName: string
): string {
  const prefix = `${institutionName} - `;
  const cleaned = (
    product.name.startsWith(prefix)
      ? product.name.slice(prefix.length)
      : product.name
  ).trim();

  const isGeneric =
    cleaned === product.kind || cleaned.startsWith(`${product.kind} (`);
  if (isGeneric || !cleaned) {
    if (product.kind === "crypto") return product.currency;
    return KIND_INFO[product.kind].labelEs;
  }
  return cleaned;
}

/** The product's credit limit (cupo) as observed in its latest metrics. */
export function productLimit(product: InstitutionProduct): number | null {
  const m = product.metrics;
  if (m && (m.kind === "credit_card" || m.kind === "line_of_credit")) {
    return m.limit ?? null;
  }
  return null;
}

/** Human-readable detail chips pulled from the product's typed attributes
 *  (snake_case registry keys) + account, plus the reported revolving debt. */
export function productDetailChips(product: InstitutionProduct): string[] {
  const chips: string[] = [];
  const a = product.attributes;

  if (product.accountName && product.accountName !== "Personal") {
    chips.push(product.accountName);
  }
  if ("brand" in a && a.brand != null) chips.push(a.brand);
  if ("last4" in a && a.last4 != null) chips.push(`•••• ${a.last4}`);
  if ("portfolio" in a && a.portfolio != null) chips.push(a.portfolio);
  if ("risk_profile" in a && a.risk_profile != null) chips.push(a.risk_profile);
  if ("statement_day" in a && a.statement_day != null)
    chips.push(`corte día ${a.statement_day}`);
  if ("due_day" in a && a.due_day != null)
    chips.push(`vence día ${a.due_day}`);

  // Reported drawn amount (Utilizado) on a card / línea, in product currency.
  const m = product.metrics;
  if (
    m &&
    (m.kind === "credit_card" || m.kind === "line_of_credit") &&
    m.owed != null
  ) {
    const owed = formatBalance(product.currency, m.owed);
    if (owed) chips.push(`Utilizado ${owed}`);
  }

  return chips;
}

type ProductSortKey = "producto" | "tipo" | "saldo" | "cupo" | "actualizado";

/** The per-institution products table, with client-side column sorting. Sort
 *  state is local so each institution's table sorts independently; the caller's
 *  subtotals footer stays computed from the full, unsorted set. Each product
 *  row links to its detail page under `/institutions/{institutionSlug}/{slug}`.
 */
export function InstitutionProductsTable({
  products,
  institutionName,
  institutionSlug,
}: {
  products: InstitutionProduct[];
  institutionName: string;
  institutionSlug: string;
}) {
  const getValue = useCallback(
    (product: InstitutionProduct, key: ProductSortKey): string | number | null => {
      switch (key) {
        case "producto":
          return displayProductName(product, institutionName);
        case "tipo":
          return KIND_INFO[product.kind].labelEs;
        case "saldo":
          // Sort by the CLP-normalized value so cross-currency rows are
          // comparable; a product with no conversion (foreign/crypto without a
          // rate) stays null and sorts last rather than mixing raw amounts in.
          return product.currentBalanceClp;
        case "cupo":
          return productLimit(product);
        case "actualizado":
          return product.balanceAsOf; // ISO strings sort correctly as strings.
      }
    },
    [institutionName]
  );

  const { sorted, sort, toggleSort } = useSortableData(products, getValue);
  // Bridge the generic header's string key to our typed key union.
  const handleSort = (key: string) => toggleSort(key as ProductSortKey);

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <SortableTableHead
            label="Producto"
            columnKey="producto"
            active={sort?.key === "producto"}
            direction={sort?.key === "producto" ? sort.direction : undefined}
            onSort={handleSort}
          />
          <SortableTableHead
            label="Tipo"
            columnKey="tipo"
            active={sort?.key === "tipo"}
            direction={sort?.key === "tipo" ? sort.direction : undefined}
            onSort={handleSort}
          />
          <SortableTableHead
            label="Saldo"
            columnKey="saldo"
            align="right"
            active={sort?.key === "saldo"}
            direction={sort?.key === "saldo" ? sort.direction : undefined}
            onSort={handleSort}
          />
          <SortableTableHead
            label="Cupo"
            columnKey="cupo"
            align="right"
            active={sort?.key === "cupo"}
            direction={sort?.key === "cupo" ? sort.direction : undefined}
            onSort={handleSort}
          />
          <SortableTableHead
            label="Actualizado"
            columnKey="actualizado"
            align="right"
            active={sort?.key === "actualizado"}
            direction={
              sort?.key === "actualizado" ? sort.direction : undefined
            }
            onSort={handleSort}
          />
        </TableRow>
      </TableHeader>
      <TableBody>
        {sorted.map((product) => {
          const chips = productDetailChips(product);
          const balance = formatBalance(
            product.currency,
            product.currentBalance
          );
          const cupo = productLimit(product);
          const isLiability = KIND_INFO[product.kind].role === "liability";
          return (
            <TableRow
              key={product.id}
              className={cn(
                "relative",
                !product.isActive && "opacity-50",
                "hover:bg-muted/40"
              )}
            >
              <TableCell>
                <Link
                  href={`/institutions/${institutionSlug}/${product.slug}`}
                  className="font-medium hover:text-primary after:absolute after:inset-0 after:content-['']"
                >
                  {displayProductName(product, institutionName)}
                </Link>
                {chips.length > 0 && (
                  <div className="mt-0.5 text-xs text-muted-foreground">
                    {chips.join(" · ")}
                  </div>
                )}
              </TableCell>
              <TableCell>
                <Badge
                  variant="outline"
                  className={cn(
                    "text-xs",
                    isLiability
                      ? "border-red-200 text-red-600"
                      : "border-green-200 text-green-700"
                  )}
                >
                  {KIND_INFO[product.kind].labelEs}
                </Badge>
              </TableCell>
              <TableCell className="text-right font-medium tabular-nums">
                {balance ?? (
                  <span className="text-muted-foreground">sin dato</span>
                )}
                {product.currency !== "CLP" &&
                  product.currentBalanceClp != null && (
                    <div className="text-xs font-normal text-muted-foreground">
                      ≈ {formatCLP(product.currentBalanceClp)}
                    </div>
                  )}
              </TableCell>
              <TableCell className="text-right tabular-nums text-muted-foreground">
                {cupo != null ? formatCLP(cupo) : "—"}
              </TableCell>
              <TableCell className="text-right text-sm text-muted-foreground">
                {product.balanceAsOf ? timeAgo(product.balanceAsOf) : "—"}
              </TableCell>
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}
