"use client";

// Per-product insight graphs for the product detail page. Which graphs render
// depends on the product kind: credit products get cupo usage, products with
// transactions get a movement waterfall + monthly flow, investments get their
// value composition, and stock positions their daily price history. Shared
// types with the product API response live here so the page and the route
// stay in sync.

import {
  Area,
  AreaChart,
  Bar,
  CartesianGrid,
  Cell,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  Pie,
  PieChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { useYAxisRange } from "@/components/charts/y-axis-range";
import {
  InteractiveChart,
  useTimeSeriesChart,
} from "@/components/charts/interactive-chart";
import { TimeRangeControl } from "@/components/charts/time-range-control";
import { fixedWindowMs } from "@/components/charts/x-axis-range";
import {
  formatBalance,
  productLimit,
  type InstitutionProduct,
} from "@/components/institutions/shared";
import {
  cn,
  formatAmount,
  formatAxisValue,
  formatPercent,
  formatPlainDateEs,
  formatPriceAxisValue,
  formatTimestampEs,
} from "@/lib/utils";
import {
  buildWaterfallData,
  type WaterfallDatum,
} from "@/lib/product-movements";
import { countsTowardFlow } from "@/lib/transactions";
import { investmentComposition } from "@/lib/investment-composition";
import {
  buildPriceRows,
  rangeChange,
  type ProductPricePoint,
} from "@/lib/price-history";
import type { ProductMetrics } from "@chanchito/product-model";

export interface ProductTransaction {
  id: string;
  /** What to show: the custom override or alias, else the scraped text. */
  description: string;
  /** The scraped (or manual) text, which a search also matches. */
  rawDescription: string;
  /** Signed amount in the product's currency: negative = expense, positive = income. */
  amount: number;
  /** Plain YYYY-MM-DD. */
  transactionDate: string;
  source: string;
  /** Source dedup key (the institution's operation id for some sources). */
  externalId: string | null;
  /** "Lugar · Ciudad, País" when a location is stored, else null. */
  location: string | null;
  /** Linked to an internal transfer: left out of Ingresos/Gastos, still part
   *  of the balance walk. */
  isInternalTransfer: boolean;
}

export interface ProductHistoryPoint {
  /** ISO timestamp of the observation. */
  asOf: string;
  /** Balance in the product's own currency. */
  balance: number;
  /** Balance converted to CLP with current rates; null when unconvertible. */
  balanceClp: number | null;
  /** Full typed metrics payload at `asOf` (empty object when unknown). */
  metrics: ProductMetrics | Record<string, never>;
}

/** Products whose charted balance is money the user holds (value convention). */
const VALUE_KINDS = new Set(["checking", "savings", "vista", "wallet", "prepaid_card"]);
/** Products that track a credit limit (cupo): balance is the available credit. */
const CREDIT_KINDS = new Set(["credit_card", "line_of_credit"]);

/** Products that can reconstruct a per-movement balance waterfall. */
export const TRANSACTION_KINDS = new Set<string>([...VALUE_KINDS, ...CREDIT_KINDS]);

const COLORS = {
  expense: "#dc2626",
  income: "#16a34a",
  balance: "#2563eb",
  deposited: "#64748b",
};

/** YYYY-MM-DD -> "jul 26" (es-CL, short month). */
function formatMonthEs(key: string): string {
  const [year, month] = key.split("-").map(Number);
  return new Date(year, month - 1, 1).toLocaleDateString("es-CL", {
    month: "short",
    year: "2-digit",
  });
}

// ---------------------------------------------------------------------------
// Cupo utilization (credit_card, line_of_credit)
// ---------------------------------------------------------------------------

function buildCupoHistory(history: ProductHistoryPoint[]) {
  const rows: { t: number; Disponible: number; Utilizado: number }[] = [];
  for (const p of history) {
    const m = p.metrics;
    if (!m || (m.kind !== "credit_card" && m.kind !== "line_of_credit")) continue;
    if (m.available == null || m.owed == null) continue;
    rows.push({
      t: new Date(p.asOf).getTime(),
      Disponible: m.available,
      Utilizado: m.owed,
    });
  }
  return rows;
}

/** Proportion of the credit limit spent vs remaining, plus its history. */
export function CupoUtilizationCard({
  product,
  history,
}: {
  product: InstitutionProduct;
  history: ProductHistoryPoint[];
}) {
  const chart = useTimeSeriesChart();
  const m = product.metrics;
  let owed: number | null = null;
  let available: number | null = null;
  if (m && (m.kind === "credit_card" || m.kind === "line_of_credit")) {
    owed = m.owed ?? null;
    available = m.available ?? null;
  }
  const limit = productLimit(product);

  if (owed == null || available == null) return null;

  const total = limit ?? owed + available;
  const usedPct = total > 0 ? (owed / total) * 100 : 0;
  const historyRows = buildCupoHistory(history);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Uso del cupo</CardTitle>
        <CardDescription>
          Proporción del cupo utilizado y el que queda disponible
        </CardDescription>
        <CardAction>
          <TimeRangeControl control={chart.x} allowAll />
        </CardAction>
      </CardHeader>
      <CardContent>
        <div className="grid items-center gap-6 lg:grid-cols-[minmax(0,280px)_1fr]">
          <div className="relative mx-auto h-52 w-full max-w-[280px]">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={[
                    { name: "Utilizado", value: owed, color: COLORS.expense },
                    { name: "Disponible", value: available, color: COLORS.income },
                  ]}
                  dataKey="value"
                  nameKey="name"
                  innerRadius={64}
                  outerRadius={92}
                  paddingAngle={2}
                  strokeWidth={0}
                >
                  {[
                    { name: "Utilizado", color: COLORS.expense },
                    { name: "Disponible", color: COLORS.income },
                  ].map((d) => (
                    <Cell key={d.name} fill={d.color} />
                  ))}
                </Pie>
                <Tooltip
                  formatter={(value) =>
                    formatBalance(product.currency, Number(value)) ?? "—"
                  }
                />
              </PieChart>
            </ResponsiveContainer>
            <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
              <span className="text-2xl font-semibold tabular-nums">
                {usedPct.toFixed(0)}%
              </span>
              <span className="text-xs text-muted-foreground">utilizado</span>
            </div>
          </div>

          <div className="min-w-0">
            {historyRows.length >= 2 ? (
              <>
                <InteractiveChart {...chart.interactiveProps} height={220}>
                  <AreaChart data={historyRows}>
                    <CartesianGrid strokeDasharray="3 3" />
                    <XAxis {...chart.xAxisProps} />
                    <YAxis {...chart.yAxisProps} />
                    <Tooltip
                      formatter={(value, name) => [
                        formatBalance(product.currency, Number(value)) ?? "—",
                        name,
                      ]}
                      labelFormatter={chart.labelFormatter}
                    />
                    <Legend />
                    <Area
                      type="monotone"
                      dataKey="Disponible"
                      stackId="cupo"
                      stroke={COLORS.income}
                      fill={COLORS.income}
                      fillOpacity={0.5}
                      strokeWidth={2}
                      isAnimationActive={false}
                    />
                    <Area
                      type="monotone"
                      dataKey="Utilizado"
                      stackId="cupo"
                      stroke={COLORS.expense}
                      fill={COLORS.expense}
                      fillOpacity={0.5}
                      strokeWidth={2}
                      isAnimationActive={false}
                    />
                  </AreaChart>
                </InteractiveChart>
                <p className="mt-2 text-xs text-muted-foreground">
                  Evolución del cupo según cada observación reportada
                </p>
              </>
            ) : (
              <p className="text-sm text-muted-foreground">
                Sin historial de cupo.
              </p>
            )}
          </div>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-1 border-t pt-3 text-sm">
          <span className="flex items-center gap-1.5">
            <span
              className="h-2.5 w-2.5 rounded-sm"
              style={{ backgroundColor: COLORS.expense }}
            />
            Utilizado{" "}
            <span className="font-medium tabular-nums">
              {formatBalance(product.currency, owed)}
            </span>
          </span>
          <span className="flex items-center gap-1.5">
            <span
              className="h-2.5 w-2.5 rounded-sm"
              style={{ backgroundColor: COLORS.income }}
            />
            Disponible{" "}
            <span className="font-medium tabular-nums">
              {formatBalance(product.currency, available)}
            </span>
          </span>
          {limit != null && (
            <span className="ml-auto text-muted-foreground">
              Cupo total:{" "}
              <span className="font-medium tabular-nums text-foreground">
                {formatBalance(product.currency, limit)}
              </span>
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Movement waterfall (checking, savings, vista, wallet, credit cards, ...)
// ---------------------------------------------------------------------------

function WaterfallTooltip({
  active,
  payload,
  currency,
  balanceLabel,
}: {
  active?: boolean;
  payload?: ReadonlyArray<{ payload: WaterfallDatum }>;
  currency: string;
  balanceLabel: string;
}) {
  if (!active || !payload?.length) return null;
  const d = payload[0].payload;
  return (
    <div className="rounded-md border bg-popover px-3 py-2 text-xs shadow-md">
      <p className="font-medium">{d.description}</p>
      <p className="text-muted-foreground">{d.date}</p>
      <p
        className={cn(
          "mt-0.5 font-medium tabular-nums",
          d.amount >= 0 ? "text-green-600" : "text-red-600"
        )}
      >
        {d.amount >= 0 ? "+" : ""}
        {formatBalance(currency, d.amount)}
      </p>
      <p className="mt-1 text-muted-foreground">
        {balanceLabel} antes: {formatBalance(currency, d.before)}
      </p>
      <p className="text-muted-foreground">
        {balanceLabel} después: {formatBalance(currency, d.after)}
      </p>
    </div>
  );
}

/** Each known movement as a bar spanning before -> after balance; expenses in
 *  red, income in green, and a blue line tracing the resulting balance. When
 *  fewer than two bars can be reconstructed the card stays, with a note, so it
 *  never silently disappears. */
export function TransactionWaterfallCard({
  product,
  transactions,
  currentBalance,
}: {
  product: InstitutionProduct;
  transactions: ProductTransaction[];
  /** Anchor for the walk back; null when the product has no balance. */
  currentBalance: number | null;
}) {
  const yAxis = useYAxisRange();
  if (transactions.length === 0) return null;

  const isCredit = CREDIT_KINDS.has(product.kind);
  const balanceLabel = isCredit ? "Cupo disponible" : "Saldo";
  // A card's cupo disponible can never exceed its limit nor go below zero; a
  // value account has no such bound (an overdraft is legitimately negative).
  const limit = productLimit(product);
  const { rows: data, truncated } =
    currentBalance != null
      ? buildWaterfallData(
          transactions,
          currentBalance,
          isCredit ? { min: 0, max: limit ?? undefined } : {}
        )
      : { rows: [], truncated: false };

  return (
    <Card>
      <CardHeader>
        <CardTitle>Movimientos</CardTitle>
        <CardDescription>
          {balanceLabel} antes y después de cada movimiento conocido; gastos en
          rojo, ingresos en verde
        </CardDescription>
      </CardHeader>
      <CardContent>
        {data.length < 2 ? (
          <p className="text-sm text-muted-foreground">
            No es posible reconstruir el {balanceLabel.toLowerCase()} antes y
            después de cada movimiento a partir del saldo reportado. El detalle
            de cada movimiento está en Transacciones.
          </p>
        ) : (
          <>
            <InteractiveChart yControl={yAxis} height={360}>
              <ComposedChart data={data}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis
                  dataKey="i"
                  fontSize={12}
                  minTickGap={24}
                  tickFormatter={(value: number) => data[value]?.date ?? ""}
                />
                <YAxis
                  tickFormatter={formatAxisValue}
                  fontSize={12}
                  {...yAxis.yAxisProps}
                />
                <Tooltip
                  content={
                    <WaterfallTooltip
                      currency={product.currency}
                      balanceLabel={balanceLabel}
                    />
                  }
                />
                <Bar
                  dataKey="base"
                  stackId="waterfall"
                  fill="transparent"
                  isAnimationActive={false}
                />
                <Bar
                  dataKey="delta"
                  stackId="waterfall"
                  radius={[2, 2, 0, 0]}
                  isAnimationActive={false}
                >
                  {data.map((d) => (
                    <Cell
                      key={d.key}
                      fill={d.amount >= 0 ? COLORS.income : COLORS.expense}
                    />
                  ))}
                </Bar>
                <Line
                  type="monotone"
                  dataKey="after"
                  stroke={COLORS.balance}
                  strokeWidth={1.5}
                  dot={false}
                  isAnimationActive={false}
                />
              </ComposedChart>
            </InteractiveChart>
            <div className="mt-2 flex flex-wrap items-center gap-x-6 gap-y-1 text-xs text-muted-foreground">
              <span className="flex items-center gap-1.5">
                <span className="h-2.5 w-2.5 rounded-sm bg-red-600" /> Gasto
              </span>
              <span className="flex items-center gap-1.5">
                <span className="h-2.5 w-2.5 rounded-sm bg-green-600" /> Ingreso
              </span>
              <span className="flex items-center gap-1.5">
                <span className="h-0.5 w-4 rounded bg-blue-600" /> {balanceLabel}
              </span>
              <span className="ml-auto">
                {truncated
                  ? "Reconstruido desde el saldo actual hasta donde alcanza el historial de movimientos conocido"
                  : "Reconstruido desde el saldo actual con las transacciones conocidas"}
              </span>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Monthly flow (products with transactions)
// ---------------------------------------------------------------------------

/** Ingresos/Gastos per month. Rows an internal transfer links are skipped:
 *  the money only changed product (the balance waterfall still walks them). */
function buildMonthlyFlow(transactions: ProductTransaction[]) {
  const byMonth = new Map<string, { Ingresos: number; Gastos: number }>();
  for (const t of transactions) {
    if (!countsTowardFlow(t)) continue;
    const key = t.transactionDate.slice(0, 7);
    const entry = byMonth.get(key) ?? { Ingresos: 0, Gastos: 0 };
    if (t.amount >= 0) entry.Ingresos += t.amount;
    else entry.Gastos += t.amount;
    byMonth.set(key, entry);
  }
  return [...byMonth.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, v]) => ({
      month: formatMonthEs(key),
      Ingresos: v.Ingresos,
      Gastos: v.Gastos,
      Neto: v.Ingresos + v.Gastos,
    }));
}

/** Monthly income (green) vs expenses (red) bars with a net-flow line. */
export function MonthlyFlowCard({
  product,
  transactions,
}: {
  product: InstitutionProduct;
  transactions: ProductTransaction[];
}) {
  const yAxis = useYAxisRange();
  const data = buildMonthlyFlow(transactions);
  if (data.length < 2) return null;

  const totalIngresos = data.reduce((sum, d) => sum + d.Ingresos, 0);
  const totalGastos = data.reduce((sum, d) => sum + d.Gastos, 0);
  const neto = totalIngresos + totalGastos;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Flujo mensual</CardTitle>
        <CardDescription>
          Ingresos y gastos por mes, con el flujo neto resultante
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="mb-4 flex flex-wrap items-center gap-x-6 gap-y-1 text-sm">
          <span>
            Ingresos{" "}
            <span className="font-medium tabular-nums text-green-600">
              {formatBalance(product.currency, totalIngresos)}
            </span>
          </span>
          <span>
            Gastos{" "}
            <span className="font-medium tabular-nums text-red-600">
              {formatBalance(product.currency, totalGastos)}
            </span>
          </span>
          <span className="ml-auto">
            Neto{" "}
            <span
              className={cn(
                "font-medium tabular-nums",
                neto < 0 ? "text-red-600" : "text-green-600"
              )}
            >
              {neto >= 0 ? "+" : ""}
              {formatBalance(product.currency, neto)}
            </span>
          </span>
        </div>
        <InteractiveChart yControl={yAxis} height={260}>
          <ComposedChart data={data}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="month" fontSize={12} />
            <YAxis
              tickFormatter={formatAxisValue}
              fontSize={12}
              {...yAxis.yAxisProps}
            />
            <Tooltip
              formatter={(value, name) => [
                formatBalance(product.currency, Number(value)) ?? "—",
                name,
              ]}
            />
            <Legend />
            <Bar
              dataKey="Ingresos"
              fill={COLORS.income}
              radius={[3, 3, 0, 0]}
              isAnimationActive={false}
            />
            <Bar
              dataKey="Gastos"
              fill={COLORS.expense}
              radius={[3, 3, 0, 0]}
              isAnimationActive={false}
            />
            <Line
              type="monotone"
              dataKey="Neto"
              stroke={COLORS.balance}
              strokeWidth={2}
              dot={{ r: 3 }}
              isAnimationActive={false}
            />
          </ComposedChart>
        </InteractiveChart>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Investment value composition (investment, stock)
// ---------------------------------------------------------------------------

/** Current value split into what it grew from (a goal's deposits, or what a
 *  stock position's shares cost) and the result on top. */
export function InvestmentCompositionCard({
  product,
}: {
  product: InstitutionProduct;
}) {
  const m = product.metrics;
  if (!m || (m.kind !== "investment" && m.kind !== "stock")) return null;
  const composition = investmentComposition(m);
  if (!composition) return null;

  const { base, result, pct, nav, byUnits } = composition;
  const baseLabel = byUnits ? "Costo" : "Aportado";
  const resultLabel = result >= 0 ? "Ganancia" : "Pérdida";
  const resultColor = result >= 0 ? COLORS.income : COLORS.expense;
  const slices = [
    { name: baseLabel, value: base, color: COLORS.deposited },
    { name: resultLabel, value: Math.abs(result), color: resultColor },
  ];

  return (
    <Card>
      <CardHeader>
        <CardTitle>Composición del valor</CardTitle>
        <CardDescription>
          {byUnits
            ? "Valor actual dividido en lo que costaron las unidades que tienes y su resultado"
            : "Valor actual dividido en lo aportado y el resultado acumulado"}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="flex flex-wrap items-center justify-center gap-8">
          <div className="relative h-52 w-52">
            <ResponsiveContainer width="100%" height="100%">
              <PieChart>
                <Pie
                  data={slices}
                  dataKey="value"
                  nameKey="name"
                  innerRadius={64}
                  outerRadius={92}
                  paddingAngle={2}
                  strokeWidth={0}
                >
                  {slices.map((d) => (
                    <Cell key={d.name} fill={d.color} />
                  ))}
                </Pie>
                <Tooltip
                  formatter={(value) =>
                    formatBalance(product.currency, Number(value)) ?? "—"
                  }
                />
              </PieChart>
            </ResponsiveContainer>
            <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
              <span
                className={cn(
                  "text-2xl font-semibold tabular-nums",
                  result < 0 && "text-red-600"
                )}
              >
                {pct >= 0 ? "+" : ""}
                {pct.toFixed(1)}%
              </span>
              <span className="text-xs text-muted-foreground">
                rentabilidad
              </span>
            </div>
          </div>

          <div className="space-y-2 text-sm">
            <p className="flex items-center gap-2">
              <span
                className="h-2.5 w-2.5 rounded-sm"
                style={{ backgroundColor: COLORS.deposited }}
              />
              {baseLabel}
              <span className="pl-6 font-medium tabular-nums">
                {formatBalance(product.currency, base)}
              </span>
            </p>
            <p className="flex items-center gap-2">
              <span
                className="h-2.5 w-2.5 rounded-sm"
                style={{ backgroundColor: resultColor }}
              />
              {resultLabel}
              <span className="pl-6 font-medium tabular-nums">
                {formatBalance(product.currency, result)}
              </span>
            </p>
            <p className="flex items-center gap-2 border-t pt-2">
              <span
                className="h-2.5 w-2.5 rounded-sm"
                style={{ backgroundColor: COLORS.balance }}
              />
              Valor actual
              <span className="pl-6 font-semibold tabular-nums">
                {formatBalance(product.currency, nav)}
              </span>
            </p>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Daily price history (stock positions)
// ---------------------------------------------------------------------------

/** A stock's daily closes over time, with the position's average cost as a
 *  dashed reference line. Rendered for products with a ticker that have
 *  closes or still hold units (`showsPriceHistory`); until the scraper has
 *  stored two closes the card stays, with a note. */
export function PriceHistoryCard({
  product,
  ticker,
  prices,
  institutionName,
}: {
  product: InstitutionProduct;
  ticker: string;
  /** Daily closes, oldest first (the product API's `prices`). */
  prices: ProductPricePoint[];
  institutionName: string;
}) {
  // "Fit": a price far from zero would sit squashed against an "auto" zero
  // baseline.
  const chart = useTimeSeriesChart({ kind: "all" }, { kind: "fit" });
  const rows = buildPriceRows(prices);
  const change = rangeChange(rows, fixedWindowMs(chart.x.range));
  const latest = rows.at(-1) ?? null;

  const m = product.metrics;
  const stock = m && m.kind === "stock" ? m : null;
  const averageCost =
    stock?.average_cost != null && stock.average_cost > 0
      ? stock.average_cost
      : null;
  const unitPrice = stock?.unit_price ?? null;
  const unitPriceAsOf = stock?.unit_price_as_of ?? null;
  const format = (value: number) => formatAmount(product.currency, value);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Historial de precio</CardTitle>
        <CardDescription>
          Cierre diario de {ticker} en {product.currency}, según{" "}
          {institutionName}
        </CardDescription>
        {rows.length >= 2 && (
          <CardAction>
            <TimeRangeControl control={chart.x} allowAll />
          </CardAction>
        )}
      </CardHeader>
      <CardContent>
        {rows.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Aún no hay historial de precios. Se carga en la próxima
            actualización de {institutionName}.
          </p>
        ) : rows.length === 1 ? (
          <p className="text-sm text-muted-foreground">
            Hace falta más de un cierre para graficar.
          </p>
        ) : (
          <InteractiveChart
            {...chart.interactiveProps}
            formatYValue={formatPriceAxisValue}
            height={300}
          >
            <LineChart data={rows}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis {...chart.xAxisProps} />
              <YAxis {...chart.yAxisProps} tickFormatter={formatPriceAxisValue} />
              <Tooltip
                formatter={(value) => [format(Number(value)), "Precio de cierre"]}
                labelFormatter={chart.labelFormatter}
              />
              {averageCost != null && (
                <ReferenceLine
                  y={averageCost}
                  stroke={COLORS.deposited}
                  strokeDasharray="6 4"
                  ifOverflow="extendDomain"
                  label={{
                    value: "Costo promedio",
                    position: "insideTopLeft",
                    fill: COLORS.deposited,
                    fontSize: 12,
                  }}
                />
              )}
              <Line
                type="monotone"
                dataKey="price"
                name="Precio de cierre"
                stroke={COLORS.balance}
                strokeWidth={2}
                dot={false}
                isAnimationActive={false}
              />
            </LineChart>
          </InteractiveChart>
        )}

        <div className="mt-4 flex flex-wrap items-center gap-x-6 gap-y-1 border-t pt-3 text-sm">
          {latest && (
            <span className="flex items-center gap-1.5">
              <span
                className="h-0.5 w-4 rounded"
                style={{ backgroundColor: COLORS.balance }}
              />
              Último cierre{" "}
              <span className="font-medium tabular-nums">
                {format(latest.price)}
              </span>
              <span className="text-muted-foreground">
                ({formatPlainDateEs(latest.date)})
              </span>
            </span>
          )}
          {averageCost != null && (
            <span className="flex items-center gap-1.5">
              <span
                className="w-4 border-t-2 border-dashed"
                style={{ borderColor: COLORS.deposited }}
              />
              Costo promedio{" "}
              <span className="font-medium tabular-nums">
                {format(averageCost)}
              </span>
            </span>
          )}
          {unitPrice != null && (
            <span>
              Precio actual{" "}
              <span className="font-medium tabular-nums">{format(unitPrice)}</span>
              {unitPriceAsOf && (
                <span className="text-muted-foreground">
                  {" "}
                  ({formatTimestampEs(unitPriceAsOf)})
                </span>
              )}
            </span>
          )}
          {rows.length >= 2 && (
            <span className="ml-auto">
              Variación en el rango{" "}
              {change ? (
                <span
                  className={cn(
                    "font-medium tabular-nums",
                    change.change < 0 ? "text-red-600" : "text-green-600"
                  )}
                >
                  {change.change >= 0 ? "+" : ""}
                  {format(change.change)} (
                  {formatPercent(change.pct, { signed: true })})
                </span>
              ) : (
                <span className="text-muted-foreground">sin dato</span>
              )}
            </span>
          )}
        </div>
      </CardContent>
    </Card>
  );
}
