"use client";

import { useState } from "react";
import { Check, FlaskConical, Pencil, Trash2, X } from "lucide-react";
import { TableCell, TableRow } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  RULE_KEYWORD_MAX_LENGTH,
  RULE_PRIORITY_MAX,
  RULE_PRIORITY_MIN,
} from "@/lib/categories";
import { RulePreview } from "./RulePreview";
import {
  CategoryLabel,
  PRIORITY_ERROR,
  SELECT_CLASS,
  parsePriority,
  readApiError,
  type ApiCategory,
  type ApiCategoryRule,
} from "./shared";

type RuleDraft = { keyword: string; categoryId: string; priority: string };

export function RuleRow({
  rule,
  categories,
  categoriesById,
  version,
  onChange,
}: {
  rule: ApiCategoryRule;
  categories: ApiCategory[];
  categoriesById: Map<string, ApiCategory>;
  version: number;
  onChange: () => void;
}) {
  // null while viewing; the fields being edited otherwise.
  const [draft, setDraft] = useState<RuleDraft | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The keyword under test (the draft's while editing), or null when closed.
  const [previewKeyword, setPreviewKeyword] = useState<string | null>(null);

  const category = categoriesById.get(rule.categoryId);

  function startEditing() {
    setError(null);
    setDraft({
      keyword: rule.keyword,
      categoryId: rule.categoryId,
      priority: String(rule.priority),
    });
  }

  function stopEditing() {
    setDraft(null);
    setError(null);
  }

  function handleTest() {
    const keyword = (draft?.keyword ?? rule.keyword).trim();
    if (!keyword) {
      setError("Escribe una palabra clave para probarla");
      return;
    }
    setPreviewKeyword(keyword);
  }

  async function handleSave() {
    if (!draft) return;
    const keyword = draft.keyword.trim().toLowerCase();
    const priority = parsePriority(draft.priority);
    if (!keyword) {
      setError("La palabra clave es obligatoria");
      return;
    }
    if (priority === null) {
      setError(PRIORITY_ERROR);
      return;
    }
    const patch: Record<string, string | number> = {};
    if (keyword !== rule.keyword) patch.keyword = keyword;
    if (draft.categoryId !== rule.categoryId) patch.categoryId = draft.categoryId;
    if (priority !== rule.priority) patch.priority = priority;
    if (Object.keys(patch).length === 0) {
      stopEditing();
      return;
    }

    setSaving(true);
    setError(null);
    try {
      const res = await fetch(`/api/categories/rules/${rule.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!res.ok) {
        setError(await readApiError(res, "No se pudo guardar la regla"));
        return;
      }
      stopEditing();
      onChange();
    } catch {
      setError("No se pudo guardar la regla");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (
      !window.confirm(
        `¿Eliminar la regla "${rule.keyword}"?\n\nLas transacciones que ya categorizó mantienen su categoría.`
      )
    ) {
      return;
    }
    setError(null);
    try {
      const res = await fetch(`/api/categories/rules/${rule.id}`, {
        method: "DELETE",
      });
      if (!res.ok) {
        setError(await readApiError(res, "No se pudo eliminar la regla"));
        return;
      }
      onChange();
    } catch {
      setError("No se pudo eliminar la regla");
    }
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Enter") handleSave();
    if (e.key === "Escape") stopEditing();
  }

  return (
    <>
      <TableRow>
        <TableCell>
          {draft ? (
            <Input
              aria-label="Palabra clave de la regla"
              value={draft.keyword}
              maxLength={RULE_KEYWORD_MAX_LENGTH}
              onChange={(e) => setDraft({ ...draft, keyword: e.target.value })}
              onKeyDown={onKeyDown}
              className="max-w-56"
              autoFocus
            />
          ) : (
            <span className="font-mono text-sm">{rule.keyword}</span>
          )}
          {error && (
            <p className="mt-1 max-w-56 text-sm whitespace-normal text-destructive">
              {error}
            </p>
          )}
        </TableCell>
        <TableCell>
          {draft ? (
            <select
              aria-label="Categoría de la regla"
              value={draft.categoryId}
              onChange={(e) =>
                setDraft({ ...draft, categoryId: e.target.value })
              }
              className={`${SELECT_CLASS} max-w-56`}
            >
              {categories.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          ) : category ? (
            <CategoryLabel category={category} />
          ) : null}
        </TableCell>
        <TableCell className="text-right tabular-nums">
          {draft ? (
            <Input
              aria-label="Prioridad de la regla"
              type="number"
              step={1}
              min={RULE_PRIORITY_MIN}
              max={RULE_PRIORITY_MAX}
              value={draft.priority}
              onChange={(e) => setDraft({ ...draft, priority: e.target.value })}
              onKeyDown={onKeyDown}
              className="ml-auto w-24 text-right"
            />
          ) : (
            rule.priority
          )}
        </TableCell>
        <TableCell>
          <div className="flex justify-end gap-1">
            <Button variant="ghost" size="sm" onClick={handleTest}>
              <FlaskConical className="h-4 w-4" />
              Probar
            </Button>
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
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Editar regla ${rule.keyword}`}
                  title="Editar"
                  onClick={startEditing}
                >
                  <Pencil />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Eliminar regla ${rule.keyword}`}
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
      {previewKeyword !== null && (
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={4} className="whitespace-normal">
            <RulePreview
              key={previewKeyword}
              keyword={previewKeyword}
              categoriesById={categoriesById}
              version={version}
              onClose={() => setPreviewKeyword(null)}
            />
          </TableCell>
        </TableRow>
      )}
    </>
  );
}
