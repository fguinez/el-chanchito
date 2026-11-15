"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, LogIn } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  SIGN_IN_CODE_PATTERN,
  isSignInPending,
  parseSignInStatus,
  signInProgressText,
  type SignInMethod,
  type SignInStatus,
} from "@/lib/scraper-login";

const POLL_MS = 2000;

async function readError(res: Response): Promise<string> {
  const data = await res.json().catch(() => ({}));
  return typeof data?.error === "string" ? data.error : `HTTP ${res.status}`;
}

/**
 * The "Iniciar sesión" button of a needs_sign_in notice: starts the
 * institution's interactive sign-in on the scraper service, follows it, asks
 * for the e-mailed code when the service waits for one, and calls
 * `onSucceeded` once it went through (the service refreshes the institution;
 * the notice clears when that run lands).
 */
export function SignInAction({
  institution,
  method,
  onSucceeded,
}: {
  institution: string;
  method: SignInMethod;
  onSucceeded: (institution: string) => void;
}) {
  const [status, setStatus] = useState<SignInStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [sending, setSending] = useState(false);
  const base = `/api/scrapers/login/${encodeURIComponent(institution)}`;
  // Bumped by every POST: a poll that began before one answers with a state
  // the POST has already moved past, so its result is dropped.
  const generation = useRef(0);

  const apply = useCallback(
    (next: SignInStatus | null) => {
      setStatus(next);
      if (next?.state === "succeeded") onSucceeded(institution);
    },
    [institution, onSucceeded]
  );

  // A sign-in started before a reload is picked up again; a finished one is
  // not replayed.
  useEffect(() => {
    let cancelled = false;
    fetch(base)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        const current = parseSignInStatus(data);
        if (!cancelled && current && isSignInPending(current.state)) setStatus(current);
      })
      .catch(() => {});
    return () => {
      cancelled = true;
    };
  }, [base]);

  const pending = status !== null && isSignInPending(status.state);
  useEffect(() => {
    if (!pending) return;
    const timer = setInterval(() => {
      const startedAt = generation.current;
      fetch(base)
        .then((res) => (res.ok ? res.json() : null))
        .then((data) => {
          const next = parseSignInStatus(data);
          if (next && generation.current === startedAt) apply(next);
        })
        .catch(() => {});
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [base, pending, apply]);

  async function start() {
    setError(null);
    setSending(true);
    generation.current += 1;
    try {
      const res = await fetch(base, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: "{}",
      });
      if (!res.ok) {
        setError(await readError(res));
        return;
      }
      apply(parseSignInStatus(await res.json()));
    } catch {
      setError("Servicio de scrapers no disponible.");
    } finally {
      setSending(false);
    }
  }

  async function sendCode(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    setSending(true);
    generation.current += 1;
    try {
      const res = await fetch(`${base}/code`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ code: code.trim() }),
      });
      if (!res.ok) {
        setError(await readError(res));
        return;
      }
      setCode("");
      apply(parseSignInStatus(await res.json()));
    } catch {
      setError("Servicio de scrapers no disponible.");
    } finally {
      setSending(false);
    }
  }

  const showButton = !pending && status?.state !== "succeeded";
  return (
    <div className="mt-2 space-y-2">
      {status && status.state !== "idle" && (
        <p className="flex items-center gap-2 text-xs" role="status">
          {pending && status.state === "running" && (
            <Loader2 className="h-3 w-3 animate-spin" />
          )}
          {signInProgressText(status)}
        </p>
      )}
      {status?.state === "awaiting_code" && (
        <form onSubmit={sendCode} className="flex items-center gap-2">
          <Input
            aria-label="Código de verificación"
            inputMode="numeric"
            autoComplete="one-time-code"
            maxLength={6}
            placeholder="000000"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            className="h-8 w-28 bg-background"
          />
          <Button
            type="submit"
            size="sm"
            disabled={sending || !SIGN_IN_CODE_PATTERN.test(code.trim())}
          >
            Enviar
          </Button>
        </form>
      )}
      {showButton && (
        <Button size="sm" variant="outline" onClick={start} disabled={sending}>
          {sending ? <Loader2 className="animate-spin" /> : <LogIn />}
          {status?.state === "failed" ? "Reintentar" : "Iniciar sesión"}
        </Button>
      )}
      {error && <p className="text-xs text-red-600">{error}</p>}
      {method === "refresh" && !status && (
        <p className="text-xs opacity-80">
          Abre Chrome en este computador; si aparece una verificación, márcala.
        </p>
      )}
    </div>
  );
}
