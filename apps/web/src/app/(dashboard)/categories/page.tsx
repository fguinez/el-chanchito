"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { WandSparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CategoriesCard } from "@/components/categories/CategoriesCard";
import { CategoryDetailSheet } from "@/components/categories/CategoryDetailSheet";
import { RulesCard } from "@/components/categories/RulesCard";
import { SuggestionsCard } from "@/components/categories/SuggestionsCard";
import { plural, type ApiCategory } from "@/components/categories/shared";
import { TransactionDetailSheet } from "@/components/transactions/transaction-detail-sheet";
import { useSelectedTransaction } from "@/components/transactions/use-selected-transaction";

// useSearchParams needs a Suspense boundary for the static prerender.
export default function CategoriesPage() {
  return (
    <Suspense
      fallback={<p className="text-muted-foreground">Cargando...</p>}
    >
      <CategoriesView />
    </Suspense>
  );
}

function CategoriesView() {
  const { selectedTxnId, selectTransaction } = useSelectedTransaction();
  const [categories, setCategories] = useState<ApiCategory[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  // Bumped on every reload so open rule previews refetch.
  const [version, setVersion] = useState(0);
  const [categorizing, setCategorizing] = useState(false);
  const [categorizeResult, setCategorizeResult] = useState<string | null>(null);
  const [categorizeError, setCategorizeError] = useState<string | null>(null);
  // Only the latest load may update state, so a slow earlier response never
  // overwrites a newer one.
  const loadSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++loadSeq.current;
    try {
      const res = await fetch("/api/categories");
      if (!res.ok) throw new Error("failed");
      const data: ApiCategory[] = await res.json();
      if (seq !== loadSeq.current) return;
      setCategories(data);
      setVersion((v) => v + 1);
      setLoadError(false);
    } catch {
      if (seq !== loadSeq.current) return;
      setLoadError(true);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function handleCategorize() {
    setCategorizing(true);
    setCategorizeResult(null);
    setCategorizeError(null);
    try {
      const res = await fetch("/api/categories", { method: "PUT" });
      if (!res.ok) throw new Error("failed");
      const { assigned } = (await res.json()) as { assigned: number };
      setCategorizeResult(
        assigned === 0
          ? "No había transacciones por categorizar"
          : plural(
              assigned,
              "transacción categorizada",
              "transacciones categorizadas"
            )
      );
      await load();
    } catch {
      setCategorizeError("No se pudo categorizar");
    } finally {
      setCategorizing(false);
    }
  }

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-3">
        <div className="max-w-2xl">
          <h2 className="text-2xl font-bold">Categorías</h2>
          <p className="text-sm text-muted-foreground">
            Las transacciones nuevas se categorizan automáticamente al
            importarse, según tus reglas. &quot;Categorizar ahora&quot; aplica
            las reglas nuevas o modificadas a las que siguen sin categoría.
          </p>
        </div>
        <div className="flex flex-col items-end gap-1">
          <Button
            size="sm"
            onClick={handleCategorize}
            disabled={categorizing || !categories}
          >
            <WandSparkles className="h-4 w-4" />
            {categorizing ? "Categorizando..." : "Categorizar ahora"}
          </Button>
          {categorizeResult && (
            <p role="status" className="text-sm text-muted-foreground">
              {categorizeResult}
            </p>
          )}
          {categorizeError && (
            <p className="text-sm text-destructive">{categorizeError}</p>
          )}
        </div>
      </div>

      {loadError && !categories ? (
        <p className="text-muted-foreground">
          No se pudieron cargar las categorías.
        </p>
      ) : !categories ? (
        <p className="text-muted-foreground">Cargando...</p>
      ) : (
        <>
          {loadError && (
            <p className="text-sm text-destructive">
              No se pudieron actualizar las categorías; lo que ves puede estar
              desactualizado.
            </p>
          )}
          <CategoriesCard
            categories={categories}
            version={version}
            onChange={load}
          />
          <SuggestionsCard
            categories={categories}
            version={version}
            onChange={load}
          />
          <RulesCard categories={categories} version={version} onChange={load} />
          {/* useSearchParams needs a Suspense boundary for the prerender. */}
          <Suspense fallback={null}>
            <CategoryDetailSheet
              categories={categories}
              refreshKey={version}
              onChanged={load}
            />
          </Suspense>
        </>
      )}
      <TransactionDetailSheet
        transactionId={selectedTxnId}
        onClose={() => selectTransaction(null)}
        onChanged={load}
      />
    </div>
  );
}
