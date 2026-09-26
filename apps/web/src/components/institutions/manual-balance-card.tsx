"use client";

// Manual balance entry for products with no automatic balance source (wallets
// such as MACH or Tenpo): POSTs the typed CLP amount to the product's
// /balance endpoint and lets the page re-fetch so every card reflects it.

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
    if (!value) return;
    setSaving(true);
    setError(null);
    try {
      // Validation lives server-side (whole, non-negative CLP); a decimal
      // comes back as a 400 whose message is shown as is.
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ balance: Number(value) }),
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
          Para billeteras sin fuente automática de saldo: ingresa el saldo que
          muestra la app.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="flex max-w-sm gap-2">
          <Input
            type="number"
            inputMode="numeric"
            min={0}
            step={1}
            placeholder="2500000"
            aria-label="Saldo en CLP"
            value={value}
            onChange={(event) => setValue(event.target.value)}
          />
          <Button type="submit" disabled={saving || !value}>
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
