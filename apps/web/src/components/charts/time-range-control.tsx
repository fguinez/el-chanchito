"use client";

// Header control for a chart's time window: quick trailing-day pills (plus an
// optional "Todo" pill) and a Datadog/GCP-style picker for month presets and
// exact dates. Day granularity only. Operates on the XAxisRange the on-axis
// drag overlay also writes to, so a dragged window shows up here as its date
// range.

import { useState } from "react";
import { Calendar, ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { formatDateTickMs } from "@/lib/utils";
import { msToDay, type XAxisRange, type XAxisRangeControl } from "./x-axis-range";

export const DAY_PRESETS = [
  { days: 7, label: "7d" },
  { days: 30, label: "30d" },
  { days: 90, label: "90d" },
  { days: 180, label: "180d" },
  { days: 365, label: "1a" },
] as const;

/** YYYY-MM-DD from a Date's local parts. */
function toDay(date: Date): string {
  const y = date.getFullYear();
  const m = String(date.getMonth() + 1).padStart(2, "0");
  const d = String(date.getDate()).padStart(2, "0");
  return `${y}-${m}-${d}`;
}

/** Inclusive day count between two YYYY-MM-DD strings. */
function spanDays(from: string, to: string): number {
  const ms = (s: string) => {
    const [y, m, d] = s.split("-").map(Number);
    return Date.UTC(y, m - 1, d);
  };
  return Math.round((ms(to) - ms(from)) / (24 * 60 * 60 * 1000)) + 1;
}

type AbsoluteRange = Extract<XAxisRange, { kind: "absolute" }>;

function monthToDate(now = new Date()): AbsoluteRange {
  return {
    kind: "absolute",
    from: toDay(new Date(now.getFullYear(), now.getMonth(), 1)),
    to: toDay(now),
    label: "Este mes",
  };
}

function previousMonth(now = new Date()): AbsoluteRange {
  return {
    kind: "absolute",
    from: toDay(new Date(now.getFullYear(), now.getMonth() - 1, 1)),
    to: toDay(new Date(now.getFullYear(), now.getMonth(), 0)),
    label: "Mes pasado",
  };
}

/** Short es-CL day label; the year appears only when it isn't the current. */
function dayLabel(dateStr: string): string {
  const [year, month, day] = dateStr.split("-").map(Number);
  const options: Intl.DateTimeFormatOptions = {
    day: "2-digit",
    month: "short",
  };
  if (year !== new Date().getFullYear()) options.year = "numeric";
  return new Date(year, month - 1, day).toLocaleDateString("es-CL", options);
}

function triggerLabel(range: XAxisRange): string {
  if (range.kind === "absolute") {
    return range.label ?? `${dayLabel(range.from)} a ${dayLabel(range.to)}`;
  }
  if (range.kind === "custom") {
    return `${formatDateTickMs(range.min)} a ${formatDateTickMs(range.max)}`;
  }
  return "Personalizado";
}

export function ChartRangePicker({
  value,
  onChange,
  maxDays,
}: {
  value: XAxisRange;
  onChange: (range: XAxisRange) => void;
  /** Cap on the exact-dates span; set it when an API enforces one. */
  maxDays?: number;
}) {
  const [open, setOpen] = useState(false);
  const [fromDraft, setFromDraft] = useState("");
  const [toDraft, setToDraft] = useState("");

  const today = toDay(new Date());
  const draftsComplete = fromDraft !== "" && toDraft !== "";
  const draftsOrdered = draftsComplete && fromDraft <= toDraft;
  const draftsTooLong =
    maxDays != null && draftsOrdered && spanDays(fromDraft, toDraft) > maxDays;

  const handleOpenChange = (next: boolean) => {
    // Seed the inputs with the active range (or clear leftovers from an
    // abandoned edit) so reopening always reflects the current selection.
    if (next) {
      if (value.kind === "absolute") {
        setFromDraft(value.from);
        setToDraft(value.to);
      } else if (value.kind === "custom") {
        const to = msToDay(value.max);
        setFromDraft(msToDay(value.min));
        setToDraft(to > today ? today : to);
      } else {
        setFromDraft("");
        setToDraft("");
      }
    }
    setOpen(next);
  };

  const select = (range: XAxisRange) => {
    onChange(range);
    setOpen(false);
  };

  const monthPreset = (preset: AbsoluteRange) => (
    <Button
      variant={
        value.kind === "absolute" && value.label === preset.label
          ? "secondary"
          : "ghost"
      }
      size="sm"
      className="w-full justify-start"
      onClick={() => select(preset)}
    >
      {preset.label}
    </Button>
  );

  return (
    <Popover open={open} onOpenChange={handleOpenChange}>
      <PopoverTrigger asChild>
        <Button
          variant={
            value.kind === "absolute" || value.kind === "custom"
              ? "secondary"
              : "ghost"
          }
          size="xs"
        >
          <Calendar className="h-3 w-3" />
          {triggerLabel(value)}
          <ChevronDown className="h-3 w-3 opacity-60" />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 space-y-3">
        <div className="space-y-1">
          {monthPreset(monthToDate())}
          {monthPreset(previousMonth())}
        </div>
        <Separator />
        <div className="space-y-2">
          <p className="text-xs font-medium text-muted-foreground">
            Fechas exactas
          </p>
          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="mb-1 block text-xs text-muted-foreground">
                Desde
              </label>
              <Input
                type="date"
                max={toDraft || today}
                value={fromDraft}
                onChange={(e) => setFromDraft(e.target.value)}
              />
            </div>
            <div>
              <label className="mb-1 block text-xs text-muted-foreground">
                Hasta
              </label>
              <Input
                type="date"
                min={fromDraft || undefined}
                max={today}
                value={toDraft}
                onChange={(e) => setToDraft(e.target.value)}
              />
            </div>
          </div>
          {draftsComplete && !draftsOrdered && (
            <p className="text-xs text-destructive">
              &quot;Desde&quot; debe ser anterior o igual a &quot;Hasta&quot;.
            </p>
          )}
          {draftsTooLong && (
            <p className="text-xs text-destructive">
              El rango no puede superar {maxDays} días.
            </p>
          )}
          <Button
            size="sm"
            className="w-full"
            disabled={!draftsOrdered || draftsTooLong}
            onClick={() =>
              select({ kind: "absolute", from: fromDraft, to: toDraft })
            }
          >
            Aplicar
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}

/** The full header control: day pills, optional "Todo", and the date picker.
 *  Wire it to the same `useXAxisRange` control the chart's axis spreads. */
export function TimeRangeControl({
  control,
  allowAll = false,
  maxDays,
}: {
  control: Pick<XAxisRangeControl, "range" | "setRange">;
  allowAll?: boolean;
  /** Cap on the exact-dates span; set it when an API enforces one. */
  maxDays?: number;
}) {
  const { range, setRange } = control;
  return (
    <div
      className="flex flex-wrap items-center gap-1"
      role="group"
      aria-label="Rango de tiempo del gráfico"
    >
      {allowAll && (
        <Button
          variant={range.kind === "all" ? "secondary" : "ghost"}
          size="xs"
          onClick={() => setRange({ kind: "all" })}
        >
          Todo
        </Button>
      )}
      {DAY_PRESETS.map((option) => (
        <Button
          key={option.days}
          variant={
            range.kind === "days" && range.days === option.days
              ? "secondary"
              : "ghost"
          }
          size="xs"
          onClick={() => setRange({ kind: "days", days: option.days })}
        >
          {option.label}
        </Button>
      ))}
      <ChartRangePicker value={range} onChange={setRange} maxDays={maxDays} />
    </div>
  );
}
