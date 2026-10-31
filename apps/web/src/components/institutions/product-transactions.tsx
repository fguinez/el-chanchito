"use client";

import {
  Suspense,
  useDeferredValue,
  useMemo,
  useState,
  type ReactNode,
} from "react";
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
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { SearchInput } from "@/components/ui/search-input";
import { TransactionDetailSheet } from "@/components/transactions/transaction-detail-sheet";
import { useSelectedTransaction } from "@/components/transactions/use-selected-transaction";
import type { ProductTransaction } from "@/components/institutions/product-graphs";
import { transactionDisplayId } from "@/lib/transaction-selection";
import {
  matchesTransactionSearch,
  searchTokens,
} from "@/lib/transaction-search";
import { cn, formatAmount, formatPlainDateEs } from "@/lib/utils";

/** Rows rendered per "Ver más" step. */
const PAGE_SIZE = 50;

interface ProductTransactionsCardProps {
  currency: string;
  /** Ascending by date, as the product detail endpoint returns them. */
  transactions: ProductTransaction[];
  /** The endpoint capped the list and older transactions were left out. */
  truncated: boolean;
  /** A transfer link changed in the detail panel: reload the list. */
  onTransferChanged?: () => void;
}

/**
 * Every loaded transaction of one product, newest first, with a search over
 * all of them (not only the rows shown so far). A row opens the shared detail
 * panel through the `?txn=` param, like the Gastos table.
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
  onTransferChanged,
}: ProductTransactionsCardProps) {
  const { selectedTxnId, selectTransaction, rowProps } =
    useSelectedTransaction();
  const [visible, setVisible] = useState(PAGE_SIZE);
  const [search, setSearch] = useState("");
  // Filtering a few hundred rows can lag a keystroke: let typing go first.
  const deferredSearch = useDeferredValue(search);
  // The endpoint sorts by date descending before reversing, so reversing
  // back yields newest first.
  const newestFirst = useMemo(
    () => transactions.slice().reverse(),
    [transactions]
  );
  const tokens = useMemo(
    () => searchTokens(deferredSearch),
    [deferredSearch]
  );
  const searching = tokens.length > 0;
  const matching = useMemo(
    () =>
      tokens.length === 0
        ? newestFirst
        : newestFirst.filter((txn) =>
            matchesTransactionSearch(
              tokens,
              txn.description,
              txn.rawDescription
            )
          ),
    [newestFirst, tokens]
  );
  const shown = matching.slice(0, visible);
  const total = newestFirst.length;

  const noun = total === 1 ? "transacción" : "transacciones";
  const description =
    total === 0
      ? "Movimientos registrados para este producto"
      : truncated
        ? searching
          ? `${matching.length} de las ${total} transacciones más recientes`
          : `Las ${total} transacciones más recientes`
        : searching
          ? `${matching.length} de ${total} ${noun}`
          : `${total} ${noun}`;

  return (
    <TransactionsShell description={description}>
      {total === 0 ? (
        <p className="text-muted-foreground">
          Sin transacciones registradas para este producto.
        </p>
      ) : (
        <>
          <div className="mb-4 space-y-1">
            <SearchInput
              className="w-full sm:max-w-xs"
              value={search}
              onValueChange={(value) => {
                setSearch(value);
                // A new search starts again from the first page.
                setVisible(PAGE_SIZE);
              }}
            />
            {searching && truncated && (
              <p className="text-xs text-muted-foreground">
                La búsqueda cubre las {total} transacciones más recientes.
              </p>
            )}
          </div>
          {matching.length === 0 ? (
            <p className="text-muted-foreground">
              Sin resultados para «{deferredSearch.trim()}».
            </p>
          ) : (
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
                        {txn.isInternalTransfer && (
                          <Badge variant="secondary" className="ml-2 text-xs">
                            Transferencia interna
                          </Badge>
                        )}
                        {txn.location && (
                          <span className="block text-xs text-muted-foreground">
                            {txn.location}
                          </span>
                        )}
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
          )}
          {matching.length > shown.length && (
            <div className="mt-4 flex items-center justify-between gap-2 text-sm text-muted-foreground">
              <span>
                Mostrando {shown.length} de {matching.length}
                {searching && " resultados"}
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
        onTransferChanged={onTransferChanged}
      />
    </TransactionsShell>
  );
}
