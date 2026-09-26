"use client";

import { useState } from "react";
import { Check, Pencil, Trash2, X } from "lucide-react";
import { TableCell, TableRow } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  CATEGORY_NAME_MAX_LENGTH,
  type CategoryIconName,
} from "@/lib/categories";
import { IconPicker } from "./IconPicker";
import { toIconName } from "./CategoryIcon";
import {
  COLOR_INPUT_CLASS,
  CategoryLabel,
  DEFAULT_CATEGORY_COLOR,
  NAME_CLASH_MESSAGE,
  readApiError,
  type ApiCategory,
} from "./shared";

type CategoryDraft = {
  name: string;
  color: string;
  icon: CategoryIconName | null;
};

/** The delete confirmation, stating what happens to the category's
 *  transactions and rules. */
function deleteMessage({ name, transactionCount, rules }: ApiCategory): string {
  const txs =
    transactionCount === 1
      ? "Su transacción quedará sin categoría"
      : `Sus ${transactionCount.toLocaleString("es-CL")} transacciones quedarán sin categoría`;
  const ruleCount = rules.length;
  const deletedRules =
    ruleCount === 1
      ? "se eliminará su regla"
      : `se eliminarán sus ${ruleCount} reglas`;
  let detail = "No tiene transacciones ni reglas.";
  if (transactionCount > 0 && ruleCount > 0) {
    detail = `${txs} y ${deletedRules}.`;
  } else if (transactionCount > 0) {
    detail = `${txs}.`;
  } else if (ruleCount > 0) {
    detail = `${deletedRules.charAt(0).toUpperCase()}${deletedRules.slice(1)}.`;
  }
  return `¿Eliminar la categoría "${name}"?\n\n${detail}\nEsta acción no se puede deshacer.`;
}

export function CategoryRow({
  category,
  onChange,
}: {
  category: ApiCategory;
  onChange: () => void;
}) {
  // null while viewing; the fields being edited otherwise.
  const [draft, setDraft] = useState<CategoryDraft | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const originalColor = category.color ?? DEFAULT_CATEGORY_COLOR;
  const originalIcon = toIconName(category.icon);

  function startEditing() {
    setError(null);
    setDraft({ name: category.name, color: originalColor, icon: originalIcon });
  }

  async function handleSave() {
    if (!draft) return;
    const name = draft.name.trim();
    if (!name) {
      setError("El nombre es obligatorio");
      return;
    }
    // Only what changed, so an untouched color stays unset.
    const patch: Record<string, string | null> = {};
    if (name !== category.name) patch.name = name;
    if (draft.color !== originalColor) patch.color = draft.color;
    if (draft.icon !== originalIcon) patch.icon = draft.icon;
    if (Object.keys(patch).length === 0) {
      setDraft(null);
      return;
    }

    setSaving(true);
    setError(null);
    try {
      const res = await fetch(`/api/categories/${category.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!res.ok) {
        setError(
          await readApiError(res, "No se pudo guardar la categoría", {
            409: NAME_CLASH_MESSAGE,
          })
        );
        return;
      }
      setDraft(null);
      onChange();
    } catch {
      setError("No se pudo guardar la categoría");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (!window.confirm(deleteMessage(category))) return;
    setError(null);
    try {
      const res = await fetch(`/api/categories/${category.id}`, {
        method: "DELETE",
      });
      if (!res.ok) {
        setError(await readApiError(res, "No se pudo eliminar la categoría"));
        return;
      }
      onChange();
    } catch {
      setError("No se pudo eliminar la categoría");
    }
  }

  const errorNote = error && (
    <p className="mt-1 max-w-md text-sm whitespace-normal text-destructive">
      {error}
    </p>
  );

  return (
    <TableRow>
      <TableCell className="font-medium">
        {draft ? (
          <div className="flex items-center gap-2">
            <input
              type="color"
              aria-label="Color"
              value={draft.color}
              onChange={(e) => setDraft({ ...draft, color: e.target.value })}
              className={COLOR_INPUT_CLASS}
            />
            <IconPicker
              value={draft.icon}
              onChange={(icon) => setDraft({ ...draft, icon })}
            />
            <Input
              aria-label="Nombre de la categoría"
              value={draft.name}
              maxLength={CATEGORY_NAME_MAX_LENGTH}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
              onKeyDown={(e) => {
                if (e.key === "Enter") handleSave();
                if (e.key === "Escape") setDraft(null);
              }}
              aria-invalid={error != null}
              className="max-w-64"
              autoFocus
            />
          </div>
        ) : (
          <CategoryLabel category={category} />
        )}
        {errorNote}
      </TableCell>
      <TableCell className="text-right tabular-nums">
        {category.transactionCount.toLocaleString("es-CL")}
      </TableCell>
      <TableCell className="text-right tabular-nums">
        {category.rules.length}
      </TableCell>
      <TableCell>
        <div className="flex justify-end gap-1">
          {draft ? (
            <>
              <Button size="sm" onClick={handleSave} disabled={saving}>
                <Check className="h-4 w-4" />
                {saving ? "Guardando..." : "Guardar"}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setDraft(null);
                  setError(null);
                }}
                disabled={saving}
              >
                <X className="h-4 w-4" />
                Cancelar
              </Button>
            </>
          ) : (
            <>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label={`Editar ${category.name}`}
                title="Editar"
                onClick={startEditing}
              >
                <Pencil />
              </Button>
              <Button
                variant="ghost"
                size="icon-sm"
                aria-label={`Eliminar ${category.name}`}
                title="Eliminar"
                onClick={handleDelete}
                className="text-muted-foreground hover:text-destructive"
              >
                <Trash2 />
              </Button>
            </>
          )}
        </div>
      </TableCell>
    </TableRow>
  );
}
