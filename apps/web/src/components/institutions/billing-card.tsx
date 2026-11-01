// "Facturación" card of a credit card's detail page: the billed debt still
// owed and the charges not billed yet, then the billing cycle's dates and the
// latest statement's figures. Every figure comes from the product route's
// `billing` block (lib/card-billing); estimated dates say so.

import type { ReactNode } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { formatBalance } from "@/components/institutions/shared";
import {
  billedDebtState,
  daysBetween,
  isMinimumPaid,
  isPaymentOverdue,
  type BillingDate,
  type CardBillingSummary,
} from "@/lib/card-billing";
import { formatPlainDateEs } from "@/lib/utils";

export function BillingCard({
  currency,
  billing,
}: {
  /** The card's currency: every amount in `billing` is in it. */
  currency: string;
  billing: CardBillingSummary;
}) {
  const { cycle, statement } = billing;
  const nothingKnown =
    cycle == null &&
    billing.statusCount === 0 &&
    statement.billedAmount == null &&
    statement.minimumPayment == null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Facturación</CardTitle>
        <CardDescription>
          Estado de cuenta y compras por facturar
        </CardDescription>
      </CardHeader>
      <CardContent>
        {nothingKnown ? (
          <p className="text-sm text-muted-foreground">
            Esta tarjeta no informa su fecha de facturación.
          </p>
        ) : (
          <div className="space-y-6">
            <div className="grid gap-6 sm:grid-cols-2">
              <BilledDebt currency={currency} billing={billing} />
              <Unbilled currency={currency} billing={billing} />
            </div>

            <dl className="grid grid-cols-[repeat(auto-fit,minmax(9rem,1fr))] gap-x-6 gap-y-4 border-t pt-4">
              <Field label="Fecha de facturación">
                <CycleDate value={cycle?.lastClosing ?? null} />
              </Field>
              <Field
                label="Próxima facturación"
                note={
                  cycle && closingCountdown(billing.asOf, cycle.nextClosing.date)
                }
              >
                <CycleDate value={cycle?.nextClosing ?? null} />
              </Field>
              <Field label="Vencimiento">
                <span className="inline-flex flex-wrap items-center gap-1.5">
                  <CycleDate value={cycle?.dueDate ?? null} />
                  {isPaymentOverdue(billing) && (
                    <Badge variant="destructive">vencido</Badge>
                  )}
                  {isMinimumPaid(billing) && (
                    <Badge variant="secondary">mínimo pagado</Badge>
                  )}
                </span>
              </Field>
              <Field
                label="Monto facturado"
                note={
                  cycle?.rolledOver && statement.date
                    ? `Estado de cuenta del ${formatPlainDateEs(statement.date)}`
                    : null
                }
              >
                <Amount currency={currency} value={statement.billedAmount} />
              </Field>
              <Field label="Pago mínimo">
                <Amount currency={currency} value={statement.minimumPayment} />
              </Field>
            </dl>

            <Footnotes billing={billing} />
          </div>
        )}
      </CardContent>
    </Card>
  );
}

/** The billed debt still owed: Pendiente, Pagada, none billed, or unknown. */
function BilledDebt({
  currency,
  billing,
}: {
  currency: string;
  billing: CardBillingSummary;
}) {
  const state = billedDebtState(billing);
  const { cycle } = billing;

  let figure: ReactNode;
  let note: string | null = null;
  if (state === "none") {
    figure = <span className="text-lg">Sin deuda facturada</span>;
  } else if (state === "unknown") {
    figure = <Muted>sin dato</Muted>;
    if (cycle?.rolledOver && cycle.basis === "statement") {
      const closed = formatPlainDateEs(cycle.lastClosing.date);
      note = `Falta leer el estado de cuenta del ${closed}.`;
    }
  } else {
    figure = (
      <span className="inline-flex flex-wrap items-center gap-2">
        {formatBalance(currency, billing.billedPending)}
        {state === "pending" ? (
          <Badge
            variant="secondary"
            className="bg-amber-100 text-amber-900 dark:bg-amber-950 dark:text-amber-200"
          >
            Pendiente
          </Badge>
        ) : (
          <Badge
            variant="outline"
            className="border-green-200 text-green-700 dark:border-green-900 dark:text-green-400"
          >
            Pagada
          </Badge>
        )}
      </span>
    );
    if (billing.billedPendingEstimated) {
      note = "Monto facturado menos pagos desde la facturación (estimado)";
    }
  }

  return (
    <div>
      <p className="text-sm text-muted-foreground">Deuda facturada</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums">{figure}</p>
      {note && <p className="mt-1 text-xs text-muted-foreground">{note}</p>}
    </div>
  );
}

/** The charges not on a statement yet. */
function Unbilled({
  currency,
  billing,
}: {
  currency: string;
  billing: CardBillingSummary;
}) {
  const known = billing.statusCount > 0;
  const count = billing.unbilledCount;
  return (
    <div>
      <p className="text-sm text-muted-foreground">No facturado</p>
      <p className="mt-1 text-2xl font-semibold tabular-nums">
        {known ? (
          formatBalance(currency, billing.unbilledCharges)
        ) : (
          <Muted>sin dato</Muted>
        )}
      </p>
      {known && (
        <p className="mt-1 text-xs text-muted-foreground">
          {count === 0
            ? "Sin compras por facturar"
            : `${count} ${count === 1 ? "compra" : "compras"} por facturar`}
        </p>
      )}
    </div>
  );
}

function Field({
  label,
  note,
  children,
}: {
  label: string;
  note?: string | null;
  children: ReactNode;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="mt-0.5 text-sm font-medium tabular-nums">{children}</dd>
      {note && <dd className="text-xs text-muted-foreground">{note}</dd>}
    </div>
  );
}

function CycleDate({ value }: { value: BillingDate | null }) {
  if (!value) return <Muted>sin dato</Muted>;
  return (
    <>
      {formatPlainDateEs(value.date)}
      {value.estimated && (
        <span className="font-normal text-muted-foreground"> (estimada)</span>
      )}
    </>
  );
}

function Amount({
  currency,
  value,
}: {
  currency: string;
  value: number | null;
}) {
  return <>{formatBalance(currency, value) ?? <Muted>sin dato</Muted>}</>;
}

function Muted({ children }: { children: ReactNode }) {
  return <span className="font-normal text-muted-foreground">{children}</span>;
}

/** "hoy" or "en N días" until the next closing. */
function closingCountdown(today: string, closing: string): string {
  const days = daysBetween(today, closing);
  if (days <= 0) return "hoy";
  return days === 1 ? "en 1 día" : `en ${days} días`;
}

/** Where the estimates come from, and what the totals leave out. */
function Footnotes({ billing }: { billing: CardBillingSummary }) {
  const { cycle } = billing;
  const notes: string[] = [];
  if (!cycle) {
    notes.push("Esta tarjeta no informa su fecha de facturación.");
  } else if (
    cycle.lastClosing.estimated ||
    cycle.nextClosing.estimated ||
    cycle.dueDate?.estimated
  ) {
    notes.push(
      cycle.basis === "statement_day"
        ? "Fechas estimadas a partir del día de facturación de la tarjeta."
        : "Fechas estimadas a partir de la última facturación conocida."
    );
  }
  if (!billing.complete) {
    notes.push("Los totales cubren solo las transacciones más recientes.");
  }
  if (notes.length === 0) return null;
  return (
    <div className="space-y-0.5 text-xs text-muted-foreground">
      {notes.map((note) => (
        <p key={note}>{note}</p>
      ))}
    </div>
  );
}
