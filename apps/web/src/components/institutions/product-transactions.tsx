"use client";

import {
  Suspense,
  useDeferredValue,
  useMemo,
  useReducer,
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
  BILLING_STATUS_LABELS,
  type BillingFilter,
  type TransactionBilling,
} from "@/lib/card-billing";
import {
  INITIAL_LIST_CONTROLS,
  listControlsReducer,
  productTransactionsView,
} from "@/lib/product-transactions-view";
import { cn, formatAmount, formatPlainDateEs } from "@/lib/utils";

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
  const [controls, dispatch] = useReducer(
    listControlsReducer,
    INITIAL_LIST_CONTROLS
  );
  // Filtering a few hundred rows can lag a keystroke: let typing go first.
  const deferredSearch = useDeferredValue(controls.search);
  // The endpoint sorts by date descending before reversing, so reversing
  // back yields newest first.
  const newestFirst = useMemo(
    () => transactions.slice().reverse(),
    [transactions]
  );
  const view = useMemo(
    () =>
      productTransactionsView({
        rows: newestFirst,
        search: deferredSearch,
        billingFilter: controls.billingFilter,
        visible: controls.visible,
        showBilling,
        truncated,
      }),
    [
      newestFirst,
      deferredSearch,
      controls.billingFilter,
      controls.visible,
      showBilling,
      truncated,
    ]
  );

  return (
    <TransactionsShell description={view.description}>
      {view.total === 0 ? (
        <p className="text-muted-foreground">{view.emptyMessage}</p>
      ) : (
        <>
          <div className="mb-4 space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <SearchInput
                className="w-full sm:max-w-xs"
                value={controls.search}
                onValueChange={(search) => dispatch({ type: "search", search })}
              />
              {view.showsBilling && (
                <div
                  className="flex items-center gap-1"
                  role="group"
                  aria-label="Facturación"
                >
                  {BILLING_FILTERS.map((option) => (
                    <Button
                      key={option.value}
                      variant={
                        view.billingFilter === option.value
                          ? "secondary"
                          : "ghost"
                      }
                      size="sm"
                      aria-pressed={view.billingFilter === option.value}
                      onClick={() =>
                        dispatch({
                          type: "billingFilter",
                          billingFilter: option.value,
                        })
                      }
                    >
                      {option.label}
                    </Button>
                  ))}
                </div>
              )}
            </div>
            {view.coverageNote && (
              <p className="text-xs text-muted-foreground">
                {view.coverageNote}
              </p>
            )}
            {view.unknownNote && (
              <p className="text-xs text-muted-foreground">
                {view.unknownNote}
              </p>
            )}
          </div>
          {view.emptyMessage ? (
            <p className="text-muted-foreground">{view.emptyMessage}</p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Fecha</TableHead>
                  <TableHead>Descripción</TableHead>
                  <TableHead className="hidden md:table-cell">ID</TableHead>
                  {/* Below lg the sidebar leaves no room for one more
                      column: the badge goes under the description. */}
                  {view.showsBilling && (
                    <TableHead className="hidden lg:table-cell">
                      Estado
                    </TableHead>
                  )}
                  <TableHead className="text-right">Monto</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {view.shown.map((txn) => {
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
                        {view.showsBilling && txn.billing && (
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
                      {view.showsBilling && (
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
          {view.showsEstimateLegend && (
            <p className="mt-2 text-xs text-muted-foreground">
              Borde punteado: estado estimado según la fecha de facturación.
            </p>
          )}
          {view.pagingNote && (
            <div className="mt-4 flex items-center justify-between gap-2 text-sm text-muted-foreground">
              <span>{view.pagingNote}</span>
              <Button
                variant="outline"
                size="sm"
                onClick={() => dispatch({ type: "showMore" })}
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

/** A status derived from the billing cycle, not reported by the bank, is an
 *  estimate: dashed border, and the tooltip and the screen reader say so. */
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
