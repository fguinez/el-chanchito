"use client";

// Y-axis range model shared by every dashboard graph: "auto" keeps the
// chart's default range, "fit" zooms to the plotted values, and "custom"
// pins explicit min/max bounds (either may stay null = auto). `useYAxisRange`
// owns the selection and translates it into props to spread on a recharts
// <YAxis>. The interactive control living on the axis itself is
// `InteractiveChart` in interactive-chart.tsx.

import { useState } from "react";
import type { NumberDomain, YAxisProps } from "recharts";

/** How the Y axis picks its bounds; custom bounds left null fall back to auto. */
export type YAxisRange =
  | { kind: "auto" }
  | { kind: "fit" }
  | { kind: "custom"; min: number | null; max: number | null };

export const DEFAULT_Y_AXIS_RANGE: YAxisRange = { kind: "auto" };

/** Fraction of the data span kept as breathing room in "fit" mode. */
const FIT_PADDING = 0.05;

/** Snaps a bound outward to 3 significant digits so fit bounds (and the
 *  ticks derived from them) land on clean numbers instead of float noise. */
function niceBound(value: number, roundUp: boolean): number {
  if (value === 0) return 0;
  const magnitude = 10 ** (Math.floor(Math.log10(Math.abs(value))) - 2);
  return (roundUp ? Math.ceil : Math.floor)(value / magnitude) * magnitude;
}

/** "Fit" bounds: the data extent plus a little padding so lines touching the
 *  extremes stay visible. A flat series pads by 5% of its magnitude. */
function fitDomain([dataMin, dataMax]: NumberDomain): NumberDomain {
  const span = dataMax - dataMin;
  const pad =
    span > 0 ? span * FIT_PADDING : Math.max(Math.abs(dataMax), 1) * FIT_PADDING;
  return [niceBound(dataMin - pad, false), niceBound(dataMax + pad, true)];
}

type YAxisRangeProps = Pick<YAxisProps, "domain" | "allowDataOverflow">;

/** recharts <YAxis> props for a selection. Auto passes nothing so the chart
 *  keeps its default range; custom clips data outside the pinned bounds. */
export function yAxisRangeProps(range: YAxisRange): YAxisRangeProps {
  switch (range.kind) {
    case "auto":
      return {};
    case "fit":
      return { domain: fitDomain };
    case "custom":
      return {
        domain: [range.min ?? "auto", range.max ?? "auto"],
        allowDataOverflow: true,
      };
  }
}

/** Selection state plus the matching <YAxis> props, one per chart. */
export function useYAxisRange(initial: YAxisRange = DEFAULT_Y_AXIS_RANGE) {
  const [range, setRange] = useState<YAxisRange>(initial);
  return { range, setRange, yAxisProps: yAxisRangeProps(range) };
}
