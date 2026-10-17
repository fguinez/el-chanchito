"use client";

// The editable form the detail panel shows when "Editar" is pressed. It edits
// the fields the PATCH /api/transactions/[id] route accepts and sends only the
// ones that changed, so (for instance) a category left as it was does not get
// marked manual. On success the panel reloads from the server.

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SELECT_CLASS } from "@/components/ui/native-select";
import { parseNumberInput } from "@/lib/utils";
import type { TransactionDetail } from "@/app/api/transactions/[id]/route";
import {
  ProductSelect,
  useActiveProducts,
  useCategories,
} from "@/components/transactions/pickers";

/** "YYYY-MM-DD" -> "YYYY-MM" for an <input type="month">, "" when unset. */
function toMonthInput(day: string | null): string {
  return day ? day.slice(0, 7) : "";
}

export function TransactionEditForm({
  txn,
  onSaved,
  onCancel,
}: {
  txn: TransactionDetail;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const categories = useCategories();
  // Products of any currency so the row's own product is always selectable.
  const { institutions } = useActiveProducts();

  const [description, setDescription] = useState(txn.description);
  const [amount, setAmount] = useState(String(Math.abs(txn.amount)));
  const [isExpense, setIsExpense] = useState(txn.amount < 0);
  const [transactionDate, setTransactionDate] = useState(txn.transactionDate);
  const [scheduledMonth, setScheduledMonth] = useState(
    toMonthInput(txn.scheduledMonth)
  );
  const [categoryId, setCategoryId] = useState(txn.categoryId ?? "");
  const [isInternalTransfer, setIsInternalTransfer] = useState(
    txn.isInternalTransfer
  );
  const [productId, setProductId] = useState(txn.productId);

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    const parsedAmount = parseNumberInput(amount);
    if (!description.trim() || parsedAmount === null) {
      setError("Revisa la descripción y el monto");
      return;
    }
    const signedAmount =
      (isExpense ? -1 : 1) * Math.abs(Math.round(parsedAmount));

    // Only the fields that changed, so an unchanged category is not re-marked.
    const body: Record<string, unknown> = {};
    if (description.trim() !== txn.description) body.description = description.trim();
    if (signedAmount !== txn.amount) body.amount = signedAmount;
    if (transactionDate !== txn.transactionDate)
      body.transactionDate = transactionDate;
    const nextMonth = scheduledMonth ? `${scheduledMonth}-01` : null;
    if (nextMonth !== txn.scheduledMonth) body.scheduledMonth = nextMonth;
    const nextCategory = categoryId || null;
    if (nextCategory !== txn.categoryId) body.categoryId = nextCategory;
    if (isInternalTransfer !== txn.isInternalTransfer)
      body.isInternalTransfer = isInternalTransfer;
    if (productId !== txn.productId) body.productId = productId;

    if (Object.keys(body).length === 0) {
      onCancel();
      return;
    }

    setSaving(true);
    setError(null);
    try {
      const res = await fetch(
        `/api/transactions/${encodeURIComponent(txn.id)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      );
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        setError(data?.error ?? "No se pudo guardar");
        return;
      }
      onSaved();
    } catch {
      setError("No se pudo guardar");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-3">
      <div>
        <label className="mb-1 block text-sm text-muted-foreground">
          Descripción
        </label>
        <Input
          value={description}
          onChange={(e) => setDescription(e.target.value)}
        />
      </div>

      <div className="flex items-end gap-2">
        <div className="flex-1">
          <label className="mb-1 block text-sm text-muted-foreground">
            Monto ({txn.product.currency})
          </label>
          <Input
            type="number"
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
          />
        </div>
        <div className="flex gap-1">
          <Button
            type="button"
            size="sm"
            variant={isExpense ? "default" : "outline"}
            onClick={() => setIsExpense(true)}
          >
            Gasto
          </Button>
          <Button
            type="button"
            size="sm"
            variant={isExpense ? "outline" : "default"}
            onClick={() => setIsExpense(false)}
          >
            Ingreso
          </Button>
        </div>
      </div>

      <div className="flex gap-2">
        <div className="flex-1">
          <label className="mb-1 block text-sm text-muted-foreground">
            Fecha
          </label>
          <Input
            type="date"
            value={transactionDate}
            onChange={(e) => setTransactionDate(e.target.value)}
          />
        </div>
        <div className="flex-1">
          <label className="mb-1 block text-sm text-muted-foreground">
            Mes asignado
          </label>
          <Input
            type="month"
            value={scheduledMonth}
            onChange={(e) => setScheduledMonth(e.target.value)}
          />
        </div>
      </div>

      <div>
        <label className="mb-1 block text-sm text-muted-foreground">
          Categoría
        </label>
        <select
          className={SELECT_CLASS}
          value={categoryId}
          onChange={(e) => setCategoryId(e.target.value)}
        >
          <option value="">Sin categoría</option>
          {categories.map((c) => (
            <option key={c.id} value={c.id}>
              {c.name}
            </option>
          ))}
        </select>
      </div>

      <div>
        <label className="mb-1 block text-sm text-muted-foreground">
          Producto
        </label>
        <ProductSelect
          value={productId}
          onChange={setProductId}
          institutions={institutions}
          emptyLabel="Selecciona un producto"
        />
      </div>

      <label className="flex items-center gap-2 text-sm">
        <input
          type="checkbox"
          checked={isInternalTransfer}
          onChange={(e) => setIsInternalTransfer(e.target.checked)}
        />
        Transferencia interna
      </label>

      {error && <p className="text-sm text-destructive">{error}</p>}

      <div className="flex gap-2 pt-1">
        <Button size="sm" onClick={save} disabled={saving}>
          {saving ? "Guardando..." : "Guardar"}
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={onCancel}
          disabled={saving}
        >
          Cancelar
        </Button>
      </div>
    </div>
  );
}
