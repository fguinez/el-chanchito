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
import {
  BILLING_STATUS_LABELS,
  filterByBilling,
  type BillingFilter,
  type TransactionBilling,
} from "@/lib/card-billing";
import { cn, formatAmount, formatPlainDateEs } from "@/lib/utils";

/** Rows rendered per "Ver más" step. */
const PAGE_SIZE = 50;

const BILLING_FILTERS: { value: BillingFilter; label: string }[] = [
  { value: "all", label: "Todos" },
  { value: "billed", label: "Facturados" },
  { value: "unbilled", label: "No facturados" },
];

interface ProductTransactionsCardProps {
  currency: string;
  /** Ascending by date, as the product detail endpoint returns them. */
  transactions: ProductTransaction[];
  /** The endpoint capped the list and older transactions were left out. */
  truncated: boolean;
  /** A credit card: badge each movement's billing status and filter by it
   *  (once at least one movement has a status). */
  showBilling?: boolean;
  /** A transfer link changed in the detail panel: reload the list. */
  onTransferChanged?: () => void;
}

/**
 * Every loaded transaction of one product, newest first, with a search over
 * all of them (not only the rows shown so far). A credit card's list also
 * shows each movement's billing status and filters by it, together with the
 * search. A row opens the shared detail panel through the `?txn=` param, like
 * the Gastos table.
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
  showBilling = false,
  onTransferChanged,
}: ProductTransactionsCardProps) {
  const { selectedTxnId, selectTransaction, rowProps } =
    useSelectedTransaction();
  const [visible, setVisible] = useState(PAGE_SIZE);
  const [search, setSearch] = useState("");
  const [billingFilter, setBillingFilter] = useState<BillingFilter>("all");
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
  // The status column and its filter only mean something once a movement
  // has a status (the product route computes them for credit cards only).
  const hasBilling =
    showBilling && transactions.some((txn) => txn.billing != null);
  const activeFilter: BillingFilter = hasBilling ? billingFilter : "all";
  const billingFiltered = useMemo(
    () => filterByBilling(newestFirst, activeFilter),
    [newestFirst, activeFilter]
  );
  const filtering = searching || activeFilter !== "all";
  const matching = useMemo(
    () =>
      tokens.length === 0
        ? billingFiltered
        : billingFiltered.filter((txn) =>
            matchesTransactionSearch(
              tokens,
              txn.description,
              txn.rawDescription
            )
          ),
    [billingFiltered, tokens]
  );
  const shown = matching.slice(0, visible);
  const total = newestFirst.length;

  const noun = total === 1 ? "transacción" : "transacciones";
  const description =
    total === 0
      ? "Movimientos registrados para este producto"
      : truncated
        ? filtering
          ? `${matching.length} de las ${total} transacciones más recientes`
          : `Las ${total} transacciones más recientes`
        : filtering
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
            <div className="flex flex-wrap items-center gap-2">
              <SearchInput
                className="w-full sm:max-w-xs"
                value={search}
                onValueChange={(value) => {
                  setSearch(value);
                  // A new search starts again from the first page.
                  setVisible(PAGE_SIZE);
                }}
              />
              {hasBilling && (
                <div
                  className="flex items-center gap-1"
                  role="group"
                  aria-label="Facturación"
                >
                  {BILLING_FILTERS.map((option) => (
                    <Button
                      key={option.value}
                      variant={
                        activeFilter === option.value ? "secondary" : "ghost"
                      }
                      size="sm"
                      aria-pressed={activeFilter === option.value}
                      onClick={() => {
                        setBillingFilter(option.value);
                        setVisible(PAGE_SIZE);
                      }}
                    >
                      {option.label}
                    </Button>
                  ))}
                </div>
              )}
            </div>
            {filtering && truncated && (
              <p className="text-xs text-muted-foreground">
                {searching ? "La búsqueda" : "El filtro"} cubre las {total}{" "}
                transacciones más recientes.
              </p>
            )}
          </div>
          {matching.length === 0 ? (
            <p className="text-muted-foreground">
              {searching
                ? `Sin resultados para «${deferredSearch.trim()}».`
                : activeFilter === "unbilled"
                  ? "Sin movimientos no facturados."
                  : "Sin movimientos facturados."}
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Fecha</TableHead>
                  <TableHead>Descripción</TableHead>
                  <TableHead className="hidden md:table-cell">ID</TableHead>
                  {/* Below lg the sidebar leaves no room for one more
                      column: the badge goes under the description. */}
                  {hasBilling && (
                    <TableHead className="hidden lg:table-cell">
                      Estado
                    </TableHead>
                  )}
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
                        {hasBilling && txn.billing && (
                          <span className="mt-1 block lg:hidden">
                            <BillingBadge billing={txn.billing} />
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
                      {hasBilling && (
                        <TableCell className="hidden lg:table-cell">
                          {txn.billing && (
                            <BillingBadge billing={txn.billing} />
                          )}
                        </TableCell>
                      )}
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
          {hasBilling && shown.some((txn) => txn.billing?.estimated) && (
            <p className="mt-2 text-xs text-muted-foreground">
              Borde punteado: estado estimado según la fecha de facturación.
            </p>
          )}
          {matching.length > shown.length && (
            <div className="mt-4 flex items-center justify-between gap-2 text-sm text-muted-foreground">
              <span>
                Mostrando {shown.length} de {matching.length}
                {filtering && " resultados"}
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

/** A movement's billing status. One derived from the billing cycle instead of
 *  reported by the bank is an estimate: dashed border, and the tooltip and the
 *  screen reader say so. */
function BillingBadge({ billing }: { billing: TransactionBilling }) {
  return (
    <Badge
      variant={billing.status === "billed" ? "secondary" : "outline"}
      className={cn(
        billing.estimated && "border-dashed border-muted-foreground/50"
      )}
      title={
        billing.estimated
          ? "Estimado según la fecha de facturación"
          : "Según el banco"
      }
    >
      {BILLING_STATUS_LABELS[billing.status]}
      {billing.estimated && <span className="sr-only"> (estimado)</span>}
    </Badge>
  );
}
