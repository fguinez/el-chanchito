"use client";

import { useCallback, useMemo, useState } from "react";
import { FlaskConical, Plus } from "lucide-react";
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
  RULE_KEYWORD_MAX_LENGTH,
  RULE_PRIORITY_MAX,
  RULE_PRIORITY_MIN,
} from "@/lib/categories";
import { RulePreview } from "./RulePreview";
import { RuleRow } from "./RuleRow";
import {
  PRIORITY_ERROR,
  SELECT_CLASS,
  parsePriority,
  plural,
  readApiError,
  type ApiCategory,
  type ApiCategoryRule,
} from "./shared";

type RuleSortKey = "palabra" | "categoria" | "prioridad";

/** The order rules apply in: priority desc, then oldest first. */
function byApplicationOrder(a: ApiCategoryRule, b: ApiCategoryRule): number {
  if (a.priority !== b.priority) return b.priority - a.priority;
  if (a.createdAt !== b.createdAt) return a.createdAt < b.createdAt ? -1 : 1;
  return a.id < b.id ? -1 : 1;
}

function AddRuleForm({
  categories,
  categoriesById,
  version,
  onCreated,
}: {
  categories: ApiCategory[];
  categoriesById: Map<string, ApiCategory>;
  version: number;
  onCreated: () => void;
}) {
  const [keyword, setKeyword] = useState("");
  const [categoryId, setCategoryId] = useState("");
  const [priority, setPriority] = useState("0");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [previewKeyword, setPreviewKeyword] = useState<string | null>(null);
  // Falls back to the placeholder if the chosen category was deleted.
  const selectedCategoryId = categoriesById.has(categoryId) ? categoryId : "";

  function handleTest() {
    if (!keyword.trim()) {
      setError("Escribe una palabra clave para probarla");
      return;
    }
    setError(null);
    setPreviewKeyword(keyword.trim());
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const parsedPriority = parsePriority(priority);
    if (!keyword.trim()) {
      setError("La palabra clave es obligatoria");
      return;
    }
    if (!selectedCategoryId) {
      setError("Elige una categoría");
      return;
    }
    if (parsedPriority === null) {
      setError(PRIORITY_ERROR);
      return;
    }
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const res = await fetch("/api/categories/rules", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          keyword,
          categoryId: selectedCategoryId,
          priority: parsedPriority,
        }),
      });
      if (!res.ok) {
        setError(await readApiError(res, "No se pudo crear la regla"));
        return;
      }
      setKeyword("");
      setPriority("0");
      setPreviewKeyword(null);
      setNotice(
        'Regla agregada. Usa "Categorizar ahora" para aplicarla a las transacciones sin categoría.'
      );
      onCreated();
    } catch {
      setError("No se pudo crear la regla");
    } finally {
      setSaving(false);
    }
  }

  if (categories.length === 0) {
    return (
      <p className="text-muted-foreground">
        Crea una categoría antes de agregar reglas.
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <form onSubmit={handleSubmit}>
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-48 flex-1">
            <label
              htmlFor="new-rule-keyword"
              className="mb-1 block text-sm text-muted-foreground"
            >
              Palabra clave
            </label>
            <Input
              id="new-rule-keyword"
              value={keyword}
              maxLength={RULE_KEYWORD_MAX_LENGTH}
              onChange={(e) => {
                setKeyword(e.target.value);
                setError(null);
                setNotice(null);
              }}
              placeholder="Ej: uber"
            />
          </div>
          <div className="w-56">
            <label
              htmlFor="new-rule-category"
              className="mb-1 block text-sm text-muted-foreground"
            >
              Categoría
            </label>
            <select
              id="new-rule-category"
              value={selectedCategoryId}
              onChange={(e) => setCategoryId(e.target.value)}
              className={SELECT_CLASS}
            >
              <option value="" disabled>
                Elige una categoría
              </option>
              {categories.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </div>
          <div className="w-28">
            <label
              htmlFor="new-rule-priority"
              className="mb-1 block text-sm text-muted-foreground"
            >
              Prioridad
            </label>
            <Input
              id="new-rule-priority"
              type="number"
              step={1}
              min={RULE_PRIORITY_MIN}
              max={RULE_PRIORITY_MAX}
              value={priority}
              onChange={(e) => setPriority(e.target.value)}
            />
          </div>
          <Button type="button" variant="outline" onClick={handleTest}>
            <FlaskConical className="h-4 w-4" />
            Probar
          </Button>
          <Button type="submit" disabled={saving}>
            <Plus className="h-4 w-4" />
            {saving ? "Guardando..." : "Agregar"}
          </Button>
        </div>
        {error && <p className="mt-1 text-sm text-destructive">{error}</p>}
        {notice && (
          <p className="mt-1 text-sm text-muted-foreground">{notice}</p>
        )}
      </form>
      {previewKeyword !== null && (
        <RulePreview
          key={previewKeyword}
          keyword={previewKeyword}
          categoriesById={categoriesById}
          version={version}
          onClose={() => setPreviewKeyword(null)}
        />
      )}
    </div>
  );
}

export function RulesCard({
  categories,
  version,
  onChange,
}: {
  categories: ApiCategory[];
  version: number;
  onChange: () => void;
}) {
  const categoriesById = useMemo(
    () => new Map(categories.map((c) => [c.id, c])),
    [categories]
  );
  const rules = useMemo(
    () => categories.flatMap((c) => c.rules).sort(byApplicationOrder),
    [categories]
  );

  const getValue = useCallback(
    (rule: ApiCategoryRule, key: RuleSortKey): string | number | null => {
      switch (key) {
        case "palabra":
          return rule.keyword;
        case "categoria":
          return categoriesById.get(rule.categoryId)?.name ?? null;
        case "prioridad":
          return rule.priority;
      }
    },
    [categoriesById]
  );
  const { sorted, sort, toggleSort } = useSortableData(rules, getValue);
  // Bridge the generic header's string key to our typed key union.
  const handleSort = (key: string) => toggleSort(key as RuleSortKey);
  const sortProps = (key: RuleSortKey) => ({
    columnKey: key,
    active: sort?.key === key,
    direction: sort?.key === key ? sort.direction : undefined,
    onSort: handleSort,
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>Reglas</CardTitle>
        <CardDescription className="space-y-2">
          <p>
            Una regla coincide cuando la descripción de la transacción contiene
            su palabra clave, sin importar mayúsculas o minúsculas. Si
            coinciden varias, gana la de mayor prioridad y, con igual
            prioridad, la más antigua: una palabra más específica como
            &quot;uber eats&quot;, agregada después de &quot;uber&quot;,
            necesita una prioridad mayor.
          </p>
          <p>
            Las transacciones nuevas se categorizan automáticamente al
            importarse. Las reglas solo completan transacciones sin categoría:
            nunca reemplazan una categoría asignada a mano, y editar o eliminar
            una regla no cambia lo que ya categorizó. &quot;Categorizar
            ahora&quot; aplica las reglas nuevas o modificadas a las
            transacciones que siguen sin categoría.
          </p>
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <AddRuleForm
          categories={categories}
          categoriesById={categoriesById}
          version={version}
          onCreated={onChange}
        />
        {rules.length === 0 ? (
          categories.length > 0 && (
            <p className="text-muted-foreground">
              No hay reglas. Agrega una arriba.
            </p>
          )
        ) : (
          <div className="space-y-2">
            <p className="text-sm text-muted-foreground">
              {plural(rules.length, "regla", "reglas")}
              {!sort && ", en el orden en que se aplican"}.
            </p>
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableTableHead
                    label="Palabra clave"
                    {...sortProps("palabra")}
                  />
                  <SortableTableHead
                    label="Categoría"
                    {...sortProps("categoria")}
                  />
                  <SortableTableHead
                    label="Prioridad"
                    align="right"
                    className="w-32"
                    {...sortProps("prioridad")}
                  />
                  <TableHead className="w-72"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sorted.map((rule) => (
                  <RuleRow
                    key={rule.id}
                    rule={rule}
                    categories={categories}
                    categoriesById={categoriesById}
                    version={version}
                    onChange={onChange}
                  />
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
