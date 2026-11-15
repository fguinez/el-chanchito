"use client";

import { useCallback, useEffect, useState } from "react";
import { CredentialCard } from "@/components/credentials/credential-card";
import { SAVE_BLOCKED_MESSAGES } from "@/lib/credentials/availability";
import { describeKeyInfo } from "@/lib/credentials/key-info";
import {
  CREDENTIAL_SPECS,
  type CredentialsOverview,
} from "@/lib/credentials/registry";

/** Messages mark commands with backticks; render them as code. */
function withCode(text: string) {
  return text
    .split("`")
    .map((part, i) => (i % 2 === 1 ? <code key={i}>{part}</code> : part));
}

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

  const blocked = overview?.saveBlocked ?? null;
  const keyLines = overview?.key ? describeKeyInfo(overview.key) : null;

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
      {blocked && (
        <p
          role="alert"
          className={
            blocked === "key_mismatch" || blocked === "key_unavailable"
              ? "rounded-md border border-destructive/40 p-3 text-sm text-destructive"
              : "rounded-md border p-3 text-sm"
          }
        >
          <span className="font-medium">No se puede guardar por ahora. </span>
          {withCode(SAVE_BLOCKED_MESSAGES[blocked])}
        </p>
      )}

      {keyLines && (
        <section
          aria-labelledby="credentials-key-heading"
          className="max-w-3xl space-y-1 rounded-md border p-3 text-sm"
        >
          <h3 id="credentials-key-heading" className="font-medium">
            Clave de cifrado
          </h3>
          <p className="text-muted-foreground">{keyLines.age}</p>
          {keyLines.schedule && <p className="text-muted-foreground">{keyLines.schedule}</p>}
          {keyLines.failure && <p className="text-destructive">{keyLines.failure}</p>}
          <p className="text-xs text-muted-foreground">
            Rotar la clave vuelve a cifrar todo con una nueva y borra el archivo de la anterior,
            así que los respaldos antiguos de la base de datos ya no se pueden leer con ella (salvo
            que esa clave siga en una instantánea del disco, un respaldo del volumen o una copia
            del archivo). También puedes rotarla a mano con <code>make credentials-rotate</code>.
          </p>
        </section>
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
              canSave={Boolean(overview?.canSave)}
              onChanged={load}
            />
          ))}
        </div>
      )}
    </div>
  );
}
