"use client";

import { useEffect, useState } from "react";
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
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { formatCLP, cn } from "@/lib/utils";
import { Trash2, Check } from "lucide-react";

interface InternalTransfer {
  id: string;
  description: string;
  amount: number;
  fromProductId: string | null;
  toProductId: string | null;
  transferDate: string;
  status: string;
  notes: string | null;
}

export default function TransfersPage() {
  const [transfers, setTransfers] = useState<InternalTransfer[]>([]);
  const [description, setDescription] = useState("");
  const [amount, setAmount] = useState("");
  const [transferDate, setTransferDate] = useState(
    new Date().toISOString().split("T")[0]
  );
  const [notes, setNotes] = useState("");
  const [saving, setSaving] = useState(false);

  const loadTransfers = () => {
    fetch("/api/transfers")
      .then((res) => res.json())
      .then(setTransfers)
      .catch(console.error);
  };

  useEffect(() => {
    loadTransfers();
  }, []);

  const handleAdd = async () => {
    if (!description || !amount) return;
    setSaving(true);
    try {
      await fetch("/api/transfers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          description,
          amount: parseInt(amount),
          transferDate,
          notes: notes || null,
        }),
      });
      setDescription("");
      setAmount("");
      setNotes("");
      loadTransfers();
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
            Autoprestamos y movimientos entre cuentas propias
          </CardDescription>
        </CardHeader>
        <CardContent>
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
            <Button onClick={handleAdd} disabled={saving}>
              {saving ? "..." : "Agregar"}
            </Button>
          </div>
          <div className="mt-2">
            <Input
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="Notas (opcional)"
            />
          </div>
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
                  <TableHead>Fecha</TableHead>
                  <TableHead>Descripcion</TableHead>
                  <TableHead className="text-right">Monto</TableHead>
                  <TableHead>Notas</TableHead>
                  <TableHead className="w-24"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {pending.map((t) => (
                  <TableRow key={t.id}>
                    <TableCell className="whitespace-nowrap">
                      {new Date(t.transferDate).toLocaleDateString("es-CL")}
                    </TableCell>
                    <TableCell>{t.description}</TableCell>
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
                  <TableHead>Fecha</TableHead>
                  <TableHead>Descripcion</TableHead>
                  <TableHead className="text-right">Monto</TableHead>
                  <TableHead>Estado</TableHead>
                  <TableHead className="w-10"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {resolved.map((t) => (
                  <TableRow key={t.id} className="opacity-60">
                    <TableCell className="whitespace-nowrap">
                      {new Date(t.transferDate).toLocaleDateString("es-CL")}
                    </TableCell>
                    <TableCell>{t.description}</TableCell>
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
