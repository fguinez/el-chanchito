"use client";

// Chart time-range selection for the monitor detail view: quick trailing-day
// pills plus a Datadog/GCP-style picker for month presets and exact dates.
// Day granularity only; ranges are inclusive YYYY-MM-DD pairs.

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

/** A trailing day-count window or an exact (inclusive) day range. Month
 *  presets are absolute ranges that keep their name for the trigger label. */
export type ChartRange =
  | { kind: "days"; days: number }
  | { kind: "absolute"; from: string; to: string; label?: string };

export const DAY_PRESETS = [
  { days: 7, label: "7d" },
  { days: 30, label: "30d" },
  { days: 90, label: "90d" },
  { days: 180, label: "180d" },
  { days: 365, label: "1a" },
] as const;

export const DEFAULT_CHART_RANGE: ChartRange = { kind: "days", days: 30 };

const MAX_RANGE_DAYS = 365;

/** Query string for the /api/monitors/[id] history params. */
export function rangeQuery(range: ChartRange): string {
  return range.kind === "days"
    ? `days=${range.days}`
    : `from=${range.from}&to=${range.to}`;
}

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

type AbsoluteRange = Extract<ChartRange, { kind: "absolute" }>;

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

function triggerLabel(range: ChartRange): string {
  if (range.kind === "days") return "Personalizado";
  return range.label ?? `${dayLabel(range.from)} a ${dayLabel(range.to)}`;
}

export function ChartRangePicker({
  value,
  onChange,
}: {
  value: ChartRange;
  onChange: (range: ChartRange) => void;
}) {
  const [open, setOpen] = useState(false);
  const [fromDraft, setFromDraft] = useState("");
  const [toDraft, setToDraft] = useState("");

  const today = toDay(new Date());
  const draftsComplete = fromDraft !== "" && toDraft !== "";
  const draftsOrdered = draftsComplete && fromDraft <= toDraft;
  const draftsTooLong =
    draftsOrdered && spanDays(fromDraft, toDraft) > MAX_RANGE_DAYS;

  const handleOpenChange = (next: boolean) => {
    // Seed the inputs with the active range (or clear leftovers from an
    // abandoned edit) so reopening always reflects the current selection.
    if (next) {
      setFromDraft(value.kind === "absolute" ? value.from : "");
      setToDraft(value.kind === "absolute" ? value.to : "");
    }
    setOpen(next);
  };

  const select = (range: ChartRange) => {
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
          variant={value.kind === "absolute" ? "secondary" : "ghost"}
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
              El rango no puede superar 1 año.
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
