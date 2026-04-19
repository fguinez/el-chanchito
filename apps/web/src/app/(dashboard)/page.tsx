"use client";

import { useEffect, useState } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { formatCLP, cn } from "@/lib/utils";
import type { TodayStatus, DayPlan } from "@/lib/budget-engine";
import {
  CalendarDays,
  TrendingDown,
  TrendingUp,
  Wallet,
  ArrowRight,
} from "lucide-react";
import Link from "next/link";
import { ScraperStatus } from "@/components/dashboard/ScraperStatus";

interface PlanningData {
  plans: DayPlan[];
  todayStatus: TodayStatus;
  month: string;
  daysInMonth: number;
}

export default function HomePage() {
  const [data, setData] = useState<PlanningData | null>(null);
  const [noConfig, setNoConfig] = useState(false);

  useEffect(() => {
    fetch("/api/planning")
      .then((res) => {
        if (res.status === 404) {
          setNoConfig(true);
          return null;
        }
        return res.json();
      })
      .then((d) => d && setData(d))
      .catch(() => setNoConfig(true));
  }, []);

  if (noConfig) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Inicio</h2>
        <Card>
          <CardContent className="pt-6">
            <div className="text-center space-y-4">
              <p className="text-muted-foreground">
                No hay configuracion de presupuesto para este mes.
              </p>
              <Link href="/settings">
                <Button>Configurar presupuesto</Button>
              </Link>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Inicio</h2>
        <p className="text-muted-foreground">Cargando...</p>
      </div>
    );
  }

  const { todayStatus, plans } = data;
  const todayPlan = plans.find((p) => p.isToday);

  // Get the next 5 days from planning table for preview
  const upcomingDays = plans.filter(
    (p) => p.day >= todayStatus.todayDay && p.day <= todayStatus.todayDay + 5
  );

  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-bold">Inicio</h2>

      {/* Main status cards */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardDescription>Saldo esperado hoy</CardDescription>
            <Wallet className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">
              {formatCLP(todayStatus.expectedBalance)}
            </div>
            <p className="text-xs text-muted-foreground">
              Dia {todayStatus.todayDay} de {todayStatus.daysInMonth}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardDescription>Presupuesto diario</CardDescription>
            <CalendarDays className="h-4 w-4 text-muted-foreground" />
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">
              {formatCLP(todayStatus.presupDiario)}
            </div>
            <p className="text-xs text-muted-foreground">
              {todayStatus.daysRemaining} dias restantes
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardDescription>Presupuesto restante</CardDescription>
            {todayStatus.budgetRemainingMonth >= 0 ? (
              <TrendingUp className="h-4 w-4 text-green-600" />
            ) : (
              <TrendingDown className="h-4 w-4 text-red-600" />
            )}
          </CardHeader>
          <CardContent>
            <div
              className={cn(
                "text-2xl font-bold",
                todayStatus.budgetRemainingMonth >= 0
                  ? "text-green-600"
                  : "text-red-600"
              )}
            >
              {formatCLP(todayStatus.budgetRemainingMonth)}
            </div>
            <p className="text-xs text-muted-foreground">
              Del presupuesto variable mensual
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row items-center justify-between pb-2">
            <CardDescription>Desviacion</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="text-2xl font-bold">
              {todayStatus.drift !== null
                ? formatCLP(todayStatus.drift)
                : "Sin datos"}
            </div>
            <p className="text-xs text-muted-foreground">
              {todayStatus.drift !== null
                ? todayStatus.drift >= 0
                  ? "Bajo presupuesto"
                  : "Sobre presupuesto"
                : "Conecta una cuenta para ver"}
            </p>
          </CardContent>
        </Card>
      </div>

      {/* Upcoming days preview */}
      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>Proximos dias</CardTitle>
            <CardDescription>
              Saldo esperado para los proximos dias
            </CardDescription>
          </div>
          <Link href="/planning">
            <Button variant="outline" size="sm">
              Ver tabla completa <ArrowRight className="ml-2 h-4 w-4" />
            </Button>
          </Link>
        </CardHeader>
        <CardContent>
          <div className="grid gap-2">
            {upcomingDays.map((day) => (
              <div
                key={day.day}
                className={cn(
                  "flex items-center justify-between rounded-md px-3 py-2",
                  day.isToday ? "bg-primary/10 font-semibold" : "hover:bg-muted/50"
                )}
              >
                <span>
                  Dia {day.day}
                  {day.isToday && (
                    <span className="ml-2 text-xs text-primary">hoy</span>
                  )}
                </span>
                <span className="font-medium">
                  {formatCLP(day.expectedTotal)}
                </span>
              </div>
            ))}
          </div>
        </CardContent>
      </Card>

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
        <Link href="/settings">
          <Card className="cursor-pointer transition-colors hover:bg-muted/50">
            <CardHeader className="pb-2">
              <CardTitle className="text-base">Configuracion</CardTitle>
            </CardHeader>
            <CardContent>
              <p className="text-sm text-muted-foreground">
                Ajustar parametros del presupuesto
              </p>
            </CardContent>
          </Card>
        </Link>
      </div>
    </div>
  );
}
