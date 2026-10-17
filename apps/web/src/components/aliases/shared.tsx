// Shared pieces of the Alias page: typed mirrors of the /api/aliases response
// shapes plus the match-type labels. Generic form helpers are reused from the
// categories cluster so the two pages behave the same.

import type { MatchType } from "@/lib/db/schema";
import { formatAmount, parseNumberInput } from "@/lib/utils";

export {
  readApiError,
  plural,
  parsePriority,
  PRIORITY_ERROR,
  SELECT_CLASS,
} from "@/components/categories/shared";

export interface ApiAlias {
  id: string;
  matchDescription: string;
  matchType: MatchType;
  alias: string;
  priority: number;
  // Optional amount condition (V036): inclusive signed bounds, scoped to currency.
  amountMin: number | null;
  amountMax: number | null;
  currency: string | null;
  createdAt: string;
  updatedAt: string;
}

// The amount-condition currency picker. The app has no shared currency constant
// (products carry CLP, USD and others as free TEXT; lib/rates derives fiat/crypto
// codes at runtime), so this is a minimal, editable list of the common ones. The
// API accepts any currency a product holds, so this only steers the UI select.
export const CURRENCY_OPTIONS = ["CLP", "USD"] as const;

/** Whether an alias carries an amount bound or a currency scope: V036 applies
 *  these "conditional" aliases ahead of unconditional ones. */
export function isConditionalAlias(alias: {
  amountMin: number | null;
  amountMax: number | null;
  currency: string | null;
}): boolean {
  return (
    alias.amountMin !== null || alias.amountMax !== null || alias.currency !== null
  );
}

/** A compact, human reading of an alias's amount condition for the table cell:
 *  "Sin condicion" when unset, "Solo <currency>" for a currency-only scope,
 *  "= <amount>" for an exact value (min = max),
 *  ">= <amount>" / "<= <amount>" for an open end, or "<min> - <max>" for a
 *  range. Each amount is formatted in the condition's own currency. */
export function formatAmountCondition(alias: {
  amountMin: number | null;
  amountMax: number | null;
  currency: string | null;
}): string {
  const { amountMin, amountMax, currency } = alias;
  if (amountMin === null && amountMax === null) {
    return currency ? `Solo ${currency}` : "Sin condicion";
  }
  const fmt = (n: number) => formatAmount(currency ?? "", n);
  if (amountMin !== null && amountMax !== null) {
    return amountMin === amountMax
      ? `= ${fmt(amountMin)}`
      : `${fmt(amountMin)} - ${fmt(amountMax)}`;
  }
  if (amountMin !== null) return `>= ${fmt(amountMin)}`;
  return `<= ${fmt(amountMax as number)}`;
}

export interface AliasPreviewTransaction {
  id: string;
  /** The raw scraped description: what is matched and what the alias replaces. */
  description: string;
  amount: number;
  currency: string;
  transactionDate: string;
  customDescription: string | null;
}

export interface AliasPreviewResult {
  matchDescription: string;
  matchType: MatchType;
  total: number;
  transactions: AliasPreviewTransaction[];
}

/** Spanish labels for each match type, for the selects and the table. */
export const MATCH_TYPE_LABELS: Record<MatchType, string> = {
  exact: "Coincidencia exacta",
  substring: "Contiene",
};

/** A complete amount condition, ready to send. */
export type AmountCondition = {
  amountMin: number | null;
  amountMax: number | null;
  currency: string | null;
};

/** Read the amount-condition inputs the forms share, with the same cross-field
 *  rules the API enforces, so the user sees a Spanish error before any request.
 *  Returns the parsed condition, or `{ error }` with a message to show. The
 *  inputs are `type="number"`, so parseNumberInput yields null for an empty
 *  field. */
export function readAmountCondition(
  amountMinRaw: string,
  amountMaxRaw: string,
  currencyRaw: string
): AmountCondition | { error: string } {
  const amountMin = parseNumberInput(amountMinRaw);
  const amountMax = parseNumberInput(amountMaxRaw);
  const currency = currencyRaw.trim() ? currencyRaw.trim().toUpperCase() : null;
  const hasBound = amountMin !== null || amountMax !== null;
  if (hasBound && !currency) {
    return { error: "Elige la moneda de la condicion de monto" };
  }
  if (amountMin !== null && amountMax !== null && amountMin > amountMax) {
    return { error: "El monto maximo debe ser mayor o igual al minimo" };
  }
  return { amountMin, amountMax, currency };
}
