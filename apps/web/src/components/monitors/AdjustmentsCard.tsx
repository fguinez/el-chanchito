"use client";

import { Fragment, useState } from "react";
import {
  Check,
  ChevronLeft,
  ChevronRight,
  Pencil,
  Trash2,
  X,
} from "lucide-react";
import {
  Card,
  CardAction,
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
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { groupAdjustmentsByDay } from "@/lib/monitors/adjustments";
import { formatLocalDate } from "@/lib/monitors/dates";
import { formatPlainDateEs } from "@/lib/utils";
import {
  formatSignedAmount,
  type ApiAdjustment,
} from "@/components/monitors/shared";

type Draft = { adjustmentDate: string; amount: string; description: string };

type AdjustmentBody = {
  adjustmentDate: string;
  amount: number;
  description: string | null;
};

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

/** The request body for a draft, or a Spanish error for the form. */
function draftBody(
  draft: Draft
): { ok: true; body: AdjustmentBody } | { ok: false; error: string } {
  if (draft.adjustmentDate === "") {
    return { ok: false, error: "Elige la fecha de la variación." };
  }
  const amount = draft.amount.trim() === "" ? NaN : Number(draft.amount);
  if (!Number.isFinite(amount)) {
    return { ok: false, error: "El monto debe ser un número." };
  }
  if (amount === 0) {
    return { ok: false, error: "El monto no puede ser cero." };
  }
  const description = draft.description.trim();
  return {
    ok: true,
    body: {
      adjustmentDate: draft.adjustmentDate,
      amount,
      description: description === "" ? null : description,
    },
  };
}

/** Spanish message for a failed write. */
async function writeError(res: Response): Promise<string> {
  if (res.status === 404) return "La variación o el monitor ya no existe.";
  const data = await res.json().catch(() => null);
  return data?.field === "amount"
    ? "Monto inválido."
    : data?.field === "adjustmentDate"
      ? "Fecha inválida."
      : data?.field === "description"
        ? "Descripción demasiado larga."
        : "No se pudo guardar la variación.";
}

/**
 * The monitor's variaciones for one month: add, edit and delete them. Every
 * variación adds its amount to all thresholds from its day to the end of the
 * month; same-day entries show their summed total. `onChanged` runs after
 * each write so the page refetches the evaluation, the chart and this list.
 */
export function AdjustmentsCard({
  monitorId,
  currency,
  adjustments,
  onChanged,
}: {
  monitorId: string;
  currency: string;
  adjustments: ApiAdjustment[];
  onChanged: () => void;
}) {
  const today = formatLocalDate(new Date());
  const [month, setMonth] = useState(today.slice(0, 7));
  const [draft, setDraft] = useState<Draft>({
    adjustmentDate: today,
    amount: "",
    description: "",
  });
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState<Draft | null>(null);
  const [savingEdit, setSavingEdit] = useState(false);
  const [rowError, setRowError] = useState<string | null>(null);

  const days = groupAdjustmentsByDay(adjustments, month);
  const monthTotal = days.length > 0 ? days[days.length - 1].runningTotal : 0;
  const baseUrl = `/api/monitors/${monitorId}/adjustments`;

  async function handleAdd() {
    const parsed = draftBody(draft);
    if (!parsed.ok) {
      setAddError(parsed.error);
      return;
    }
    setAdding(true);
    setAddError(null);
    try {
      const res = await fetch(baseUrl, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(parsed.body),
      });
      if (!res.ok) {
        setAddError(await writeError(res));
        return;
      }
      setDraft({ ...draft, amount: "", description: "" });
      // Show the month the new variación landed in.
      setMonth(parsed.body.adjustmentDate.slice(0, 7));
      onChanged();
    } catch {
      setAddError("No se pudo guardar la variación.");
    } finally {
      setAdding(false);
    }
  }

  function startEdit(adjustment: ApiAdjustment) {
    setEditingId(adjustment.id);
    setEditDraft({
      adjustmentDate: adjustment.adjustmentDate,
      amount: String(adjustment.amount),
      description: adjustment.description ?? "",
    });
    setRowError(null);
  }

  function cancelEdit() {
    setEditingId(null);
    setEditDraft(null);
    setRowError(null);
  }

  async function handleSaveEdit() {
    if (editingId == null || editDraft == null) return;
    const parsed = draftBody(editDraft);
    if (!parsed.ok) {
      setRowError(parsed.error);
      return;
    }
    setSavingEdit(true);
    setRowError(null);
    try {
      const res = await fetch(`${baseUrl}/${editingId}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(parsed.body),
      });
      if (!res.ok) {
        setRowError(await writeError(res));
        return;
      }
      cancelEdit();
      setMonth(parsed.body.adjustmentDate.slice(0, 7));
      onChanged();
    } catch {
      setRowError("No se pudo guardar la variación.");
    } finally {
      setSavingEdit(false);
    }
  }

  async function handleDelete(adjustment: ApiAdjustment) {
    if (!window.confirm("¿Eliminar esta variación?")) return;
    setRowError(null);
    try {
      const res = await fetch(`${baseUrl}/${adjustment.id}`, {
        method: "DELETE",
      });
      if (!res.ok && res.status !== 404) throw new Error("failed");
      if (editingId === adjustment.id) cancelEdit();
      onChanged();
    } catch {
      setRowError("No se pudo eliminar la variación.");
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Variaciones</CardTitle>
        <CardDescription>
          Ajustes puntuales, como un reembolso o un presupuesto extra. Cada
          variación se suma a todos los umbrales desde su día hasta fin de mes
          (usa un monto negativo para bajarlos); el mes siguiente parte sin
          variaciones.
        </CardDescription>
        <CardAction>
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="icon-sm"
              aria-label="Mes anterior"
              onClick={() => setMonth(shiftMonth(month, -1))}
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
              onClick={() => setMonth(shiftMonth(month, 1))}
            >
              <ChevronRight />
            </Button>
          </div>
        </CardAction>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="flex flex-wrap items-end gap-3">
          <div className="w-40">
            <label
              htmlFor="adjustment-date"
              className="mb-1 block text-sm text-muted-foreground"
            >
              Fecha
            </label>
            <Input
              id="adjustment-date"
              type="date"
              value={draft.adjustmentDate}
              onChange={(e) =>
                setDraft({ ...draft, adjustmentDate: e.target.value })
              }
            />
          </div>
          <div className="w-40">
            <label
              htmlFor="adjustment-amount"
              className="mb-1 block text-sm text-muted-foreground"
            >
              Monto ({currency})
            </label>
            <Input
              id="adjustment-amount"
              type="number"
              step="any"
              value={draft.amount}
              onChange={(e) => setDraft({ ...draft, amount: e.target.value })}
              placeholder="-50000"
            />
          </div>
          <div className="min-w-48 flex-1">
            <label
              htmlFor="adjustment-description"
              className="mb-1 block text-sm text-muted-foreground"
            >
              Descripción
            </label>
            <Input
              id="adjustment-description"
              value={draft.description}
              maxLength={200}
              onChange={(e) =>
                setDraft({ ...draft, description: e.target.value })
              }
              placeholder="Ej: Reembolso del seguro"
            />
          </div>
          <Button onClick={handleAdd} disabled={adding}>
            {adding ? "Guardando..." : "Agregar variación"}
          </Button>
        </div>
        {addError && <p className="text-sm text-destructive">{addError}</p>}

        {days.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Sin variaciones en {monthLabel(month)}.
          </p>
        ) : (
          <>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Día</TableHead>
                  <TableHead>Descripción</TableHead>
                  <TableHead className="text-right">Monto</TableHead>
                  <TableHead className="text-right">Total del día</TableHead>
                  <TableHead className="text-right">Acumulado</TableHead>
                  <TableHead className="w-20"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {days.map((day) => (
                  <Fragment key={day.adjustmentDate}>
                    {day.entries.map((adjustment, index) => {
                      const editing =
                        editingId === adjustment.id && editDraft != null;
                      const span = day.entries.length;
                      // Shared day cells sit at the top of a multi-entry day.
                      const spanAlign = span > 1 ? "align-top" : "";
                      return (
                        <TableRow key={adjustment.id}>
                          {index === 0 && (
                            <TableCell rowSpan={span} className={spanAlign}>
                              {formatPlainDateEs(day.adjustmentDate)}
                            </TableCell>
                          )}
                          {editing ? (
                            <>
                              <TableCell>
                                <div className="flex flex-wrap gap-2">
                                  <Input
                                    type="date"
                                    aria-label="Fecha"
                                    className="w-40"
                                    value={editDraft.adjustmentDate}
                                    onChange={(e) =>
                                      setEditDraft({
                                        ...editDraft,
                                        adjustmentDate: e.target.value,
                                      })
                                    }
                                  />
                                  <Input
                                    aria-label="Descripción"
                                    className="min-w-40 flex-1"
                                    maxLength={200}
                                    value={editDraft.description}
                                    onChange={(e) =>
                                      setEditDraft({
                                        ...editDraft,
                                        description: e.target.value,
                                      })
                                    }
                                  />
                                </div>
                              </TableCell>
                              <TableCell className="text-right">
                                <Input
                                  type="number"
                                  step="any"
                                  aria-label="Monto"
                                  className="ml-auto w-32 text-right"
                                  value={editDraft.amount}
                                  onChange={(e) =>
                                    setEditDraft({
                                      ...editDraft,
                                      amount: e.target.value,
                                    })
                                  }
                                />
                              </TableCell>
                            </>
                          ) : (
                            <>
                              <TableCell>
                                {adjustment.description ?? (
                                  <span className="text-muted-foreground">
                                    Sin descripción
                                  </span>
                                )}
                              </TableCell>
                              <TableCell className="text-right tabular-nums">
                                {formatSignedAmount(currency, adjustment.amount)}
                              </TableCell>
                            </>
                          )}
                          {index === 0 && (
                            <>
                              <TableCell
                                rowSpan={span}
                                className={`text-right tabular-nums ${spanAlign}`}
                              >
                                {formatSignedAmount(currency, day.dayTotal)}
                              </TableCell>
                              <TableCell
                                rowSpan={span}
                                className={`text-right font-medium tabular-nums ${spanAlign}`}
                              >
                                {formatSignedAmount(currency, day.runningTotal)}
                              </TableCell>
                            </>
                          )}
                          <TableCell>
                            {editing ? (
                              <div className="flex justify-end gap-1">
                                <Button
                                  variant="ghost"
                                  size="icon-xs"
                                  aria-label="Guardar"
                                  disabled={savingEdit}
                                  onClick={handleSaveEdit}
                                >
                                  <Check />
                                </Button>
                                <Button
                                  variant="ghost"
                                  size="icon-xs"
                                  aria-label="Cancelar"
                                  onClick={cancelEdit}
                                >
                                  <X />
                                </Button>
                              </div>
                            ) : (
                              <div className="flex justify-end gap-1 text-muted-foreground">
                                <Button
                                  variant="ghost"
                                  size="icon-xs"
                                  aria-label="Editar variación"
                                  onClick={() => startEdit(adjustment)}
                                >
                                  <Pencil />
                                </Button>
                                <Button
                                  variant="ghost"
                                  size="icon-xs"
                                  aria-label="Eliminar variación"
                                  className="hover:text-destructive"
                                  onClick={() => handleDelete(adjustment)}
                                >
                                  <Trash2 />
                                </Button>
                              </div>
                            )}
                          </TableCell>
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
                {formatSignedAmount(currency, monthTotal)}
              </span>
            </p>
          </>
        )}
      </CardContent>
    </Card>
  );
}
