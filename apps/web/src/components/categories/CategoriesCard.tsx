"use client";

import { useCallback, useMemo, useState } from "react";
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
import { categoryTree, childrenByParent } from "@/lib/category-tree";
import { AddSubcategoryRow, CategoryRow } from "./CategoryRow";
import {
  CategoryTrendHeads,
  TREND_MONTHS,
  trendSortValue,
  UncategorizedRow,
  type TrendSortKey,
} from "./CategoryTrends";
import { useCategoryTrends } from "@/components/charts/use-category-trends";
import { IconPicker } from "./IconPicker";
import {
  COLOR_INPUT_CLASS,
  DEFAULT_CATEGORY_COLOR,
  NAME_CLASH_MESSAGE,
  SELECT_CLASS,
  plural,
  readApiError,
  type ApiCategory,
} from "./shared";

type CategorySortKey = "nombre" | "transacciones" | "reglas" | TrendSortKey;

/** localStorage key of the parents whose subcategories are collapsed. */
const COLLAPSED_KEY = "categorias:colapsadas";

function readCollapsed(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = window.localStorage.getItem(COLLAPSED_KEY);
    const ids: unknown = raw ? JSON.parse(raw) : [];
    return new Set(
      Array.isArray(ids) ? ids.filter((id) => typeof id === "string") : []
    );
  } catch {
    return new Set();
  }
}

function writeCollapsed(ids: Set<string>) {
  try {
    window.localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...ids]));
  } catch {
    // Storage blocked: the state lasts until the page closes.
  }
}

function AddCategoryForm({
  topLevel,
  onCreated,
}: {
  /** The categories a new one may go under. */
  topLevel: ApiCategory[];
  onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const [color, setColor] = useState(DEFAULT_CATEGORY_COLOR);
  // A subcategory keeps no color of its own (it shows its parent's) unless
  // one is picked.
  const [colorTouched, setColorTouched] = useState(false);
  const [icon, setIcon] = useState<CategoryIconName | null>(null);
  const [parentId, setParentId] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const parent = topLevel.find((c) => c.id === parentId);
  const shownColor =
    parent && !colorTouched ? (parent.color ?? DEFAULT_CATEGORY_COLOR) : color;

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
        body: JSON.stringify({
          name,
          color: parent && !colorTouched ? null : color,
          icon,
          parentId: parent ? parent.id : null,
        }),
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
      setColorTouched(false);
      setIcon(null);
      setParentId("");
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
        <div className="w-48">
          <label
            htmlFor="new-category-parent"
            className="mb-1 block text-sm text-muted-foreground"
          >
            Categoría padre
          </label>
          <select
            id="new-category-parent"
            value={parentId}
            onChange={(e) => setParentId(e.target.value)}
            className={SELECT_CLASS}
          >
            <option value="">Ninguna</option>
            {topLevel.map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </select>
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
            value={shownColor}
            onChange={(e) => {
              setColor(e.target.value);
              setColorTouched(true);
            }}
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
  version = 0,
}: {
  categories: ApiCategory[];
  onChange: () => void;
  /** Bumped on every reload, so the spend columns refetch too. */
  version?: number;
}) {
  // Top-level rows show their subcategories rolled in; subcategory rows read
  // the split figures.
  const { trends, error: trendsError } = useCategoryTrends(TREND_MONTHS, version);
  const { trends: splitTrends, error: splitTrendsError } = useCategoryTrends(
    TREND_MONTHS,
    version,
    { split: true }
  );
  const byId = useMemo(
    () => new Map(categories.map((c) => [c.id, c])),
    [categories]
  );
  const isChild = useCallback(
    (category: ApiCategory) =>
      category.parentId !== null && byId.has(category.parentId),
    [byId]
  );
  const getValue = useCallback(
    (category: ApiCategory, key: CategorySortKey): string | number | null => {
      switch (key) {
        case "nombre":
          return category.name;
        case "transacciones":
          return category.transactionCount;
        case "reglas":
          return category.rules.length;
        default:
          return trendSortValue(
            isChild(category) ? splitTrends : trends,
            category.id,
            key
          );
      }
    },
    [trends, splitTrends, isChild]
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

  const tree = useMemo(() => categoryTree(sorted), [sorted]);
  const children = useMemo(() => childrenByParent(categories), [categories]);
  const topLevel = useMemo(
    () =>
      categories
        .filter((c) => !isChild(c))
        .sort((a, b) => a.name.localeCompare(b.name, "es")),
    [categories, isChild]
  );

  // The card first renders once the categories arrive, client-side, so
  // storage is there to read.
  const [collapsed, setCollapsed] = useState<Set<string>>(readCollapsed);
  const toggleCollapsed = (id: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      writeCollapsed(next);
      return next;
    });
  };
  // The parent whose inline "new subcategory" row is open.
  const [addingTo, setAddingTo] = useState<string | null>(null);
  const startAdding = (id: string) => {
    setAddingTo(id);
    if (collapsed.has(id)) toggleCollapsed(id);
  };

  const subcategoryCount = categories.length - topLevel.length;
  const rows: React.ReactNode[] = [];
  tree.forEach((entry, index) => {
    const { category, depth, parent } = entry;
    if (depth === 1 && parent && collapsed.has(parent.id)) return;
    const kids = depth === 0 ? (children.get(category.id) ?? []) : [];
    rows.push(
      <CategoryRow
        key={category.id}
        category={category}
        parent={parent}
        childCount={kids.length}
        expanded={!collapsed.has(category.id)}
        onToggle={() => toggleCollapsed(category.id)}
        onAddSubcategory={() => startAdding(category.id)}
        topLevel={topLevel}
        onChange={onChange}
        trends={depth === 1 ? splitTrends : trends}
        trendsError={depth === 1 ? splitTrendsError : trendsError}
      />
    );
    // The inline add row closes the parent's group.
    const groupOwner = depth === 0 ? category : parent;
    const next = tree[index + 1];
    const groupEnds = !next || next.depth === 0;
    if (groupOwner && groupEnds && addingTo === groupOwner.id) {
      rows.push(
        <AddSubcategoryRow
          key={`add-${groupOwner.id}`}
          parent={groupOwner}
          onDone={(created) => {
            setAddingTo(null);
            if (created) onChange();
          }}
        />
      );
    }
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>Categorías</CardTitle>
        <CardDescription>
          {plural(topLevel.length, "categoría", "categorías")}
          {subcategoryCount > 0 &&
            ` y ${plural(subcategoryCount, "subcategoría", "subcategorías")}`}
          . Gasto en CLP sin transferencias internas; el de cada categoría
          incluye sus subcategorías. Elige una categoría para ver su detalle.
          {trendsError && " No se pudieron cargar los gastos por categoría."}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <AddCategoryForm topLevel={topLevel} onCreated={onChange} />
        {categories.length === 0 && (
          <p className="text-muted-foreground">
            No hay categorías. Agrega una arriba.
          </p>
        )}
        {/* Rendered even with no categories: the Sin categoría row still
            holds the spend and opens its detail panel. */}
        <Table>
          <TableHeader>
            <TableRow>
              <SortableTableHead label="Categoría" {...sortProps("nombre")} />
              <CategoryTrendHeads sortProps={sortProps} />
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
            {rows}
            <UncategorizedRow trends={trends} trailingCells={3} />
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  );
}
