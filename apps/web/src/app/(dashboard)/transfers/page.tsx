"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
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
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { Input } from "@/components/ui/input";
import { SearchInput } from "@/components/ui/search-input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { SELECT_CLASS } from "@/components/ui/native-select";
import { useSortableData } from "@/lib/use-sortable-data";
import { cn, formatCLP, formatPlainDateEs, parseNumberInput } from "@/lib/utils";
import {
  TRANSFER_CURRENCY,
  transferMatchesSearch,
  type TransferLinkedTransaction,
  type TransferProductRef,
} from "@/lib/transfers";
import { descriptionTitle, searchTokens } from "@/lib/transaction-search";
import { hrefWithSearchParam, TXN_PARAM } from "@/lib/transaction-selection";
import { TransferLinksDialog } from "@/components/transfers/transfer-links-dialog";
import { TransferSuggestionsCard } from "@/components/transfers/transfer-suggestions-card";
import { InstitutionLogo } from "@/components/institutions/institution-logo";
import { Trash2, Check, Link2, Undo2 } from "lucide-react";

interface InternalTransfer {
  id: string;
  description: string;
  amount: number;
  fromProductId: string | null;
  toProductId: string | null;
  fromProduct: TransferProductRef | null;
  toProduct: TransferProductRef | null;
  /** The scraped rows this transfer accounts for (null = unlinked). */
  fromTransactionId: string | null;
  toTransactionId: string | null;
  fromTransaction: TransferLinkedTransaction | null;
  toTransaction: TransferLinkedTransaction | null;
  transferDate: string;
  status: string;
  /** Linked by the suggestions on their own (V032). */
  autoLinked: boolean;
  notes: string | null;
}

// Picker data, built from GET /api/institutions (active CLP products only).
interface PickerInstitution {
  slug: string;
  name: string;
  products: { id: string; name: string }[];
}
interface InstitutionsResponse {
  institutions: {
    slug: string;
    name: string;
    products: {
      id: string;
      name: string;
      currency: string;
      isActive: boolean;
    }[];
  }[];
}

type TransferSortKey =
  | "fecha"
  | "descripcion"
  | "desde"
  | "hacia"
  | "monto"
  | "notas"
  | "estado";

function productLabel(p: TransferProductRef | null): string | null {
  return p ? `${p.institutionName} · ${p.name}` : null;
}

/** A transfer endpoint, linked to its product page ("-" on legacy rows), and
 *  under it the transaction it links (its display description; the original
 *  in the tooltip), which opens in the product's detail panel. */
function ProductCell({
  product,
  transaction,
}: {
  product: TransferProductRef | null;
  transaction: TransferLinkedTransaction | null;
}) {
  if (!product) return <span className="text-muted-foreground">-</span>;
  const productHref = `/institutions/${product.institutionSlug}/${product.slug}`;
  return (
    <>
      <Link href={productHref} className="hover:underline">
        <span className="flex items-center gap-1 text-xs text-muted-foreground">
          <InstitutionLogo slug={product.institutionSlug} size="sm" />
          {product.institutionName}
        </span>
        {product.name}
      </Link>
      {transaction && (
        <Link
          href={hrefWithSearchParam(productHref, "", TXN_PARAM, transaction.id)}
          className="mt-1 flex items-center gap-1 text-xs text-muted-foreground hover:underline"
          title={descriptionTitle(
            transaction.displayDescription,
            transaction.description
          )}
        >
          <Link2 className="h-3 w-3 shrink-0" />
          <span className="truncate">
            {formatPlainDateEs(transaction.transactionDate)} ·{" "}
            {transaction.displayDescription}
          </span>
        </Link>
      )}
    </>
  );
}

/** The description, with an "Automática" badge on a transfer the suggestions
 *  linked on their own. */
function DescriptionCell({ transfer }: { transfer: InternalTransfer }) {
  return (
    <>
      {transfer.description}
      {transfer.autoLinked && (
        <Badge variant="secondary" className="ml-2">
          Automática
        </Badge>
      )}
    </>
  );
}

/** Delete, or for an automatic transfer its undo: deleting it also dismisses
 *  the pair, so it is not linked again. */
function DeleteButton({
  transfer,
  onDelete,
}: {
  transfer: InternalTransfer;
  onDelete: (id: string) => void;
}) {
  if (transfer.autoLinked) {
    return (
      <button
        onClick={() => onDelete(transfer.id)}
        className="text-muted-foreground hover:text-destructive"
        title="Deshacer vínculo automático"
        aria-label={`Deshacer vínculo automático: ${transfer.description}`}
      >
        <Undo2 className="h-4 w-4" />
      </button>
    );
  }
  return (
    <button
      onClick={() => onDelete(transfer.id)}
      className="text-muted-foreground hover:text-destructive"
      title="Eliminar"
    >
      <Trash2 className="h-4 w-4" />
    </button>
  );
}

function ProductSelect({
  label,
  value,
  onChange,
  institutions,
}: {
  label: string;
  value: string;
  onChange: (id: string) => void;
  institutions: PickerInstitution[];
}) {
  return (
    <div>
      <label className="mb-1 block text-sm text-muted-foreground">{label}</label>
      <select
        className={SELECT_CLASS}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">Selecciona un producto</option>
        {institutions.map((inst) => (
          <optgroup key={inst.slug} label={inst.name}>
            {inst.products.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </optgroup>
        ))}
      </select>
    </div>
  );
}

export default function TransfersPage() {
  const [transfers, setTransfers] = useState<InternalTransfer[]>([]);
  const [institutions, setInstitutions] = useState<PickerInstitution[]>([]);
  const [pickerError, setPickerError] = useState(false);
  const [fromProductId, setFromProductId] = useState("");
  const [toProductId, setToProductId] = useState("");
  const [description, setDescription] = useState("");
  const [amount, setAmount] = useState("");
  const [transferDate, setTransferDate] = useState(
    new Date().toISOString().split("T")[0]
  );
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadTransfers = () => {
    fetch("/api/transfers")
      .then((res) => res.json())
      .then(setTransfers)
      .catch(console.error);
  };

  useEffect(() => {
    loadTransfers();
  }, []);

  // Deleting a transfer or changing its links can free rows for the
  // suggestions, so those reload both lists; adding a transfer or resolving
  // one by hand links nothing and reloads only the transfers.
  const [suggestionsKey, setSuggestionsKey] = useState(0);
  const refreshAll = () => {
    loadTransfers();
    setSuggestionsKey((n) => n + 1);
  };

  // A `#transfer-<id>` link (from the transaction detail panel) names a row
  // that only exists once the list has loaded: scroll to it then, once, and
  // highlight it briefly.
  const [highlightId, setHighlightId] = useState<string | null>(null);
  const scrolledToHash = useRef(false);
  useEffect(() => {
    if (scrolledToHash.current || transfers.length === 0) return;
    const hash = window.location.hash.slice(1);
    if (!hash.startsWith("transfer-")) return;
    const row = document.getElementById(hash);
    if (!row) return;
    scrolledToHash.current = true;
    row.scrollIntoView({ block: "center" });
    const id = hash.slice("transfer-".length);
    // Deferred so the highlight is not a synchronous setState in the effect.
    const show = window.setTimeout(() => setHighlightId(id), 0);
    const hide = window.setTimeout(() => setHighlightId(null), 2500);
    return () => {
      window.clearTimeout(show);
      window.clearTimeout(hide);
    };
  }, [transfers]);

  useEffect(() => {
    fetch("/api/institutions")
      .then((res) => {
        if (!res.ok) throw new Error("failed");
        return res.json();
      })
      .then((data: InstitutionsResponse) => {
        setInstitutions(
          data.institutions
            .map((inst) => ({
              slug: inst.slug,
              name: inst.name,
              products: inst.products
                .filter((p) => p.isActive && p.currency === TRANSFER_CURRENCY)
                .map((p) => ({ id: p.id, name: p.name })),
            }))
            .filter((inst) => inst.products.length > 0)
        );
      })
      .catch(() => setPickerError(true));
  }, []);

  const sameProduct = fromProductId !== "" && fromProductId === toProductId;
  const canAdd =
    !!description &&
    !!amount &&
    !!fromProductId &&
    !!toProductId &&
    !sameProduct;

  const handleAdd = async () => {
    const parsedAmount = parseNumberInput(amount);
    if (!canAdd || parsedAmount === null) return;
    setSaving(true);
    setError(null);
    try {
      const res = await fetch("/api/transfers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          description,
          amount: Math.round(parsedAmount),
          fromProductId,
          toProductId,
          transferDate,
          notes: notes || null,
        }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        setError(data?.error ?? "No se pudo registrar el movimiento");
        return;
      }
      setDescription("");
      setAmount("");
      setNotes("");
      loadTransfers();
    } catch {
      setError("No se pudo registrar el movimiento");
    } finally {
      setSaving(false);
    }
  };

  const handleResolve = async (id: string) => {
    await fetch("/api/transfers", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id, status: "resolved" }),
    });
    loadTransfers();
  };

  const handleDelete = async (id: string) => {
    await fetch("/api/transfers", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    refreshAll();
  };

  const pending = transfers.filter((t) => t.status === "pending");
  const resolved = transfers.filter((t) => t.status === "resolved");
  const pendingTotal = pending.reduce((sum, t) => sum + t.amount, 0);

  // The page's search narrows the suggestions and both tables (the summary
  // keeps every transfer): a transfer stays when each word is in its
  // description or in the original or displayed description of a row it
  // links.
  const [search, setSearch] = useState("");
  const tokens = useMemo(() => searchTokens(search), [search]);
  const pendingShown = pending.filter((t) => transferMatchesSearch(tokens, t));
  const resolvedShown = resolved.filter((t) => transferMatchesSearch(tokens, t));
  const noResults = (
    <p className="text-muted-foreground">
      Sin resultados para «{search.trim()}».
    </p>
  );

  // Shared value extractor: the two tables' columns overlap, so one getValue
  // covers both. Each table keeps its own independent sort state below.
  const getValue = useCallback(
    (t: InternalTransfer, key: TransferSortKey): string | number | null => {
      switch (key) {
        case "fecha":
          return t.transferDate; // ISO strings sort correctly as strings.
        case "descripcion":
          return t.description;
        case "desde":
          return productLabel(t.fromProduct);
        case "hacia":
          return productLabel(t.toProduct);
        case "monto":
          return t.amount;
        case "notas":
          return t.notes;
        case "estado":
          return t.status;
      }
    },
    []
  );

  const {
    sorted: pendingSorted,
    sort: pendingSort,
    toggleSort: togglePending,
  } = useSortableData(pendingShown, getValue);
  const {
    sorted: resolvedSorted,
    sort: resolvedSort,
    toggleSort: toggleResolved,
  } = useSortableData(resolvedShown, getValue);
  // Bridge the generic header's string key to our typed key union.
  const handlePendingSort = (key: string) =>
    togglePending(key as TransferSortKey);
  const handleResolvedSort = (key: string) =>
    toggleResolved(key as TransferSortKey);

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-2xl font-bold">Movimientos Internos</h2>
        <SearchInput
          className="w-full sm:w-72"
          aria-label="Buscar movimientos por descripción"
          value={search}
          onValueChange={setSearch}
        />
      </div>

      {/* Summary */}
      <div className="grid gap-4 md:grid-cols-2">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Pendientes</CardDescription>
            <CardTitle className="text-xl">
              {pending.length} ({formatCLP(pendingTotal)})
            </CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Resueltos</CardDescription>
            <CardTitle className="text-xl">{resolved.length}</CardTitle>
          </CardHeader>
        </Card>
      </div>

      {/* Add form */}
      <Card>
        <CardHeader>
          <CardTitle>Registrar movimiento</CardTitle>
          <CardDescription>
            Autoprestamos y movimientos entre productos propios (en CLP)
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 md:grid-cols-2">
            <ProductSelect
              label="Desde"
              value={fromProductId}
              onChange={setFromProductId}
              institutions={institutions}
            />
            <ProductSelect
              label="Hacia"
              value={toProductId}
              onChange={setToProductId}
              institutions={institutions}
            />
          </div>
          <div className="flex items-end gap-3">
            <div className="flex-1">
              <label className="mb-1 block text-sm text-muted-foreground">
                Descripcion
              </label>
              <Input
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder="Ej: Prestamo de MercadoPago a BanChile"
              />
            </div>
            <div className="w-36">
              <label className="mb-1 block text-sm text-muted-foreground">
                Monto
              </label>
              <Input
                type="number"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                placeholder="200000"
              />
            </div>
            <div className="w-40">
              <label className="mb-1 block text-sm text-muted-foreground">
                Fecha
              </label>
              <Input
                type="date"
                value={transferDate}
                onChange={(e) => setTransferDate(e.target.value)}
              />
            </div>
            <Button onClick={handleAdd} disabled={saving || !canAdd}>
              {saving ? "..." : "Agregar"}
            </Button>
          </div>
          <Input
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Notas (opcional)"
          />
          {pickerError && (
            <p className="text-sm text-destructive">
              No se pudieron cargar los productos; recarga la pagina.
            </p>
          )}
          {(sameProduct || error) && (
            <p className="text-sm text-destructive">
              {sameProduct
                ? "El origen y el destino deben ser productos distintos."
                : error}
            </p>
          )}
        </CardContent>
      </Card>

      {/* Suggested transfers (hidden when there are none) */}
      <TransferSuggestionsCard
        reloadKey={suggestionsKey}
        onConfirmed={loadTransfers}
        search={search}
      />

      {/* Pending transfers */}
      <Card>
        <CardHeader>
          <CardTitle>Pendientes</CardTitle>
          <CardDescription>
            Movimientos que deben ser devueltos o regularizados. Vincula el
            cargo y el abono que importaron los scrapers y el movimiento se
            resuelve solo; si un lado no tiene transacciones, márcalo como
            resuelto a mano
          </CardDescription>
        </CardHeader>
        <CardContent>
          {pending.length === 0 ? (
            <p className="text-muted-foreground">
              No hay movimientos pendientes.
            </p>
          ) : pendingShown.length === 0 ? (
            noResults
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableTableHead
                    label="Fecha"
                    columnKey="fecha"
                    active={pendingSort?.key === "fecha"}
                    direction={
                      pendingSort?.key === "fecha"
                        ? pendingSort.direction
                        : undefined
                    }
                    onSort={handlePendingSort}
                  />
                  <SortableTableHead
                    label="Descripcion"
                    columnKey="descripcion"
                    active={pendingSort?.key === "descripcion"}
                    direction={
                      pendingSort?.key === "descripcion"
                        ? pendingSort.direction
                        : undefined
                    }
                    onSort={handlePendingSort}
                  />
                  <SortableTableHead
                    label="Desde"
                    columnKey="desde"
                    active={pendingSort?.key === "desde"}
                    direction={
                      pendingSort?.key === "desde"
                        ? pendingSort.direction
                        : undefined
                    }
                    onSort={handlePendingSort}
                  />
                  <SortableTableHead
                    label="Hacia"
                    columnKey="hacia"
                    active={pendingSort?.key === "hacia"}
                    direction={
                      pendingSort?.key === "hacia"
                        ? pendingSort.direction
                        : undefined
                    }
                    onSort={handlePendingSort}
                  />
                  <SortableTableHead
                    label="Monto"
                    columnKey="monto"
                    align="right"
                    active={pendingSort?.key === "monto"}
                    direction={
                      pendingSort?.key === "monto"
                        ? pendingSort.direction
                        : undefined
                    }
                    onSort={handlePendingSort}
                  />
                  <SortableTableHead
                    label="Notas"
                    columnKey="notas"
                    active={pendingSort?.key === "notas"}
                    direction={
                      pendingSort?.key === "notas"
                        ? pendingSort.direction
                        : undefined
                    }
                    onSort={handlePendingSort}
                  />
                  <TableHead className="w-36"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {pendingSorted.map((t) => (
                  <TableRow
                    key={t.id}
                    id={`transfer-${t.id}`}
                    className={cn(
                      "transition-colors",
                      highlightId === t.id && "bg-muted"
                    )}
                  >
                    <TableCell className="whitespace-nowrap">
                      {formatPlainDateEs(t.transferDate)}
                    </TableCell>
                    <TableCell>
                      <DescriptionCell transfer={t} />
                    </TableCell>
                    <TableCell>
                      <ProductCell
                        product={t.fromProduct}
                        transaction={t.fromTransaction}
                      />
                    </TableCell>
                    <TableCell>
                      <ProductCell
                        product={t.toProduct}
                        transaction={t.toTransaction}
                      />
                    </TableCell>
                    <TableCell className="text-right font-medium">
                      {formatCLP(t.amount)}
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {t.notes || "-"}
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center gap-1">
                        <TransferLinksDialog
                          transfer={t}
                          onSaved={refreshAll}
                        />
                        <button
                          onClick={() => handleResolve(t.id)}
                          className="text-muted-foreground hover:text-green-600"
                          title="Marcar como resuelto"
                        >
                          <Check className="h-4 w-4" />
                        </button>
                        <DeleteButton transfer={t} onDelete={handleDelete} />
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Resolved transfers */}
      {resolved.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Resueltos</CardTitle>
          </CardHeader>
          <CardContent>
            {resolvedShown.length === 0 ? (
              noResults
            ) : (
              <Table>
                <TableHeader>
                  <TableRow>
                    <SortableTableHead
                      label="Fecha"
                      columnKey="fecha"
                      active={resolvedSort?.key === "fecha"}
                      direction={
                        resolvedSort?.key === "fecha"
                          ? resolvedSort.direction
                          : undefined
                      }
                      onSort={handleResolvedSort}
                    />
                    <SortableTableHead
                      label="Descripcion"
                      columnKey="descripcion"
                      active={resolvedSort?.key === "descripcion"}
                      direction={
                        resolvedSort?.key === "descripcion"
                          ? resolvedSort.direction
                          : undefined
                      }
                      onSort={handleResolvedSort}
                    />
                    <SortableTableHead
                      label="Desde"
                      columnKey="desde"
                      active={resolvedSort?.key === "desde"}
                      direction={
                        resolvedSort?.key === "desde"
                          ? resolvedSort.direction
                          : undefined
                      }
                      onSort={handleResolvedSort}
                    />
                    <SortableTableHead
                      label="Hacia"
                      columnKey="hacia"
                      active={resolvedSort?.key === "hacia"}
                      direction={
                        resolvedSort?.key === "hacia"
                          ? resolvedSort.direction
                          : undefined
                      }
                      onSort={handleResolvedSort}
                    />
                    <SortableTableHead
                      label="Monto"
                      columnKey="monto"
                      align="right"
                      active={resolvedSort?.key === "monto"}
                      direction={
                        resolvedSort?.key === "monto"
                          ? resolvedSort.direction
                          : undefined
                      }
                      onSort={handleResolvedSort}
                    />
                    <SortableTableHead
                      label="Estado"
                      columnKey="estado"
                      active={resolvedSort?.key === "estado"}
                      direction={
                        resolvedSort?.key === "estado"
                          ? resolvedSort.direction
                          : undefined
                      }
                      onSort={handleResolvedSort}
                    />
                    <TableHead className="w-28"></TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {resolvedSorted.map((t) => (
                    <TableRow
                      key={t.id}
                      id={`transfer-${t.id}`}
                      className={cn(
                        "opacity-60 transition-colors",
                        highlightId === t.id && "bg-muted opacity-100"
                      )}
                    >
                      <TableCell className="whitespace-nowrap">
                        {formatPlainDateEs(t.transferDate)}
                      </TableCell>
                      <TableCell>
                        <DescriptionCell transfer={t} />
                      </TableCell>
                      <TableCell>
                        <ProductCell
                          product={t.fromProduct}
                          transaction={t.fromTransaction}
                        />
                      </TableCell>
                      <TableCell>
                        <ProductCell
                          product={t.toProduct}
                          transaction={t.toTransaction}
                        />
                      </TableCell>
                      <TableCell className="text-right">
                        {formatCLP(t.amount)}
                      </TableCell>
                      <TableCell>
                        <Badge variant="outline" className="text-green-600">
                          Resuelto
                        </Badge>
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center gap-1">
                          <TransferLinksDialog
                            transfer={t}
                            onSaved={refreshAll}
                          />
                          <DeleteButton transfer={t} onDelete={handleDelete} />
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
