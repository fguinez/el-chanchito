"use client";

import { Suspense, useMemo, useState, type ReactNode } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { TransactionDetailSheet } from "@/components/transactions/transaction-detail-sheet";
import { useSelectedTransaction } from "@/components/transactions/use-selected-transaction";
import type { ProductTransaction } from "@/components/institutions/product-graphs";
import { transactionDisplayId } from "@/lib/transaction-selection";
import { cn, formatAmount, formatPlainDateEs } from "@/lib/utils";

/** Rows rendered per "Ver más" step. */
const PAGE_SIZE = 50;

interface ProductTransactionsCardProps {
  currency: string;
  /** Ascending by date, as the product detail endpoint returns them. */
  transactions: ProductTransaction[];
  /** The endpoint capped the list and older transactions were left out. */
  truncated: boolean;
}

/**
 * Every loaded transaction of one product, newest first. A row opens the
 * shared detail panel through the `?txn=` param, like the Gastos table.
 */
export function ProductTransactionsCard(props: ProductTransactionsCardProps) {
  // useSearchParams (in the selection hook) needs a Suspense boundary.
  return (
    <Suspense fallback={<TransactionsShell description="Cargando..." />}>
      <TransactionsList {...props} />
    </Suspense>
  );
}

function TransactionsShell({
  description,
  children,
}: {
  description: string;
  children?: ReactNode;
}) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Transacciones</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      {children && <CardContent>{children}</CardContent>}
    </Card>
  );
}

function TransactionsList({
  currency,
  transactions,
  truncated,
}: ProductTransactionsCardProps) {
  const { selectedTxnId, selectTransaction, rowProps } =
    useSelectedTransaction();
  const [visible, setVisible] = useState(PAGE_SIZE);
  // The endpoint sorts by date descending before reversing, so reversing
  // back yields newest first.
  const newestFirst = useMemo(
    () => transactions.slice().reverse(),
    [transactions]
  );
  const shown = newestFirst.slice(0, visible);
  const total = newestFirst.length;

  const description =
    total === 0
      ? "Movimientos registrados para este producto"
      : truncated
        ? `Las ${total} transacciones más recientes`
        : total === 1
          ? "1 transacción"
          : `${total} transacciones`;

  return (
    <TransactionsShell description={description}>
      {total === 0 ? (
        <p className="text-muted-foreground">
          Sin transacciones registradas para este producto.
        </p>
      ) : (
        <>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Fecha</TableHead>
                <TableHead>Descripción</TableHead>
                <TableHead className="hidden md:table-cell">ID</TableHead>
                <TableHead className="text-right">Monto</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {shown.map((txn) => {
                const displayId = transactionDisplayId(txn);
                return (
                  <TableRow key={txn.id} {...rowProps(txn.id)}>
                    <TableCell className="text-sm">
                      {formatPlainDateEs(txn.transactionDate)}
                    </TableCell>
                    <TableCell className="whitespace-normal break-words">
                      {txn.description}
                    </TableCell>
                    <TableCell className="hidden md:table-cell">
                      <code
                        className={cn(
                          "block max-w-[10rem] truncate font-mono text-xs",
                          !displayId.external && "text-muted-foreground"
                        )}
                        title={
                          displayId.external
                            ? `ID de origen: ${displayId.value}`
                            : `ID interno: ${displayId.value}`
                        }
                      >
                        {displayId.value}
                      </code>
                    </TableCell>
                    <TableCell
                      className={cn(
                        "text-right font-medium tabular-nums",
                        txn.amount < 0 ? "text-red-600" : "text-green-600"
                      )}
                    >
                      {txn.amount < 0 ? "-" : "+"}
                      {formatAmount(currency, Math.abs(txn.amount))}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
          {total > shown.length && (
            <div className="mt-4 flex items-center justify-between gap-2 text-sm text-muted-foreground">
              <span>
                Mostrando {shown.length} de {total}
              </span>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setVisible((n) => n + PAGE_SIZE)}
              >
                Ver más
              </Button>
            </div>
          )}
        </>
      )}
      <TransactionDetailSheet
        transactionId={selectedTxnId}
        onClose={() => selectTransaction(null)}
      />
    </TransactionsShell>
  );
}
