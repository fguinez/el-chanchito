"use client";

import Link from "next/link";
import {
  useEffect,
  useRef,
  useState,
  useSyncExternalStore,
  type ReactNode,
} from "react";
import { Check, Copy } from "lucide-react";
import { KIND_INFO } from "@chanchito/product-model";
import { displayProductName } from "@/components/institutions/shared";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import {
  cn,
  formatAmount,
  formatPlainDateEs,
  formatTimestampEs,
} from "@/lib/utils";
import type { TransactionDetail } from "@/app/api/transactions/[id]/route";
import type { TransactionLocation } from "@/lib/transaction-location";
import { TransactionLocationField } from "@/components/transactions/transaction-location-field";
import { SUGGESTIONS_ANCHOR } from "@/lib/transfer-suggestions";

type LoadResult =
  | { id: string; status: "ok"; data: TransactionDetail }
  | { id: string; status: "not_found" }
  | { id: string; status: "error" };

/**
 * Side panel with everything stored for one transaction; only the location
 * can be edited from here. Open while
 * `transactionId` is set; `onClose` should clear it (the caller owns the id,
 * e.g. in the URL).
 */
export function TransactionDetailSheet({
  transactionId,
  onClose,
}: {
  transactionId: string | null;
  onClose: () => void;
}) {
  const [result, setResult] = useState<LoadResult | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!transactionId) return;
    const controller = new AbortController();
    fetch(`/api/transactions/${encodeURIComponent(transactionId)}`, {
      signal: controller.signal,
    })
      .then(async (res) => {
        if (controller.signal.aborted) return;
        // A malformed id (400) cannot match any transaction either.
        if (res.status === 404 || res.status === 400) {
          setResult({ id: transactionId, status: "not_found" });
          return;
        }
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data: TransactionDetail = await res.json();
        if (controller.signal.aborted) return;
        setResult({ id: transactionId, status: "ok", data });
      })
      .catch(() => {
        if (controller.signal.aborted) return;
        setResult({ id: transactionId, status: "error" });
      });
    return () => controller.abort();
  }, [transactionId, attempt]);

  // While closing, keep showing the last result so the exit animation does
  // not flash a loading state.
  const loading = transactionId !== null && result?.id !== transactionId;
  const current = loading ? null : result;

  // Applied only to the transaction it was saved for: the panel may have
  // moved on to another one while the request was in flight.
  const locationSaved = (
    savedId: string,
    location: TransactionLocation | null
  ) =>
    setResult((previous) =>
      previous?.status === "ok" && previous.id === savedId
        ? {
            ...previous,
            data: { ...previous.data, location, locationIsManual: true },
          }
        : previous
    );

  const retry = () => {
    setResult(null);
    setAttempt((n) => n + 1);
  };
  // Refetch in place (the transfer link changed): no loading state.
  const reload = () => setAttempt((n) => n + 1);

  return (
    <Sheet
      open={transactionId !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <SheetContent className="gap-0">
        {current?.status === "ok" ? (
          <TransactionDetailBody
            txn={current.data}
            onLocationSaved={locationSaved}
            onTransferChanged={reload}
          />
        ) : (
          <SheetHeader>
            <SheetTitle>
              {current?.status === "not_found"
                ? "Transacción no encontrada"
                : current?.status === "error"
                  ? "No se pudo cargar la transacción"
                  : "Cargando transacción..."}
            </SheetTitle>
            <SheetDescription>
              {current?.status === "not_found"
                ? "Puede que haya sido eliminada o que el enlace sea incorrecto."
                : current?.status === "error"
                  ? "Ocurrió un error al consultar el servidor."
                  : "Detalle de la transacción"}
            </SheetDescription>
            {current?.status === "error" && (
              <Button
                variant="outline"
                size="sm"
                className="mt-2 w-fit"
                onClick={retry}
              >
                Reintentar
              </Button>
            )}
          </SheetHeader>
        )}
      </SheetContent>
    </Sheet>
  );
}

function TransactionDetailBody({
  txn,
  onLocationSaved,
  onTransferChanged,
}: {
  txn: TransactionDetail;
  onLocationSaved: (
    savedId: string,
    location: TransactionLocation | null
  ) => void;
  onTransferChanged: () => void;
}) {
  const productHref = `/institutions/${txn.institution.slug}/${txn.product.slug}`;
  const productName = displayProductName(txn.product, txn.institution.name);
  const kindLabel = KIND_INFO[txn.product.kind]?.labelEs ?? txn.product.kind;

  return (
    <>
      <SheetHeader className="border-b">
        <SheetTitle className="text-lg leading-snug break-words">
          {txn.description}
        </SheetTitle>
        <p
          className={cn(
            "text-2xl font-semibold tabular-nums",
            txn.amount < 0 ? "text-red-600" : "text-green-600"
          )}
        >
          {txn.amount < 0 ? "-" : "+"}
          {formatAmount(txn.product.currency, Math.abs(txn.amount))}
        </p>
        <SheetDescription>
          {formatPlainDateEs(txn.transactionDate)}
        </SheetDescription>
      </SheetHeader>

      <div className="flex-1 space-y-5 overflow-y-auto p-4">
        <Section title="Detalles">
          <Field label="Fecha de transacción">
            {formatPlainDateEs(txn.transactionDate)}
          </Field>
          {txn.accountingDate && (
            <Field label="Fecha contable">
              {formatPlainDateEs(txn.accountingDate)}
            </Field>
          )}
          <Field label="Mes asignado">
            {txn.scheduledMonth ? (
              formatMonthEs(txn.scheduledMonth)
            ) : (
              <Muted>Sin asignar</Muted>
            )}
          </Field>
          <Field label="Categoría">
            <span className="inline-flex flex-wrap items-center gap-2">
              {txn.category ? txn.category.name : <Muted>Sin categoría</Muted>}
              {txn.isManuallyCategorized && (
                <Badge variant="secondary">manual</Badge>
              )}
            </span>
          </Field>
          <Field label="Transferencia interna">
            {txn.transfer ? (
              <>
                <Link
                  href={`/transfers#transfer-${txn.transfer.id}`}
                  className="underline-offset-4 hover:underline"
                >
                  {txn.transfer.description}
                </Link>
                <span className="block text-xs text-muted-foreground">
                  {txn.transfer.side === "from" ? "Salida" : "Entrada"} ·{" "}
                  {formatPlainDateEs(txn.transfer.transferDate)} ·{" "}
                  {txn.transfer.status === "resolved" ? "Resuelta" : "Pendiente"}
                </span>
                {txn.transfer.autoLinked && (
                  <AutoLinkUndo
                    key={txn.transfer.id}
                    transferId={txn.transfer.id}
                    onUndone={onTransferChanged}
                  />
                )}
              </>
            ) : txn.isInternalTransfer ? (
              "Sí"
            ) : (
              <>
                No
                <SuggestionHint
                  key={txn.id}
                  transactionId={txn.id}
                  onAutoLinked={onTransferChanged}
                />
              </>
            )}
          </Field>
          <TransactionLocationField
            key={txn.id}
            transactionId={txn.id}
            location={txn.location}
            isManual={txn.locationIsManual}
            onSaved={onLocationSaved}
            renderRow={(label, children) => (
              <Field label={label}>{children}</Field>
            )}
          />
        </Section>

        <Separator />

        <Section title="Origen">
          <Field label="Institución">
            <Link
              href={`/institutions/${txn.institution.slug}`}
              className="underline-offset-4 hover:underline"
            >
              {txn.institution.name}
            </Link>
          </Field>
          <Field label="Producto">
            <Link
              href={productHref}
              className="underline-offset-4 hover:underline"
            >
              {productName}
            </Link>
            {kindLabel !== productName && (
              <span className="block text-xs text-muted-foreground">
                {kindLabel}
              </span>
            )}
          </Field>
          <Field label="Fuente">
            <Badge variant="outline">{txn.source}</Badge>
          </Field>
          <Field label="ID externo">
            {txn.externalId ? (
              <CopyableCode value={txn.externalId} label="ID externo" />
            ) : (
              <Muted>Sin ID externo</Muted>
            )}
          </Field>
        </Section>

        <Separator />

        <Section title="Notas">
          {txn.notes ? (
            <p className="text-sm break-words whitespace-pre-wrap">
              {txn.notes}
            </p>
          ) : (
            <p className="text-sm text-muted-foreground">Sin notas</p>
          )}
        </Section>

        <Separator />

        <Section title="Metadatos">
          <Field label="ID">
            <CopyableCode value={txn.id} label="ID de la transacción" />
          </Field>
          <Field label="Creado">{formatTimestampEs(txn.createdAt)}</Field>
          <Field label="Actualizado">{formatTimestampEs(txn.updatedAt)}</Field>
        </Section>
      </div>
    </>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-2">
      <h3 className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
        {title}
      </h3>
      {children}
    </section>
  );
}

function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <dl className="grid grid-cols-[minmax(0,9rem)_minmax(0,1fr)] gap-3 py-1 text-sm">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </dl>
  );
}

function Muted({ children }: { children: ReactNode }) {
  return <span className="text-muted-foreground">{children}</span>;
}

const noSubscribe = () => () => {};

/** The "Automática" badge of a transfer the suggestions linked on their own,
 *  and its undo: deleting it also dismisses the pair, so it is not linked
 *  again. */
function AutoLinkUndo({
  transferId,
  onUndone,
}: {
  transferId: string;
  onUndone: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);

  const undo = async () => {
    setBusy(true);
    setFailed(false);
    try {
      const res = await fetch("/api/transfers", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: transferId }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      onUndone();
    } catch {
      setFailed(true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <span className="mt-1 flex flex-wrap items-center gap-2">
      <Badge variant="secondary">Automática</Badge>
      <Button size="xs" variant="outline" disabled={busy} onClick={undo}>
        Deshacer vínculo
      </Button>
      {failed && (
        <span className="text-xs text-destructive">No se pudo deshacer</span>
      )}
    </span>
  );
}

/** "Parece una transferencia interna" when an unlinked row is in a current
 *  transfer suggestion, linking to the Sugeridas card. Loading the
 *  suggestions may link some on their own (this row among them), so
 *  `onAutoLinked` reloads the panel then. Silent on errors: it is only a
 *  hint. */
function SuggestionHint({
  transactionId,
  onAutoLinked,
}: {
  transactionId: string;
  onAutoLinked: () => void;
}) {
  const [suggested, setSuggested] = useState(false);
  // Read from the effect without refetching on every render.
  const onAutoLinkedRef = useRef(onAutoLinked);
  useEffect(() => {
    onAutoLinkedRef.current = onAutoLinked;
  });

  useEffect(() => {
    const controller = new AbortController();
    fetch(
      `/api/transfers/suggestions?transactionId=${encodeURIComponent(transactionId)}`,
      { signal: controller.signal }
    )
      .then((res) => (res.ok ? res.json() : null))
      .then((data: { suggestions: unknown[]; autoLinked: string[] } | null) => {
        if (!controller.signal.aborted) {
          setSuggested((data?.suggestions.length ?? 0) > 0);
          if ((data?.autoLinked.length ?? 0) > 0) onAutoLinkedRef.current();
        }
      })
      .catch(() => {});
    return () => controller.abort();
  }, [transactionId]);

  if (!suggested) return null;
  return (
    <Link
      href={`/transfers#${SUGGESTIONS_ANCHOR}`}
      className="block text-xs text-muted-foreground underline-offset-4 hover:underline"
    >
      Parece una transferencia interna
    </Link>
  );
}

/** Clipboard API exists only in secure contexts; false while server rendering. */
function useCanCopy(): boolean {
  return useSyncExternalStore(
    noSubscribe,
    () => typeof navigator !== "undefined" && !!navigator.clipboard,
    () => false
  );
}

function CopyableCode({ value, label }: { value: string; label: string }) {
  const canCopy = useCanCopy();
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const timer = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(timer);
  }, [copied]);

  const copy = () => {
    navigator.clipboard
      .writeText(value)
      .then(() => setCopied(true))
      .catch(() => {});
  };

  return (
    <span className="inline-flex max-w-full items-start gap-1">
      <code className="min-w-0 font-mono text-xs break-all">{value}</code>
      {canCopy && (
        <Button
          type="button"
          variant="ghost"
          size="icon-xs"
          className="-mt-0.5 shrink-0"
          onClick={copy}
          aria-label={copied ? "Copiado" : `Copiar ${label}`}
          title={copied ? "Copiado" : "Copiar"}
        >
          {copied ? <Check /> : <Copy />}
        </Button>
      )}
    </span>
  );
}

/** "enero de 2026" for a plain YYYY-MM-DD month start. */
function formatMonthEs(date: string): string {
  const [year, month] = date.split("-").map(Number);
  return new Date(year, month - 1, 1).toLocaleDateString("es-CL", {
    month: "long",
    year: "numeric",
  });
}
