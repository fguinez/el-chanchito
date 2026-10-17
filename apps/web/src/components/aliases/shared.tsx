// Shared pieces of the Alias page: typed mirrors of the /api/aliases response
// shapes plus the match-type labels. Generic form helpers are reused from the
// categories cluster so the two pages behave the same.

import type { MatchType } from "@/lib/db/schema";

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
  createdAt: string;
  updatedAt: string;
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
