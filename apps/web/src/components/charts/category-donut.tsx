"use client";

// Spending per category as a donut plus a ranked legend (share and amount),
// in each category's own color. A slice and its legend row link to the same
// place when `hrefFor` is given, or call `onSelect` instead; with neither the
// chart is read-only. Feed it a summary from GET /api/categories/spend.
//
// New figures animate as one: each slice (matched by category, not by
// position) sweeps to its new angles while the center total and the legend
// figures count along, and legend rows slide to their new rank.

import { useLayoutEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { cn, formatAmount, formatPercent } from "@/lib/utils";
import { UNCATEGORIZED_PARAM, type CategorySpend } from "@/lib/category-spend";
import {
  TRANSITION_EASING,
  TRANSITION_MS,
  lerp,
  prefersReducedMotion,
  useTween,
} from "@/components/charts/use-tween";
import { SIZE, arcPath, sliceAngles } from "@/components/charts/donut-geometry";

/** Stable key of a category, UNCATEGORIZED_PARAM for "Sin categoría". */
export function categoryKey(categoryId: string | null): string {
  return categoryId ?? UNCATEGORIZED_PARAM;
}

/** A category's slice; angles in degrees, counterclockwise from 3 o'clock. */
interface Slice {
  key: string;
  category: CategorySpend;
  amount: number;
  percent: number;
  start: number;
  end: number;
}

interface Donut {
  total: number;
  slices: Slice[];
}

function layout(categories: CategorySpend[], total: number): Donut {
  const angles = sliceAngles(categories.map((c) => c.amount));
  const slices = categories.map((c, i) => ({
    key: categoryKey(c.categoryId),
    category: c,
    amount: c.amount,
    percent: c.percent,
    ...angles[i],
  }));
  return { total, slices };
}

/** A slice collapsed to nothing at its middle: where an entering slice grows
 *  from and a leaving one shrinks to. */
function collapsed(slice: Slice): Slice {
  const middle = (slice.start + slice.end) / 2;
  return { ...slice, amount: 0, percent: 0, start: middle, end: middle };
}

function interpolateDonut(from: Donut, to: Donut, t: number): Donut {
  const before = new Map(from.slices.map((s) => [s.key, s]));
  const after = new Set(to.slices.map((s) => s.key));
  const mix = (a: Slice, b: Slice): Slice => ({
    ...b,
    amount: lerp(a.amount, b.amount, t),
    percent: lerp(a.percent, b.percent, t),
    start: lerp(a.start, b.start, t),
    end: lerp(a.end, b.end, t),
  });
  const slices = to.slices.map((s) => mix(before.get(s.key) ?? collapsed(s), s));
  if (t < 1) {
    for (const s of from.slices) {
      if (!after.has(s.key)) slices.push(mix(s, collapsed(s)));
    }
  }
  return { total: lerp(from.total, to.total, t), slices };
}

/** Slides legend rows from their previous rank to the new one (FLIP) and
 *  fades in rows that just appeared. */
function useRowReorder(order: string) {
  const rows = useRef(new Map<string, HTMLElement>());
  const tops = useRef(new Map<string, number>());

  useLayoutEffect(() => {
    const animate = tops.current.size > 0 && !prefersReducedMotion();
    const next = new Map<string, number>();
    for (const [key, row] of rows.current) {
      const top = row.offsetTop;
      next.set(key, top);
      if (!animate) continue;
      const previous = tops.current.get(key);
      const timing = { duration: TRANSITION_MS, easing: TRANSITION_EASING };
      if (previous === undefined) {
        row.animate([{ opacity: 0 }, { opacity: 1 }], timing);
      } else if (previous !== top) {
        row.animate(
          [{ transform: `translateY(${previous - top}px)` }, { transform: "none" }],
          timing
        );
      }
    }
    tops.current = next;
  }, [order]);

  return (key: string) => (row: HTMLElement | null) => {
    if (row) rows.current.set(key, row);
    else rows.current.delete(key);
  };
}

export function CategoryDonut({
  categories,
  total,
  currency,
  hrefFor,
  onSelect,
  selectedId,
  emptyMessage = "Sin gastos en este período.",
  className,
}: {
  categories: CategorySpend[];
  total: number;
  currency: string;
  hrefFor?: (categoryId: string | null) => string;
  onSelect?: (categoryId: string | null) => void;
  /** Highlights one category (null = "Sin categoría"); undefined = none. */
  selectedId?: string | null;
  emptyMessage?: string;
  className?: string;
}) {
  const router = useRouter();
  const [hover, setHover] = useState<{ key: string; x: number; y: number } | null>(null);
  const signature = `${total}|${categories
    .map((c) => `${categoryKey(c.categoryId)}=${c.amount}`)
    .join(",")}`;
  const donut = useTween(layout(categories, total), signature, interpolateDonut);
  const rowRef = useRowReorder(categories.map((c) => categoryKey(c.categoryId)).join(","));

  if (categories.length === 0) {
    return <p className="text-sm text-muted-foreground">{emptyMessage}</p>;
  }

  const interactive = hrefFor != null || onSelect != null;
  const select = (categoryId: string | null) => {
    if (onSelect) onSelect(categoryId);
    else if (hrefFor) router.push(hrefFor(categoryId));
  };
  // A selection with no slice (e.g. a category whose refunds outweigh its
  // spend) dims nothing rather than the whole donut.
  const hasSelection =
    selectedId !== undefined &&
    categories.some((c) => c.categoryId === selectedId);
  const dimmed = (categoryId: string | null) =>
    hasSelection && selectedId !== categoryId;
  const shown = new Map(donut.slices.map((s) => [s.key, s]));
  const hovered = hover ? shown.get(hover.key) : undefined;

  return (
    <div
      className={cn(
        "flex flex-col items-center gap-6 sm:flex-row sm:items-start",
        className
      )}
    >
      <div className="relative h-52 w-52 shrink-0">
        <svg
          viewBox={`0 0 ${SIZE} ${SIZE}`}
          className="size-full"
          aria-hidden
          onMouseLeave={() => setHover(null)}
        >
          {donut.slices.map(
            (s) =>
              s.end - s.start > 0.01 && (
                <path
                  key={s.key}
                  d={arcPath(s.start, s.end)}
                  fill={s.category.color}
                  fillOpacity={dimmed(s.category.categoryId) ? 0.3 : 1}
                  className={cn(
                    "transition-[fill-opacity] duration-200",
                    interactive && "cursor-pointer"
                  )}
                  onClick={interactive ? () => select(s.category.categoryId) : undefined}
                  onMouseLeave={() => setHover(null)}
                  onMouseMove={(event) => {
                    const box = event.currentTarget.ownerSVGElement!.getBoundingClientRect();
                    setHover({
                      key: s.key,
                      x: event.clientX - box.left,
                      y: event.clientY - box.top,
                    });
                  }}
                />
              )
          )}
        </svg>
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center">
          <span className="text-lg font-semibold tabular-nums">
            {formatAmount(currency, Math.round(donut.total))}
          </span>
          <span className="text-xs text-muted-foreground">gastado</span>
        </div>
        {hover && hovered && (
          <div
            className="pointer-events-none absolute z-10 flex items-center gap-2 whitespace-nowrap rounded-md border bg-background px-2.5 py-1.5 text-xs shadow-sm"
            style={{ left: hover.x + 12, top: hover.y + 12 }}
          >
            <span
              aria-hidden
              className="size-2.5 shrink-0 rounded-sm"
              style={{ backgroundColor: hovered.category.color }}
            />
            {hovered.category.name}
            <span className="font-medium tabular-nums">
              {formatAmount(currency, Math.round(hovered.amount))}
            </span>
          </div>
        )}
      </div>

      <ol className="relative w-full min-w-0 flex-1 space-y-0.5 text-sm">
        {categories.map((c) => {
          const key = categoryKey(c.categoryId);
          const figures = shown.get(key) ?? { amount: c.amount, percent: c.percent };
          const content = (
            <>
              <span
                aria-hidden
                className="size-2.5 shrink-0 rounded-sm"
                style={{ backgroundColor: c.color }}
              />
              <span className="min-w-0 flex-1 truncate">{c.name}</span>
              <span className="w-14 text-right tabular-nums text-muted-foreground">
                {formatPercent(figures.percent)}
              </span>
              <span className="w-28 text-right font-medium tabular-nums">
                {formatAmount(currency, Math.round(figures.amount))}
              </span>
            </>
          );
          const rowClass = cn(
            "flex items-center gap-2 rounded-md px-2 py-1 transition-[opacity,background-color] duration-200",
            interactive && "hover:bg-muted",
            dimmed(c.categoryId) && "opacity-50",
            selectedId === c.categoryId && "bg-muted"
          );
          return (
            <li key={key} ref={rowRef(key)}>
              {hrefFor && !onSelect ? (
                <Link href={hrefFor(c.categoryId)} className={rowClass}>
                  {content}
                </Link>
              ) : onSelect ? (
                <button
                  type="button"
                  onClick={() => onSelect(c.categoryId)}
                  className={cn(rowClass, "w-full text-left")}
                >
                  {content}
                </button>
              ) : (
                <div className={rowClass}>{content}</div>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
