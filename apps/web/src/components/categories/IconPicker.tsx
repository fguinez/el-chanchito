"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import { CATEGORY_ICON_NAMES, type CategoryIconName } from "@/lib/categories";
import { CATEGORY_ICONS, CategoryIcon } from "./CategoryIcon";

/** Icon button that opens a grid of the curated category icons. */
export function IconPicker({
  value,
  onChange,
  disabled,
}: {
  value: CategoryIconName | null;
  onChange: (value: CategoryIconName | null) => void;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const label = value ? CATEGORY_ICONS[value].label : "Sin ícono";

  function pick(name: CategoryIconName | null) {
    onChange(name);
    setOpen(false);
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          size="icon"
          disabled={disabled}
          aria-label={`Ícono: ${label}`}
          title={`Ícono: ${label}`}
        >
          <CategoryIcon icon={value} />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-auto p-2">
        <div className="grid grid-cols-6 gap-1">
          {CATEGORY_ICON_NAMES.map((name) => (
            <Button
              key={name}
              type="button"
              variant={value === name ? "secondary" : "ghost"}
              size="icon-sm"
              aria-label={CATEGORY_ICONS[name].label}
              aria-pressed={value === name}
              title={CATEGORY_ICONS[name].label}
              onClick={() => pick(name)}
            >
              <CategoryIcon icon={name} />
            </Button>
          ))}
        </div>
        <Button
          type="button"
          variant={value === null ? "secondary" : "ghost"}
          size="xs"
          className="mt-1 w-full"
          onClick={() => pick(null)}
        >
          Sin ícono
        </Button>
      </PopoverContent>
    </Popover>
  );
}
