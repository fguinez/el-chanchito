"use client";

// The monthly income as a donut: one slice per budget item kind plus "Sobra"
// (what is left), and a breakdown listing each kind's total and items with
// their share of the income. The add form's draft item gets its own hatched
// slice next to its kind, so the split moves while the user types.
//
// Slices are proportional to the income while it covers the allocations.
// With a deficit, or no income yet, there is no Sobra slice and the donut
// splits the allocations themselves.

import { useId } from "react";
import { cn, formatCLP, formatPercent } from "@/lib/utils";
import type { FixedExpenseKind } from "@/lib/db/schema";
import { FIXED_EXPENSE_KIND_LABELS } from "@/lib/fixed-expenses";
import type { BudgetSummary } from "@/lib/budget";
import { lerp, useTween } from "@/components/charts/use-tween";
import { SIZE, arcPath, sliceAngles } from "@/components/charts/donut-geometry";

/** Each kind's color token (globals.css), usable in style props. */
export const KIND_COLORS: Record<FixedExpenseKind, string> = {
  fixed: "var(--budget-fixed)",
  estimated: "var(--budget-estimated)",
  savings: "var(--budget-savings)",
};

const REMAINING_COLOR = "var(--budget-remaining)";
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

/** Diagonal stripes in `color`, the draft's look in the legend. */
function hatch(color: string): string {
  return `repeating-linear-gradient(135deg, ${color} 0 2px, transparent 2px 4px)`;
}

function Swatch({ color, draft = false }: { color: string; draft?: boolean }) {
  return (
    <span
      aria-hidden
      className={cn("size-2.5 shrink-0 rounded-sm", draft && "ring-1 ring-inset")}
      style={
        draft
          ? { backgroundImage: hatch(color), color }
          : { backgroundColor: color }
      }
    />
  );
}

function share(value: number | null): string {
  return value == null ? "" : formatPercent(value);
}

export function BudgetDonut({
  summary,
  draftName,
  className,
}: {
  summary: BudgetSummary;
  /** Name shown for the draft's slice (the add form's name, if any). */
  draftName?: string | null;
  className?: string;
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

  return (
    <div
      className={cn(
        "flex flex-col items-center gap-6 sm:flex-row sm:items-start",
        className
      )}
    >
      <div className="relative h-52 w-52 shrink-0">
        <svg viewBox={`0 0 ${SIZE} ${SIZE}`} className="size-full" aria-hidden>
          <defs>
            {(Object.keys(KIND_COLORS) as FixedExpenseKind[]).map((kind) => (
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
                  style={{ fill: KIND_COLORS[kind], fillOpacity: 0.3 }}
                />
                <rect width="3" height="6" style={{ fill: KIND_COLORS[kind] }} />
              </pattern>
            ))}
          </defs>
          {slices.length === 0 && (
            // Nothing to split yet: an empty ring.
            <path d={arcPath(0, 360)} style={{ fill: REMAINING_COLOR }} />
          )}
          {slices.map(
            (s) =>
              s.end - s.start > 0.01 && (
                <path
                  key={s.key}
                  d={arcPath(s.start, s.end)}
                  style={{
                    fill: s.draft
                      ? `url(#${patternPrefix}-${s.kind})`
                      : s.kind
                        ? KIND_COLORS[s.kind]
                        : REMAINING_COLOR,
                  }}
                >
                  <title>{`${s.label}: ${formatCLP(Math.round(s.value))}`}</title>
                </path>
              )
          )}
        </svg>
        <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center px-10 text-center">
          {!hasIncome ? (
            <>
              <span className="text-xs text-muted-foreground">Asignado</span>
              <span className="text-lg font-semibold tabular-nums">
                {formatCLP(allocated)}
              </span>
              <span className="text-xs text-muted-foreground">sin ingreso</span>
            </>
          ) : inDeficit ? (
            <>
              <span className="text-xs font-medium text-destructive dark:text-destructive-foreground">
                Déficit
              </span>
              <span className="text-lg font-semibold tabular-nums text-destructive dark:text-destructive-foreground">
                {formatCLP(deficit)}
              </span>
              <span className="text-xs text-muted-foreground">
                {share(summary.deficitShare)} sobre el ingreso
              </span>
            </>
          ) : (
            <>
              <span className="text-xs text-muted-foreground">Sobra</span>
              <span className="text-lg font-semibold tabular-nums">
                {formatCLP(remaining)}
              </span>
              <span className="text-xs text-muted-foreground">
                {share(summary.remainingShare)} del ingreso
              </span>
            </>
          )}
        </div>
      </div>

      <div className="w-full min-w-0 flex-1 space-y-3 text-sm">
        {summary.kinds.map((k) => (
          <section key={k.kind}>
            <div className="flex items-center gap-2 px-2 py-1">
              <Swatch color={KIND_COLORS[k.kind]} />
              <span className="min-w-0 flex-1 truncate font-medium">
                {KIND_HEADINGS[k.kind]}
              </span>
              <span className="w-16 text-right tabular-nums text-muted-foreground">
                {share(k.share)}
              </span>
              <span className="w-28 text-right font-medium tabular-nums">
                {formatCLP(k.total)}
              </span>
            </div>
            {k.items.length === 0 ? (
              <p className="px-2 pl-6.5 text-xs text-muted-foreground">
                Sin items vigentes
              </p>
            ) : (
              <ul className="text-xs">
                {k.items.map((item) => (
                  <li
                    key={item.id}
                    className={cn(
                      "flex items-center gap-2 px-2 py-0.5 pl-6.5 text-muted-foreground",
                      item.draft && "italic"
                    )}
                  >
                    {item.draft && <Swatch color={KIND_COLORS[k.kind]} draft />}
                    <span className="min-w-0 flex-1 truncate">
                      {item.name}
                      {item.draft && " (sin guardar)"}
                    </span>
                    <span className="w-16 text-right tabular-nums">
                      {share(item.share)}
                    </span>
                    <span className="w-28 text-right tabular-nums">
                      {formatCLP(item.amount)}
                    </span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        ))}

        <div className="flex items-center gap-2 border-t px-2 pt-3">
          {!hasIncome ? (
            <p className="text-xs text-muted-foreground">
              Ingresa tu ingreso mensual para ver cuánto sobra.
            </p>
          ) : inDeficit ? (
            <>
              <span
                aria-hidden
                className="size-2.5 shrink-0 rounded-sm bg-destructive"
              />
              <span className="min-w-0 flex-1 truncate font-medium text-destructive dark:text-destructive-foreground">
                Déficit
              </span>
              <span className="w-16 text-right tabular-nums text-muted-foreground">
                {share(summary.deficitShare)}
              </span>
              <span className="w-28 text-right font-medium tabular-nums text-destructive dark:text-destructive-foreground">
                {formatCLP(deficit)}
              </span>
            </>
          ) : (
            <>
              <Swatch color={REMAINING_COLOR} />
              <span className="min-w-0 flex-1 truncate font-medium">Sobra</span>
              <span className="w-16 text-right tabular-nums text-muted-foreground">
                {share(summary.remainingShare)}
              </span>
              <span className="w-28 text-right font-medium tabular-nums">
                {formatCLP(remaining)}
              </span>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
