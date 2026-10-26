"use client";

// Monthly spending stacked by category, in each category's own color. Feed it
// the month groups of GET /api/categories/spend?groupBy=month (every month of
// the window, empty ones included). Clicking a segment calls `onSelect` with
// its category and month, or follows `hrefFor`.
//
// recharts draws the axes, grid and tooltip from the target rows (behind
// transparent bars); the visible bars are a layer of our own so a new window
// animates by month, like a camera over the timeline: months on both sides
// slide and resize into their new slot, months entering fade in whole as they
// come into view, and months leaving fade out whole as they go. In the auto Y
// range the axis bounds tween along with them.

import { useId } from "react";
import { useRouter } from "next/navigation";
import {
  Bar,
  BarChart,
  CartesianGrid,
  DefaultZIndexes,
  Tooltip,
  XAxis,
  YAxis,
  ZIndexLayer,
  getNiceTickValues,
  usePlotArea,
  useYAxisScale,
} from "recharts";
import { InteractiveChart } from "@/components/charts/interactive-chart";
import { useYAxisRange } from "@/components/charts/y-axis-range";
import { categoryKey } from "@/components/charts/category-donut";
import { lerp, useTween } from "@/components/charts/use-tween";
import { formatAmount, formatAxisValue } from "@/lib/utils";
import type { CategorySpend, CategorySpendGroup } from "@/lib/category-spend";

/** YYYY-MM -> "ene 26" (es-CL, short month). */
export function formatSpendMonth(month: string): string {
  const [year, m] = month.split("-").map(Number);
  return new Date(year, m - 1, 1).toLocaleDateString("es-CL", {
    month: "short",
    year: "2-digit",
  });
}

/** Every category of the groups once, largest overall first, "Sin categoría"
 *  last, so the stack order and colors stay the same in every bar. */
function seriesOf(groups: CategorySpendGroup[]): CategorySpend[] {
  const byKey = new Map<string, CategorySpend>();
  for (const group of groups) {
    for (const c of group.categories) {
      const key = categoryKey(c.categoryId);
      const seen = byKey.get(key);
      byKey.set(key, seen ? { ...seen, amount: seen.amount + c.amount } : c);
    }
  }
  return [...byKey.values()].sort((a, b) => {
    if ((a.categoryId == null) !== (b.categoryId == null)) {
      return a.categoryId == null ? 1 : -1;
    }
    return b.amount - a.amount;
  });
}

/** Share of a month's slot the bar fills (recharts' default 10% gap a side). */
const BAR_FILL = 0.8;

/** One category's slice of a month's stack, in data units. */
interface Segment {
  key: string;
  category: CategorySpend;
  y0: number;
  y1: number;
}

interface Column {
  month: string;
  opacity: number;
  segments: Segment[];
}

/** Months since year 0, so consecutive months are consecutive integers. */
function monthIndex(month: string): number {
  const [year, m] = month.split("-").map(Number);
  return year * 12 + m - 1;
}

/** A month's slot center sits at `band * monthIndex + offset`, as a fraction
 *  of the plot width. Tweening the two numbers moves every month along one
 *  continuous timeline, so slots never cross. */
interface Frame {
  /** Slot width as a fraction of the plot width. */
  band: number;
  offset: number;
  yMin: number;
  yMax: number;
  columns: Column[];
}

function columnX(frame: Frame, month: string): number {
  return frame.band * monthIndex(month) + frame.offset;
}

/** The resting frame of a window: one slot per month, stacked in series
 *  order, with the nice Y bounds recharts' auto range would pick. */
function layout(groups: CategorySpendGroup[], series: CategorySpend[]): Frame {
  const band = 1 / Math.max(groups.length, 1);
  const first = groups.length > 0 ? monthIndex(groups[0].key) : 0;
  let low = 0;
  let high = 0;
  const columns = groups.map((group) => {
    const amounts = new Map(group.categories.map((c) => [categoryKey(c.categoryId), c.amount]));
    let top = 0;
    const segments: Segment[] = [];
    for (const category of series) {
      const key = categoryKey(category.categoryId);
      const amount = amounts.get(key);
      if (!amount) continue;
      segments.push({ key, category, y0: top, y1: top + amount });
      top += amount;
      low = Math.min(low, top);
      high = Math.max(high, top);
    }
    return { month: group.key, opacity: 1, segments };
  });
  const ticks = getNiceTickValues([low, high], 5, true);
  return {
    band,
    offset: (0.5 - first) * band,
    yMin: ticks[0],
    yMax: ticks[ticks.length - 1],
    columns,
  };
}

function interpolateFrame(from: Frame, to: Frame, t: number): Frame {
  const before = new Map(from.columns.map((c) => [c.month, c]));
  const after = new Set(to.columns.map((c) => c.month));
  const mixSegments = (a: Segment[], b: Segment[]): Segment[] => {
    const previous = new Map(a.map((s) => [s.key, s]));
    const next = new Set(b.map((s) => s.key));
    const mixed = b.map((s) => {
      const p = previous.get(s.key) ?? { ...s, y1: s.y0 };
      return { ...s, y0: lerp(p.y0, s.y0, t), y1: lerp(p.y1, s.y1, t) };
    });
    for (const s of a) {
      if (!next.has(s.key)) mixed.push({ ...s, y1: lerp(s.y1, s.y0, t) });
    }
    return mixed;
  };
  const columns = to.columns.map((c) => {
    const p = before.get(c.month);
    // A month entering fades in whole as it comes into view.
    if (!p) return { ...c, opacity: lerp(0, c.opacity, t) };
    return {
      ...c,
      opacity: lerp(p.opacity, c.opacity, t),
      segments: mixSegments(p.segments, c.segments),
    };
  });
  // A month leaving fades out whole as it goes.
  if (t < 1) {
    for (const c of from.columns) {
      if (!after.has(c.month)) columns.push({ ...c, opacity: lerp(c.opacity, 0, t) });
    }
    columns.sort((a, b) => a.month.localeCompare(b.month));
  }
  return {
    band: lerp(from.band, to.band, t),
    offset: lerp(from.offset, to.offset, t),
    yMin: lerp(from.yMin, to.yMin, t),
    yMax: lerp(from.yMax, to.yMax, t),
    columns,
  };
}

/** The visible bars, drawn in recharts' bar layer from the tweened frame. */
function BarsLayer({
  frame,
  onSelect,
}: {
  frame: Frame;
  onSelect?: (categoryId: string | null, month: string) => void;
}) {
  const clipId = useId();
  const plot = usePlotArea();
  const yScale = useYAxisScale();
  if (!plot || !yScale) return null;
  const width = frame.band * plot.width * BAR_FILL;
  const y = (value: number) => Number(yScale(value));

  return (
    <ZIndexLayer zIndex={DefaultZIndexes.bar}>
      <defs>
        <clipPath id={clipId}>
          <rect x={plot.x} y={plot.y} width={plot.width} height={plot.height} />
        </clipPath>
      </defs>
      <g clipPath={`url(#${clipId})`}>
        {frame.columns.map((column) => (
          <g key={column.month} opacity={column.opacity}>
            {column.segments.map((s) => {
              const top = y(Math.max(s.y0, s.y1));
              const height = Math.abs(y(s.y0) - y(s.y1));
              if (!(height > 0)) return null;
              return (
                <rect
                  key={s.key}
                  x={plot.x + columnX(frame, column.month) * plot.width - width / 2}
                  y={top}
                  width={width}
                  height={height}
                  fill={s.category.color}
                  className={onSelect ? "cursor-pointer" : undefined}
                  onClick={
                    onSelect ? () => onSelect(s.category.categoryId, column.month) : undefined
                  }
                />
              );
            })}
          </g>
        ))}
      </g>
    </ZIndexLayer>
  );
}

export function CategoryStackedBars({
  groups,
  currency,
  height = 260,
  hrefFor,
  onSelect,
  emptyMessage = "Sin gastos en este período.",
}: {
  groups: CategorySpendGroup[];
  currency: string;
  height?: number;
  hrefFor?: (categoryId: string | null, month: string) => string;
  onSelect?: (categoryId: string | null, month: string) => void;
  emptyMessage?: string;
}) {
  const router = useRouter();
  const yAxis = useYAxisRange();
  const series = seriesOf(groups);
  const signature = groups
    .map((g) => `${g.key}:${g.categories.map((c) => `${categoryKey(c.categoryId)}=${c.amount}`).join(",")}`)
    .join("|");
  const frame = useTween(layout(groups, series), signature, interpolateFrame);
  if (series.length === 0) {
    return <p className="text-sm text-muted-foreground">{emptyMessage}</p>;
  }

  // The target rows feed recharts' tooltip; `x` places them on the same
  // fractional axis the drawn bars use.
  const data = groups.map((group) => {
    const row: Record<string, number | string> = {
      x: columnX(frame, group.key),
      month: group.key,
    };
    for (const c of group.categories) row[categoryKey(c.categoryId)] = c.amount;
    return row;
  });
  const monthAt = (x: number) =>
    frame.columns.find((c) => Math.abs(columnX(frame, c.month) - x) < 1e-9);
  const labelAt = (x: unknown) => {
    const column = monthAt(Number(x));
    return column ? formatSpendMonth(column.month) : "";
  };
  const interactive = hrefFor != null || onSelect != null;
  const select = (categoryId: string | null, month: string) => {
    if (onSelect) onSelect(categoryId, month);
    else if (hrefFor) router.push(hrefFor(categoryId, month));
  };
  const yAxisProps =
    yAxis.range.kind === "auto"
      ? { domain: [frame.yMin, frame.yMax], allowDataOverflow: true }
      : yAxis.yAxisProps;

  return (
    <InteractiveChart yControl={yAxis} height={height}>
      <BarChart data={data}>
        <CartesianGrid strokeDasharray="3 3" vertical={false} />
        <XAxis
          type="number"
          dataKey="x"
          domain={[0, 1]}
          allowDataOverflow
          interval="equidistantPreserveEnd"
          ticks={frame.columns
            .map((c) => columnX(frame, c.month))
            .filter((x) => x >= 0 && x <= 1)}
          fontSize={12}
          // Also what recharts measures to space the labels.
          tickFormatter={labelAt}
          tick={({ x, y, payload, fill }) => (
            <text
              x={x}
              y={y}
              dy="0.71em"
              textAnchor="middle"
              fill={fill}
              fontSize={12}
              opacity={monthAt(Number(payload.value))?.opacity ?? 1}
            >
              {labelAt(payload.value)}
            </text>
          )}
        />
        <YAxis tickFormatter={formatAxisValue} fontSize={12} {...yAxisProps} />
        <Tooltip
          labelFormatter={labelAt}
          formatter={(value, name) => [formatAmount(currency, Number(value)), name]}
        />
        {series.map((c) => (
          <Bar
            key={categoryKey(c.categoryId)}
            dataKey={categoryKey(c.categoryId)}
            name={c.name}
            stackId="spend"
            fill={c.color}
            fillOpacity={0}
            activeBar={false}
            isAnimationActive={false}
          />
        ))}
        <BarsLayer frame={frame} onSelect={interactive ? select : undefined} />
      </BarChart>
    </InteractiveChart>
  );
}
