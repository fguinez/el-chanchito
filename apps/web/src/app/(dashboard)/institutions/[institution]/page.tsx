"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import {
  Card,
  CardContent,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatCLP, cn } from "@/lib/utils";
import { AlertTriangle, ArrowLeft, Building2, ExternalLink, RefreshCw } from "lucide-react";
import {
  INSTITUTION_KIND_LABELS,
  SCRAPER_SLUGS,
  formatBalance,
  InstitutionProductsTable,
  type ApiInstitution,
} from "@/components/institutions/shared";
import { useInstitutionRefresh } from "@/components/institutions/use-institution-refresh";

export default function InstitutionDetailPage() {
  const { institution: slug } = useParams<{ institution: string }>();

  const [institution, setInstitution] = useState<ApiInstitution | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState(false);

  const loadInstitution = useCallback(async () => {
    try {
      const res = await fetch(`/api/institutions/${slug}`);
      if (res.status === 404) {
        setNotFound(true);
        setInstitution(null);
        return;
      }
      if (!res.ok) throw new Error("failed");
      const data: { institution: ApiInstitution } = await res.json();
      setInstitution(data.institution);
      setNotFound(false);
      setError(false);
    } catch {
      setError(true);
    }
  }, [slug]);

  useEffect(() => {
    loadInstitution();
  }, [loadInstitution]);

  const { syncing, serviceError, refresh } = useInstitutionRefresh(loadInstitution);

  if (notFound) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Institución</h2>
        <p className="text-muted-foreground">Institución no encontrada.</p>
        <Button asChild variant="outline" size="sm">
          <Link href="/institutions">Volver a Instituciones</Link>
        </Button>
      </div>
    );
  }
  if (error && !institution) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Institución</h2>
        <p className="text-muted-foreground">
          No se pudo cargar la institución.
        </p>
      </div>
    );
  }
  if (!institution) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Institución</h2>
        <p className="text-muted-foreground">Cargando...</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Link
          href="/institutions"
          className="flex items-center gap-1 hover:text-foreground"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          Instituciones
        </Link>
      </div>

      <div className="flex items-start justify-between gap-3">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-md bg-muted">
            <Building2 className="h-5 w-5 text-muted-foreground" />
          </div>
          <div>
            <h2 className="flex flex-wrap items-center gap-2 text-2xl font-bold">
              {institution.name}
              <Badge variant="secondary" className="text-xs">
                {INSTITUTION_KIND_LABELS[institution.kind] ?? institution.kind}
              </Badge>
            </h2>
            <p className="text-sm text-muted-foreground">
              {institution.products.length}{" "}
              {institution.products.length === 1 ? "producto" : "productos"}
              {institution.country ? ` · ${institution.country}` : ""}
            </p>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {institution.url && (
            <a
              href={institution.url}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
            >
              Sitio <ExternalLink className="h-3.5 w-3.5" />
            </a>
          )}
          <Button
            variant="outline"
            size="sm"
            onClick={() => refresh(institution.slug)}
            disabled={!SCRAPER_SLUGS.has(institution.slug) || syncing.has(institution.slug)}
            title={
              SCRAPER_SLUGS.has(institution.slug)
                ? "Actualizar"
                : "Sin scraper disponible"
            }
          >
            <RefreshCw
              className={cn("h-4 w-4", syncing.has(institution.slug) && "animate-spin")}
            />
            {syncing.has(institution.slug) ? "Actualizando…" : "Actualizar"}
          </Button>
        </div>
      </div>

      {serviceError && (
        <div className="flex items-start gap-3 rounded-md border border-red-200 bg-red-50 p-3 dark:border-red-900 dark:bg-red-950">
          <AlertTriangle className="mt-0.5 h-4 w-4 text-red-600" />
          <p className="text-sm text-red-800 dark:text-red-200">{serviceError}</p>
        </div>
      )}

      <Card>
        <CardContent className="pt-6">
          <InstitutionProductsTable
            products={institution.products}
            institutionName={institution.name}
            institutionSlug={institution.slug}
          />

          {/* Per-institution subtotals: holdings by currency + CLP total */}
          {institution.subtotals.clp != null && (
            <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t pt-3 text-sm">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span className="font-medium">Saldos por moneda:</span>
                {institution.subtotals.byCurrency.length > 0 ? (
                  institution.subtotals.byCurrency.map((s) => (
                    <Badge
                      key={s.currency}
                      variant="secondary"
                      className="font-normal tabular-nums"
                    >
                      {formatBalance(s.currency, s.amount)}
                    </Badge>
                  ))
                ) : (
                  <span className="text-muted-foreground">sin saldos</span>
                )}
              </div>
              <div className="text-right">
                <span className="text-muted-foreground">Total en CLP </span>
                <span
                  className={cn(
                    "font-semibold tabular-nums",
                    institution.subtotals.clp < 0 && "text-red-600"
                  )}
                >
                  ≈ {formatCLP(institution.subtotals.clp)}
                </span>
                {!institution.subtotals.convertible && (
                  <span className="ml-1 text-xs text-muted-foreground">
                    (algunos saldos sin conversión)
                  </span>
                )}
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
