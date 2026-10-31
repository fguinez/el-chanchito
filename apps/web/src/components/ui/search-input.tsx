"use client"

import * as React from "react"
import { Loader2, Search, X } from "lucide-react"

import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"
import { SEARCH_QUERY_MAX_LENGTH } from "@/lib/transaction-search"

type SearchInputProps = Omit<
  React.ComponentProps<"input">,
  "type" | "value" | "onChange" | "ref"
> & {
  value: string
  onValueChange: (value: string) => void
  /** A search is in flight: the magnifier turns into a spinner. */
  loading?: boolean
  /** Classes for the field itself; `className` sizes the wrapper. */
  inputClassName?: string
}

/**
 * A search field: magnifier icon, a clear button while it holds text, and an
 * Enter that never submits the form around it. Named by `aria-label`, else by
 * its placeholder.
 */
function SearchInput({
  value,
  onValueChange,
  loading = false,
  placeholder = "Buscar por descripción",
  className,
  inputClassName,
  onKeyDown,
  ...props
}: SearchInputProps) {
  const inputRef = React.useRef<HTMLInputElement>(null)
  const Icon = loading ? Loader2 : Search

  return (
    <div className={cn("relative", className)}>
      <Icon
        aria-hidden
        className={cn(
          "pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground",
          loading && "animate-spin"
        )}
      />
      <Input
        ref={inputRef}
        type="search"
        aria-label={placeholder}
        placeholder={placeholder}
        maxLength={SEARCH_QUERY_MAX_LENGTH}
        autoComplete="off"
        {...props}
        value={value}
        onChange={(e) => onValueChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") e.preventDefault()
          onKeyDown?.(e)
        }}
        className={cn(
          // The clear button replaces the browser's own.
          "pr-8 pl-8 [&::-webkit-search-cancel-button]:hidden",
          inputClassName
        )}
      />
      {value && (
        <button
          type="button"
          onClick={() => {
            onValueChange("")
            inputRef.current?.focus()
          }}
          // A 24px tap target around the small icon.
          className="absolute top-1/2 right-1 inline-flex size-6 -translate-y-1/2 items-center justify-center rounded-sm text-muted-foreground outline-none hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50"
          aria-label="Limpiar búsqueda"
        >
          <X className="size-3.5" />
        </button>
      )}
    </div>
  )
}

export { SearchInput }
