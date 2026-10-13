// Pure helpers behind manual balance entry: the product_snapshots `source`
// vocabulary, which kinds accept a hand-typed balance, and validation of the
// POST body. No db import here so the rules stay unit-testable; the route in
// app/api/institutions/[slug]/products/[product]/balance does the writes.

import type { ProductKind, ProductMetrics } from "@chanchito/product-model";

/** Who recorded a product_snapshots row, with its Spanish UI label. */
export const SNAPSHOT_SOURCES = {
  scraper: "Scraper",
  manual: "Manual",
  // Reserved: a running balance computed from transactions.
  derived: "Derivado",
  // V009 backfill of the legacy wealth snapshots.
  wealth_snapshot: "Histórico",
} as const;

export type SnapshotSource = keyof typeof SNAPSHOT_SOURCES;

/** UI label for a snapshot source; unknown values pass through unchanged. */
export function snapshotSourceLabel(source: string): string {
  return Object.hasOwn(SNAPSHOT_SOURCES, source)
    ? SNAPSHOT_SOURCES[source as SnapshotSource]
    : source;
}

// A kind qualifies only when its whole metrics payload is determined by one balance number.
export const MANUAL_BALANCE_KINDS: ReadonlySet<ProductKind> = new Set<ProductKind>([
  "wallet",
]);

export function acceptsManualBalance(kind: ProductKind): boolean {
  return MANUAL_BALANCE_KINDS.has(kind);
}

export type ManualBalanceResult =
  | { ok: true; balance: number }
  | { ok: false; error: string };

/** product_snapshots.balance is NUMERIC(20, 8): at most 12 integer digits. */
export const MAX_MANUAL_BALANCE = 999_999_999_999;

/**
 * Read a CLP amount typed the way the apps print it ("$ 2.500.000") or bare
 * ("2500000"). Dots are accepted only as thousands separators, so a decimal
 * like "2500.50" or "250,5" is rejected instead of read as another amount.
 */
export function parseClpInput(text: string): number | null {
  const compact = text.replace(/[$\s]/g, "");
  if (!/^(\d+|\d{1,3}(\.\d{3})+)$/.test(compact)) return null;
  return Number(compact.replace(/\./g, ""));
}

/**
 * Validate a `{ balance }` POST body. The balance is whole CLP (no decimals),
 * so anything but a non-negative safe integer is rejected rather than rounded.
 */
export function parseManualBalance(body: unknown): ManualBalanceResult {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    return { ok: false, error: "Cuerpo inválido" };
  }
  const { balance } = body as { balance?: unknown };
  if (balance === undefined) {
    return { ok: false, error: "Falta el saldo" };
  }
  if (typeof balance !== "number" || !Number.isSafeInteger(balance)) {
    return { ok: false, error: "El saldo debe ser un número entero de pesos" };
  }
  if (balance < 0) {
    return { ok: false, error: "El saldo no puede ser negativo" };
  }
  if (balance > MAX_MANUAL_BALANCE) {
    return { ok: false, error: "El saldo es demasiado grande" };
  }
  return { ok: true, balance };
}

/** The metrics payload a manual balance stands for, for an accepted kind. */
export function manualBalanceMetrics(
  kind: ProductKind,
  balance: number
): ProductMetrics {
  switch (kind) {
    case "wallet":
      return { kind: "wallet", balance };
    default:
      throw new Error(`Kind ${kind} does not accept a manual balance`);
  }
}
