// The credential registry (generated from packages/product-model) plus the
// rules both the form and the API apply to a submitted value, and the status
// shapes GET /api/credentials answers with. No value ever appears in a
// status: only whether one exists, where it comes from and when it changed.

import {
  CREDENTIAL_SPECS,
  type CredentialFieldSpec,
  type InstitutionCredentialSpec,
} from "@chanchito/product-model";

export { CREDENTIAL_SPECS };
export type { CredentialFieldSpec, InstitutionCredentialSpec };

/** Upper bound for a PUT body: every field of the widest card at its
 *  maximum length, with room for JSON escaping. */
export const MAX_CREDENTIAL_BODY_BYTES = 16 * 1024;

export function credentialSpec(slug: string): InstitutionCredentialSpec | null {
  return CREDENTIAL_SPECS.find((spec) => spec.slug === slug) ?? null;
}

export function credentialField(
  spec: InstitutionCredentialSpec,
  key: string
): CredentialFieldSpec | null {
  return spec.fields.find((field) => field.key === key) ?? null;
}

// Control characters never belong in a login and cannot all live in an env var.
const CONTROL_CHARS = /[\u0000-\u001f\u007f]/;

export type ValueCheck = { ok: true; value: string } | { ok: false; error: string };

/** The value to store, or why it is refused. Identifiers are trimmed;
 *  secrets are kept exactly as typed. Messages never repeat the value. */
export function checkCredentialValue(field: CredentialFieldSpec, raw: string): ValueCheck {
  const value = field.kind === "identifier" ? raw.trim() : raw;
  if (value === "") return { ok: false, error: `${field.labelEs}: no puede quedar vacío.` };
  if (value.length > field.maxLength) {
    return {
      ok: false,
      error: `${field.labelEs}: máximo ${field.maxLength} caracteres.`,
    };
  }
  if (CONTROL_CHARS.test(value)) {
    return { ok: false, error: `${field.labelEs}: tiene caracteres no permitidos.` };
  }
  if (field.pattern && !new RegExp(`^(?:${field.pattern})$`).test(value)) {
    return {
      ok: false,
      error: `${field.labelEs}: ${field.patternHintEs ?? "formato no válido."}`,
    };
  }
  return { ok: true, value };
}

/** One field's state, never its value. `fallback` is null when the scraper
 *  service could not be asked (its local secret store and env are unknown). */
export interface CredentialFieldStatus {
  key: string;
  stored: boolean;
  needsReentry: boolean;
  fallback: boolean | null;
  updatedAt: string | null;
}

export interface InstitutionCredentialStatus {
  slug: string;
  /** Whether one of its scrapers is running; null when the service is down. */
  enabled: boolean | null;
  fields: CredentialFieldStatus[];
}

export interface CredentialsOverview {
  /** The scraper service answered. */
  serviceReachable: boolean;
  /** The service has a usable key; null when it could not be asked. */
  keyAvailable: boolean | null;
  /** A public key is published, so saving is possible. */
  keyPublished: boolean;
  institutions: InstitutionCredentialStatus[];
}

export type FieldState = "stored" | "needs_reentry" | "fallback" | "missing" | "unknown";

export function fieldState(status: CredentialFieldStatus): FieldState {
  if (status.needsReentry) return "needs_reentry";
  if (status.stored) return "stored";
  if (status.fallback === null) return "unknown";
  return status.fallback ? "fallback" : "missing";
}

export const FIELD_STATE_LABELS: Record<FieldState, string> = {
  stored: "Guardado aquí",
  needs_reentry: "Requiere reingreso",
  fallback: "Desde almacén local",
  missing: "Falta",
  unknown: "Sin datos del servicio",
};
