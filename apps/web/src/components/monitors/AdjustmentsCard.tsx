"use client";

import { Fragment, useEffect, useMemo, useState } from "react";
import { ChevronLeft, ChevronRight, Loader2, Plus, Trash2 } from "lucide-react";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
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
import { Input } from "@/components/ui/input";
import { SearchInput } from "@/components/ui/search-input";
import { InstitutionLogo } from "@/components/institutions/institution-logo";
import { TransactionDetailSheet } from "@/components/transactions/transaction-detail-sheet";
import { useSelectedTransaction } from "@/components/transactions/use-selected-transaction";
import {
  groupAdjustmentsByDay,
  searchAdjustmentsByMonth,
} from "@/lib/monitors/adjustments";
import { formatLocalDate } from "@/lib/monitors/dates";
import { cn, formatAmount, formatPlainDateEs } from "@/lib/utils";
import { descriptionTitle, searchTokens } from "@/lib/transaction-search";
import {
  formatSignedAmount,
  type AdjustmentCandidate,
  type ApiAdjustment,
} from "@/components/monitors/shared";

/** YYYY-MM of `month` shifted by `delta` months. */
function shiftMonth(month: string, delta: number): string {
  const [year, monthNumber] = month.split("-").map(Number);
  return formatLocalDate(new Date(year, monthNumber - 1 + delta, 1)).slice(0, 7);
}

/** "septiembre de 2026" for "2026-09". */
function monthLabel(month: string): string {
  const [year, monthNumber] = month.split("-").map(Number);
  return new Date(year, monthNumber - 1, 1).toLocaleDateString("es-CL", {
    month: "long",
    year: "numeric",
  });
}

/** Previous/next month arrows around the month's name. */
function MonthNav({
  month,
  onChange,
}: {
  month: string;
  onChange: (month: string) => void;
}) {
  return (
    <div className="flex items-center gap-1">
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label="Mes anterior"
        onClick={() => onChange(shiftMonth(month, -1))}
      >
        <ChevronLeft />
      </Button>
      <span className="min-w-36 text-center text-sm font-medium first-letter:uppercase">
        {monthLabel(month)}
      </span>
      <Button
        variant="ghost"
        size="icon-sm"
        aria-label="Mes siguiente"
        onClick={() => onChange(shiftMonth(month, 1))}
      >
        <ChevronRight />
      </Button>
    </div>
  );
}

/** A threshold shift in the monitor currency, or "sin datos" without data. */
function Shift({ currency, value }: { currency: string; value: number | null }) {
  if (value == null) {
    return <span className="text-muted-foreground">sin datos</span>;
  }
  return <>{formatSignedAmount(currency, value)}</>;
}

/** Spanish message for a failed link. */
async function linkError(res: Response): Promise<string> {
  if (res.status === 404) return "El monitor ya no existe.";
  if (res.status === 409) return "Esa transacción ya es una variación.";
  const data = await res.json().catch(() => null);
  return data?.field === "transactionId"
    ? "Esa transacción ya no está disponible para este monitor."
    : "No se pudo agregar la variación.";
}

type CandidatesState =
  | { status: "loading" }
  | { status: "error" }
  | {
      status: "ok";
      candidates: AdjustmentCandidate[];
      /** The expression references no product at all. */
      noProducts: boolean;
    };

/** Search and pick a transaction of the expression's products. `onAdded`
 *  runs after a link is stored, with the month it landed in. */
function AddAdjustmentDialog({
  monitorId,
  initialMonth,
  disabled,
  onAdded,
}: {
  monitorId: string;
  initialMonth: string;
  disabled: boolean;
  onAdded: (month: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [month, setMonth] = useState(initialMonth);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [state, setState] = useState<CandidatesState>({ status: "loading" });
  const [savingId, setSavingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  // The query the shown list answers; differs while a search is in flight.
  const [shownQuery, setShownQuery] = useState<string | null>(null);
  const searching =
    state.status === "ok" && (search.trim() !== query || shownQuery !== query);

  // Debounce typing, so the list does not refetch on every key.
  useEffect(() => {
    const timer = setTimeout(() => setQuery(search.trim()), 250);
    return () => clearTimeout(timer);
  }, [search]);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    const params = new URLSearchParams({ month });
    if (query) params.set("q", query);
    fetch(`/api/monitors/${monitorId}/adjustments/candidates?${params}`, {
      signal: controller.signal,
    })
      .then(async (res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data: {
          productIds: string[];
          candidates: AdjustmentCandidate[];
        } = await res.json();
        if (controller.signal.aborted) return;
        setShownQuery(query);
        setState({
          status: "ok",
          candidates: data.candidates,
          noProducts: data.productIds.length === 0,
        });
      })
      .catch(() => {
        if (!controller.signal.aborted) setState({ status: "error" });
      });
    return () => controller.abort();
  }, [open, monitorId, month, query, attempt]);

  function handleOpenChange(next: boolean) {
    setOpen(next);
    if (next) {
      setMonth(initialMonth);
      setSearch("");
      setQuery("");
      setError(null);
      setState({ status: "loading" });
    }
  }

  async function handleAdd(candidate: AdjustmentCandidate) {
    setSavingId(candidate.id);
    setError(null);
    try {
      const res = await fetch(`/api/monitors/${monitorId}/adjustments`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ transactionId: candidate.id }),
      });
      if (!res.ok) {
        setError(await linkError(res));
        setAttempt((n) => n + 1);
        return;
      }
      setOpen(false);
      onAdded(candidate.transactionDate.slice(0, 7));
    } catch {
      setError("No se pudo agregar la variación.");
    } finally {
      setSavingId(null);
    }
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button disabled={disabled}>
          <Plus />
          Agregar variación
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Agregar variación</DialogTitle>
          <DialogDescription>
            Elige una transacción de los productos de la ecuación para que
            este monitor la ignore.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-wrap items-center gap-3">
          <div className="relative min-w-48 flex-1">
            <Input
              aria-label="Buscar transacción"
              placeholder="Buscar por descripción o monto"
              className="pr-8"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
            {searching && (
              <Loader2
                aria-label="Buscando"
                className="absolute top-1/2 right-2 size-4 -translate-y-1/2 animate-spin text-muted-foreground"
              />
            )}
          </div>
          <MonthNav
            month={month}
            onChange={(next) => {
              setMonth(next);
              setState({ status: "loading" });
            }}
          />
        </div>
        {error && <p className="text-sm text-destructive">{error}</p>}
        <div
          className={cn(
            "min-h-40",
            searching && "opacity-60 transition-opacity"
          )}
        >
          {state.status === "loading" ? (
            <p className="text-sm text-muted-foreground">Cargando...</p>
          ) : state.status === "error" ? (
            <p className="text-sm text-destructive">
              No se pudieron cargar las transacciones.
            </p>
          ) : state.noProducts ? (
            <p className="text-sm text-muted-foreground">
              La ecuación no usa productos: no hay transacciones que ignorar.
            </p>
          ) : state.candidates.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Sin resultados en {monthLabel(month)}.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Día</TableHead>
                  <TableHead>Transacción</TableHead>
                  <TableHead className="text-right">Monto</TableHead>
                  <TableHead className="w-24"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {state.candidates.map((candidate) => (
                  <TableRow key={candidate.id}>
                    <TableCell className="whitespace-nowrap">
                      {formatPlainDateEs(candidate.transactionDate)}
                    </TableCell>
                    <TableCell>
                      <div className="whitespace-normal">
                        {candidate.description}
                      </div>
                      <ProductLine
                        slug={candidate.institutionSlug}
                        institutionName={candidate.institutionName}
                        productName={candidate.productName}
                      />
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {formatAmount(candidate.productCurrency, candidate.amount)}
                    </TableCell>
                    <TableCell className="text-right">
                      {candidate.linked ? (
                        <span className="text-xs text-muted-foreground">
                          Ya agregada
                        </span>
                      ) : (
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={savingId != null}
                          onClick={() => handleAdd(candidate)}
                        >
                          {savingId === candidate.id ? "Agregando..." : "Agregar"}
                        </Button>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

/** Institution logo plus product name, under a transaction's description. */
function ProductLine({
  slug,
  institutionName,
  productName,
}: {
  slug: string;
  institutionName: string;
  productName: string;
}) {
  return (
    <div className="mt-0.5 flex items-center gap-1.5 text-xs text-muted-foreground">
      <InstitutionLogo slug={slug} size="sm" />
      <span>
        {institutionName} · {productName}
      </span>
    </div>
  );
}

/** A variación's transaction: its description (opens the detail panel; the
 *  original in the tooltip), flagged once deleted, over its product. */
function AdjustmentTransaction({
  adjustment,
  onOpen,
}: {
  adjustment: ApiAdjustment;
  onOpen: (transactionId: string) => void;
}) {
  const title = descriptionTitle(adjustment.description, adjustment.rawDescription);
  return (
    <>
      <div className="flex flex-wrap items-center gap-2">
        {adjustment.deleted ? (
          <span className="text-muted-foreground line-through" title={title}>
            {adjustment.description}
          </span>
        ) : (
          <button
            type="button"
            className="text-left hover:underline"
            title={title}
            onClick={() => onOpen(adjustment.transactionId)}
          >
            {adjustment.description}
          </button>
        )}
        {adjustment.deleted && (
          <Badge variant="outline" className="text-muted-foreground">
            transacción eliminada
          </Badge>
        )}
      </div>
      <ProductLine
        slug={adjustment.institutionSlug}
        institutionName={adjustment.institutionName}
        productName={adjustment.productName}
      />
    </>
  );
}

/** A variación's own shift of the alert threshold, or why it has none. */
function AdjustmentEffect({
  adjustment,
  currency,
}: {
  adjustment: ApiAdjustment;
  currency: string;
}) {
  if (adjustment.deleted) {
    return <span className="text-muted-foreground">no aplica</span>;
  }
  if (!adjustment.affects) {
    return (
      <span className="text-xs text-muted-foreground">
        no afecta a la expresión actual
      </span>
    );
  }
  return <Shift currency={currency} value={adjustment.effect} />;
}

/**
 * The monitor's variaciones for one month: transactions it ignores. Each one
 * shifts every threshold, from its day to the end of the month, by what
 * undoing it changes in the expression (its "efecto"); same-day entries show
 * their combined total. A search lists the matching ones of every month
 * instead, without the day and month totals (they add up the whole month).
 * `onChanged` runs after each write so the page refetches the evaluation, the
 * chart and this list; `refreshing` is true while that refetch is in flight.
 */
export function AdjustmentsCard({
  monitorId,
  currency,
  adjustments,
  refreshing,
  onChanged,
}: {
  monitorId: string;
  currency: string;
  adjustments: ApiAdjustment[];
  refreshing: boolean;
  onChanged: () => void;
}) {
  const today = formatLocalDate(new Date());
  const [month, setMonth] = useState(today.slice(0, 7));
  const [rowError, setRowError] = useState<string | null>(null);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const { selectedTxnId, selectTransaction } = useSelectedTransaction();
  const [search, setSearch] = useState("");
  const tokens = useMemo(() => searchTokens(search), [search]);
  const searching = tokens.length > 0;

  const days = groupAdjustmentsByDay(adjustments, month);
  const busy = deletingId != null || refreshing;
  const monthTotal = days.length > 0 ? days[days.length - 1].runningTotal : 0;
  const results = searching ? searchAdjustmentsByMonth(adjustments, tokens) : [];

  async function handleDelete(adjustment: ApiAdjustment) {
    if (!window.confirm("¿Quitar esta variación?")) return;
    setRowError(null);
    setDeletingId(adjustment.id);
    try {
      const res = await fetch(
        `/api/monitors/${monitorId}/adjustments/${adjustment.id}`,
        { method: "DELETE" }
      );
      if (!res.ok && res.status !== 404) throw new Error("failed");
      onChanged();
    } catch {
      setRowError("No se pudo quitar la variación.");
    } finally {
      setDeletingId(null);
    }
  }

  const removeButton = (adjustment: ApiAdjustment) => (
    <div className="flex justify-end text-muted-foreground">
      <Button
        variant="ghost"
        size="icon-xs"
        aria-label={`Quitar variación del ${formatPlainDateEs(
          adjustment.transactionDate
        )} (${adjustment.description})`}
        className="hover:text-destructive"
        disabled={busy}
        onClick={() => handleDelete(adjustment)}
      >
        <Trash2 />
      </Button>
    </div>
  );

  return (
    <Card>
      <CardHeader>
        <CardTitle>Variaciones</CardTitle>
        <CardDescription>
          Transacciones que este monitor ignora, como un gasto reembolsado o
          una compra puntual. Desde su fecha hasta fin de mes, cada umbral se
          corre para que la comparación quede como si la transacción no
          hubiera ocurrido (al tipo de cambio de hoy); el mes siguiente parte
          sin variaciones. El efecto y los totales son los del umbral de
          alerta.
        </CardDescription>
        {/* A search covers every month: the month picker steps aside. */}
        {!searching && (
          <CardAction>
            <MonthNav month={month} onChange={setMonth} />
          </CardAction>
        )}
      </CardHeader>
      <CardContent
        className={cn("space-y-4", refreshing && "opacity-60 transition-opacity")}
      >
        <div className="flex flex-wrap items-center gap-3">
          <AddAdjustmentDialog
            monitorId={monitorId}
            initialMonth={month}
            disabled={busy}
            onAdded={(landed) => {
              // Show the month the new variación landed in.
              setSearch("");
              setMonth(landed);
              onChanged();
            }}
          />
          {(adjustments.length > 0 || search !== "") && (
            <SearchInput
              className="w-full sm:ml-auto sm:w-64"
              aria-label="Buscar variaciones por descripción"
              value={search}
              onValueChange={setSearch}
            />
          )}
        </div>

        {searching ? (
          results.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Sin resultados para «{search.trim()}».
            </p>
          ) : (
            <>
              <p className="text-sm text-muted-foreground">
                Coincidencias de todos los meses. Los totales del día y del mes
                se ven sin búsqueda.
              </p>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Día</TableHead>
                    <TableHead>Transacción</TableHead>
                    <TableHead className="text-right">Monto</TableHead>
                    <TableHead className="text-right">Efecto en el umbral</TableHead>
                    <TableHead className="w-12"></TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {results.map((group) => (
                    <Fragment key={group.month}>
                      <TableRow className="hover:bg-transparent">
                        <TableCell
                          colSpan={5}
                          className="pt-4 font-medium first-letter:uppercase"
                        >
                          {monthLabel(group.month)}
                        </TableCell>
                      </TableRow>
                      {group.entries.map((adjustment) => (
                        <TableRow key={adjustment.id}>
                          <TableCell>
                            {formatPlainDateEs(adjustment.transactionDate)}
                          </TableCell>
                          <TableCell className="whitespace-normal">
                            <AdjustmentTransaction
                              adjustment={adjustment}
                              onOpen={selectTransaction}
                            />
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {formatAmount(
                              adjustment.productCurrency,
                              adjustment.amount
                            )}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            <AdjustmentEffect
                              adjustment={adjustment}
                              currency={currency}
                            />
                          </TableCell>
                          <TableCell>{removeButton(adjustment)}</TableCell>
                        </TableRow>
                      ))}
                    </Fragment>
                  ))}
                </TableBody>
              </Table>
              {rowError && <p className="text-sm text-destructive">{rowError}</p>}
            </>
          )
        ) : days.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Sin variaciones en {monthLabel(month)}.
          </p>
        ) : (
          <>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Día</TableHead>
                  <TableHead>Transacción</TableHead>
                  <TableHead className="text-right">Monto</TableHead>
                  <TableHead className="text-right">Efecto en el umbral</TableHead>
                  <TableHead className="text-right">Total del día</TableHead>
                  <TableHead className="text-right">Acumulado</TableHead>
                  <TableHead className="w-12"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {days.map((day) => (
                  <Fragment key={day.transactionDate}>
                    {day.entries.map((adjustment, index) => {
                      const span = day.entries.length;
                      // Shared day cells sit at the top of a multi-entry day.
                      const spanAlign = span > 1 ? "align-top" : "";
                      return (
                        <TableRow key={adjustment.id}>
                          {index === 0 && (
                            <TableCell rowSpan={span} className={spanAlign}>
                              {formatPlainDateEs(day.transactionDate)}
                            </TableCell>
                          )}
                          <TableCell className="whitespace-normal">
                            <AdjustmentTransaction
                              adjustment={adjustment}
                              onOpen={selectTransaction}
                            />
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {formatAmount(
                              adjustment.productCurrency,
                              adjustment.amount
                            )}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            <AdjustmentEffect
                              adjustment={adjustment}
                              currency={currency}
                            />
                          </TableCell>
                          {index === 0 && (
                            <>
                              <TableCell
                                rowSpan={span}
                                className={`text-right tabular-nums ${spanAlign}`}
                              >
                                <Shift currency={currency} value={day.dayTotal} />
                              </TableCell>
                              <TableCell
                                rowSpan={span}
                                className={`text-right font-medium tabular-nums ${spanAlign}`}
                              >
                                <Shift
                                  currency={currency}
                                  value={day.runningTotal}
                                />
                              </TableCell>
                            </>
                          )}
                          <TableCell>{removeButton(adjustment)}</TableCell>
                        </TableRow>
                      );
                    })}
                  </Fragment>
                ))}
              </TableBody>
            </Table>
            {rowError && <p className="text-sm text-destructive">{rowError}</p>}
            <p className="text-sm text-muted-foreground">
              Total del mes:{" "}
              <span className="font-medium tabular-nums text-foreground">
                <Shift currency={currency} value={monthTotal} />
              </span>
            </p>
          </>
        )}
      </CardContent>
      <TransactionDetailSheet
        transactionId={selectedTxnId}
        onClose={() => selectTransaction(null)}
        onChanged={onChanged}
      />
    </Card>
  );
}
