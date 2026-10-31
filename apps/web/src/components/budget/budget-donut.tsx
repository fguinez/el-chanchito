"use client";

// The monthly income as a donut: one slice per budget item kind plus "Sobra"
// (what is left), and a breakdown (a separate component, so the page can
// place it beside or below the donut) listing each kind's total and items
// with their share of the income. The add form's draft item gets its own hatched
// slice next to its kind, so the split moves while the user types.
//
// Slices are proportional to the income while it covers the allocations.
// With a deficit, or no income yet, there is no Sobra slice and the donut
// splits the allocations themselves.

import { useId } from "react";
import { cn, formatCLP, formatPercent } from "@/lib/utils";
import { FIXED_EXPENSE_KINDS, type FixedExpenseKind } from "@/lib/db/schema";
import { FIXED_EXPENSE_KIND_LABELS } from "@/lib/fixed-expenses";
import type { BudgetSummary } from "@/lib/budget";
import { lerp, useTween } from "@/components/charts/use-tween";
import { SIZE, arcPath, sliceAngles } from "@/components/charts/donut-geometry";

/** Each kind's theme color (--color-budget-* in globals.css) as Tailwind
 *  classes: a swatch background, an SVG fill, and the text color the hatched
 *  swatch draws its stripes with (currentColor). Literal strings, so Tailwind
 *  generates them. */
export const KIND_SWATCH_CLASSES: Record<FixedExpenseKind, string> = {
  fixed: "bg-budget-fixed",
  estimated: "bg-budget-estimated",
  savings: "bg-budget-savings",
};
const KIND_FILL_CLASSES: Record<FixedExpenseKind, string> = {
  fixed: "fill-budget-fixed",
  estimated: "fill-budget-estimated",
  savings: "fill-budget-savings",
};
const KIND_TEXT_CLASSES: Record<FixedExpenseKind, string> = {
  fixed: "text-budget-fixed",
  estimated: "text-budget-estimated",
  savings: "text-budget-savings",
};

const DRAFT_KEY = "draft";
const REMAINING_KEY = "remaining";

/** Plural headings of the breakdown. */
const KIND_HEADINGS: Record<FixedExpenseKind, string> = {
  fixed: "Gastos fijos",
  estimated: "Consumo estimado",
  savings: "Ahorro",
};

interface Slice {
  key: string;
  label: string;
  value: number;
  /** null for Sobra. */
  kind: FixedExpenseKind | null;
  draft: boolean;
  start: number;
  end: number;
}

function layout(summary: BudgetSummary, draftName: string | null): Slice[] {
  const parts: Omit<Slice, "start" | "end">[] = [];
  for (const k of summary.kinds) {
    const saved = k.total - k.draftAmount;
    if (saved > 0) {
      parts.push({
        key: k.kind,
        label: FIXED_EXPENSE_KIND_LABELS[k.kind],
        value: saved,
        kind: k.kind,
        draft: false,
      });
    }
    if (k.draftAmount > 0) {
      parts.push({
        key: DRAFT_KEY,
        label: draftName ?? "Nuevo item",
        value: k.draftAmount,
        kind: k.kind,
        draft: true,
      });
    }
  }
  if (summary.remaining > 0) {
    parts.push({
      key: REMAINING_KEY,
      label: "Sobra",
      value: summary.remaining,
      kind: null,
      draft: false,
    });
  }
  const angles = sliceAngles(parts.map((p) => p.value));
  return parts.map((p, i) => ({ ...p, ...angles[i] }));
}

/** A slice collapsed to nothing at its middle: where an entering slice grows
 *  from and a leaving one shrinks to. */
function collapsed(slice: Slice): Slice {
  const middle = (slice.start + slice.end) / 2;
  return { ...slice, value: 0, start: middle, end: middle };
}

function interpolate(from: Slice[], to: Slice[], t: number): Slice[] {
  const before = new Map(from.map((s) => [s.key, s]));
  const after = new Set(to.map((s) => s.key));
  const mix = (a: Slice, b: Slice): Slice => ({
    ...b,
    value: lerp(a.value, b.value, t),
    start: lerp(a.start, b.start, t),
    end: lerp(a.end, b.end, t),
  });
  const slices = to.map((s) => mix(before.get(s.key) ?? collapsed(s), s));
  if (t < 1) {
    for (const s of from) {
      if (!after.has(s.key)) slices.push(mix(s, collapsed(s)));
    }
  }
  return slices;
}

/** A legend swatch; a draft one is hatched in its kind's color. */
function Swatch({ className, draft = false }: { className: string; draft?: boolean }) {
  return (
    <span
      aria-hidden
      className={cn("size-2.5 shrink-0 rounded-sm", className, draft && "ring-1 ring-inset")}
      style={
        draft
          ? {
              backgroundImage:
                "repeating-linear-gradient(135deg, currentColor 0 2px, transparent 2px 4px)",
            }
          : undefined
      }
    />
  );
}

function share(value: number | null): string {
  return value == null ? "" : formatPercent(value);
}

/** The donut and its center label. */
export function BudgetDonut({
  summary,
  draftName,
}: {
  summary: BudgetSummary;
  /** Name shown for the draft's slice (the add form's name, if any). */
  draftName?: string | null;
}) {
  const patternPrefix = useId().replace(/:/g, "");
  const target = layout(summary, draftName ?? null);
  const signature = target
    .map((s) => `${s.key}:${s.kind}=${s.value}`)
    .join(",");
  const slices = useTween(target, signature, interpolate);

  const { income, allocated, remaining, deficit } = summary;
  const hasIncome = income > 0;
  const inDeficit = hasIncome && deficit > 0;
  // The hole is 128 px across: three short lines, the widest an amount.
  const center = !hasIncome
    ? { label: "Asignado", amount: allocated, note: "sin ingreso", alert: false }
    : inDeficit
      ? {
          label: "Déficit",
          amount: deficit,
          note: `+${share(summary.deficitShare)}`,
          alert: true,
        }
      : {
          label: "Sobra",
          amount: remaining,
          note: share(summary.remainingShare),
          alert: false,
        };

  return (
    <div className="relative mx-auto h-52 w-52 shrink-0">
      <svg viewBox={`0 0 ${SIZE} ${SIZE}`} className="size-full" aria-hidden>
        <defs>
          {FIXED_EXPENSE_KINDS.map((kind) => (
            <pattern
              key={kind}
              id={`${patternPrefix}-${kind}`}
              patternUnits="userSpaceOnUse"
              width="6"
              height="6"
              patternTransform="rotate(45)"
            >
              <rect
                width="6"
                height="6"
                className={KIND_FILL_CLASSES[kind]}
                fillOpacity={0.3}
              />
              <rect width="3" height="6" className={KIND_FILL_CLASSES[kind]} />
            </pattern>
          ))}
        </defs>
        {slices.length === 0 && (
          // Nothing to split yet: an empty ring.
          <path d={arcPath(0, 360)} className="fill-budget-remaining" />
        )}
        {slices.map(
          (s) =>
            s.end - s.start > 0.01 && (
              <path
                key={s.key}
                d={arcPath(s.start, s.end)}
                className={
                  s.draft
                    ? undefined
                    : s.kind
                      ? KIND_FILL_CLASSES[s.kind]
                      : "fill-budget-remaining"
                }
                fill={s.draft ? `url(#${patternPrefix}-${s.kind})` : undefined}
              >
                <title>{`${s.label}: ${formatCLP(Math.round(s.value))}`}</title>
              </path>
            )
        )}
      </svg>
      <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
        <div className="flex max-w-28 flex-col items-center text-center leading-tight">
          <span
            className={cn(
              "text-xs",
              center.alert ? `font-medium ${ALERT_TEXT}` : "text-muted-foreground"
            )}
          >
            {center.label}
          </span>
          <span
            className={cn(
              "max-w-full truncate text-base font-semibold tabular-nums",
              center.alert && ALERT_TEXT
            )}
          >
            {formatCLP(center.amount)}
          </span>
          <span className="text-xs tabular-nums text-muted-foreground">
            {center.note}
          </span>
        </div>
      </div>
    </div>
  );
}

const ALERT_TEXT = "text-destructive dark:text-destructive-foreground";

/** One legend row: swatch, name (ellipsis, full text on hover), share,
 *  amount. Name gets whatever the figures leave and never collapses away. */
function Row({
  swatch,
  name,
  percent,
  amount,
  className,
  nameClassName,
}: {
  swatch: React.ReactNode;
  name: string;
  percent: string;
  amount: number;
  className?: string;
  nameClassName?: string;
}) {
  return (
    <div className={cn("flex items-center gap-2 px-2", className)}>
      {swatch}
      <span title={name} className={cn("min-w-12 flex-1 truncate", nameClassName)}>
        {name}
      </span>
      <span className="shrink-0 text-right tabular-nums text-muted-foreground">
        {percent}
      </span>
      <span className="w-24 shrink-0 text-right tabular-nums">{formatCLP(amount)}</span>
    </div>
  );
}

/** Each kind with its total and items, then Sobra or Déficit. */
export function BudgetBreakdown({
  summary,
  className,
}: {
  summary: BudgetSummary;
  className?: string;
}) {
  const hasIncome = summary.income > 0;
  const inDeficit = hasIncome && summary.deficit > 0;

  return (
    <div className={cn("min-w-0 space-y-3 text-sm", className)}>
      {summary.kinds.map((k) => (
        <section key={k.kind}>
          <Row
            className="py-1 font-medium"
            swatch={<Swatch className={KIND_SWATCH_CLASSES[k.kind]} />}
            name={KIND_HEADINGS[k.kind]}
            percent={share(k.share)}
            amount={k.total}
          />
          {k.items.length === 0 ? (
            <p className="px-2 pl-6.5 text-xs text-muted-foreground">
              Sin items vigentes
            </p>
          ) : (
            <ul className="text-xs text-muted-foreground">
              {k.items.map((item) => (
                <li key={item.id}>
                  <Row
                    className={cn("py-0.5", !item.draft && "pl-6.5", item.draft && "italic")}
                    swatch={
                      item.draft ? (
                        <Swatch className={KIND_TEXT_CLASSES[k.kind]} draft />
                      ) : null
                    }
                    name={item.draft ? `${item.name} (sin guardar)` : item.name}
                    percent={share(item.share)}
                    amount={item.amount}
                  />
                </li>
              ))}
            </ul>
          )}
        </section>
      ))}

      <div className="border-t pt-3">
        {!hasIncome ? (
          <p className="px-2 text-xs text-muted-foreground">
            Ingresa tu ingreso mensual para ver cuánto sobra.
          </p>
        ) : inDeficit ? (
          <Row
            className="font-medium"
            swatch={<Swatch className="bg-destructive" />}
            name="Déficit"
            nameClassName={ALERT_TEXT}
            percent={share(summary.deficitShare)}
            amount={summary.deficit}
          />
        ) : (
          <Row
            className="font-medium"
            swatch={<Swatch className="bg-budget-remaining" />}
            name="Sobra"
            percent={share(summary.remainingShare)}
            amount={summary.remaining}
          />
        )}
      </div>
    </div>
  );
}
