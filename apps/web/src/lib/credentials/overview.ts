// Builds GET /api/credentials from the stored rows' metadata and what the
// scraper service reports. Pure, so it can be tested without a database.

import { NextResponse } from "next/server";
import { saveBlocker } from "./availability";
import type { ControlCredentialStatus } from "./control";
import type { CredentialMeta, StoredKeyInfo } from "./db";
import {
  CREDENTIAL_SPECS,
  type CredentialsOverview,
  type InstitutionCredentialStatus,
} from "./registry";
import { schemeForAlgorithm, type SealingKey } from "./seal";

export function buildCredentialsOverview({
  key,
  meta,
  control,
  enabledScrapers,
  keyInfo = null,
}: {
  key: SealingKey | null;
  meta: CredentialMeta[];
  control: ControlCredentialStatus | null;
  enabledScrapers: string[] | null;
  keyInfo?: StoredKeyInfo | null;
}): CredentialsOverview {
  const activeScheme = key ? schemeForAlgorithm(key.algorithm) : null;
  const institutions: InstitutionCredentialStatus[] = CREDENTIAL_SPECS.map((spec) => ({
    slug: spec.slug,
    enabled: enabledScrapers
      ? spec.scraperSlugs.some((slug) => enabledScrapers.includes(slug))
      : null,
    fields: spec.fields.map((field) => {
      const row = meta.find(
        (m) => m.institutionSlug === spec.slug && m.fieldKey === field.key
      );
      const reported = control?.fields[field.envVar];
      // A row sealed to a key other than the published one (or with another
      // scheme than that key's), or one the service reports it could not
      // open, has to be typed again.
      const needsReentry = Boolean(
        row &&
          ((key !== null && (row.keyId !== key.keyId || row.scheme !== activeScheme)) ||
            reported?.needs_reentry)
      );
      return {
        key: field.key,
        stored: Boolean(row) && !needsReentry,
        needsReentry,
        fallback: control ? (reported?.fallback ?? false) : null,
        updatedAt: row ? row.updatedAt.toISOString() : null,
      };
    }),
  }));
  const blocked = saveBlocker(key, control);
  return {
    serviceReachable: control !== null,
    keyAvailable: control ? control.key_available : null,
    keyPublished: key !== null,
    keyMismatch: control ? Boolean(control.key_mismatch) : null,
    canSave: blocked === null,
    saveBlocked: blocked,
    key: keyInfo
      ? {
          createdAt: keyInfo.createdAt.toISOString(),
          lastRotatedAt: keyInfo.lastRotatedAt?.toISOString() ?? null,
          // The database row survives restarts; the service's own record
          // covers a failure it could not write there.
          rotationFailedAt:
            keyInfo.rotationFailedAt?.toISOString() ?? control?.rotation_failed_at ?? null,
          rotationError: keyInfo.rotationFailedAt
            ? keyInfo.rotationError
            : (control?.rotation_error ?? null),
          maxAgeDays: control?.max_age_days ?? null,
        }
      : null,
    institutions,
  };
}

/** Credential responses are personal and never cacheable. */
export function noStoreJson(body: unknown, status = 200, headers?: HeadersInit): NextResponse {
  const response = NextResponse.json(body, { status, headers });
  response.headers.set("cache-control", "no-store");
  return response;
}
