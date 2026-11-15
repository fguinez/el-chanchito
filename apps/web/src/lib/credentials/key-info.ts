// The read-only key lines of the Credenciales page. Pure and client-safe:
// dates and a short failure reason only, never key material.

import { formatDateEs } from "@/lib/utils";

export interface CredentialKeyInfo {
  /** When the active key was created (or last made active). */
  createdAt: string;
  /** When a key was last retired by a rotation or reset; null if never. */
  lastRotatedAt: string | null;
  rotationFailedAt: string | null;
  rotationError: string | null;
  /** CREDENTIALS_KEY_MAX_AGE_DAYS as the scraper service reports it (0 is
   *  off); null when the service could not be asked. */
  maxAgeDays: number | null;
}

const DAY_MS = 24 * 60 * 60 * 1000;

function daysAgo(iso: string, now: Date): string {
  const days = Math.max(0, Math.floor((now.getTime() - new Date(iso).getTime()) / DAY_MS));
  if (days === 0) return "hoy";
  return days === 1 ? "hace 1 día" : `hace ${days} días`;
}

export interface KeyInfoLines {
  age: string;
  schedule: string | null;
  failure: string | null;
}

export function describeKeyInfo(info: CredentialKeyInfo, now: Date = new Date()): KeyInfoLines {
  const rotated = info.lastRotatedAt
    ? `Última rotación: ${formatDateEs(info.lastRotatedAt)}.`
    : "Aún no se ha rotado.";
  const age = `Clave activa desde el ${formatDateEs(info.createdAt)} (${daysAgo(info.createdAt, now)}). ${rotated}`;

  let schedule: string | null = null;
  if (info.maxAgeDays === 0) {
    schedule = "La rotación automática está desactivada (CREDENTIALS_KEY_MAX_AGE_DAYS=0).";
  } else if (info.maxAgeDays !== null) {
    const due = new Date(new Date(info.createdAt).getTime() + info.maxAgeDays * DAY_MS);
    schedule =
      due.getTime() <= now.getTime()
        ? `Se rota sola cada ${info.maxAgeDays} días; la próxima rotación está pendiente.`
        : `Se rota sola cada ${info.maxAgeDays} días; la próxima, cerca del ${formatDateEs(due.toISOString())}.`;
  }

  const failure = info.rotationFailedAt
    ? `La última rotación automática falló el ${formatDateEs(info.rotationFailedAt)}` +
      (info.rotationError ? ` (${info.rotationError})` : "") +
      ". El servicio la reintentará; mientras tanto la clave actual sigue funcionando."
    : null;
  return { age, schedule, failure };
}
