"use client";

import { Suspense, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { PiggyBank } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { safeNextPath } from "@/lib/auth/config";

/** Spanish message for a failed login, including the rate-limit lockout. */
function loginErrorMessage(response: Response): string {
  if (response.status === 401) return "Contraseña incorrecta";

  if (response.status === 429) {
    const seconds = Number(response.headers.get("retry-after"));
    if (Number.isFinite(seconds) && seconds > 0) {
      const minutes = Math.ceil(seconds / 60);
      const wait =
        seconds < 60
          ? `${seconds} segundo${seconds === 1 ? "" : "s"}`
          : `${minutes} minuto${minutes === 1 ? "" : "s"}`;
      return `Demasiados intentos fallidos. Espera ${wait} e inténtalo de nuevo.`;
    }
    return "Demasiados intentos fallidos. Espera unos minutos e inténtalo de nuevo.";
  }

  return "No se pudo iniciar sesión";
}

/**
 * Whether the browser actually kept the session cookie the login just set.
 *
 * A `Secure` cookie handed to a browser on a plain-HTTP leg is dropped without a
 * word, and navigating anyway would bounce straight back to `/login` with no
 * message at all (see USAGE.md > Authentication). Asking the session endpoint
 * turns that silent loop into a real error. A failure to ask is treated as "it
 * stuck", so a transient hiccup never blocks a good login.
 */
async function sessionCookieStuck(): Promise<boolean> {
  try {
    const response = await fetch("/api/auth/session", { cache: "no-store" });
    if (!response.ok) return true;
    const body = await response.json();
    return body?.authenticated !== false;
  } catch {
    return true;
  }
}

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    setSubmitting(true);
    setError(null);

    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });

      if (!response.ok) {
        setError(loginErrorMessage(response));
        setPassword("");
        return;
      }

      if (!(await sessionCookieStuck())) {
        setError(
          "La contraseña es correcta, pero el navegador descartó la cookie de " +
            "sesión. Suele pasar al entrar por http:// cuando el servidor la " +
            "marca como Secure: abre el panel por HTTPS."
        );
        setPassword("");
        return;
      }

      const target = safeNextPath(searchParams.get("next"));
      router.replace(target);
      router.refresh();
    } catch {
      setError("No se pudo iniciar sesión");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Card className="w-full max-w-sm">
      <CardHeader className="items-center text-center">
        <PiggyBank className="mx-auto h-8 w-8 text-muted-foreground" />
        <CardTitle className="text-xl">El Chanchito</CardTitle>
        <CardDescription>Ingresa la contraseña para continuar</CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="space-y-2">
            <label htmlFor="password" className="text-sm font-medium">
              Contraseña
            </label>
            <Input
              id="password"
              name="password"
              type="password"
              autoComplete="current-password"
              autoFocus
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </div>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <Button type="submit" className="w-full" disabled={submitting}>
            {submitting ? "Entrando..." : "Entrar"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}

export default function LoginPage() {
  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <Suspense>
        <LoginForm />
      </Suspense>
    </div>
  );
}
