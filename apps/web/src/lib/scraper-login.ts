// Dashboard-started sign-ins for scrapers whose run ended `needs_sign_in`.
// The scraper service runs them (apps/scrapers/interactive_login.py, which
// documents the methods) behind the `/api/scrapers/login` proxy routes.

export type SignInMethod = "code" | "browser" | "refresh";

export type SignInState =
  | "idle"
  | "running"
  | "awaiting_code"
  | "succeeded"
  | "failed";

export interface SupportedSignIn {
  institution: string;
  method: SignInMethod;
}

export interface SignInStatus {
  institution: string;
  method: SignInMethod;
  state: SignInState;
  message?: string | null;
}

const METHODS: readonly string[] = ["code", "browser", "refresh"];
const STATES: readonly string[] = [
  "idle",
  "running",
  "awaiting_code",
  "succeeded",
  "failed",
];

/** The well-formed entries of the service's `{ logins: [...] }`; anything
 *  else (a service that is down, an older one) is no sign-in at all. */
export function parseSupportedSignIns(data: unknown): SupportedSignIn[] {
  const list = (data as { logins?: unknown } | null)?.logins;
  if (!Array.isArray(list)) return [];
  return list.filter(
    (item): item is SupportedSignIn =>
      typeof item?.institution === "string" && METHODS.includes(item?.method)
  );
}

/** A status body from the service, or null when it is not one. */
export function parseSignInStatus(data: unknown): SignInStatus | null {
  const d = data as Partial<SignInStatus> | null;
  if (
    typeof d?.institution !== "string" ||
    !METHODS.includes(d.method as string) ||
    !STATES.includes(d.state as string)
  ) {
    return null;
  }
  return d as SignInStatus;
}

/** True while the service is still working on (or waiting for) the sign-in. */
export function isSignInPending(state: SignInState): boolean {
  return state === "running" || state === "awaiting_code";
}

export const SIGN_IN_CODE_PATTERN = /^\d{6}$/;

/** What the notice says while a sign-in is under way, in Spanish like the
 *  rest of the dashboard. */
export function signInProgressText(status: SignInStatus): string {
  if (status.state === "awaiting_code") {
    return "Ingresa el código de 6 dígitos que te llegó por correo.";
  }
  if (status.state === "succeeded") {
    return status.method === "refresh"
      ? "Actualizando. Si se abre una ventana de Chrome con una verificación, márcala ahí."
      : "Sesión iniciada. Actualizando...";
  }
  if (status.state === "failed") {
    return status.message
      ? `No se pudo iniciar sesión: ${status.message}`
      : "No se pudo iniciar sesión.";
  }
  if (status.method === "browser") {
    return "Se abrió una ventana de Chrome: inicia sesión ahí. Nada se escribe por ti.";
  }
  if (status.method === "code") return "Iniciando sesión...";
  return "Actualizando...";
}

/** The control server's base URL, or null when the service isn't configured. */
export function scraperControlUrl(): string | null {
  const url = process.env.SCRAPER_CONTROL_URL;
  return url ? url.replace(/\/$/, "") : null;
}

/**
 * The institutions still worth re-reading runs for after their sign-in went
 * through. `watching` maps an institution to when its sign-in succeeded (ms
 * since epoch); a watch ends once a run started after that has finished, or
 * after `maxMs`. Returns `watching` itself when nothing ended.
 */
export function pruneSignInWatches(
  watching: Record<string, number>,
  runs: { institution: string; started_at: string; finished_at: string | null }[],
  now: number,
  maxMs: number
): Record<string, number> {
  const next = { ...watching };
  let changed = false;
  for (const [institution, since] of Object.entries(watching)) {
    const run = runs.find((r) => r.institution === institution);
    const landed =
      run?.finished_at != null && new Date(run.started_at).getTime() >= since;
    if (landed || now - since > maxMs) {
      delete next[institution];
      changed = true;
    }
  }
  return changed ? next : watching;
}
