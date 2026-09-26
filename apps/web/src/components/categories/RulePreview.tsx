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
import { formatCLP, formatPlainDateEs } from "@/lib/utils";
import {
  CategoryLabel,
  plural,
  readApiError,
  type ApiCategory,
  type RulePreviewResult,
} from "./shared";

type PreviewState =
  | { status: "loading" }
  | { status: "error"; error: string }
  | { status: "done"; result: RulePreviewResult };

function summary({ total, uncategorized }: RulePreviewResult): string {
  const matches = plural(
    total,
    "transacción contiene",
    "transacciones contienen"
  );
  const pending =
    uncategorized === 0
      ? "ninguna queda por categorizar"
      : plural(uncategorized, "queda por categorizar", "quedan por categorizar");
  return `${matches} esta palabra; ${pending}.`;
}

/**
 * What a rule with `keyword` would match: counts plus the most recent
 * matches with their current category. Refetches when `version` (bumped on
 * every data reload) changes; callers key it by keyword.
 */
export function RulePreview({
  keyword,
  categoriesById,
  version,
  onClose,
}: {
  keyword: string;
  categoriesById: Map<string, ApiCategory>;
  version: number;
  onClose: () => void;
}) {
  const [state, setState] = useState<PreviewState>({ status: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    fetch(
      `/api/categories/rules/preview?keyword=${encodeURIComponent(keyword)}`,
      { signal: controller.signal }
    )
      .then(async (res) => {
        if (!res.ok) {
          const error = await readApiError(res, "No se pudo probar la regla");
          setState({ status: "error", error });
          return;
        }
        setState({ status: "done", result: await res.json() });
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setState({ status: "error", error: "No se pudo probar la regla" });
        }
      });
    return () => controller.abort();
  }, [keyword, version]);

  return (
    <div className="space-y-3 rounded-md border bg-muted/30 p-4">
      <div className="flex items-start justify-between gap-3">
        <div>
          <p className="text-sm font-medium">
            Prueba de &quot;{keyword.trim().toLowerCase()}&quot;
          </p>
          {state.status === "done" && state.result.total > 0 && (
            <p className="text-sm text-muted-foreground">
              {summary(state.result)}
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

      {state.status === "loading" && (
        <p className="text-sm text-muted-foreground">Cargando...</p>
      )}
      {state.status === "error" && (
        <p className="text-sm text-destructive">{state.error}</p>
      )}
      {state.status === "done" && state.result.total === 0 && (
        <p className="text-sm text-muted-foreground">
          Ninguna transacción contiene esta palabra.
        </p>
      )}
      {state.status === "done" && state.result.total > 0 && (
        <>
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
                  <TableRow key={txn.id}>
                    <TableCell className="text-muted-foreground">
                      {formatPlainDateEs(txn.transactionDate)}
                    </TableCell>
                    <TableCell className="max-w-80 truncate" title={txn.description}>
                      {txn.description}
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatCLP(txn.amount)}
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
        </>
      )}
    </div>
  );
}
