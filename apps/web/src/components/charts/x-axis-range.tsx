"use client";

// Time-window model for charts with a numeric time X axis (rows carry a `t`
// column in epoch ms): "all" frames the whole loaded history, "days" is a
// trailing window anchored to today, "absolute" an inclusive day range, and
// "custom" pins exact ms bounds (produced by dragging the axis). The header
// control lives in time-range-control.tsx and the on-axis drag overlay in
// interactive-chart.tsx; both operate on this same state.

import { useCallback, useRef, useState } from "react";
import type { NumberDomain, XAxisProps } from "recharts";
import { formatDateTickMs } from "@/lib/utils";

/** How the time axis picks its window. */
export type XAxisRange =
  | { kind: "all" }
  | { kind: "days"; days: number }
  | { kind: "absolute"; from: string; to: string; label?: string }
  | { kind: "custom"; min: number; max: number };

export const DAY_MS = 24 * 60 * 60 * 1000;

/** Epoch ms of a plain YYYY-MM-DD's local midnight; local parsing keeps the
 *  point on its calendar day across timezones. */
export function dayStartMs(day: string): number {
  const [year, month, date] = day.split("-").map(Number);
  return new Date(year, month - 1, date).getTime();
}

/** Epoch ms of the last instant of a plain YYYY-MM-DD, local time. */
export function dayEndMs(day: string): number {
  const [year, month, date] = day.split("-").map(Number);
  return new Date(year, month - 1, date + 1).getTime() - 1;
}

/** Local YYYY-MM-DD containing an epoch ms (for date inputs). */
export function msToDay(ms: number): string {
  const d = new Date(ms);
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

/** The data extent plus breathing room so edge points (and their dots) stay
 *  clear of the plot borders. "All" renders exactly this frame, and the
 *  interactive pan/zoom is clamped to it so the window never wanders into
 *  empty space. */
export function paddedExtent(dataMin: number, dataMax: number): [number, number] {
  const pad = Math.max((dataMax - dataMin) * 0.02, DAY_MS / 2);
  return [dataMin - pad, dataMax + pad];
}

function allDomain([dataMin, dataMax]: NumberDomain): NumberDomain {
  return paddedExtent(dataMin, dataMax);
}

/** Candidate gaps between time ticks, in days. */
const TICK_STEP_DAYS = [1, 2, 3, 5, 7, 14, 21, 30, 61, 91, 182, 365];
/** Room one tick label needs so neighbors never touch. */
const TICK_GAP_PX = 72;

/** Day-aligned tick positions for a [min, max] ms window: the smallest
 *  calendar step whose labels still get TICK_GAP_PX of room, so density
 *  tracks the plot width. Each tick snaps to a local midnight so labels
 *  stay on honest calendar days across timezones and DST. */
export function timeTicks(min: number, max: number, plotWidth: number): number[] {
  const span = max - min;
  if (!(span > 0) || !(plotWidth > 0)) return [];
  const pxPerDay = plotWidth / (span / DAY_MS);
  const stepDays =
    TICK_STEP_DAYS.find((s) => s * pxPerDay >= TICK_GAP_PX) ??
    Math.ceil(TICK_GAP_PX / pxPerDay / 365) * 365;
  const stepMs = stepDays * DAY_MS;
  const ticks: number[] = [];
  for (
    let grid = Math.floor(min / stepMs) * stepMs;
    grid <= max + stepMs;
    grid += stepMs
  ) {
    const d = new Date(grid);
    const tick = new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
    if (tick < min || tick > max) continue;
    if (ticks[ticks.length - 1] !== tick) ticks.push(tick);
  }
  return ticks;
}

/** The rendered [min, max] window in ms; null for "all" (data-driven). */
function fixedWindowMs(range: XAxisRange): [number, number] | null {
  switch (range.kind) {
    case "all":
      return null;
    case "days": {
      // Calendar arithmetic: fixed 24h steps drift an hour across DST
      // changes and would clip the boundary day's midnight point.
      const now = new Date();
      const y = now.getFullYear();
      const m = now.getMonth();
      const d = now.getDate();
      return [
        new Date(y, m, d - (range.days - 1)).getTime(),
        new Date(y, m, d + 1).getTime() - 1,
      ];
    }
    case "absolute":
      return [dayStartMs(range.from), dayEndMs(range.to)];
    case "custom":
      return [range.min, range.max];
  }
}

type XAxisRangeProps = Pick<
  XAxisProps,
  | "dataKey"
  | "type"
  | "domain"
  | "allowDataOverflow"
  | "tickFormatter"
  | "fontSize"
  | "ticks"
  | "interval"
>;

/** recharts <XAxis> props for a selection. Convention over configuration:
 *  chart rows expose their timestamp as `t` (epoch ms). */
export function xAxisRangeProps(range: XAxisRange): XAxisRangeProps {
  const window = fixedWindowMs(range);
  return {
    dataKey: "t",
    type: "number",
    domain: window ?? allDomain,
    allowDataOverflow: true,
    tickFormatter: (ms: number) => formatDateTickMs(ms),
    fontSize: 12,
  };
}

/** Query string for APIs that accept a fetchable window (days / from&to);
 *  null for kinds that only re-frame already-loaded data (all / custom). */
export function rangeQuery(range: XAxisRange): string | null {
  if (range.kind === "days") return `days=${range.days}`;
  if (range.kind === "absolute") return `from=${range.from}&to=${range.to}`;
  return null;
}

/** The rendered window plus the plot width, as measured by InteractiveChart. */
export interface XAxisView {
  width: number;
  min: number;
  max: number;
}

export interface XAxisRangeControl {
  range: XAxisRange;
  setRange: (range: XAxisRange) => void;
  /** Back to the chart's initial window (double-click on the axis). */
  reset: () => void;
  /** Fed by InteractiveChart so tick density can track the plot's size. */
  notifyView: (view: XAxisView) => void;
}

/** Selection state plus the matching <XAxis> props, one per chart. Tick
 *  density adapts to the plot width once the chart reports its geometry. */
export function useXAxisRange(
  initial: XAxisRange = { kind: "all" }
): XAxisRangeControl & { xAxisProps: XAxisRangeProps } {
  const [range, setRange] = useState<XAxisRange>(initial);
  const [view, setView] = useState<XAxisView | null>(null);
  const initialRef = useRef(initial);
  const notifyView = useCallback((next: XAxisView) => {
    setView((prev) =>
      prev &&
      prev.width === next.width &&
      prev.min === next.min &&
      prev.max === next.max
        ? prev
        : next
    );
  }, []);
  const props = xAxisRangeProps(range);
  const ticks = view ? timeTicks(view.min, view.max, view.width) : [];
  return {
    range,
    setRange,
    reset: () => setRange(initialRef.current),
    notifyView,
    xAxisProps:
      ticks.length >= 2 ? { ...props, ticks, interval: 0 } : props,
  };
}
