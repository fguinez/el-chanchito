"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { ScraperStatus } from "@/components/dashboard/ScraperStatus";
import { MonitorCard } from "@/components/monitors/MonitorCard";
import { type ApiMonitor } from "@/components/monitors/shared";
import { STALE_AFTER_HOURS, groupForInicio } from "@/lib/monitors/overview";

const STALE_AFTER_DAYS = STALE_AFTER_HOURS / 24;

/** Note for the OK monitors computed from stale data, singular or plural. */
function staleOkNote(count: number): string {
  const subject =
    count === 1 ? "1 monitor en orden usa" : `${count} monitores en orden usan`;
  return `${subject} datos de hace más de ${STALE_AFTER_DAYS} días.`;
}

export default function HomePage() {
  const [monitorList, setMonitorList] = useState<ApiMonitor[] | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    fetch("/api/monitors")
      .then((res) => {
        if (!res.ok) throw new Error("failed");
        return res.json();
      })
      .then((data: { monitors: ApiMonitor[] }) => setMonitorList(data.monitors))
      .catch(() => setError(true));
  }, []);

  const { attention, noData, staleOk } = groupForInicio(
    monitorList ?? [],
    new Date()
  );
  const hasActive = monitorList?.some((m) => m.isActive) ?? false;
  const staleNote = staleOk.length > 0 && (
    <p className="text-xs text-muted-foreground">
      {staleOkNote(staleOk.length)}
    </p>
  );

  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-bold">Inicio</h2>

      {/* Monitores overview */}
      <div className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <CardTitle className="text-base">Monitores</CardTitle>
            <p className="text-sm text-muted-foreground">
              Aquí aparecen las alertas, las advertencias y los monitores sin
              datos.
            </p>
          </div>
          <Button asChild variant="outline" size="sm">
            <Link href="/monitors">Ver todos</Link>
          </Button>
        </div>

        {error ? (
          <p className="text-muted-foreground">
            No se pudieron cargar los monitores.
          </p>
        ) : !monitorList ? (
          <p className="text-muted-foreground">Cargando...</p>
        ) : monitorList.length === 0 ? (
          <Card>
            <CardContent className="space-y-4 text-center">
              <p className="text-muted-foreground">
                Todavía no hay monitores. Crea el primero para vigilar saldos,
                deudas y umbrales de tus productos.
              </p>
              <Button asChild>
                <Link href="/monitors/new">Nuevo monitor</Link>
              </Button>
            </CardContent>
          </Card>
        ) : attention.length === 0 && noData.length === 0 ? (
          <Card>
            <CardContent className="space-y-1 text-center">
              <p className="text-muted-foreground">
                {hasActive
                  ? "Todo en orden: ningún monitor necesita atención."
                  : "No hay monitores activos."}
              </p>
              {staleNote}
            </CardContent>
          </Card>
        ) : (
          <div className="space-y-3">
            {attention.map((monitor) => (
              <MonitorCard key={monitor.id} monitor={monitor} />
            ))}
            {noData.length > 0 && (
              <div className="space-y-3 pt-2">
                <div>
                  <h3 className="text-sm font-semibold">Sin datos</h3>
                  <p className="text-sm text-muted-foreground">
                    No se pueden calcular: revisa el motivo en cada uno.
                  </p>
                </div>
                {noData.map((monitor) => (
                  <MonitorCard key={monitor.id} monitor={monitor} />
                ))}
              </div>
            )}
            {staleNote}
          </div>
        )}
      </div>

      {/* Scraper status */}
      <ScraperStatus />

      {/* Quick actions */}
      <div className="grid gap-4 md:grid-cols-3">
        <Link href="/expenses">
          <Card className="cursor-pointer transition-colors hover:bg-muted/50">
            <CardHeader className="pb-2">
              <CardTitle className="text-base">Agregar gasto</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-sm text-muted-foreground">
                Registrar un gasto manual
              </p>
            </CardContent>
          </Card>
        </Link>
        <Link href="/fixed">
          <Card className="cursor-pointer transition-colors hover:bg-muted/50">
            <CardHeader className="pb-2">
              <CardTitle className="text-base">Gastos fijos</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-sm text-muted-foreground">
                Administrar gastos mensuales fijos
              </p>
            </CardContent>
          </Card>
        </Link>
        <Link href="/monitors">
          <Card className="cursor-pointer transition-colors hover:bg-muted/50">
            <CardHeader className="pb-2">
              <CardTitle className="text-base">Monitores</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-sm text-muted-foreground">
                Vigilar saldos y umbrales de tus productos
              </p>
            </CardContent>
          </Card>
        </Link>
      </div>
    </div>
  );
}
