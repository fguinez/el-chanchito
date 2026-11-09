"use client";

// A searchable category picker (Gmail-label style): the trigger shows the
// chosen category as "Viajes / Japón", and the list shows each top-level
// category with its subcategories indented under it. The search matches
// either segment (lib/category-tree); the list logic is lib/category-picker.

import { useId, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { Check, ChevronsUpDown } from "lucide-react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { Input } from "@/components/ui/input";
import { SELECT_CLASS } from "@/components/ui/native-select";
import {
  categoryPath,
  effectiveColor,
  type TreeCategory,
} from "@/lib/category-tree";
import {
  CLOSED_PICKER_STATE,
  nextActiveIndex,
  openPickerState,
  pickerOptions,
  type PickerState,
} from "@/lib/category-picker";
import { cn } from "@/lib/utils";
import { CategorySwatch } from "./shared";

export function CategoryPicker({
  categories,
  value,
  onChange,
  emptyLabel,
  placeholder = "Elige una categoría",
  disabled,
  id,
  "aria-label": ariaLabel,
  className,
}: {
  categories: readonly TreeCategory[];
  /** The chosen category id, "" for none. */
  value: string;
  onChange: (id: string) => void;
  /** Offer an empty choice with this label (e.g. "Sin categoría"). */
  emptyLabel?: string;
  /** What the trigger shows with nothing chosen and no empty choice. */
  placeholder?: string;
  disabled?: boolean;
  id?: string;
  "aria-label"?: string;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const [{ query, active }, setState] = useState<PickerState>(CLOSED_PICKER_STATE);
  const listRef = useRef<HTMLUListElement>(null);
  const listId = useId();

  const byId = useMemo(
    () => new Map(categories.map((c) => [c.id, c])),
    [categories]
  );
  const options = useMemo(
    () => pickerOptions(categories, query, emptyLabel),
    [categories, emptyLabel, query]
  );

  const selected = value ? byId.get(value) : undefined;
  const triggerLabel = selected
    ? categoryPath(selected, byId)
    : (emptyLabel ?? placeholder);

  function onOpenChange(next: boolean) {
    setOpen(next);
    // Opening highlights the current choice in the full list; closing drops
    // the search, so a stale filtered list never decides what Enter picks.
    setState(
      next ? openPickerState(categories, value, emptyLabel) : CLOSED_PICKER_STATE
    );
  }

  function choose(optionId: string) {
    // Re-picking the current value is a no-op, like a native select.
    if (optionId !== value) onChange(optionId);
    onOpenChange(false);
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    const index = nextActiveIndex(e.key, active, options.length);
    if (index !== null) {
      e.preventDefault();
      setState({ query, active: index });
      listRef.current
        ?.querySelector<HTMLElement>(`[data-index="${index}"]`)
        ?.scrollIntoView({ block: "nearest" });
    } else if (e.key === "Enter") {
      e.preventDefault();
      const option = options[active];
      if (option) choose(option.id);
    }
  }

  const optionId = (index: number) => `${listId}-${index}`;

  return (
    // modal: the list is portaled outside any open Sheet/Dialog, whose scroll
    // lock would otherwise swallow wheel/trackpad events over it.
    <Popover modal open={open} onOpenChange={onOpenChange}>
      <PopoverTrigger asChild>
        <button
          type="button"
          id={id}
          aria-haspopup="listbox"
          aria-label={ariaLabel}
          disabled={disabled}
          className={cn(
            SELECT_CLASS,
            "flex items-center gap-2 text-left",
            className
          )}
        >
          {selected && (
            <CategorySwatch color={effectiveColor(selected, byId)} />
          )}
          <span
            className={cn(
              "min-w-0 flex-1 truncate",
              !selected && "text-muted-foreground"
            )}
          >
            {triggerLabel}
          </span>
          <ChevronsUpDown className="size-4 shrink-0 opacity-50" />
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="start"
        className="w-(--radix-popover-trigger-width) min-w-64 p-1"
      >
        <Input
          role="combobox"
          aria-expanded={open}
          aria-controls={listId}
          aria-autocomplete="list"
          aria-activedescendant={options[active] ? optionId(active) : undefined}
          value={query}
          onChange={(e) => setState({ query: e.target.value, active: 0 })}
          onKeyDown={onKeyDown}
          placeholder="Buscar categoría"
          aria-label="Buscar categoría"
          className="mb-1 h-8"
          autoFocus
        />
        <ul
          ref={listRef}
          id={listId}
          role="listbox"
          aria-label={ariaLabel ?? "Categorías"}
          className="max-h-64 overflow-y-auto"
        >
          {options.length === 0 && (
            <li className="px-2 py-1.5 text-sm text-muted-foreground">
              Sin resultados
            </li>
          )}
          {options.map((option, index) => (
            <li
              key={option.id || "__empty"}
              id={optionId(index)}
              data-index={index}
              role="option"
              aria-selected={option.id === value}
              onMouseEnter={() => setState({ query, active: index })}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => choose(option.id)}
              className={cn(
                "flex cursor-pointer items-center gap-2 rounded-sm px-2 py-1.5 text-sm",
                index === active && "bg-accent text-accent-foreground",
                option.depth === 1 && "pl-7",
                option.id === "" && "text-muted-foreground"
              )}
            >
              {option.id !== "" && (
                <CategorySwatch
                  color={option.color}
                  className={option.depth === 1 ? "size-2" : undefined}
                />
              )}
              <span className="min-w-0 flex-1 truncate">{option.label}</span>
              {option.id === value && <Check className="size-4 shrink-0" />}
            </li>
          ))}
        </ul>
      </PopoverContent>
    </Popover>
  );
}
