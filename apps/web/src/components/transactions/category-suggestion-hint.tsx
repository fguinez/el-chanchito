"use client";

import { useEffect, useState } from "react";
import { Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { RULE_KEYWORD_MAX_LENGTH, trimKeyword } from "@/lib/categories";
import type { CategorySuggestion } from "@/lib/category-suggestions";
import { ruleNotice } from "@/components/transactions/transaction-edit-form";

// Suggestions ignored in the panel stay hidden until the page reloads; the
// Categorías page's "Ignorar" is the one that persists.
const ignoredThisSession = new Set<string>();

/**
 * The suggested category of an uncategorized transaction, with "Aplicar"
 * (this row only, as a manual recategorization), "Aplicar siempre" (an exact
 * rule for its description, as the edit form's option) and "Ignorar". Nothing
 * is applied until the user picks an action. `onApplied` gets the notice to
 * show and should reload the panel. Silent on load errors: it is only a hint.
 */
export function CategorySuggestionHint({
  transactionId,
  description,
  onApplied,
}: {
  transactionId: string;
  description: string;
  onApplied: (notice: string) => void;
}) {
  const [suggestion, setSuggestion] = useState<CategorySuggestion | null>(null);
  const [ignored, setIgnored] = useState(() => ignoredThisSession.has(transactionId));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const tooLongForRule = trimKeyword(description).length > RULE_KEYWORD_MAX_LENGTH;

  useEffect(() => {
    const controller = new AbortController();
    fetch(
      `/api/transactions/${encodeURIComponent(transactionId)}/category-suggestion`,
      { signal: controller.signal }
    )
      .then((res) => (res.ok ? res.json() : null))
      .then((data: { suggestion: CategorySuggestion | null } | null) => {
        if (!controller.signal.aborted) setSuggestion(data?.suggestion ?? null);
      })
      .catch(() => {});
    return () => controller.abort();
  }, [transactionId]);

  if (!suggestion || ignored) return null;

  const base = `/api/transactions/${encodeURIComponent(transactionId)}`;

  const run = async (always: boolean) => {
    setBusy(true);
    setError(null);
    try {
      const res = always
        ? await fetch(`${base}/category-rule`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ categoryId: suggestion.categoryId }),
          })
        : await fetch(base, {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ categoryId: suggestion.categoryId }),
          });
      const data = await res.json().catch(() => null);
      if (!res.ok) {
        setError(data?.error ?? "No se pudo aplicar la sugerencia");
        return;
      }
      onApplied(
        always
          ? ruleNotice(data.ruleCreated, data.updatedCount)
          : `Categoría ${suggestion.categoryName} aplicada.`
      );
    } catch {
      setError("No se pudo aplicar la sugerencia");
    } finally {
      setBusy(false);
    }
  };

  const ignore = () => {
    ignoredThisSession.add(transactionId);
    setIgnored(true);
  };

  return (
    <div className="mt-2 space-y-2 rounded-md border border-dashed p-2">
      <p className="flex items-start gap-1.5 text-xs">
        <Sparkles className="mt-0.5 size-3 shrink-0 text-muted-foreground" />
        <span>
          Sugerencia: <span className="font-medium">{suggestion.categoryName}</span>
          <span className="block text-muted-foreground">{suggestion.reason}</span>
        </span>
      </p>
      <div className="flex flex-wrap gap-1.5">
        <Button size="xs" disabled={busy} onClick={() => run(false)}>
          Aplicar
        </Button>
        <Button
          size="xs"
          variant="outline"
          disabled={busy || tooLongForRule}
          onClick={() => run(true)}
          title={
            tooLongForRule
              ? `La descripción supera los ${RULE_KEYWORD_MAX_LENGTH} caracteres de una regla`
              : "Crea una regla exacta para esta descripción y categoriza las demás transacciones con la misma descripción"
          }
        >
          Aplicar siempre
        </Button>
        <Button size="xs" variant="ghost" disabled={busy} onClick={ignore}>
          Ignorar
        </Button>
      </div>
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  );
}
