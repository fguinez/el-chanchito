"use client";

// The editable form the detail panel shows when "Editar" is pressed. It edits
// the fields the PATCH /api/transactions/[id] route accepts and sends only the
// ones that changed, so (for instance) a category left as it was does not get
// marked manual. The opt-in "apply to every transaction with this description"
// sends the category through POST /api/transactions/[id]/category-rule instead,
// which also keeps a rule for it. On success the panel reloads from the server.

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { SELECT_CLASS } from "@/components/ui/native-select";
import { parseNumberInput } from "@/lib/utils";
import { RULE_KEYWORD_MAX_LENGTH } from "@/lib/categories";
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
  /** `notice`: what the save did beyond the row, for the panel to show. */
  onSaved: (notice?: string) => void;
  onCancel: () => void;
}) {
  const categories = useCategories();
  // Products of any currency so the row's own product is always selectable.
  const { institutions } = useActiveProducts();

  // A scraped row (it has an external id) keeps the bank's `description`
  // immutable; the box edits the one-time custom override instead (V034).
  const scraped = txn.externalId !== null && txn.externalId !== "";
  const initialDescription = scraped
    ? (txn.customDescription ?? "")
    : txn.description;
  const [description, setDescription] = useState(initialDescription);
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
  const [applyToSame, setApplyToSame] = useState(false);
  // The rule keys on the raw scraped description, which must fit a keyword.
  const ruleKeyword = txn.description.trim();
  const keywordFits =
    ruleKeyword.length > 0 && ruleKeyword.length <= RULE_KEYWORD_MAX_LENGTH;
  // Not while the description is being edited: the rule would key on the
  // text from before this save.
  const descriptionEdited = description !== initialDescription;
  const ruleAllowed = keywordFits && !descriptionEdited;
  const withRule = applyToSame && ruleAllowed && categoryId !== "";

  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const save = async () => {
    const parsedAmount = parseNumberInput(amount);
    // A scraped row may leave the box empty (that clears the custom override and
    // restores the bank's text); a manual row still needs a description.
    if ((!scraped && !description.trim()) || parsedAmount === null) {
      setError("Revisa la descripción y el monto");
      return;
    }
    // CLP rows are whole pesos; other currencies (USD, V023) keep their cents.
    const magnitude =
      txn.product.currency === "CLP"
        ? Math.round(Math.abs(parsedAmount))
        : Math.round(Math.abs(parsedAmount) * 100) / 100;
    const signedAmount = (isExpense ? -1 : 1) * magnitude;

    // Only the fields that changed, so an unchanged category is not re-marked.
    const body: Record<string, unknown> = {};
    const trimmedDescription = description.trim();
    if (scraped) {
      // Send the override only when it changed; empty clears it (null) so the
      // row falls back to the bank's original. Never touch `description`.
      if (trimmedDescription !== (txn.customDescription ?? "")) {
        body.customDescription = trimmedDescription || null;
      }
    } else if (trimmedDescription !== txn.description) {
      body.description = trimmedDescription;
    }
    if (signedAmount !== txn.amount) body.amount = signedAmount;
    if (transactionDate !== txn.transactionDate)
      body.transactionDate = transactionDate;
    const nextMonth = scheduledMonth ? `${scheduledMonth}-01` : null;
    if (nextMonth !== txn.scheduledMonth) body.scheduledMonth = nextMonth;
    const nextCategory = categoryId || null;
    if (!withRule && nextCategory !== txn.categoryId)
      body.categoryId = nextCategory;
    if (isInternalTransfer !== txn.isInternalTransfer)
      body.isInternalTransfer = isInternalTransfer;
    if (productId !== txn.productId) body.productId = productId;

    if (Object.keys(body).length === 0 && !withRule) {
      onCancel();
      return;
    }

    setSaving(true);
    setError(null);
    try {
      const base = `/api/transactions/${encodeURIComponent(txn.id)}`;
      const patched = Object.keys(body).length > 0;
      if (patched) {
        const res = await fetch(base, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!res.ok) {
          const data = await res.json().catch(() => null);
          setError(data?.error ?? "No se pudo guardar");
          return;
        }
      }
      if (!withRule) {
        onSaved();
        return;
      }
      const res = await fetch(`${base}/category-rule`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ categoryId }),
      });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        const reason = data?.error ?? "error del servidor";
        // The PATCH already landed: close and reload, and say what failed.
        if (patched) {
          onSaved(`Cambios guardados; no se pudo crear la regla: ${reason}`);
        } else {
          setError(`No se pudo crear la regla: ${reason}`);
        }
        return;
      }
      onSaved(ruleNotice(data.ruleCreated, data.updatedCount));
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
          {scraped ? "Descripción personalizada" : "Descripción"}
        </label>
        <Input
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          placeholder={scraped ? txn.description : undefined}
        />
        {scraped && (
          <p className="mt-1 text-xs text-muted-foreground">
            Deja vacío para usar la descripción original del banco.
          </p>
        )}
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
        {categoryId !== "" && (
          <label className="mt-2 flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              className="mt-0.5"
              checked={applyToSame}
              disabled={!ruleAllowed}
              onChange={(e) => setApplyToSame(e.target.checked)}
            />
            <span>
              Aplicar a todas las transacciones con esta descripción
              <span className="block text-xs text-muted-foreground">
                {!keywordFits
                  ? ruleKeyword.length === 0
                    ? "La descripción original está vacía; no se puede crear una regla."
                    : `La descripción supera ${RULE_KEYWORD_MAX_LENGTH} caracteres; crea la regla en Categorías.`
                  : descriptionEdited
                    ? "Guarda primero el cambio de descripción para crear la regla."
                    : `Crea una regla exacta para "${ruleKeyword}" y la aplica a las existentes y futuras (no cambia las categorizadas a mano).`}
              </span>
            </span>
          </label>
        )}
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

/** "Regla creada; 2 transacciones similares actualizadas." */
function ruleNotice(created: boolean, updated: number): string {
  const rule = created
    ? "Regla exacta creada"
    : "Se reutilizó la regla exacta existente con esta descripción (ahora apunta a esta categoría)";
  const rows =
    updated === 1
      ? "1 transacción similar actualizada"
      : `${updated} transacciones similares actualizadas`;
  return `${rule}; ${rows}.`;
}
