"use client";

import { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Link2 } from "lucide-react";
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
import { formatCLP, formatPlainDateEs } from "@/lib/utils";
import { hrefWithSearchParam, TXN_PARAM } from "@/lib/transaction-selection";
import {
  SUGGESTIONS_ANCHOR,
  type SuggestionProductLeg,
  type SuggestionTransactionLeg,
  type TransferSuggestion,
} from "@/lib/transfer-suggestions";

type Leg = SuggestionTransactionLeg | SuggestionProductLeg;

/** One leg: its product (linked to the product page) and, for a scraped row,
 *  the row itself, which opens in the product's detail panel. */
function LegCell({ leg }: { leg: Leg }) {
  const productHref = `/institutions/${leg.institutionSlug}/${leg.productSlug}`;
  return (
    <>
      <Link href={productHref} className="hover:underline">
        <span className="block text-xs text-muted-foreground">
          {leg.institutionName}
        </span>
        {leg.productName}
      </Link>
      {leg.transactionId === null ? (
        <span className="mt-1 block text-xs text-muted-foreground">
          Sin movimientos
        </span>
      ) : (
        <Link
          href={hrefWithSearchParam(productHref, "", TXN_PARAM, leg.transactionId)}
          className="mt-1 flex items-center gap-1 text-xs text-muted-foreground hover:underline"
          title={leg.description}
        >
          <Link2 className="h-3 w-3 shrink-0" />
          <span className="truncate">
            {formatPlainDateEs(leg.date)} · {leg.description}
          </span>
        </Link>
      )}
    </>
  );
}

/** The confirm or dismiss body for a suggestion. */
function confirmBody(s: TransferSuggestion) {
  return s.kind === "pair"
    ? {
        sourceTransactionId: s.source.transactionId,
        targetTransactionId: s.target.transactionId,
      }
    : {
        sourceTransactionId: s.source.transactionId,
        targetProductId: s.target.productId,
      };
}

function dismissBody(s: TransferSuggestion) {
  return {
    sourceTransactionId: s.source.transactionId,
    targetTransactionId: s.kind === "pair" ? s.target.transactionId : null,
  };
}

/**
 * "Sugeridas": rows that look like internal transfers (GET
 * /api/transfers/suggestions), each with its evidence and Confirmar /
 * Descartar. Nothing is linked until the user confirms. Hidden while there is
 * nothing to suggest. `reloadKey` refetches when the page's transfers change
 * (a deleted or unlinked transfer can bring a suggestion back); `onConfirmed`
 * lets the page reload its transfer lists.
 */
export function TransferSuggestionsCard({
  reloadKey,
  onConfirmed,
}: {
  reloadKey: number;
  onConfirmed: () => void;
}) {
  const [suggestions, setSuggestions] = useState<TransferSuggestion[]>([]);
  const [loadFailed, setLoadFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<Record<string, string>>({});

  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/transfers/suggestions", { signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        return res.json();
      })
      .then((data: { suggestions: TransferSuggestion[] }) => {
        setSuggestions(data.suggestions);
        setLoadFailed(false);
      })
      .catch(() => {
        if (!controller.signal.aborted) setLoadFailed(true);
      });
    return () => controller.abort();
  }, [reloadKey, attempt]);

  // A `#sugeridas` link (the transaction detail panel's hint) names a card
  // that only renders once the list has loaded: scroll to it then, once.
  const scrolledToHash = useRef(false);
  useEffect(() => {
    if (scrolledToHash.current || suggestions.length === 0) return;
    if (window.location.hash !== `#${SUGGESTIONS_ANCHOR}`) return;
    scrolledToHash.current = true;
    document.getElementById(SUGGESTIONS_ANCHOR)?.scrollIntoView();
  }, [suggestions]);

  const act = async (
    s: TransferSuggestion,
    action: "confirm" | "dismiss"
  ) => {
    setBusy(true);
    setErrors((previous) => {
      const next = { ...previous };
      delete next[s.id];
      return next;
    });
    try {
      const res = await fetch(`/api/transfers/suggestions/${action}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(action === "confirm" ? confirmBody(s) : dismissBody(s)),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        setErrors((previous) => ({
          ...previous,
          [s.id]:
            data?.error ??
            (action === "confirm"
              ? "No se pudo confirmar la sugerencia"
              : "No se pudo descartar la sugerencia"),
        }));
        return;
      }
      if (action === "confirm") onConfirmed();
      setAttempt((n) => n + 1);
    } catch {
      setErrors((previous) => ({
        ...previous,
        [s.id]: "No se pudo conectar con el servidor",
      }));
    } finally {
      setBusy(false);
    }
  };

  if (loadFailed) {
    return (
      <Card id={SUGGESTIONS_ANCHOR}>
        <CardHeader>
          <CardTitle>Sugeridas</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-destructive">
            No se pudieron cargar las sugerencias.{" "}
            <button
              className="underline underline-offset-4"
              onClick={() => setAttempt((n) => n + 1)}
            >
              Reintentar
            </button>
          </p>
        </CardContent>
      </Card>
    );
  }
  if (suggestions.length === 0) return null;

  // A row with several possible partners shows once per partner; say so, so
  // the user knows to pick one (confirming it drops the others).
  const perSource = new Map<string, number>();
  for (const s of suggestions) {
    const id = s.source.transactionId;
    perSource.set(id, (perSource.get(id) ?? 0) + 1);
  }

  return (
    <Card id={SUGGESTIONS_ANCHOR}>
      <CardHeader>
        <CardTitle>Sugeridas</CardTitle>
        <CardDescription>
          Cargos y abonos que parecen movimientos entre tus productos, de los
          últimos 90 días. Nada se vincula hasta que confirmes: al confirmar se
          crea el movimiento resuelto y sus transacciones dejan de contar en
          Gastos e Ingresos
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Desde</TableHead>
              <TableHead>Hacia</TableHead>
              <TableHead className="text-right">Monto</TableHead>
              <TableHead>Evidencia</TableHead>
              <TableHead className="w-48"></TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {suggestions.map((s) => {
              const options = perSource.get(s.source.transactionId) ?? 1;
              return (
                <TableRow key={s.id}>
                  <TableCell className="max-w-64">
                    <LegCell leg={s.source} />
                  </TableCell>
                  <TableCell className="max-w-64">
                    <LegCell leg={s.target} />
                  </TableCell>
                  <TableCell className="text-right font-medium">
                    {formatCLP(s.amount)}
                  </TableCell>
                  <TableCell className="max-w-80">
                    <div className="flex flex-wrap gap-1">
                      {s.evidence.map((item) => (
                        <Badge
                          key={item}
                          variant="secondary"
                          className="whitespace-normal"
                        >
                          {item}
                        </Badge>
                      ))}
                    </div>
                    <span className="mt-1 block text-xs text-muted-foreground">
                      Puntaje {s.score}
                      {options > 1 && ` · una de ${options} opciones para este cargo`}
                    </span>
                  </TableCell>
                  <TableCell>
                    <div className="flex items-center gap-2">
                      <Button
                        size="sm"
                        disabled={busy}
                        onClick={() => act(s, "confirm")}
                      >
                        Confirmar
                      </Button>
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={busy}
                        onClick={() => act(s, "dismiss")}
                      >
                        Descartar
                      </Button>
                    </div>
                    {errors[s.id] && (
                      <p className="mt-1 text-xs text-destructive">
                        {errors[s.id]}
                      </p>
                    )}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
