"use client";

import { useCallback, useEffect, useState } from "react";
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
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { SELECT_CLASS } from "@/components/ui/native-select";
import { useSortableData } from "@/lib/use-sortable-data";
import { formatCLP } from "@/lib/utils";
import { TRANSFER_CURRENCY, type TransferProductRef } from "@/lib/transfers";
import { Trash2, Check } from "lucide-react";

interface InternalTransfer {
  id: string;
  description: string;
  amount: number;
  fromProductId: string | null;
  toProductId: string | null;
  fromProduct: TransferProductRef | null;
  toProduct: TransferProductRef | null;
  transferDate: string;
  status: string;
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

/** A transfer endpoint, linked to its product page; "-" on legacy rows. */
function ProductCell({ product }: { product: TransferProductRef | null }) {
  if (!product) return <span className="text-muted-foreground">-</span>;
  return (
    <Link
      href={`/institutions/${product.institutionSlug}/${product.slug}`}
      className="hover:underline"
    >
      <span className="block text-xs text-muted-foreground">
        {product.institutionName}
      </span>
      {product.name}
    </Link>
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
    if (!canAdd) return;
    setSaving(true);
    setError(null);
    try {
      const res = await fetch("/api/transfers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          description,
          amount: parseInt(amount),
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
    loadTransfers();
  };

  const pending = transfers.filter((t) => t.status === "pending");
  const resolved = transfers.filter((t) => t.status === "resolved");
  const pendingTotal = pending.reduce((sum, t) => sum + t.amount, 0);

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
  } = useSortableData(pending, getValue);
  const {
    sorted: resolvedSorted,
    sort: resolvedSort,
    toggleSort: toggleResolved,
  } = useSortableData(resolved, getValue);
  // Bridge the generic header's string key to our typed key union.
  const handlePendingSort = (key: string) =>
    togglePending(key as TransferSortKey);
  const handleResolvedSort = (key: string) =>
    toggleResolved(key as TransferSortKey);

  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-bold">Movimientos Internos</h2>

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

      {/* Pending transfers */}
      <Card>
        <CardHeader>
          <CardTitle>Pendientes</CardTitle>
          <CardDescription>
            Movimientos que deben ser devueltos o regularizados
          </CardDescription>
        </CardHeader>
        <CardContent>
          {pending.length === 0 ? (
            <p className="text-muted-foreground">
              No hay movimientos pendientes.
            </p>
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
                  <TableHead className="w-24"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {pendingSorted.map((t) => (
                  <TableRow key={t.id}>
                    <TableCell className="whitespace-nowrap">
                      {new Date(t.transferDate).toLocaleDateString("es-CL")}
                    </TableCell>
                    <TableCell>{t.description}</TableCell>
                    <TableCell>
                      <ProductCell product={t.fromProduct} />
                    </TableCell>
                    <TableCell>
                      <ProductCell product={t.toProduct} />
                    </TableCell>
                    <TableCell className="text-right font-medium">
                      {formatCLP(t.amount)}
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {t.notes || "-"}
                    </TableCell>
                    <TableCell>
                      <div className="flex gap-1">
                        <button
                          onClick={() => handleResolve(t.id)}
                          className="text-muted-foreground hover:text-green-600"
                          title="Marcar como resuelto"
                        >
                          <Check className="h-4 w-4" />
                        </button>
                        <button
                          onClick={() => handleDelete(t.id)}
                          className="text-muted-foreground hover:text-destructive"
                          title="Eliminar"
                        >
                          <Trash2 className="h-4 w-4" />
                        </button>
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
                  <TableHead className="w-10"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {resolvedSorted.map((t) => (
                  <TableRow key={t.id} className="opacity-60">
                    <TableCell className="whitespace-nowrap">
                      {new Date(t.transferDate).toLocaleDateString("es-CL")}
                    </TableCell>
                    <TableCell>{t.description}</TableCell>
                    <TableCell>
                      <ProductCell product={t.fromProduct} />
                    </TableCell>
                    <TableCell>
                      <ProductCell product={t.toProduct} />
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
                      <button
                        onClick={() => handleDelete(t.id)}
                        className="text-muted-foreground hover:text-destructive"
                      >
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </div>
  );
}
