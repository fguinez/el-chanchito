"use client";

// Direct manipulation for a chart's axes, following the conventions finance
// charting tools converge on (TradingView, Plotly): each axis gutter is a
// drag surface (middle = slide the window, edge grips = stretch one bound),
// the wheel zooms around the cursor, double-click resets, and clicking a
// grip opens an exact-value input (a number for Y, a date for X). Quick
// chips cover the common Y ranges (auto / fit / zero baseline); X presets
// live in the header (time-range-control.tsx) and share the same state.
// Everything is hover-revealed to stay subtle, but stays visible while a
// manual range is active so a modified axis is never invisible; chips, grip
// keyboard arrows, and the exact-value inputs double as the single-pointer
// alternative to dragging.
//
// Recipe for a new time-series chart with every feature:
//   const chart = useTimeSeriesChart();          // or ({ kind: "days", days: 30 })
//   <TimeRangeControl control={chart.x} allowAll />   // in the CardAction
//   <InteractiveChart {...chart.interactiveProps} height={300}>
//     <LineChart data={rows}>                    // rows carry t (epoch ms)
//       <XAxis {...chart.xAxisProps} />
//       <YAxis {...chart.yAxisProps} />
//       <Tooltip labelFormatter={chart.labelFormatter} ... />
// Charts without a time axis pass only `yControl` (see useYAxisRange).

import {
  Children,
  cloneElement,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type ReactElement,
  type ReactNode,
} from "react";
import {
  ResponsiveContainer,
  usePlotArea,
  useXAxisDomain,
  useYAxisDomain,
} from "recharts";
import { cn, formatAxisValue, formatDateMs } from "@/lib/utils";
import {
  DEFAULT_Y_AXIS_RANGE,
  useYAxisRange,
  type YAxisRange,
} from "@/components/charts/y-axis-range";
import {
  DAY_MS,
  dayEndMs,
  dayStartMs,
  msToDay,
  paddedExtent,
  useXAxisRange,
  type XAxisRange,
  type XAxisRangeControl,
} from "@/components/charts/x-axis-range";

/** Pixel geometry of the plot area plus the domains the axes are rendering;
 *  the X pair is null when the X axis is categorical. */
interface PlotGeometry {
  x: number;
  y: number;
  width: number;
  height: number;
  yMin: number;
  yMax: number;
  xMin: number | null;
  xMax: number | null;
}

export interface YAxisRangeControl {
  range: YAxisRange;
  setRange: (range: YAxisRange) => void;
}

/** The narrowest an axis window may get, as a fraction of the drag-start span. */
const MIN_SPAN_RATIO = 0.01;
/** Pointer travel below this is a click (opens the exact-value editor). */
const CLICK_SLOP_PX = 4;
/** Height of the X-axis drag strip (the tick-label band). */
const X_STRIP_PX = 26;

/** Rounds a bound to something a human would type back. */
function roundBound(value: number): number {
  return Math.abs(value) >= 100 ? Math.round(value) : Number(value.toPrecision(4));
}

/** Wheel delta in pixel terms: Firefox reports line-mode deltas (~3/notch). */
function wheelPixels(event: WheelEvent, delta: number): number {
  return event.deltaMode === 1 ? delta * 16 : delta;
}

/** Chart child that reports the plot area and rendered domains upward.
 *  recharts v3 provides them through hooks available to any chart child. */
function GeometryProbe({
  onGeometry,
}: {
  onGeometry: (geometry: PlotGeometry) => void;
}) {
  const plotArea = usePlotArea();
  const yDomain = useYAxisDomain();
  const xDomain = useXAxisDomain();
  const [yMin, yMax] =
    Array.isArray(yDomain) &&
    typeof yDomain[0] === "number" &&
    typeof yDomain[1] === "number"
      ? [yDomain[0], yDomain[1]]
      : [null, null];
  // A categorical axis reports its full category array, which can also be
  // numeric (e.g. the waterfall's bar indexes); only a numeric [min, max]
  // pair is a manipulable domain.
  const [xMin, xMax] =
    Array.isArray(xDomain) &&
    xDomain.length === 2 &&
    typeof xDomain[0] === "number" &&
    typeof xDomain[1] === "number"
      ? [xDomain[0], xDomain[1]]
      : [null, null];
  const x = plotArea?.x ?? null;
  const y = plotArea?.y ?? null;
  const width = plotArea?.width ?? null;
  const height = plotArea?.height ?? null;

  useEffect(() => {
    if (x == null || y == null || width == null || height == null) return;
    if (yMin == null || yMax == null || height <= 0) return;
    onGeometry({ x, y, width, height, yMin, yMax, xMin, xMax });
  }, [x, y, width, height, yMin, yMax, xMin, xMax, onGeometry]);

  return null;
}

type DragKind = "pan" | "min" | "max";

interface DragState {
  kind: DragKind;
  pointerId: number;
  start: number;
  startMin: number;
  startMax: number;
  moved: boolean;
}

function RangeChip({
  label,
  title,
  active,
  onClick,
}: {
  label: string;
  title: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      className={cn(
        "pointer-events-auto rounded border bg-background/85 px-1.5 py-0.5 text-[10px] leading-none backdrop-blur-sm transition-colors",
        active
          ? "border-foreground/25 text-foreground"
          : "text-muted-foreground hover:text-foreground"
      )}
    >
      {label}
    </button>
  );
}

function YAxisOverlay({
  geometry,
  range,
  setRange,
}: {
  geometry: PlotGeometry;
  range: YAxisRange;
  setRange: (range: YAxisRange) => void;
}) {
  const stripRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<DragState | null>(null);
  const [dragging, setDragging] = useState<DragKind | null>(null);
  const [dragValue, setDragValue] = useState<number | null>(null);
  const [editing, setEditing] = useState<"min" | "max" | null>(null);
  const [draft, setDraft] = useState("");
  const [draftInvalid, setDraftInvalid] = useState(false);

  // The wheel handler reads through refs so its non-passive listener (needed
  // to preventDefault page scrolling) can be attached once.
  const geometryRef = useRef(geometry);
  const setRangeRef = useRef(setRange);
  useEffect(() => {
    geometryRef.current = geometry;
    setRangeRef.current = setRange;
  }, [geometry, setRange]);

  const modified = range.kind !== "auto";
  const revealed = cn(
    "opacity-0 transition-opacity duration-150 group-hover/chart:opacity-100 group-focus-within/chart:opacity-100",
    (modified || dragging || editing) && "opacity-100"
  );

  const applyBounds = useCallback(
    (min: number, max: number) => setRange({ kind: "custom", min, max }),
    [setRange]
  );

  const beginDrag = (event: ReactPointerEvent<HTMLElement>, kind: DragKind) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    const span = geometry.yMax - geometry.yMin;
    if (!(span > 0)) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      kind,
      pointerId: event.pointerId,
      start: event.clientY,
      startMin: geometry.yMin,
      startMax: geometry.yMax,
      moved: false,
    };
    setDragging(kind);
  };

  const moveDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current;
    if (!drag || event.pointerId !== drag.pointerId) return;
    const dy = event.clientY - drag.start;
    if (Math.abs(dy) > CLICK_SLOP_PX) drag.moved = true;
    if (!drag.moved) return;
    const span = drag.startMax - drag.startMin;
    const perPixel = span / geometry.height;
    const floor = span * MIN_SPAN_RATIO;
    if (drag.kind === "pan") {
      // The ruler follows the pointer: dragging down slides the window up.
      const shift = dy * perPixel;
      applyBounds(drag.startMin + shift, drag.startMax + shift);
    } else if (drag.kind === "max") {
      const next = Math.max(drag.startMax - dy * perPixel, drag.startMin + floor);
      setDragValue(next);
      applyBounds(drag.startMin, next);
    } else {
      const next = Math.min(drag.startMin - dy * perPixel, drag.startMax - floor);
      setDragValue(next);
      applyBounds(next, drag.startMax);
    }
  };

  const openEditor = useCallback(
    (bound: "min" | "max") => {
      setDraft(
        String(roundBound(bound === "min" ? geometry.yMin : geometry.yMax))
      );
      setDraftInvalid(false);
      setEditing(bound);
    },
    [geometry.yMin, geometry.yMax]
  );

  const endDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current;
    if (!drag || event.pointerId !== drag.pointerId) return;
    dragRef.current = null;
    setDragging(null);
    setDragValue(null);
    // A grip click without travel asks for the exact value instead.
    if (!drag.moved && drag.kind !== "pan") openEditor(drag.kind);
  };

  const cancelDrag = () => {
    dragRef.current = null;
    setDragging(null);
    setDragValue(null);
  };

  // Wheel over the axis zooms around the cursor; needs a non-passive
  // listener because React's synthetic wheel handler cannot preventDefault.
  useEffect(() => {
    const strip = stripRef.current;
    if (!strip) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const g = geometryRef.current;
      const span = g.yMax - g.yMin;
      if (!(span > 0)) return;
      const factor = Math.exp(wheelPixels(event, event.deltaY) * 0.0015);
      const rect = strip.getBoundingClientRect();
      const t = Math.min(Math.max((event.clientY - rect.top) / rect.height, 0), 1);
      const anchor = g.yMax - t * span;
      const nextMin = anchor + (g.yMin - anchor) * factor;
      const nextMax = anchor + (g.yMax - anchor) * factor;
      if (nextMax - nextMin < Math.max(Math.abs(anchor), 1) * 1e-9) return;
      setRangeRef.current({ kind: "custom", min: nextMin, max: nextMax });
    };
    strip.addEventListener("wheel", onWheel, { passive: false });
    return () => strip.removeEventListener("wheel", onWheel);
  }, []);

  const onGripKeyDown =
    (bound: "min" | "max") => (event: ReactKeyboardEvent<HTMLElement>) => {
      const span = geometry.yMax - geometry.yMin;
      const floor = span * MIN_SPAN_RATIO;
      const nudge = (fraction: number) => {
        event.preventDefault();
        const delta = span * fraction;
        if (bound === "max") {
          applyBounds(
            geometry.yMin,
            Math.max(geometry.yMax + delta, geometry.yMin + floor)
          );
        } else {
          applyBounds(
            Math.min(geometry.yMin + delta, geometry.yMax - floor),
            geometry.yMax
          );
        }
      };
      switch (event.key) {
        case "ArrowUp":
          nudge(0.02);
          break;
        case "ArrowDown":
          nudge(-0.02);
          break;
        case "PageUp":
          nudge(0.1);
          break;
        case "PageDown":
          nudge(-0.1);
          break;
        case "Enter":
        case " ":
          event.preventDefault();
          openEditor(bound);
          break;
      }
    };

  const commitEditor = (): boolean => {
    if (!editing) return true;
    const value = Number(draft);
    const other = editing === "min" ? geometry.yMax : geometry.yMin;
    const valid =
      draft.trim() !== "" &&
      Number.isFinite(value) &&
      (editing === "min" ? value < other : value > other);
    if (!valid) {
      setDraftInvalid(true);
      return false;
    }
    // Keep the untouched bound as the user sees it: a custom range keeps its
    // stored bound (possibly auto); any other mode pins the rendered value.
    const stored = range.kind === "custom" ? range : null;
    if (editing === "min") {
      setRange({
        kind: "custom",
        min: value,
        max: stored ? stored.max : geometry.yMax,
      });
    } else {
      setRange({
        kind: "custom",
        min: stored ? stored.min : geometry.yMin,
        max: value,
      });
    }
    setEditing(null);
    return true;
  };

  const gripValue = (bound: "min" | "max") =>
    bound === "min" ? geometry.yMin : geometry.yMax;

  const stripWidth = Math.max(geometry.x, 24);

  return (
    <>
      {/* Drag surface over the axis gutter: slide to pan, double-click resets. */}
      <div
        ref={stripRef}
        className={cn(
          "absolute z-10 cursor-ns-resize touch-none select-none rounded-sm transition-colors hover:bg-foreground/[0.04]",
          dragging === "pan" && "bg-foreground/[0.06]"
        )}
        style={{ left: 0, top: geometry.y, width: stripWidth, height: geometry.height }}
        title="Arrastra para deslizar el eje Y; rueda para acercar; doble clic para restablecer"
        onPointerDown={(event) => beginDrag(event, "pan")}
        onPointerMove={moveDrag}
        onPointerUp={endDrag}
        onPointerCancel={cancelDrag}
        onDoubleClick={() => setRange(DEFAULT_Y_AXIS_RANGE)}
      />

      {/* Edge grips: drag to stretch one bound, click or Enter for exact value. */}
      {(["max", "min"] as const).map((bound) => (
        <button
          key={bound}
          type="button"
          aria-label={
            bound === "max"
              ? `Límite superior del eje Y: ${formatAxisValue(gripValue(bound))}. Flechas para ajustar, Enter para escribir un valor exacto`
              : `Límite inferior del eje Y: ${formatAxisValue(gripValue(bound))}. Flechas para ajustar, Enter para escribir un valor exacto`
          }
          className={cn(
            "absolute z-20 flex h-3.5 -translate-y-1/2 cursor-row-resize touch-none items-center justify-center rounded outline-none focus-visible:ring-2 focus-visible:ring-ring",
            revealed
          )}
          style={{
            left: 2,
            width: Math.max(stripWidth - 6, 20),
            top: bound === "max" ? geometry.y + 2 : geometry.y + geometry.height - 2,
          }}
          onPointerDown={(event) => {
            event.stopPropagation();
            beginDrag(event, bound);
          }}
          onPointerMove={moveDrag}
          onPointerUp={endDrag}
          onPointerCancel={cancelDrag}
          onDoubleClick={(event) => event.stopPropagation()}
          onKeyDown={onGripKeyDown(bound)}
        >
          <span className="h-1 w-5 rounded-full bg-muted-foreground/50" />
        </button>
      ))}

      {/* Quick ranges; stays visible while the axis is manually adjusted. */}
      <div
        className={cn("pointer-events-none absolute z-20 flex gap-1", revealed)}
        style={{ left: geometry.x + 6, top: geometry.y + 4 }}
      >
        <RangeChip
          label="Auto"
          title="Rango por defecto del gráfico"
          active={range.kind === "auto"}
          onClick={() => setRange({ kind: "auto" })}
        />
        <RangeChip
          label="Ajustar"
          title="Acercar a los valores graficados"
          active={range.kind === "fit"}
          onClick={() => setRange({ kind: "fit" })}
        />
        <RangeChip
          label="Desde 0"
          title="Fijar el mínimo en cero"
          active={range.kind === "custom" && range.min === 0 && range.max === null}
          onClick={() => setRange({ kind: "custom", min: 0, max: null })}
        />
      </div>

      {/* Live value while stretching a bound. */}
      {dragging && dragging !== "pan" && dragValue != null && (
        <div
          className="pointer-events-none absolute z-30 rounded border bg-popover px-1.5 py-0.5 text-[10px] tabular-nums text-popover-foreground shadow-sm"
          style={{
            left: 4,
            top:
              dragging === "max" ? geometry.y + 12 : geometry.y + geometry.height - 28,
          }}
        >
          {formatAxisValue(dragValue)}
        </div>
      )}

      {/* Exact-value editor for one bound. */}
      {editing && (
        <div
          className="absolute z-30"
          style={{
            left: 4,
            top:
              editing === "max" ? geometry.y + 10 : geometry.y + geometry.height - 38,
          }}
        >
          <input
            autoFocus
            type="number"
            value={draft}
            aria-label={
              editing === "max"
                ? "Valor máximo del eje Y"
                : "Valor mínimo del eje Y"
            }
            aria-invalid={draftInvalid}
            className={cn(
              "h-7 w-28 rounded-md border bg-popover px-2 text-xs tabular-nums text-popover-foreground shadow-md outline-none focus:ring-2 focus:ring-ring",
              draftInvalid && "border-destructive focus:ring-destructive"
            )}
            onChange={(event) => {
              setDraft(event.target.value);
              setDraftInvalid(false);
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter") commitEditor();
              if (event.key === "Escape") setEditing(null);
            }}
            onBlur={() => {
              if (!commitEditor()) setEditing(null);
            }}
          />
        </div>
      )}
    </>
  );
}

function XAxisOverlay({
  geometry,
  min,
  max,
  bounds,
  range,
  setRange,
  reset,
}: {
  geometry: PlotGeometry;
  min: number;
  max: number;
  /** Padded data extent the window may not leave; null when unknown. */
  bounds: [number, number] | null;
  range: XAxisRange;
  setRange: (range: XAxisRange) => void;
  reset: () => void;
}) {
  const stripRef = useRef<HTMLDivElement>(null);
  const dragRef = useRef<DragState | null>(null);
  const [dragging, setDragging] = useState<DragKind | null>(null);
  const [dragValue, setDragValue] = useState<number | null>(null);
  const [editing, setEditing] = useState<"min" | "max" | null>(null);
  const [draft, setDraft] = useState("");
  const [draftInvalid, setDraftInvalid] = useState(false);

  const lo = bounds ? bounds[0] : -Infinity;
  const hi = bounds ? bounds[1] : Infinity;

  const viewRef = useRef({ min, max, lo, hi });
  const setRangeRef = useRef(setRange);
  useEffect(() => {
    viewRef.current = { min, max, lo, hi };
    setRangeRef.current = setRange;
  }, [min, max, lo, hi, setRange]);

  // Dragged windows have no other visible trace than the header's date-range
  // label; presets (days/absolute/all) are already highlighted there.
  const revealed = cn(
    "opacity-0 transition-opacity duration-150 group-hover/chart:opacity-100 group-focus-within/chart:opacity-100",
    (range.kind === "custom" || dragging || editing) && "opacity-100"
  );

  const applyBounds = useCallback(
    (nextMin: number, nextMax: number) =>
      setRange({ kind: "custom", min: nextMin, max: nextMax }),
    [setRange]
  );

  const spanFloor = (span: number) => Math.max(span * MIN_SPAN_RATIO, DAY_MS);

  const beginDrag = (event: ReactPointerEvent<HTMLElement>, kind: DragKind) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    if (!(max - min > 0)) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = {
      kind,
      pointerId: event.pointerId,
      start: event.clientX,
      startMin: min,
      startMax: max,
      moved: false,
    };
    setDragging(kind);
  };

  const moveDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current;
    if (!drag || event.pointerId !== drag.pointerId) return;
    const dx = event.clientX - drag.start;
    if (Math.abs(dx) > CLICK_SLOP_PX) drag.moved = true;
    if (!drag.moved) return;
    const span = drag.startMax - drag.startMin;
    const perPixel = span / geometry.width;
    const floor = spanFloor(span);
    if (drag.kind === "pan") {
      // The ruler follows the pointer: dragging right slides the window back
      // in time (the dates under the cursor travel with it). The window
      // stops at the data extent instead of sliding into empty space.
      const shift = -dx * perPixel;
      let nextMin = drag.startMin + shift;
      let nextMax = drag.startMax + shift;
      if (span >= hi - lo) {
        nextMin = lo;
        nextMax = hi;
      } else if (nextMin < lo) {
        nextMax += lo - nextMin;
        nextMin = lo;
      } else if (nextMax > hi) {
        nextMin -= nextMax - hi;
        nextMax = hi;
      }
      applyBounds(nextMin, nextMax);
    } else if (drag.kind === "max") {
      const next = Math.min(
        Math.max(drag.startMax + dx * perPixel, drag.startMin + floor),
        hi
      );
      setDragValue(next);
      applyBounds(drag.startMin, next);
    } else {
      const next = Math.max(
        Math.min(drag.startMin + dx * perPixel, drag.startMax - floor),
        lo
      );
      setDragValue(next);
      applyBounds(next, drag.startMax);
    }
  };

  const openEditor = useCallback(
    (bound: "min" | "max") => {
      setDraft(msToDay(bound === "min" ? min : max));
      setDraftInvalid(false);
      setEditing(bound);
    },
    [min, max]
  );

  const endDrag = (event: ReactPointerEvent<HTMLElement>) => {
    const drag = dragRef.current;
    if (!drag || event.pointerId !== drag.pointerId) return;
    dragRef.current = null;
    setDragging(null);
    setDragValue(null);
    if (!drag.moved && drag.kind !== "pan") openEditor(drag.kind);
  };

  const cancelDrag = () => {
    dragRef.current = null;
    setDragging(null);
    setDragValue(null);
  };

  useEffect(() => {
    const strip = stripRef.current;
    if (!strip) return;
    const onWheel = (event: WheelEvent) => {
      event.preventDefault();
      const view = viewRef.current;
      const span = view.max - view.min;
      if (!(span > 0)) return;
      // Trackpads report horizontal deltas over a horizontal strip.
      const raw =
        Math.abs(event.deltaX) > Math.abs(event.deltaY)
          ? event.deltaX
          : event.deltaY;
      const factor = Math.exp(wheelPixels(event, raw) * 0.0015);
      const rect = strip.getBoundingClientRect();
      const t = Math.min(Math.max((event.clientX - rect.left) / rect.width, 0), 1);
      const anchor = view.min + t * span;
      const nextMin = Math.max(anchor + (view.min - anchor) * factor, view.lo);
      const nextMax = Math.min(anchor + (view.max - anchor) * factor, view.hi);
      if (nextMax - nextMin < DAY_MS) return;
      setRangeRef.current({ kind: "custom", min: nextMin, max: nextMax });
    };
    strip.addEventListener("wheel", onWheel, { passive: false });
    return () => strip.removeEventListener("wheel", onWheel);
  }, []);

  const onGripKeyDown =
    (bound: "min" | "max") => (event: ReactKeyboardEvent<HTMLElement>) => {
      const span = max - min;
      const floor = spanFloor(span);
      const nudge = (fraction: number) => {
        event.preventDefault();
        const delta = span * fraction;
        if (bound === "max") {
          applyBounds(min, Math.min(Math.max(max + delta, min + floor), hi));
        } else {
          applyBounds(Math.max(Math.min(min + delta, max - floor), lo), max);
        }
      };
      switch (event.key) {
        case "ArrowRight":
          nudge(0.02);
          break;
        case "ArrowLeft":
          nudge(-0.02);
          break;
        case "PageUp":
          nudge(0.1);
          break;
        case "PageDown":
          nudge(-0.1);
          break;
        case "Enter":
        case " ":
          event.preventDefault();
          openEditor(bound);
          break;
      }
    };

  const commitEditor = (): boolean => {
    if (!editing) return true;
    const typed = editing === "min" ? dayStartMs(draft) : dayEndMs(draft);
    const value = Math.min(Math.max(typed, lo), hi);
    const other = editing === "min" ? max : min;
    const valid =
      draft !== "" &&
      Number.isFinite(value) &&
      (editing === "min" ? value < other : value > other);
    if (!valid) {
      setDraftInvalid(true);
      return false;
    }
    if (editing === "min") setRange({ kind: "custom", min: value, max });
    else setRange({ kind: "custom", min, max: value });
    setEditing(null);
    return true;
  };

  const stripTop = geometry.y + geometry.height;

  return (
    <>
      {/* Drag surface over the time gutter: slide to pan, double-click resets. */}
      <div
        ref={stripRef}
        className={cn(
          "absolute z-10 cursor-ew-resize touch-none select-none rounded-sm transition-colors hover:bg-foreground/[0.04]",
          dragging === "pan" && "bg-foreground/[0.06]"
        )}
        style={{
          left: geometry.x,
          top: stripTop,
          width: geometry.width,
          height: X_STRIP_PX,
        }}
        title="Arrastra para deslizar el eje de tiempo; rueda para acercar; doble clic para restablecer"
        onPointerDown={(event) => beginDrag(event, "pan")}
        onPointerMove={moveDrag}
        onPointerUp={endDrag}
        onPointerCancel={cancelDrag}
        onDoubleClick={reset}
      />

      {/* Edge grips: drag to stretch one bound, click or Enter for an exact date. */}
      {(["min", "max"] as const).map((bound) => (
        <button
          key={bound}
          type="button"
          aria-label={
            bound === "min"
              ? `Inicio del rango de tiempo: ${formatDateMs(min)}. Flechas para ajustar, Enter para elegir una fecha exacta`
              : `Fin del rango de tiempo: ${formatDateMs(max)}. Flechas para ajustar, Enter para elegir una fecha exacta`
          }
          className={cn(
            "absolute z-20 flex w-3.5 -translate-x-1/2 cursor-col-resize touch-none items-center justify-center rounded outline-none focus-visible:ring-2 focus-visible:ring-ring",
            revealed
          )}
          style={{
            left: bound === "min" ? geometry.x + 2 : geometry.x + geometry.width - 2,
            top: stripTop + 2,
            height: X_STRIP_PX - 4,
          }}
          onPointerDown={(event) => {
            event.stopPropagation();
            beginDrag(event, bound);
          }}
          onPointerMove={moveDrag}
          onPointerUp={endDrag}
          onPointerCancel={cancelDrag}
          onDoubleClick={(event) => event.stopPropagation()}
          onKeyDown={onGripKeyDown(bound)}
        >
          <span className="h-5 w-1 rounded-full bg-muted-foreground/50" />
        </button>
      ))}

      {/* Live date while stretching a bound. */}
      {dragging && dragging !== "pan" && dragValue != null && (
        <div
          className="pointer-events-none absolute z-30 rounded border bg-popover px-1.5 py-0.5 text-[10px] tabular-nums text-popover-foreground shadow-sm"
          style={{
            top: stripTop - 24,
            ...(dragging === "min"
              ? { left: geometry.x + 6 }
              : { left: geometry.x + geometry.width - 96 }),
          }}
        >
          {formatDateMs(dragValue)}
        </div>
      )}

      {/* Exact-date editor for one bound. */}
      {editing && (
        <div
          className="absolute z-30"
          style={{
            top: stripTop - 36,
            left:
              editing === "min" ? geometry.x + 2 : geometry.x + geometry.width - 150,
          }}
        >
          <input
            autoFocus
            type="date"
            value={draft}
            aria-label={
              editing === "min"
                ? "Fecha inicial del eje de tiempo"
                : "Fecha final del eje de tiempo"
            }
            aria-invalid={draftInvalid}
            className={cn(
              "h-7 w-36 rounded-md border bg-popover px-2 text-xs tabular-nums text-popover-foreground shadow-md outline-none focus:ring-2 focus:ring-ring",
              draftInvalid && "border-destructive focus:ring-destructive"
            )}
            onChange={(event) => {
              setDraft(event.target.value);
              setDraftInvalid(false);
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter") commitEditor();
              if (event.key === "Escape") setEditing(null);
            }}
            onBlur={() => {
              if (!commitEditor()) setEditing(null);
            }}
          />
        </div>
      )}
    </>
  );
}

/** Drop-in replacement for a chart's <ResponsiveContainer> that makes its
 *  axes directly manipulable. Wire `yControl` to `useYAxisRange` (and spread
 *  the hook's `yAxisProps` on the <YAxis>); pass `xControl` too for charts
 *  with a numeric time axis (see `useTimeSeriesChart`). */
export function InteractiveChart({
  yControl,
  xControl,
  height,
  children,
  className,
}: {
  yControl: YAxisRangeControl;
  xControl?: XAxisRangeControl;
  height: number;
  children: ReactElement<{ children?: ReactNode; data?: unknown }>;
  className?: string;
}) {
  const [geometry, setGeometry] = useState<PlotGeometry | null>(null);
  const handleGeometry = useCallback((next: PlotGeometry) => {
    setGeometry((prev) =>
      prev &&
      prev.x === next.x &&
      prev.y === next.y &&
      prev.width === next.width &&
      prev.height === next.height &&
      prev.yMin === next.yMin &&
      prev.yMax === next.yMax &&
      prev.xMin === next.xMin &&
      prev.xMax === next.xMax
        ? prev
        : next
    );
  }, []);

  const chart = Children.only(children);
  const chartWithProbe = cloneElement(chart, undefined, [
    ...Children.toArray(chart.props.children),
    <GeometryProbe key="axis-geometry-probe" onGeometry={handleGeometry} />,
  ]);

  // The plotted rows bound how far the time window may travel.
  const rows = chart.props.data;
  const dataBounds = useMemo<[number, number] | null>(() => {
    if (!Array.isArray(rows)) return null;
    let min = Infinity;
    let max = -Infinity;
    for (const row of rows) {
      const t = (row as { t?: unknown }).t;
      if (typeof t === "number" && Number.isFinite(t)) {
        if (t < min) min = t;
        if (t > max) max = t;
      }
    }
    return min <= max ? paddedExtent(min, max) : null;
  }, [rows]);

  // Tick density tracks the measured plot width and rendered window.
  const notifyView = xControl?.notifyView;
  useEffect(() => {
    if (!notifyView || !geometry || geometry.xMin == null || geometry.xMax == null)
      return;
    notifyView({ width: geometry.width, min: geometry.xMin, max: geometry.xMax });
  }, [notifyView, geometry]);

  return (
    <div className={cn("group/chart relative", className)}>
      <ResponsiveContainer width="100%" height={height}>
        {chartWithProbe}
      </ResponsiveContainer>
      {geometry && (
        <YAxisOverlay
          geometry={geometry}
          range={yControl.range}
          setRange={yControl.setRange}
        />
      )}
      {geometry && xControl && geometry.xMin != null && geometry.xMax != null && (
        <XAxisOverlay
          geometry={geometry}
          min={geometry.xMin}
          max={geometry.xMax}
          bounds={dataBounds}
          range={xControl.range}
          setRange={xControl.setRange}
          reset={xControl.reset}
        />
      )}
    </div>
  );
}

/** One hook for a chart over time: interactive Y range + time window, plus
 *  ready-to-spread axis props and the tooltip label formatter. See the
 *  recipe at the top of this file. */
export function useTimeSeriesChart(initialX: XAxisRange = { kind: "all" }) {
  const y = useYAxisRange();
  const x = useXAxisRange(initialX);
  return {
    y,
    x,
    interactiveProps: { yControl: y, xControl: x },
    xAxisProps: x.xAxisProps,
    yAxisProps: {
      tickFormatter: formatAxisValue,
      fontSize: 12,
      ...y.yAxisProps,
    },
    /** Tooltip labelFormatter for the ms time axis. */
    labelFormatter: (label: unknown) => formatDateMs(Number(label)),
  };
}
