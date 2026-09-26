"use client";

import { useCallback, useState } from "react";
import { Plus } from "lucide-react";
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
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useSortableData } from "@/lib/use-sortable-data";
import {
  CATEGORY_NAME_MAX_LENGTH,
  type CategoryIconName,
} from "@/lib/categories";
import { CategoryRow } from "./CategoryRow";
import { IconPicker } from "./IconPicker";
import {
  COLOR_INPUT_CLASS,
  DEFAULT_CATEGORY_COLOR,
  NAME_CLASH_MESSAGE,
  plural,
  readApiError,
  type ApiCategory,
} from "./shared";

type CategorySortKey = "nombre" | "transacciones" | "reglas";

function AddCategoryForm({ onCreated }: { onCreated: () => void }) {
  const [name, setName] = useState("");
  const [color, setColor] = useState(DEFAULT_CATEGORY_COLOR);
  const [icon, setIcon] = useState<CategoryIconName | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
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
        body: JSON.stringify({ name, color, icon }),
      });
      if (!res.ok) {
        setError(
          await readApiError(res, "No se pudo crear la categoría", {
            409: NAME_CLASH_MESSAGE,
          })
        );
        return;
      }
      setName("");
      setColor(DEFAULT_CATEGORY_COLOR);
      setIcon(null);
      onCreated();
    } catch {
      setError("No se pudo crear la categoría");
    } finally {
      setSaving(false);
    }
  }

  return (
    <form onSubmit={handleSubmit}>
      <div className="flex flex-wrap items-end gap-3">
        <div className="min-w-48 flex-1">
          <label
            htmlFor="new-category-name"
            className="mb-1 block text-sm text-muted-foreground"
          >
            Nombre
          </label>
          <Input
            id="new-category-name"
            value={name}
            maxLength={CATEGORY_NAME_MAX_LENGTH}
            onChange={(e) => {
              setName(e.target.value);
              setError(null);
            }}
            placeholder="Ej: Mascotas"
            aria-invalid={error != null}
          />
        </div>
        <div>
          <label
            htmlFor="new-category-color"
            className="mb-1 block text-sm text-muted-foreground"
          >
            Color
          </label>
          <input
            id="new-category-color"
            type="color"
            value={color}
            onChange={(e) => setColor(e.target.value)}
            className={COLOR_INPUT_CLASS}
          />
        </div>
        <div>
          <span className="mb-1 block text-sm text-muted-foreground">
            Ícono
          </span>
          <IconPicker value={icon} onChange={setIcon} />
        </div>
        <Button type="submit" disabled={saving}>
          <Plus className="h-4 w-4" />
          {saving ? "Guardando..." : "Agregar"}
        </Button>
      </div>
      {error && <p className="mt-1 text-sm text-destructive">{error}</p>}
    </form>
  );
}

export function CategoriesCard({
  categories,
  onChange,
}: {
  categories: ApiCategory[];
  onChange: () => void;
}) {
  const getValue = useCallback(
    (category: ApiCategory, key: CategorySortKey): string | number => {
      switch (key) {
        case "nombre":
          return category.name;
        case "transacciones":
          return category.transactionCount;
        case "reglas":
          return category.rules.length;
      }
    },
    []
  );
  const { sorted, sort, toggleSort } = useSortableData(categories, getValue);
  // Bridge the generic header's string key to our typed key union.
  const handleSort = (key: string) => toggleSort(key as CategorySortKey);
  const sortProps = (key: CategorySortKey) => ({
    columnKey: key,
    active: sort?.key === key,
    direction: sort?.key === key ? sort.direction : undefined,
    onSort: handleSort,
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>Categorías</CardTitle>
        <CardDescription>
          {plural(categories.length, "categoría", "categorías")}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <AddCategoryForm onCreated={onChange} />
        {categories.length === 0 ? (
          <p className="text-muted-foreground">
            No hay categorías. Agrega una arriba.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <SortableTableHead label="Categoría" {...sortProps("nombre")} />
                <SortableTableHead
                  label="Transacciones"
                  align="right"
                  className="w-36"
                  {...sortProps("transacciones")}
                />
                <SortableTableHead
                  label="Reglas"
                  align="right"
                  className="w-28"
                  {...sortProps("reglas")}
                />
                <TableHead className="w-56"></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {sorted.map((category) => (
                <CategoryRow
                  key={category.id}
                  category={category}
                  onChange={onChange}
                />
              ))}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
