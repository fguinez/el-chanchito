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
import { formatAmount, formatPlainDateEs } from "@/lib/utils";
import type { MatchType } from "@/lib/db/schema";
import {
  MATCH_TYPE_LABELS,
  plural,
  readApiError,
  type AliasPreviewResult,
} from "./shared";

type PreviewState =
  | { status: "loading" }
  | { status: "error"; error: string }
  | { status: "done"; result: AliasPreviewResult };

function summary({ total }: AliasPreviewResult): string {
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
 * original (scraped) description. Refetches when `version` (bumped on every data
 * reload) or any input changes; callers key it by the match so switching the
 * description remounts it.
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

  useEffect(() => {
    const controller = new AbortController();
    const params = new URLSearchParams({ matchDescription, matchType });
    if (amountMin !== null) params.set("amountMin", String(amountMin));
    if (amountMax !== null) params.set("amountMax", String(amountMax));
    if (currency !== null && currency !== "") params.set("currency", currency);
    fetch(`/api/aliases/preview?${params.toString()}`, {
      signal: controller.signal,
    })
      .then(async (res) => {
        if (!res.ok) {
          const error = await readApiError(res, "No se pudo probar el alias");
          setState({ status: "error", error });
          return;
        }
        setState({ status: "done", result: await res.json() });
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setState({ status: "error", error: "No se pudo probar el alias" });
        }
      });
    return () => controller.abort();
  }, [matchDescription, matchType, amountMin, amountMax, currency, version]);

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
          Ninguna transacción coincide con esta descripción.
        </p>
      )}
      {state.status === "done" && state.result.total > 0 && (
        <>
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
                    title={txn.description}
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
        </>
      )}
    </div>
  );
}
