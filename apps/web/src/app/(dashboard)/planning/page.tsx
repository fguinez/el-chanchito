"use client";

import { useEffect, useState } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { cn, formatCLP } from "@/lib/utils";
import type { DayPlan, TodayStatus } from "@/lib/budget-engine";

interface PlanningData {
  plans: DayPlan[];
  todayStatus: TodayStatus;
  config: {
    variableBudget: number;
    fixedBudget: number;
    creditCardLimit: number;
    checkingInitialBalance: number;
    salary: number;
    dayStart: number;
  };
  month: string;
  daysInMonth: number;
}

export default function PlanningPage() {
  const [data, setData] = useState<PlanningData | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch("/api/planning")
      .then((res) => {
        if (!res.ok) throw new Error("No budget config found");
        return res.json();
      })
      .then(setData)
      .catch((e) => setError(e.message));
  }, []);

  if (error) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Planificacion Mensual</h2>
        <Card>
          <CardContent className="pt-6">
            <p className="text-muted-foreground">
              {error}. Ve a <a href="/settings" className="underline">Configuracion</a> para crear uno.
            </p>
          </CardContent>
        </Card>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Planificacion Mensual</h2>
        <p className="text-muted-foreground">Cargando...</p>
      </div>
    );
  }

  const { plans, todayStatus } = data;
  const now = new Date();

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-2xl font-bold">Planificacion Mensual</h2>
        <Badge variant="outline" className="text-sm">
          {now.toLocaleDateString("es-CL", { month: "long", year: "numeric" })}
        </Badge>
      </div>

      {/* Today summary cards */}
      <div className="grid gap-4 md:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Deberias tener hoy</CardDescription>
            <CardTitle className="text-xl">
              {formatCLP(todayStatus.expectedBalance)}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-xs text-muted-foreground">
              Dia {todayStatus.todayDay} de {todayStatus.daysInMonth}
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Presupuesto diario</CardDescription>
            <CardTitle className="text-xl">
              {formatCLP(todayStatus.presupDiario)}
            </CardTitle>
          </CardHeader>
          <CardContent>
            <p className="text-xs text-muted-foreground">
              {todayStatus.daysRemaining} dias restantes
            </p>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Presupuesto variable restante</CardDescription>
            <CardTitle className="text-xl">
              {formatCLP(todayStatus.budgetRemainingMonth)}
            </CardTitle>
          </CardHeader>
        </Card>
      </div>

      {/* Day-by-day table */}
      <Card>
        <CardHeader>
          <CardTitle>Tabla diaria</CardTitle>
          <CardDescription>
            Saldo esperado por dia. La fila resaltada es hoy.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-16">Dia</TableHead>
                <TableHead className="text-right">T. Credito</TableHead>
                <TableHead className="text-right">C. Corriente</TableHead>
                <TableHead className="text-right">Variaciones</TableHead>
                <TableHead className="text-right">Total esperado</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {plans.map((plan) => (
                <TableRow
                  key={plan.day}
                  className={cn(
                    plan.isToday &&
                      "bg-primary/10 font-semibold border-l-2 border-l-primary"
                  )}
                >
                  <TableCell>
                    {plan.day}
                    {plan.isToday && (
                      <span className="ml-2 text-xs text-primary">hoy</span>
                    )}
                  </TableCell>
                  <TableCell className="text-right">
                    {formatCLP(plan.expectedTcBalance)}
                  </TableCell>
                  <TableCell className="text-right">
                    {formatCLP(plan.expectedCcBalance)}
                  </TableCell>
                  <TableCell className="text-right">
                    {plan.cumulativeAdjustments !== 0
                      ? formatCLP(plan.cumulativeAdjustments)
                      : "-"}
                  </TableCell>
                  <TableCell className="text-right font-medium">
                    {formatCLP(plan.expectedTotal)}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  );
}
