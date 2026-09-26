"use client";

import { useCallback, useEffect, useState } from "react";
import { WandSparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { CategoriesCard } from "@/components/categories/CategoriesCard";
import { RulesCard } from "@/components/categories/RulesCard";
import { plural, type ApiCategory } from "@/components/categories/shared";

export default function CategoriesPage() {
  const [categories, setCategories] = useState<ApiCategory[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  // Bumped on every reload so open rule previews refetch.
  const [version, setVersion] = useState(0);
  const [categorizing, setCategorizing] = useState(false);
  const [categorizeResult, setCategorizeResult] = useState<string | null>(null);
  const [categorizeError, setCategorizeError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/categories");
      if (!res.ok) throw new Error("failed");
      setCategories(await res.json());
      setVersion((v) => v + 1);
      setLoadError(false);
    } catch {
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
          <CategoriesCard categories={categories} onChange={load} />
          <RulesCard categories={categories} version={version} onChange={load} />
        </>
      )}
    </div>
  );
}
