"use client";

import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { AliasesCard } from "@/components/aliases/AliasesCard";
import { type ApiAlias } from "@/components/aliases/shared";
import { MATCH_TYPES, type MatchType } from "@/lib/aliases";

// useSearchParams needs a Suspense boundary for the static prerender.
export default function AliasesPage() {
  return (
    <Suspense fallback={<AliasesSkeleton />}>
      <AliasesView />
    </Suspense>
  );
}

/** Static shell for the prerender, before search params are available. */
function AliasesSkeleton() {
  return (
    <div className="space-y-6" aria-busy="true">
      <h2 className="text-2xl font-bold">Alias</h2>
      <div className="h-40 animate-pulse rounded bg-muted" />
    </div>
  );
}

function isMatchType(value: string | null): value is MatchType {
  return value !== null && (MATCH_TYPES as readonly string[]).includes(value);
}

function AliasesView() {
  const searchParams = useSearchParams();
  // Prefill for the add form, from the detail-panel shortcut.
  const initialMatchDescription = searchParams.get("matchDescription") ?? "";
  const rawMatchType = searchParams.get("matchType");
  const initialMatchType: MatchType = isMatchType(rawMatchType)
    ? rawMatchType
    : "exact";

  const [aliases, setAliases] = useState<ApiAlias[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  // Bumped on every reload so open previews refetch.
  const [version, setVersion] = useState(0);
  // Only the latest load may update state, so a slow earlier response never
  // overwrites a newer one.
  const loadSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++loadSeq.current;
    try {
      const res = await fetch("/api/aliases");
      if (!res.ok) throw new Error("failed");
      const data: ApiAlias[] = await res.json();
      if (seq !== loadSeq.current) return;
      setAliases(data);
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

  return (
    <div className="space-y-6">
      <div className="max-w-2xl">
        <h2 className="text-2xl font-bold">Alias</h2>
        <p className="text-sm text-muted-foreground">
          Un alias cambia el nombre de toda transacción cuya descripción
          original coincide, de forma retroactiva y para los próximos scrapes. Se
          aplica al momento de mostrar: editar un alias actualiza las listas de
          inmediato, sin un paso de &quot;aplicar&quot; (a diferencia de las
          categorías).
        </p>
      </div>

      {loadError && !aliases ? (
        <p className="text-muted-foreground">No se pudieron cargar los alias.</p>
      ) : !aliases ? (
        <p className="text-muted-foreground">Cargando...</p>
      ) : (
        <>
          {loadError && (
            <p className="text-sm text-destructive">
              No se pudieron actualizar los alias; lo que ves puede estar
              desactualizado.
            </p>
          )}
          <AliasesCard
            aliases={aliases}
            version={version}
            initialMatchDescription={initialMatchDescription}
            initialMatchType={initialMatchType}
            onChange={load}
          />
        </>
      )}
    </div>
  );
}
