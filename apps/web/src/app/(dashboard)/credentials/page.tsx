"use client";

import { useCallback, useEffect, useState } from "react";
import { CredentialCard } from "@/components/credentials/credential-card";
import {
  CREDENTIAL_SPECS,
  type CredentialsOverview,
} from "@/lib/credentials/registry";

export default function CredentialsPage() {
  const [overview, setOverview] = useState<CredentialsOverview | null>(null);
  const [loadError, setLoadError] = useState(false);

  const load = useCallback(async () => {
    try {
      const response = await fetch("/api/credentials", { cache: "no-store" });
      if (!response.ok) throw new Error(String(response.status));
      setOverview((await response.json()) as CredentialsOverview);
      setLoadError(false);
    } catch {
      setLoadError(true);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const canSave = Boolean(overview?.keyPublished) && overview?.keyAvailable !== false;

  return (
    <div className="space-y-6">
      <div className="space-y-2">
        <h2 className="text-2xl font-bold">Credenciales</h2>
        <p className="max-w-3xl text-sm text-muted-foreground">
          Lo que guardes aquí se cifra en el servidor con la clave pública del servicio de
          scrapers, que es el único que puede leerlo. Nada vuelve a mostrarse: para corregir una
          credencial, escríbela de nuevo. Lo guardado aquí tiene prioridad sobre el almacén local
          (<code>make secrets-init</code>), que sigue funcionando como respaldo.
        </p>
      </div>

      {loadError && (
        <p role="alert" className="rounded-md border border-destructive/40 p-3 text-sm text-destructive">
          No se pudo cargar el estado de las credenciales.
        </p>
      )}
      {overview && !overview.keyPublished && (
        <p role="alert" className="rounded-md border p-3 text-sm">
          El servicio de scrapers aún no ha publicado su clave de cifrado, así que no se puede
          guardar nada todavía. Inicia el stack completo con <code>make dev</code>.
        </p>
      )}
      {overview?.keyAvailable === false && (
        <p role="alert" className="rounded-md border border-destructive/40 p-3 text-sm text-destructive">
          El servicio de scrapers no puede usar su archivo de clave (permisos o formato). Revisa{" "}
          <code>local/scrapers-dev.log</code>.
        </p>
      )}
      {overview && overview.keyPublished && !overview.serviceReachable && (
        <p className="rounded-md border p-3 text-sm text-muted-foreground">
          El servicio de scrapers no responde: no se sabe qué hay en su almacén local, y los
          cambios se aplicarán cuando arranque.
        </p>
      )}

      {overview === null && !loadError ? (
        <div className="h-40 animate-pulse rounded bg-muted" aria-busy="true" />
      ) : (
        <div className="grid gap-4 lg:grid-cols-2">
          {CREDENTIAL_SPECS.map((spec) => (
            <CredentialCard
              key={spec.slug}
              spec={spec}
              status={overview?.institutions.find((i) => i.slug === spec.slug)}
              canSave={canSave}
              onChanged={load}
            />
          ))}
        </div>
      )}
    </div>
  );
}
