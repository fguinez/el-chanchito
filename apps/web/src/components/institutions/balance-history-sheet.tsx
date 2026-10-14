"use client";

import { History } from "lucide-react";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { formatBalance } from "@/components/institutions/shared";
import type { ProductHistoryPoint } from "@/components/institutions/product-graphs";
import { formatCLP } from "@/lib/utils";

/**
 * "Registros" button that opens every stored balance snapshot, newest first,
 * in a side panel; the card itself only shows the chart.
 */
export function BalanceHistorySheet({
  currency,
  history,
}: {
  currency: string;
  /** Ascending by `asOf`, as the product detail endpoint returns it. */
  history: ProductHistoryPoint[];
}) {
  const newestFirst = history.slice().reverse();
  const count = newestFirst.length;

  return (
    <Sheet>
      <SheetTrigger asChild>
        <Button variant="outline" size="sm">
          <History />
          Registros ({count})
        </Button>
      </SheetTrigger>
      <SheetContent className="gap-0">
        <SheetHeader className="border-b">
          <SheetTitle>Historial de saldo</SheetTitle>
          <SheetDescription>
            {count === 1 ? "1 registro" : `${count} registros`}, del más
            reciente al más antiguo
          </SheetDescription>
          {currency !== "CLP" && (
            <p className="text-xs text-muted-foreground">
              Aproximación: el saldo en CLP se convierte con los tipos de
              cambio actuales.
            </p>
          )}
        </SheetHeader>
        <div className="flex-1 overflow-y-auto p-4">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Fecha</TableHead>
                <TableHead className="text-right">Saldo</TableHead>
                {currency !== "CLP" && (
                  <TableHead className="text-right">≈ CLP</TableHead>
                )}
              </TableRow>
            </TableHeader>
            <TableBody>
              {newestFirst.map((p) => (
                <TableRow key={p.asOf}>
                  <TableCell className="text-muted-foreground">
                    {formatSnapshotTime(p.asOf)}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {formatBalance(currency, p.balance) ?? "—"}
                  </TableCell>
                  {currency !== "CLP" && (
                    <TableCell className="text-right tabular-nums text-muted-foreground">
                      {p.balanceClp != null ? formatCLP(p.balanceClp) : "—"}
                    </TableCell>
                  )}
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </SheetContent>
    </Sheet>
  );
}

/** Date and time: a product can get several snapshots on the same day. */
function formatSnapshotTime(iso: string): string {
  return new Date(iso).toLocaleString("es-CL", {
    dateStyle: "short",
    timeStyle: "short",
  });
}
