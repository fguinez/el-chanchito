"use client";

// Manual balance entry for wallets (MACH and Tenpo have no automatic balance
// source): POSTs the typed CLP amount to the product's /balance endpoint and
// lets the page re-fetch so every card reflects it.

import { useState } from "react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { parseClpInput } from "@/lib/manual-balance";

export function ManualBalanceCard({
  endpoint,
  onSaved,
}: {
  /** POST /api/institutions/[slug]/products/[product]/balance for this product. */
  endpoint: string;
  /** Called after a successful save so the page can reload its data. */
  onSaved: () => void;
}) {
  const [value, setValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!value.trim()) return;
    const balance = parseClpInput(value);
    if (balance === null) {
      setError("Ingresa el saldo en pesos, por ejemplo 2.500.000");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      // The server re-validates (whole, non-negative CLP, within range) and
      // its 400 message is shown as is.
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ balance }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        setError(body?.error ?? "No se pudo guardar el saldo");
        return;
      }
      setValue("");
      onSaved();
    } catch {
      setError("No se pudo guardar el saldo");
    } finally {
      setSaving(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Registrar saldo</CardTitle>
        <CardDescription>
          Ingresa el saldo que muestra la app. Si la billetera tiene una fuente
          automática, su próxima lectura lo reemplaza si difiere.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="flex max-w-sm gap-2">
          {/* Text, not number: a number input reads "250.000" as 250. */}
          <Input
            type="text"
            inputMode="numeric"
            placeholder="2.500.000"
            aria-label="Saldo en CLP"
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
          <Button type="submit" disabled={saving || !value.trim()}>
            {saving ? "Guardando..." : "Guardar"}
          </Button>
        </form>
        {error && (
          <p role="alert" className="mt-2 text-sm text-destructive">
            {error}
          </p>
        )}
      </CardContent>
    </Card>
  );
}
