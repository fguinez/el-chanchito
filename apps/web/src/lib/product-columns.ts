// Pure helpers behind the Instituciones tables: bucket products into display
// families and resolve each registry column spec into a renderable cell. No
// React here so the logic stays unit-testable; shared.tsx does the rendering.

import {
  FAMILY_INFO,
  KIND_INFO,
  PRODUCT_FAMILIES,
  type ColumnSpec,
  type ProductFamily,
  type ProductKind,
} from "@chanchito/product-model";
import {
  formatAmount,
  formatCLP,
  formatPercent,
  formatPlainDateEs,
} from "@/lib/utils";

/** The slice of a product the column resolver reads. `InstitutionProduct`
 *  (from a "use client" module) is structurally compatible, so this lib
 *  never has to import React code. */
export interface ColumnProduct {
  kind: ProductKind;
  currency: string;
  currentBalance: number | null;
  currentBalanceClp: number | null;
  attributes: object;
  metrics: object | null;
}

/** One resolved table cell. */
export interface ColumnCell {
  /** Value the column sorts by (CLP-normalized for headline cells so
   *  cross-currency rows compare); null sorts last. */
  sortValue: number | string | null;
  /** Rendered text; null when the row has no value for the column. */
  text: string | null;
  /** "≈ CLP" sub-line amount; only on headline cells of non-CLP products. */
  clp: number | null;
  /** Tone for signed columns by sign of the value; null for zero/unsigned. */
  tone: "positive" | "negative" | null;
}

export interface FamilyGroup<T> {
  family: ProductFamily;
  products: T[];
}

/** Bucket products by their kind's family, in PRODUCT_FAMILIES order,
 *  skipping empty families; products keep their incoming order. */
export function groupProductsByFamily<T extends { kind: ProductKind }>(
  products: T[]
): FamilyGroup<T>[] {
  const buckets = new Map<ProductFamily, T[]>();
  for (const product of products) {
    const family = KIND_INFO[product.kind].family;
    const bucket = buckets.get(family);
    if (bucket) bucket.push(product);
    else buckets.set(family, [product]);
  }
  return PRODUCT_FAMILIES.flatMap((family) => {
    const members = buckets.get(family);
    return members ? [{ family, products: members }] : [];
  });
}

/** The Tipo badge column only earns its place when rows span several kinds. */
export function showKindColumn(products: { kind: ProductKind }[]): boolean {
  return new Set(products.map((product) => product.kind)).size > 1;
}

const COUNT_FORMAT = new Intl.NumberFormat("es-CL", {
  maximumFractionDigits: 0,
});

/** `payload[field]` when it is a finite number; null for legacy `{}` / null
 *  payloads, kinds without the field, or non-numeric values. */
function numberField(payload: object | null, field: string | null): number | null {
  if (payload == null || field == null) return null;
  const value = (payload as Record<string, unknown>)[field];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringField(payload: object | null, field: string | null): string | null {
  if (payload == null || field == null) return null;
  const value = (payload as Record<string, unknown>)[field];
  return typeof value === "string" ? value : null;
}

/** Render a numeric value per the column's format. `signed` columns get an
 *  explicit "+" on positive values (Intl handles it for percent). */
function formatNumber(currency: string, column: ColumnSpec, value: number): string {
  if (column.format === "percent") {
    return formatPercent(value, { signed: column.signed });
  }
  const text =
    column.format === "count"
      ? COUNT_FORMAT.format(value)
      : column.source === "clp_value"
        ? formatCLP(value)
        : formatAmount(currency, value);
  return column.signed && value > 0 ? `+${text}` : text;
}

function numericCell(
  currency: string,
  column: ColumnSpec,
  value: number | null
): ColumnCell {
  if (value == null) return { sortValue: null, text: null, clp: null, tone: null };
  const tone = !column.signed || value === 0 ? null : value > 0 ? "positive" : "negative";
  return { sortValue: value, text: formatNumber(currency, column, value), clp: null, tone };
}

/** Resolve one registry column against a product per the column's source. */
export function resolveColumnCell(
  product: ColumnProduct,
  column: ColumnSpec
): ColumnCell {
  switch (column.source) {
    case "headline":
      return {
        ...numericCell(product.currency, column, product.currentBalance),
        // Sort by the CLP-normalized value so cross-currency rows are
        // comparable; a product with no conversion (foreign/crypto without a
        // rate) stays null and sorts last rather than mixing raw amounts in.
        sortValue: product.currentBalanceClp,
        clp:
          product.currentBalance != null && product.currency !== "CLP"
            ? product.currentBalanceClp
            : null,
      };
    case "clp_value":
      return numericCell(product.currency, column, product.currentBalanceClp);
    case "metric":
      return numericCell(
        product.currency,
        column,
        numberField(product.metrics, column.field)
      );
    case "attribute": {
      if (column.format === "date" || column.format === "text") {
        const value = stringField(product.attributes, column.field);
        const text =
          value == null
            ? null
            : column.format === "date"
              ? formatPlainDateEs(value)
              : value;
        return { sortValue: value, text, clp: null, tone: null };
      }
      return numericCell(
        product.currency,
        column,
        numberField(product.attributes, column.field)
      );
    }
    case "installments": {
      const paid = numberField(product.metrics, "installments_paid");
      const total = numberField(product.attributes, "installments_total");
      let text: string | null = null;
      if (paid != null && total != null) {
        text = `${COUNT_FORMAT.format(paid)} / ${COUNT_FORMAT.format(total)}`;
      } else if (paid != null) {
        text = COUNT_FORMAT.format(paid);
      } else if (total != null) {
        text = `— / ${COUNT_FORMAT.format(total)}`;
      }
      return { sortValue: paid, text, clp: null, tone: null };
    }
  }
}

/** The family's columns minus optional ones no row in `products` fills. */
export function visibleColumns(
  family: ProductFamily,
  products: ColumnProduct[]
): ColumnSpec[] {
  return FAMILY_INFO[family].columns.filter(
    (column) =>
      !column.optional ||
      products.some(
        (product) => resolveColumnCell(product, column).text !== null
      )
  );
}
