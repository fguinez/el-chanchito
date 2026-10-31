"use client";

import { useEffect, useState } from "react";
import { X } from "lucide-react";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { SearchInput } from "@/components/ui/search-input";
import { cn, formatAmount, formatPlainDateEs } from "@/lib/utils";
import { descriptionTitle } from "@/lib/transaction-search";
import { usePreviewSearch } from "@/lib/use-preview-search";
import type { MatchType } from "@/lib/db/schema";
import {
  MATCH_TYPE_LABELS,
  plural,
  readApiError,
  type AliasPreviewResult,
} from "./shared";

/** `q` is the search the result answers ("" for none). */
type PreviewState =
  | { status: "loading" }
  | { status: "error"; error: string }
  | { status: "done"; result: AliasPreviewResult; q: string };

function summary({ total }: AliasPreviewResult, q: string): string {
  if (q) return `${plural(total, "coincidencia", "coincidencias")} con la búsqueda.`;
  // A higher-priority alias can still win at display time, so this says the rows
  // match this alias, not that they will be relabeled.
  return `${plural(
    total,
    "transacción coincide",
    "transacciones coinciden"
  )} con este alias.`;
}

/**
 * What an alias with `matchDescription`/`matchType` and the optional amount
 * condition would match: the count plus the most recent matches with their
 * original (scraped) description (what they show today in the tooltip). A
 * search box narrows the matches, server-side once typing pauses. Refetches
 * when `version` (bumped on every data reload) or any input changes; callers
 * key it by the match so switching the description remounts it.
 */
export function AliasPreview({
  matchDescription,
  matchType,
  amountMin,
  amountMax,
  currency,
  version,
  onClose,
}: {
  matchDescription: string;
  matchType: MatchType;
  amountMin: number | null;
  amountMax: number | null;
  currency: string | null;
  version: number;
  onClose: () => void;
}) {
  const [state, setState] = useState<PreviewState>({ status: "loading" });
  const { search, setSearch, q, stale, showSearch } = usePreviewSearch(
    state.status === "done" ? { q: state.q, total: state.result.total } : null
  );

  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams({ matchDescription, matchType });
    if (amountMin !== null) params.set("amountMin", String(amountMin));
    if (amountMax !== null) params.set("amountMax", String(amountMax));
    if (currency !== null && currency !== "") params.set("currency", currency);
    if (q) params.set("q", q);
    fetch(`/api/aliases/preview?${params.toString()}`, {
      signal: controller.signal,
    })
      .then(async (res) => {
        if (!res.ok) {
          const error = await readApiError(res, "No se pudo probar el alias");
          setState({ status: "error", error });
          return;
        }
        setState({ status: "done", result: await res.json(), q });
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setState({ status: "error", error: "No se pudo probar el alias" });
        }
      });
    return () => controller.abort();
  }, [matchDescription, matchType, amountMin, amountMax, currency, q, version]);

  return (
    <div className="space-y-3 rounded-md border bg-muted/30 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium">
            Prueba de &quot;{matchDescription.trim()}&quot; (
            {MATCH_TYPE_LABELS[matchType].toLowerCase()})
          </p>
          {state.status === "done" && state.result.total > 0 && (
            <p className="text-sm text-muted-foreground">
              {summary(state.result, state.q)}
            </p>
          )}
        </div>
        <Button
          variant="ghost"
          size="icon-xs"
          aria-label="Cerrar prueba"
          title="Cerrar"
          onClick={onClose}
        >
          <X />
        </Button>
      </div>

      {showSearch && (
        <SearchInput
          className="w-full sm:max-w-xs"
          inputClassName="h-8 text-sm"
          aria-label="Buscar en las coincidencias por descripción"
          value={search}
          onValueChange={setSearch}
          loading={stale}
        />
      )}
      {state.status === "loading" && (
        <p className="text-sm text-muted-foreground">Cargando...</p>
      )}
      {state.status === "error" && (
        <p className="text-sm text-destructive">{state.error}</p>
      )}
      {state.status === "done" && state.result.total === 0 && (
        <p className="text-sm text-muted-foreground">
          {state.q
            ? `Sin resultados para «${state.q}».`
            : "Ninguna transacción coincide con esta descripción."}
        </p>
      )}
      {state.status === "done" && state.result.total > 0 && (
        <div
          aria-busy={stale}
          className={cn(
            "space-y-3 transition-opacity duration-200",
            stale && "opacity-60"
          )}
        >
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Fecha</TableHead>
                <TableHead>Descripción original</TableHead>
                <TableHead className="text-right">Monto</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {state.result.transactions.map((txn) => (
                <TableRow key={txn.id}>
                  <TableCell className="text-muted-foreground">
                    {formatPlainDateEs(txn.transactionDate)}
                  </TableCell>
                  <TableCell
                    className="max-w-80 truncate"
                    title={descriptionTitle(
                      txn.description,
                      txn.displayDescription,
                      "Se muestra como"
                    )}
                  >
                    {txn.description}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {formatAmount(txn.currency, txn.amount)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
          {state.result.total > state.result.transactions.length && (
            <p className="text-xs text-muted-foreground">
              Se muestran las {state.result.transactions.length} más recientes.
            </p>
          )}
        </div>
      )}
    </div>
  );
}
