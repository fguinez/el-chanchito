"use client";

import { useState } from "react";
import { Pencil } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { SELECT_CLASS } from "@/components/ui/native-select";
import { cn } from "@/lib/utils";
import { FIXED_EXPENSE_KINDS, type FixedExpenseKind } from "@/lib/db/schema";
import {
  FIXED_EXPENSE_KIND_LABELS,
  parseFixedExpenseForm,
  ratioInput,
  type FixedExpense,
} from "@/lib/fixed-expenses";

/** Pencil button + dialog editing every field of one budget item. */
export function FixedExpenseEditDialog({
  expense,
  onSaved,
}: {
  expense: FixedExpense;
  onSaved: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState(expense.name);
  const [kind, setKind] = useState<FixedExpenseKind>(expense.kind);
  const [amount, setAmount] = useState(String(expense.amount));
  const [isShared, setIsShared] = useState(expense.isShared);
  const [sharedRatio, setSharedRatio] = useState(ratioInput(expense.sharedRatio));
  const [activeFrom, setActiveFrom] = useState(expense.activeFrom ?? "");
  const [activeTo, setActiveTo] = useState(expense.activeTo ?? "");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  function handleOpenChange(next: boolean) {
    setOpen(next);
    if (!next) return;
    // Start every opening from the expense as it is now.
    setName(expense.name);
    setKind(expense.kind);
    setAmount(String(expense.amount));
    setIsShared(expense.isShared);
    setSharedRatio(ratioInput(expense.sharedRatio));
    setActiveFrom(expense.activeFrom ?? "");
    setActiveTo(expense.activeTo ?? "");
    setError(null);
  }

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    const parsed = parseFixedExpenseForm(
      {
        name,
        kind,
        amount,
        isShared,
        sharedRatio,
        activeFrom,
        activeTo,
      },
      expense.amount
    );
    if (!parsed.ok) {
      setError(parsed.error);
      return;
    }

    setSaving(true);
    setError(null);
    try {
      const res = await fetch("/api/fixed-expenses", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: expense.id, ...parsed.body }),
      });
      if (res.status === 404) {
        setError("Este item ya no existe; recarga la página.");
        return;
      }
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        setError(data?.error ?? "No se pudo guardar el item");
        return;
      }
      setOpen(false);
      onSaved();
    } catch {
      setError("No se pudo guardar el item");
    } finally {
      setSaving(false);
    }
  }

  const idPrefix = `fixed-expense-${expense.id}`;

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <button
          type="button"
          aria-label="Editar"
          className="text-muted-foreground hover:text-foreground"
        >
          <Pencil className="h-4 w-4" />
        </button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Editar item del presupuesto</DialogTitle>
          <DialogDescription>
            Si el item dejó de existir, ponle una fecha de término en vez de
            eliminarlo: se conserva su historia y deja de sumar al presupuesto
            desde el día siguiente.
          </DialogDescription>
        </DialogHeader>

        {/* parseFixedExpenseForm validates; the browser's step checks would
            refuse a stored 0.6550 ratio or a decimal amount. */}
        <form noValidate onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-1.5">
            <label htmlFor={`${idPrefix}-name`} className="text-sm font-medium">
              Nombre
            </label>
            <Input
              id={`${idPrefix}-name`}
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
          </div>

          <div className="flex flex-wrap gap-4">
            <div className="space-y-1.5">
              <label
                htmlFor={`${idPrefix}-kind`}
                className="block text-sm font-medium"
              >
                Tipo
              </label>
              <select
                id={`${idPrefix}-kind`}
                className={cn(SELECT_CLASS, "w-48")}
                value={kind}
                onChange={(e) => setKind(e.target.value as FixedExpenseKind)}
              >
                {FIXED_EXPENSE_KINDS.map((option) => (
                  <option key={option} value={option}>
                    {FIXED_EXPENSE_KIND_LABELS[option]}
                  </option>
                ))}
              </select>
            </div>
            <div className="space-y-1.5">
              <label
                htmlFor={`${idPrefix}-amount`}
                className="block text-sm font-medium"
              >
                Monto (CLP)
              </label>
              <Input
                id={`${idPrefix}-amount`}
                type="number"
                className="w-40"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
              />
            </div>
          </div>

          <div className="flex items-end gap-4">
            <label className="flex h-9 items-center gap-2 text-sm font-medium">
              <input
                type="checkbox"
                className="h-4 w-4"
                checked={isShared}
                onChange={(e) => setIsShared(e.target.checked)}
              />
              Compartido
            </label>
            {isShared && (
              <div className="space-y-1.5">
                <label
                  htmlFor={`${idPrefix}-ratio`}
                  className="block text-sm font-medium"
                >
                  Ratio
                </label>
                <Input
                  id={`${idPrefix}-ratio`}
                  type="number"
                  step="0.01"
                  min="0"
                  max="1"
                  className="w-24"
                  value={sharedRatio}
                  onChange={(e) => setSharedRatio(e.target.value)}
                />
              </div>
            )}
          </div>

          <div className="flex flex-wrap gap-4">
            <div className="space-y-1.5">
              <label
                htmlFor={`${idPrefix}-from`}
                className="block text-sm font-medium"
              >
                Vigente desde
              </label>
              <Input
                id={`${idPrefix}-from`}
                type="date"
                className="w-44"
                value={activeFrom}
                onChange={(e) => setActiveFrom(e.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <label
                htmlFor={`${idPrefix}-to`}
                className="block text-sm font-medium"
              >
                Vigente hasta
              </label>
              <Input
                id={`${idPrefix}-to`}
                type="date"
                className="w-44"
                value={activeTo}
                onChange={(e) => setActiveTo(e.target.value)}
              />
            </div>
          </div>
          <p className="text-xs text-muted-foreground">
            Ambas fechas son opcionales y se incluyen en la vigencia.
          </p>

          {error && <p className="text-sm text-destructive">{error}</p>}
          <DialogFooter>
            <Button type="submit" disabled={saving}>
              {saving ? "Guardando…" : "Guardar cambios"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
