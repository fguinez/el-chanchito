// The credential registry (generated from packages/product-model) plus the
// rules both the form and the API apply to a submitted value, and the status
// shapes GET /api/credentials answers with. No value ever appears in a
// status: only whether one exists, where it comes from and when it changed.

import {
  CREDENTIAL_SPECS,
  type CredentialFieldSpec,
  type InstitutionCredentialSpec,
} from "@chanchito/product-model";
import type { SaveBlocker } from "./availability";
import type { CredentialKeyInfo } from "./key-info";

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

// C0, DEL and C1 controls never belong in a login (the scraper service
// refuses them too).
const CONTROL_CHARS = /[\u0000-\u001f\u007f-\u009f]/;

export type ValueCheck = { ok: true; value: string } | { ok: false; error: string };

/** The value to store, or why it is refused. Identifiers are trimmed;
 *  secrets are kept exactly as typed. Messages never repeat the value.
 *  `allowCustom` lifts `allowedValues` (the server reads `allowCustomEnv`). */
export function checkCredentialValue(
  field: CredentialFieldSpec,
  raw: string,
  allowCustom = false
): ValueCheck {
  const value = field.kind === "identifier" ? raw.trim() : raw;
  if (value === "") return { ok: false, error: `${field.labelEs}: no puede quedar vacío.` };
  if (!value.isWellFormed()) {
    return { ok: false, error: `${field.labelEs}: tiene caracteres no válidos.` };
  }
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
  if (
    field.allowedValues.length > 0 &&
    !allowCustom &&
    !field.allowedValues.some((known) => known.toLowerCase() === value.toLowerCase())
  ) {
    return {
      ok: false,
      error:
        `${field.labelEs}: no es un servidor conocido (${field.allowedValues.join(", ")}). ` +
        `Para usar otro, define ${field.allowCustomEnv}=1 en el dashboard y en el servicio de scrapers.`,
    };
  }
  return { ok: true, value };
}

/** Field keys always saved together: each binding secret and what it binds. */
export function bindingGroups(spec: InstitutionCredentialSpec): string[][] {
  return spec.fields.filter((f) => f.binds.length > 0).map((f) => [f.key, ...f.binds]);
}

/** Why a set of submitted keys breaks a binding group, or null. A field that
 *  decides where a secret goes can only change together with that secret. */
export function bindingProblem(
  spec: InstitutionCredentialSpec,
  submitted: readonly string[]
): string | null {
  for (const group of bindingGroups(spec)) {
    const touched = group.some((key) => submitted.includes(key));
    const missing = group.filter((key) => !submitted.includes(key));
    if (touched && missing.length > 0) {
      const labels = group.map((key) => credentialField(spec, key)?.labelEs ?? key);
      return `${labels.join(", ")} se guardan juntos: escribe los ${group.length} campos.`;
    }
  }
  return null;
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
  /** The database has an active key. */
  keyPublished: boolean;
  /** The database seals to another service's key; null when the service
   *  could not be asked. */
  keyMismatch: boolean | null;
  /** Saving works now: the running service holds the active key. */
  canSave: boolean;
  /** Why saving is unavailable (see lib/credentials/availability). */
  saveBlocked: SaveBlocker | null;
  /** Read-only dates about the active key; null when there is none. */
  key: CredentialKeyInfo | null;
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
