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
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SearchInput } from "@/components/ui/search-input";
import { useSelectedTransaction } from "@/components/transactions/use-selected-transaction";
import { cn, formatAmount, formatPlainDateEs } from "@/lib/utils";
import { descriptionTitle } from "@/lib/transaction-search";
import { usePreviewSearch } from "@/lib/use-preview-search";
import { trimKeyword, type RuleMatchType } from "@/lib/categories";
import {
  CategoryLabel,
  plural,
  readApiError,
  type ApiCategory,
  type RulePreviewResult,
} from "./shared";

/** `q` is the search the result answers ("" for none). */
type PreviewState =
  | { status: "loading" }
  | { status: "error"; error: string }
  | { status: "done"; result: RulePreviewResult; q: string };

function summary({ total, uncategorized }: RulePreviewResult, q: string): string {
  const matches = q
    ? `${plural(total, "coincidencia", "coincidencias")} con la búsqueda`
    : `${plural(total, "transacción contiene", "transacciones contienen")} esta palabra`;
  const pending =
    uncategorized === 0
      ? "ninguna queda por categorizar"
      : plural(uncategorized, "queda por categorizar", "quedan por categorizar");
  return `${matches}; ${pending}.`;
}

/**
 * What a rule with `keyword` and `matchType` would match: counts plus the
 * most recent matches with their current category (what each row shows today
 * in the tooltip). A search box narrows the matches, server-side once typing
 * pauses. Refetches when `version` (bumped on every data reload) changes;
 * callers key it by keyword and match type.
 */
export function RulePreview({
  keyword,
  matchType,
  categoriesById,
  version,
  onClose,
}: {
  keyword: string;
  matchType: RuleMatchType;
  categoriesById: Map<string, ApiCategory>;
  version: number;
  onClose: () => void;
}) {
  const [state, setState] = useState<PreviewState>({ status: "loading" });
  const { rowProps } = useSelectedTransaction();
  const { search, setSearch, q, stale, showSearch } = usePreviewSearch(
    state.status === "done" ? { q: state.q, total: state.result.total } : null
  );

  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams({ keyword, matchType });
    if (q) params.set("q", q);
    fetch(`/api/categories/rules/preview?${params}`, {
      signal: controller.signal,
    })
      .then(async (res) => {
        if (!res.ok) {
          const error = await readApiError(res, "No se pudo probar la regla");
          setState({ status: "error", error });
          return;
        }
        setState({ status: "done", result: await res.json(), q });
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setState({ status: "error", error: "No se pudo probar la regla" });
        }
      });
    return () => controller.abort();
  }, [keyword, matchType, q, version]);

  return (
    <div className="space-y-3 rounded-md border bg-muted/30 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium">
            Prueba de &quot;{trimKeyword(keyword).toLowerCase()}&quot;
            {matchType === "exact" && " (exacta)"}
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
            : "Ninguna transacción contiene esta palabra."}
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
                <TableHead>Descripción</TableHead>
                <TableHead className="text-right">Monto</TableHead>
                <TableHead>Categoría actual</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {state.result.transactions.map((txn) => {
                const category = txn.categoryId
                  ? categoriesById.get(txn.categoryId)
                  : undefined;
                return (
                  <TableRow key={txn.id} {...rowProps(txn.id)}>
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
                    <TableCell>
                      <span className="inline-flex items-center gap-2">
                        {category ? (
                          <CategoryLabel category={category} />
                        ) : (
                          <span className="text-muted-foreground">
                            Sin categoría
                          </span>
                        )}
                        {txn.isManuallyCategorized && (
                          <Badge
                            variant="outline"
                            title="Asignada a mano: las reglas no la cambian"
                          >
                            manual
                          </Badge>
                        )}
                      </span>
                    </TableCell>
                  </TableRow>
                );
              })}
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
