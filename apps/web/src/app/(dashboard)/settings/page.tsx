"use client";

import { useEffect, useState } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { formatCLP } from "@/lib/utils";
import { Trash2 } from "lucide-react";

interface IncomeSource {
  id: string;
  name: string;
  monthlyAmount: number;
  ratio: number;
}

interface BudgetConfigForm {
  variableBudget: number;
  fixedBudget: number;
  creditCardLimit: number;
  checkingInitialBalance: number;
  salary: number;
  sharedExpensesRatio: number;
  dayStart: number;
}

const defaultConfig: BudgetConfigForm = {
  variableBudget: 600_000,
  fixedBudget: 1_000_000,
  creditCardLimit: 2_000_000,
  checkingInitialBalance: 0,
  salary: 1_500_000,
  sharedExpensesRatio: 0.69,
  dayStart: 1,
};

export default function SettingsPage() {
  const [config, setConfig] = useState<BudgetConfigForm>(defaultConfig);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);

  // Income sources state
  const [incomeSources, setIncomeSources] = useState<IncomeSource[]>([]);
  const [incomeTotal, setIncomeTotal] = useState(0);
  const [newIncomeName, setNewIncomeName] = useState("");
  const [newIncomeAmount, setNewIncomeAmount] = useState("");
  const [splitAmount, setSplitAmount] = useState("");

  const loadIncomeSources = () => {
    fetch("/api/income-sources")
      .then((res) => res.json())
      .then((data) => {
        setIncomeSources(data.sources);
        setIncomeTotal(data.total);
      })
      .catch(console.error);
  };

  useEffect(() => {
    fetch("/api/budget")
      .then((res) => {
        if (res.ok) return res.json();
        return null;
      })
      .then((data) => {
        if (data) {
          setConfig({
            variableBudget: data.variableBudget,
            fixedBudget: data.fixedBudget,
            creditCardLimit: data.creditCardLimit,
            checkingInitialBalance: data.checkingInitialBalance,
            salary: data.salary,
            sharedExpensesRatio: parseFloat(data.sharedExpensesRatio),
            dayStart: data.dayStart,
          });
        }
        setLoaded(true);
      })
      .catch(() => setLoaded(true));

    loadIncomeSources();
  }, []);

  const handleSave = async () => {
    setSaving(true);
    setMessage(null);

    const now = new Date();
    const month = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-01`;

    try {
      const res = await fetch("/api/budget", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ...config, month }),
      });

      if (res.ok) {
        setMessage("Configuracion guardada");
      } else {
        const err = await res.json();
        setMessage(`Error: ${err.error}`);
      }
    } catch {
      setMessage("Error de conexion");
    } finally {
      setSaving(false);
    }
  };

  const fields: {
    key: keyof BudgetConfigForm;
    label: string;
    type: "currency" | "number" | "ratio";
  }[] = [
    { key: "variableBudget", label: "Presupuesto mensual variable", type: "currency" },
    { key: "fixedBudget", label: "Presupuesto mensual fijo", type: "currency" },
    { key: "creditCardLimit", label: "Cupo tarjeta de credito", type: "currency" },
    { key: "checkingInitialBalance", label: "Saldo inicial cuenta corriente", type: "currency" },
    { key: "salary", label: "Sueldo", type: "currency" },
    { key: "sharedExpensesRatio", label: "Ratio gastos compartidos", type: "ratio" },
    { key: "dayStart", label: "Dia inicio ciclo", type: "number" },
  ];

  if (!loaded) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Configuracion</h2>
        <p className="text-muted-foreground">Cargando...</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <h2 className="text-2xl font-bold">Configuracion</h2>

      <Card>
        <CardHeader>
          <CardTitle>Parametros del presupuesto</CardTitle>
          <CardDescription>
            Estos valores definen el presupuesto del mes actual (equivale a la
            hoja &quot;Inputs&quot; del Excel)
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="space-y-4">
            {fields.map((field) => (
              <div key={field.key} className="grid grid-cols-2 items-center gap-4">
                <label className="text-sm text-muted-foreground">
                  {field.label}
                </label>
                <div className="flex items-center gap-2">
                  {field.type === "currency" && (
                    <span className="text-sm text-muted-foreground">$</span>
                  )}
                  <Input
                    type="number"
                    value={config[field.key]}
                    onChange={(e) =>
                      setConfig((prev) => ({
                        ...prev,
                        [field.key]:
                          field.type === "ratio"
                            ? parseFloat(e.target.value) || 0
                            : parseInt(e.target.value) || 0,
                      }))
                    }
                    step={field.type === "ratio" ? "0.01" : "1"}
                    className="max-w-[200px]"
                  />
                  {field.type === "ratio" && (
                    <span className="text-sm text-muted-foreground">
                      ({Math.round(config[field.key] * 100)}%)
                    </span>
                  )}
                </div>
              </div>
            ))}

            <div className="border-t pt-4">
              <h4 className="mb-2 text-sm font-medium">Valores derivados</h4>
              <div className="grid grid-cols-2 gap-2 text-sm">
                <span className="text-muted-foreground">Presupuesto diario</span>
                <span>{formatCLP(Math.round(config.variableBudget / 31))}</span>
                <span className="text-muted-foreground">Gasto estimado mes</span>
                <span>
                  {formatCLP(config.variableBudget + config.fixedBudget)}
                </span>
                <span className="text-muted-foreground">Ahorro por mes</span>
                <span>
                  {formatCLP(
                    config.salary -
                      (config.variableBudget + config.fixedBudget)
                  )}
                </span>
              </div>
            </div>

            <div className="flex items-center gap-4 pt-4">
              <Button onClick={handleSave} disabled={saving}>
                {saving ? "Guardando..." : "Guardar configuracion"}
              </Button>
              {message && (
                <span
                  className={`text-sm ${
                    message.startsWith("Error")
                      ? "text-destructive"
                      : "text-green-600"
                  }`}
                >
                  {message}
                </span>
              )}
            </div>
          </div>
        </CardContent>
      </Card>

      {/* Income split calculator */}
      <Card>
        <CardHeader>
          <CardTitle>Calculadora de division de pagos</CardTitle>
          <CardDescription>
            Divide un monto entre fuentes de ingreso proporcionalmente (equivale
            a &quot;Porcentaje pagos&quot; del Excel)
          </CardDescription>
        </CardHeader>
        <CardContent>
          <div className="space-y-4">
            {/* Add income source */}
            <div className="flex items-end gap-3">
              <div className="flex-1">
                <label className="mb-1 block text-sm text-muted-foreground">
                  Nombre
                </label>
                <Input
                  value={newIncomeName}
                  onChange={(e) => setNewIncomeName(e.target.value)}
                  placeholder="Ej: Sueldo 1"
                />
              </div>
              <div className="w-40">
                <label className="mb-1 block text-sm text-muted-foreground">
                  Monto mensual
                </label>
                <Input
                  type="number"
                  value={newIncomeAmount}
                  onChange={(e) => setNewIncomeAmount(e.target.value)}
                  placeholder="1000000"
                />
              </div>
              <Button
                onClick={async () => {
                  if (!newIncomeName || !newIncomeAmount) return;
                  await fetch("/api/income-sources", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                      name: newIncomeName,
                      monthlyAmount: parseInt(newIncomeAmount),
                    }),
                  });
                  setNewIncomeName("");
                  setNewIncomeAmount("");
                  loadIncomeSources();
                }}
              >
                Agregar
              </Button>
            </div>

            {/* Income sources list */}
            {incomeSources.length > 0 && (
              <div className="space-y-2">
                {incomeSources.map((source) => (
                  <div
                    key={source.id}
                    className="flex items-center justify-between rounded-md border px-3 py-2"
                  >
                    <div>
                      <span className="font-medium">{source.name}</span>
                      <span className="ml-3 text-sm text-muted-foreground">
                        {formatCLP(source.monthlyAmount)} ({(source.ratio * 100).toFixed(1)}%)
                      </span>
                    </div>
                    <button
                      onClick={async () => {
                        await fetch("/api/income-sources", {
                          method: "DELETE",
                          headers: { "Content-Type": "application/json" },
                          body: JSON.stringify({ id: source.id }),
                        });
                        loadIncomeSources();
                      }}
                      className="text-muted-foreground hover:text-destructive"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </div>
                ))}
                <div className="text-sm text-muted-foreground">
                  Total: {formatCLP(incomeTotal)}
                </div>
              </div>
            )}

            {/* Split calculator */}
            {incomeSources.length >= 2 && (
              <div className="border-t pt-4">
                <h4 className="mb-2 text-sm font-medium">Dividir un monto</h4>
                <div className="flex items-end gap-3">
                  <div className="w-40">
                    <label className="mb-1 block text-sm text-muted-foreground">
                      Monto a dividir
                    </label>
                    <Input
                      type="number"
                      value={splitAmount}
                      onChange={(e) => setSplitAmount(e.target.value)}
                      placeholder="999999"
                    />
                  </div>
                </div>
                {splitAmount && parseInt(splitAmount) > 0 && (
                  <div className="mt-3 grid grid-cols-2 gap-2 text-sm">
                    {incomeSources.map((source) => (
                      <div key={source.id} className="contents">
                        <span className="text-muted-foreground">
                          {source.name} ({(source.ratio * 100).toFixed(1)}%)
                        </span>
                        <span className="font-medium">
                          {formatCLP(
                            Math.round(parseInt(splitAmount) * source.ratio)
                          )}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      {/* Monthly reset */}
      <Card>
        <CardHeader>
          <CardTitle>Inicio de mes</CardTitle>
          <CardDescription>
            Crea la configuracion del proximo mes copiando los parametros
            actuales (equivale a &quot;Pasos inicio mes&quot; del Excel)
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Button
            variant="outline"
            onClick={async () => {
              const res = await fetch("/api/month-reset", { method: "POST" });
              const data = await res.json();
              if (res.ok) {
                setMessage(data.message);
              } else {
                setMessage(`Error: ${data.error}`);
              }
            }}
          >
            Crear proximo mes
          </Button>
        </CardContent>
      </Card>
    </div>
  );
}
