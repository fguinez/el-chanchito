"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { formatCLP, cn } from "@/lib/utils";
import { AlertTriangle, Building2, ExternalLink, RefreshCw } from "lucide-react";
import {
  INSTITUTION_KIND_LABELS,
  SCRAPER_SLUGS,
  timeAgo,
  formatBalance,
  InstitutionProductsTable,
  type ApiInstitution,
  type InstitutionsResponse,
  type InstitutionTotals,
} from "@/components/institutions/shared";
import { useInstitutionRefresh } from "@/components/institutions/use-institution-refresh";

export default function InstitutionsPage() {
  const [institutions, setInstitutions] = useState<ApiInstitution[] | null>(
    null
  );
  const [totals, setTotals] = useState<InstitutionTotals | null>(null);
  const [error, setError] = useState(false);

  async function loadInstitutions() {
    try {
      const res = await fetch("/api/institutions");
      if (!res.ok) throw new Error("failed");
      const data: InstitutionsResponse = await res.json();
      setInstitutions(data.institutions);
      setTotals(data.totals);
    } catch {
      setError(true);
    }
  }

  useEffect(() => {
    loadInstitutions();
  }, []);

  const { syncing, serviceError, refresh } =
    useInstitutionRefresh(loadInstitutions);

  if (error) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Instituciones</h2>
        <p className="text-muted-foreground">
          No se pudieron cargar las instituciones.
        </p>
      </div>
    );
  }

  if (!institutions) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Instituciones</h2>
        <p className="text-muted-foreground">Cargando...</p>
      </div>
    );
  }

  const totalProducts = institutions.reduce(
    (sum, inst) => sum + inst.products.length,
    0
  );
  const lastUpdated = institutions
    .flatMap((inst) => inst.products)
    .map((p) => p.balanceAsOf)
    .filter((v): v is string => v != null)
    .sort()
    .at(-1);

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-2xl font-bold">Instituciones</h2>
          <p className="text-sm text-muted-foreground">
            Cada banco, fintech y exchange que sigues, con el detalle de sus
            productos.
          </p>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => refresh()}
          disabled={syncing.size > 0}
        >
          <RefreshCw
            className={cn("h-4 w-4", syncing.size > 0 && "animate-spin")}
          />
          {syncing.size > 0 ? "Sincronizando…" : "Actualizar todo"}
        </Button>
      </div>

      {serviceError && (
        <div className="flex items-start gap-3 rounded-md border border-red-200 bg-red-50 p-3 dark:border-red-900 dark:bg-red-950">
          <AlertTriangle className="mt-0.5 h-4 w-4 text-red-600" />
          <p className="text-sm text-red-800 dark:text-red-200">
            {serviceError}
          </p>
        </div>
      )}

      {/* Summary */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Instituciones</CardDescription>
            <CardTitle className="text-2xl">{institutions.length}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Productos</CardDescription>
            <CardTitle className="text-2xl">{totalProducts}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Patrimonio neto (CLP)</CardDescription>
            <CardTitle className="text-2xl">
              {totals ? formatCLP(totals.netClp) : "—"}
            </CardTitle>
            {totals && totals.deudaClp > 0 && (
              <p className="text-xs text-muted-foreground">
                deuda {formatCLP(totals.deudaClp)}
              </p>
            )}
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Última actualización</CardDescription>
            <CardTitle className="text-2xl">
              {lastUpdated ? timeAgo(lastUpdated) : "—"}
            </CardTitle>
          </CardHeader>
        </Card>
      </div>

      {institutions.length === 0 ? (
        <Card>
          <CardContent className="pt-6">
            <p className="text-center text-muted-foreground">
              Todavía no hay productos. Cuando los scrapers importen datos,
              cada institución aparecerá aquí con sus cuentas y saldos.
            </p>
          </CardContent>
        </Card>
      ) : (
        institutions.map((inst) => (
          <Card
            key={inst.id}
            className="transition-colors hover:border-primary/40"
          >
            <CardHeader>
              <div className="flex items-start justify-between gap-3">
                <Link
                  href={`/institutions/${inst.slug}`}
                  className="group flex items-center gap-3"
                >
                  <div className="flex h-9 w-9 items-center justify-center rounded-md bg-muted transition-colors group-hover:bg-primary/10">
                    <Building2 className="h-5 w-5 text-muted-foreground" />
                  </div>
                  <div>
                    <CardTitle className="flex items-center gap-2 text-lg transition-colors group-hover:text-primary">
                      {inst.name}
                      <Badge variant="secondary" className="text-xs">
                        {INSTITUTION_KIND_LABELS[inst.kind] ?? inst.kind}
                      </Badge>
                    </CardTitle>
                    <CardDescription>
                      {inst.products.length}{" "}
                      {inst.products.length === 1 ? "producto" : "productos"}
                      {inst.country ? ` · ${inst.country}` : ""}
                    </CardDescription>
                  </div>
                </Link>
                <div className="flex items-center gap-2">
                  {inst.url && (
                    <a
                      href={inst.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground"
                    >
                      Sitio <ExternalLink className="h-3.5 w-3.5" />
                    </a>
                  )}
                  <Button
                    variant="ghost"
                    size="icon-sm"
                    onClick={() => refresh(inst.slug)}
                    disabled={
                      !SCRAPER_SLUGS.has(inst.slug) || syncing.has(inst.slug)
                    }
                    title={
                      SCRAPER_SLUGS.has(inst.slug)
                        ? "Actualizar"
                        : "Sin scraper disponible"
                    }
                  >
                    <RefreshCw
                      className={cn(
                        "h-4 w-4",
                        syncing.has(inst.slug) && "animate-spin"
                      )}
                    />
                  </Button>
                </div>
              </div>
            </CardHeader>
            <CardContent>
              <InstitutionProductsTable
                products={inst.products}
                institutionName={inst.name}
                institutionSlug={inst.slug}
              />

              {/* Per-institution subtotals: holdings by currency + CLP total */}
              {inst.subtotals.clp != null && (
                <div className="mt-3 flex flex-wrap items-center justify-between gap-2 border-t pt-3 text-sm">
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                    <span className="font-medium">Saldos por moneda:</span>
                    {inst.subtotals.byCurrency.length > 0 ? (
                      inst.subtotals.byCurrency.map((s) => (
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
                    <span className="text-muted-foreground">
                      Total en CLP{" "}
                    </span>
                    <span
                      className={cn(
                        "font-semibold tabular-nums",
                        inst.subtotals.clp < 0 && "text-red-600"
                      )}
                    >
                      ≈ {formatCLP(inst.subtotals.clp)}
                    </span>
                    {!inst.subtotals.convertible && (
                      <span className="ml-1 text-xs text-muted-foreground">
                        (algunos saldos sin conversión)
                      </span>
                    )}
                  </div>
                </div>
              )}
            </CardContent>
          </Card>
        ))
      )}
    </div>
  );
}
