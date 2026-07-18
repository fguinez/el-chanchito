"use client";

import { ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { TableHead } from "@/components/ui/table";

type SortDirection = "asc" | "desc";

interface SortableTableHeadProps {
  label: string;
  columnKey: string;
  active: boolean;
  direction: SortDirection | undefined;
  onSort: (key: string) => void;
  align?: "left" | "right";
  className?: string;
}

/** A `TableHead` whose label is a ghost button that toggles column sorting.
 *  Presentational: it holds no sort state, it just reflects `active`/`direction`
 *  and calls `onSort(columnKey)` on click. */
export function SortableTableHead({
  label,
  columnKey,
  active,
  direction,
  onSort,
  align = "left",
  className,
}: SortableTableHeadProps) {
  const ariaSort = active
    ? direction === "asc"
      ? "ascending"
      : "descending"
    : "none";

  const Icon =
    active && direction === "asc"
      ? ArrowUp
      : active && direction === "desc"
        ? ArrowDown
        : ChevronsUpDown;

  return (
    <TableHead
      aria-sort={ariaSort}
      className={cn(align === "right" && "text-right", className)}
    >
      <Button
        variant="ghost"
        size="xs"
        onClick={() => onSort(columnKey)}
        className={cn(
          "-mx-2 h-8 w-full font-medium",
          align === "right" ? "justify-end" : "justify-start"
        )}
      >
        {label}
        <Icon
          className={cn(
            "ml-1 size-3.5",
            !active && "text-muted-foreground/50"
          )}
        />
      </Button>
    </TableHead>
  );
}
