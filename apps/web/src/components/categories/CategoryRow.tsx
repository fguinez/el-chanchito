"use client";

import { useState } from "react";
import {
  Check,
  ChevronDown,
  ChevronRight,
  Pencil,
  Plus,
  Trash2,
  X,
} from "lucide-react";
import { TableCell, TableRow } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  CATEGORY_NAME_MAX_LENGTH,
  type CategoryIconName,
} from "@/lib/categories";
import { cn } from "@/lib/utils";
import { IconPicker } from "./IconPicker";
import {
  CategoryDetailLink,
  CategoryTrendCells,
  useOpenCategoryRow,
} from "./CategoryTrends";
import type { CategoryTrend } from "@/lib/category-trends";
import { toIconName } from "./CategoryIcon";
import {
  COLOR_INPUT_CLASS,
  CategoryLabel,
  DEFAULT_CATEGORY_COLOR,
  NAME_CLASH_MESSAGE,
  SELECT_CLASS,
  plural,
  readApiError,
  type ApiCategory,
} from "./shared";

const CATEGORY_GONE_MESSAGE = "La categoría ya no existe";

/** Columns of the Categorías table (name, 4 spend, transactions, rules,
 *  actions), for a row that spans them. */
const COLUMN_COUNT = 8;

type CategoryDraft = {
  name: string;
  color: string;
  icon: CategoryIconName | null;
  /** "" for top-level. */
  parentId: string;
};

/** The delete confirmation, stating what happens to the category's
 *  transactions, rules and subcategories. */
function deleteMessage(
  { name, directTransactionCount, rules }: ApiCategory,
  childCount: number
): string {
  const txs =
    directTransactionCount === 1
      ? "Su transacción quedará sin categoría"
      : `Sus ${directTransactionCount.toLocaleString("es-CL")} transacciones quedarán sin categoría`;
  const ruleCount = rules.length;
  const deletedRules =
    ruleCount === 1
      ? "se eliminará su regla"
      : `se eliminarán sus ${ruleCount} reglas`;
  let detail = "No tiene transacciones ni reglas.";
  if (directTransactionCount > 0 && ruleCount > 0) {
    detail = `${txs} y ${deletedRules}.`;
  } else if (directTransactionCount > 0) {
    detail = `${txs}.`;
  } else if (ruleCount > 0) {
    detail = `${deletedRules.charAt(0).toUpperCase()}${deletedRules.slice(1)}.`;
  }
  if (childCount > 0) {
    detail +=
      childCount === 1
        ? "\nSu subcategoría pasará a ser una categoría principal, con sus transacciones y reglas."
        : `\nSus ${childCount} subcategorías pasarán a ser categorías principales, con sus transacciones y reglas.`;
  }
  return `¿Eliminar la categoría "${name}"?\n\n${detail}\nEsta acción no se puede deshacer.`;
}

export function CategoryRow({
  category,
  parent,
  childCount,
  expanded,
  onToggle,
  onAddSubcategory,
  topLevel,
  onChange,
  trends,
  trendsError = false,
}: {
  category: ApiCategory;
  parent: ApiCategory | null;
  childCount: number;
  expanded: boolean;
  onToggle: () => void;
  onAddSubcategory: () => void;
  /** The categories this one may move under, by name. */
  topLevel: ApiCategory[];
  onChange: () => void;
  /** Spend per category (see CategoryTrends); null while loading. */
  trends: Map<string, CategoryTrend> | null;
  /** The spend fetch failed: `trends` stays null for good. */
  trendsError?: boolean;
}) {
  const openRow = useOpenCategoryRow();
  // null while viewing; the fields being edited otherwise.
  const [draft, setDraft] = useState<CategoryDraft | null>(null);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isChild = parent !== null;
  // A subcategory without a color of its own shows its parent's.
  const shownColor = category.color ?? parent?.color ?? null;
  const originalColor = shownColor ?? DEFAULT_CATEGORY_COLOR;
  const originalIcon = toIconName(category.icon);
  const originalParentId = parent?.id ?? "";

  function startEditing() {
    setError(null);
    setDraft({
      name: category.name,
      color: originalColor,
      icon: originalIcon,
      parentId: originalParentId,
    });
  }

  function stopEditing() {
    setDraft(null);
    setError(null);
  }

  async function handleSave() {
    if (!draft) return;
    const name = draft.name.trim();
    if (!name) {
      setError("El nombre es obligatorio");
      return;
    }
    // Only what changed, so a category without a color keeps none unless
    // one is picked.
    const patch: Record<string, string | null> = {};
    if (name !== category.name) patch.name = name;
    if (draft.color !== originalColor) patch.color = draft.color;
    if (draft.icon !== originalIcon) patch.icon = draft.icon;
    if (draft.parentId !== originalParentId) {
      patch.parentId = draft.parentId || null;
      // A colorless subcategory promoted to top-level keeps the color it
      // showed (its parent's) instead of falling back to the default gray.
      if (!draft.parentId && category.color === null && patch.color === undefined) {
        patch.color = draft.color;
      }
    }
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
            404: CATEGORY_GONE_MESSAGE,
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
    if (deleting || !window.confirm(deleteMessage(category, childCount))) return;
    setDeleting(true);
    setError(null);
    try {
      const res = await fetch(`/api/categories/${category.id}`, {
        method: "DELETE",
      });
      if (!res.ok) {
        setError(
          await readApiError(res, "No se pudo eliminar la categoría", {
            404: CATEGORY_GONE_MESSAGE,
          })
        );
        return;
      }
      onChange();
    } catch {
      setError("No se pudo eliminar la categoría");
    } finally {
      setDeleting(false);
    }
  }

  const errorNote = error && (
    <p className="mt-1 max-w-md text-sm whitespace-normal text-destructive">
      {error}
    </p>
  );
  const rowProps = draft ? undefined : openRow(category.id);
  const parentOptions = topLevel.filter((c) => c.id !== category.id);

  return (
    <TableRow
      onClick={rowProps?.onClick}
      className={cn("group", rowProps?.className)}
    >
      <TableCell className="font-medium">
        <div className={cn("flex items-start", isChild && "pl-3")}>
          {isChild ? (
            // The thin connector ties a subcategory to the parent above.
            <span aria-hidden className="mr-2 self-stretch border-l" />
          ) : childCount > 0 ? (
            <Button
              variant="ghost"
              size="icon-xs"
              className="mr-1 text-muted-foreground"
              aria-label={`${expanded ? "Ocultar" : "Mostrar"} subcategorías de ${category.name}`}
              title={expanded ? "Ocultar subcategorías" : "Mostrar subcategorías"}
              onClick={onToggle}
            >
              {expanded ? <ChevronDown /> : <ChevronRight />}
            </Button>
          ) : (
            <span aria-hidden className="mr-1 size-6 shrink-0" />
          )}
          <div className="min-w-0 py-0.5">
            {draft ? (
              <div className="flex flex-wrap items-center gap-2">
                <input
                  type="color"
                  aria-label="Color"
                  value={draft.color}
                  onChange={(e) => setDraft({ ...draft, color: e.target.value })}
                  disabled={saving}
                  className={COLOR_INPUT_CLASS}
                />
                <IconPicker
                  value={draft.icon}
                  onChange={(icon) => setDraft({ ...draft, icon })}
                  disabled={saving}
                />
                <Input
                  aria-label="Nombre de la categoría"
                  value={draft.name}
                  maxLength={CATEGORY_NAME_MAX_LENGTH}
                  onChange={(e) => setDraft({ ...draft, name: e.target.value })}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !saving) handleSave();
                    if (e.key === "Escape") stopEditing();
                  }}
                  readOnly={saving}
                  aria-invalid={error != null}
                  className="max-w-64"
                  autoFocus
                />
                <select
                  aria-label="Categoría padre"
                  title={
                    childCount > 0
                      ? "Una categoría con subcategorías no puede ser subcategoría"
                      : "Categoría padre"
                  }
                  value={draft.parentId}
                  onChange={(e) => setDraft({ ...draft, parentId: e.target.value })}
                  disabled={saving || childCount > 0}
                  className={cn(SELECT_CLASS, "w-44")}
                >
                  <option value="">Sin categoría padre</option>
                  {parentOptions.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name}
                    </option>
                  ))}
                </select>
              </div>
            ) : (
              <>
                <CategoryDetailLink categoryId={category.id}>
                  <CategoryLabel category={{ ...category, color: shownColor }} />
                </CategoryDetailLink>
                {childCount > 0 && (
                  <span className="ml-2 text-xs font-normal text-muted-foreground">
                    {plural(childCount, "subcategoría", "subcategorías")}
                  </span>
                )}
              </>
            )}
            {errorNote}
          </div>
        </div>
      </TableCell>
      <CategoryTrendCells
        trends={trends}
        error={trendsError}
        categoryId={category.id}
        color={shownColor}
      />
      <TableCell
        className="text-right tabular-nums"
        title={
          childCount > 0
            ? `${category.directTransactionCount.toLocaleString("es-CL")} directamente en ${category.name}`
            : undefined
        }
      >
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
                onClick={stopEditing}
                disabled={saving}
              >
                <X className="h-4 w-4" />
                Cancelar
              </Button>
            </>
          ) : (
            <>
              {!isChild && (
                <Button
                  variant="ghost"
                  size="sm"
                  aria-label={`Agregar subcategoría a ${category.name}`}
                  title="Agregar subcategoría"
                  onClick={onAddSubcategory}
                  // Revealed on row hover where there is a hover (a mouse); always
                  // shown on touch screens, where nothing would reveal it.
                  className="text-muted-foreground [@media(hover:hover)]:opacity-0 group-hover:opacity-100 focus-visible:opacity-100"
                >
                  <Plus className="h-4 w-4" />
                  Subcategoría
                </Button>
              )}
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
                disabled={deleting}
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

/** The inline row that names a new subcategory under `parent`; it takes the
 *  parent's color (none of its own) and no icon, both editable afterwards.
 *  `onDone(true)` after creating one, `onDone(false)` on cancel. */
export function AddSubcategoryRow({
  parent,
  onDone,
}: {
  parent: ApiCategory;
  onDone: (created: boolean) => void;
}) {
  const [name, setName] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    if (!name.trim()) {
      setError("El nombre es obligatorio");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const res = await fetch("/api/categories", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, parentId: parent.id }),
      });
      if (!res.ok) {
        setError(
          await readApiError(res, "No se pudo crear la subcategoría", {
            409: NAME_CLASH_MESSAGE,
          })
        );
        return;
      }
      onDone(true);
    } catch {
      setError("No se pudo crear la subcategoría");
    } finally {
      setSaving(false);
    }
  }

  return (
    <TableRow className="hover:bg-transparent">
      <TableCell colSpan={COLUMN_COUNT}>
        <div className="flex items-center gap-2 pl-3">
          <span aria-hidden className="mr-2 self-stretch border-l" />
          <Input
            aria-label={`Nombre de la nueva subcategoría de ${parent.name}`}
            placeholder={`Nueva subcategoría de ${parent.name}`}
            value={name}
            maxLength={CATEGORY_NAME_MAX_LENGTH}
            onChange={(e) => {
              setName(e.target.value);
              setError(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !saving) handleSave();
              if (e.key === "Escape") onDone(false);
            }}
            readOnly={saving}
            aria-invalid={error != null}
            className="max-w-64"
            autoFocus
          />
          <Button size="sm" onClick={handleSave} disabled={saving}>
            <Check className="h-4 w-4" />
            {saving ? "Guardando..." : "Agregar"}
          </Button>
          <Button
            variant="ghost"
            size="sm"
            onClick={() => onDone(false)}
            disabled={saving}
          >
            <X className="h-4 w-4" />
            Cancelar
          </Button>
        </div>
        {error && (
          <p className="mt-1 pl-8 text-sm text-destructive">{error}</p>
        )}
      </TableCell>
    </TableRow>
  );
}
