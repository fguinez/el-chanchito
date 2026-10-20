// Monitor domain types shared by the engine, the DB schema, and the API.
// Kept dependency-free so schema.ts can `$type<>` its JSONB columns with them
// without pulling the engine (parser/evaluator) into the drizzle layer.

export type ThresholdSeverity = "alert" | "warning";

/** Equation-level comparator: the stored comparator IS the breach condition
 *  (e.g. `<` means "breached when left < right"). */
export type Comparator = "<" | "<=" | ">" | ">=" | "=" | "!=";

/** One stored threshold. `expression` is persisted in uuid-ref form. */
export type MonitorThreshold = {
  severity: ThresholdSeverity;
  comparator: Comparator;
  expression: string;
};

/** Display config JSONB; snake_case keys, passed raw like product metrics. */
export type MonitorDisplay = {
  chart: "line" | "stat";
  show_margin: boolean;
};

export type MonitorStatus = "ok" | "warning" | "breached" | "no_data";

/** A variación (V040): one transaction to ignore for a monitor. From its
 *  `transactionDate` (YYYY-MM-DD, a local day) to the end of that calendar
 *  month, every threshold shifts by what undoing it on its product changes
 *  in the comparison (see evaluate.ts adjustmentOnDate). A new month
 *  starts with none, like DAY_OF_MONTH() ramps. */
export type MonitorAdjustment = {
  transactionId: string;
  productId: string;
  transactionDate: string;
  /** Signed, in the product's currency, as the transaction stores it. */
  amount: number;
  /** The transaction was soft-deleted: the link stays but never applies. */
  deleted?: boolean;
};

/** What the engine needs to evaluate a monitor (a projection of the DB row,
 *  plus its adjustments when the caller loaded them). */
export type MonitorDefinition = {
  currency: string;
  expression: string;
  thresholds: MonitorThreshold[];
  adjustments?: readonly MonitorAdjustment[];
};

/** Per-threshold evaluation: today's threshold value and the margin before
 *  crossing it (null for `=`/`!=`, or when either side is no-data). */
export type ThresholdEvaluation = {
  severity: ThresholdSeverity;
  comparator: Comparator;
  value: number | null;
  margin: number | null;
  /** This threshold's shift from the variaciones, included in `value`. */
  adjustment: number;
};

export type MonitorEvaluation = {
  status: MonitorStatus;
  value: number | null;
  thresholds: ThresholdEvaluation[];
  /** Min across thresholds (nearest to crossing); null for =/!= or no_data. */
  margin: number | null;
  /** The alert threshold's shift from the variaciones in effect (each
   *  threshold's own is in `thresholds`, already in its value; 0 if none). */
  adjustment: number;
  /** Oldest balanceAsOf among referenced products, ISO string. */
  staleAsOf: string | null;
  noDataReason: string | null;
};
