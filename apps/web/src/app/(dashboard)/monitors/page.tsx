"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Plus } from "lucide-react";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { MonitorCard } from "@/components/monitors/MonitorCard";
import { type ApiMonitor } from "@/components/monitors/shared";

export default function MonitorsPage() {
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

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-2xl font-bold">Monitores</h2>
          <p className="text-sm text-muted-foreground">
            Ecuaciones sobre tus productos, con umbrales de alerta y
            advertencia.
          </p>
        </div>
        <Button asChild size="sm">
          <Link href="/monitors/new">
            <Plus className="h-4 w-4" />
            Nuevo monitor
          </Link>
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
      ) : (
        <div className="space-y-3">
          {monitorList.map((monitor) => (
            <MonitorCard key={monitor.id} monitor={monitor} />
          ))}
        </div>
      )}
    </div>
  );
}
