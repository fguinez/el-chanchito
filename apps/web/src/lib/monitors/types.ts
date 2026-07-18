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

/** What the engine needs to evaluate a monitor (a projection of the DB row). */
export type MonitorDefinition = {
  currency: string;
  expression: string;
  thresholds: MonitorThreshold[];
};

/** Per-threshold evaluation: today's threshold value and the margin before
 *  crossing it (null for `=`/`!=`, or when either side is no-data). */
export type ThresholdEvaluation = {
  severity: ThresholdSeverity;
  comparator: Comparator;
  value: number | null;
  margin: number | null;
};

export type MonitorEvaluation = {
  status: MonitorStatus;
  value: number | null;
  thresholds: ThresholdEvaluation[];
  /** Min across thresholds (nearest to crossing); null for =/!= or no_data. */
  margin: number | null;
  /** Oldest balanceAsOf among referenced products, ISO string. */
  staleAsOf: string | null;
  noDataReason: string | null;
};
