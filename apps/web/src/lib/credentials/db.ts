// Data access for the sealed credentials (V048, V049). The read path selects
// the metadata columns only: no query here ever returns `sealed` to a caller.

import "server-only";
import { and, eq, inArray, max } from "drizzle-orm";
import { sql } from "drizzle-orm";
import { db } from "@/lib/db";
import { credentialKeys, institutionCredentials } from "@/lib/db/schema";
import type { SealingKey } from "./seal";

/** The active key of the ring: the one the dashboard seals to. */
export async function readSealingKey(): Promise<SealingKey | null> {
  const rows = await db
    .select({
      keyId: credentialKeys.keyId,
      algorithm: credentialKeys.algorithm,
      publicKey: credentialKeys.publicKey,
    })
    .from(credentialKeys)
    .where(eq(credentialKeys.status, "active"))
    .limit(1);
  return rows[0] ?? null;
}

export interface StoredKeyInfo {
  createdAt: Date;
  /** When a key was last retired (rotated or reset); null if never. */
  lastRotatedAt: Date | null;
  rotationFailedAt: Date | null;
  rotationError: string | null;
}

/** Dates about the active key, for the Credenciales page. Never key material. */
export async function readKeyInfo(): Promise<StoredKeyInfo | null> {
  const [active] = await db
    .select({
      createdAt: credentialKeys.createdAt,
      rotationFailedAt: credentialKeys.rotationFailedAt,
      rotationError: credentialKeys.rotationError,
    })
    .from(credentialKeys)
    .where(eq(credentialKeys.status, "active"))
    .limit(1);
  if (!active) return null;
  const [retired] = await db
    .select({ lastRotatedAt: max(credentialKeys.retiredAt) })
    .from(credentialKeys);
  return { ...active, lastRotatedAt: retired?.lastRotatedAt ?? null };
}

export interface CredentialMeta {
  institutionSlug: string;
  fieldKey: string;
  keyId: string;
  scheme: string;
  updatedAt: Date;
}

export async function listCredentialMeta(): Promise<CredentialMeta[]> {
  return db
    .select({
      institutionSlug: institutionCredentials.institutionSlug,
      fieldKey: institutionCredentials.fieldKey,
      keyId: institutionCredentials.keyId,
      scheme: institutionCredentials.scheme,
      updatedAt: institutionCredentials.updatedAt,
    })
    .from(institutionCredentials);
}

export interface SealedField {
  fieldKey: string;
  sealed: Uint8Array;
}

/** Insert or replace the given fields of one institution, all or none, only
 *  while `keyId` is still the active key. The key row is locked FOR SHARE
 *  until the commit, so a rotation (which locks it FOR UPDATE) either waits
 *  and re-seals these rows or has already retired the key, and then nothing
 *  is stored: false, and the caller seals again to the new key. */
export async function upsertSealedFields(
  institutionSlug: string,
  scheme: string,
  keyId: string,
  fields: SealedField[]
): Promise<boolean> {
  if (fields.length === 0) return true;
  return db.transaction(async (tx) => {
    const active = await tx
      .select({ keyId: credentialKeys.keyId })
      .from(credentialKeys)
      .where(and(eq(credentialKeys.keyId, keyId), eq(credentialKeys.status, "active")))
      .for("share");
    if (active.length === 0) return false;
    await tx
      .insert(institutionCredentials)
      .values(
        fields.map((field) => ({
          institutionSlug,
          fieldKey: field.fieldKey,
          scheme,
          keyId,
          sealed: field.sealed,
        }))
      )
      .onConflictDoUpdate({
        target: [institutionCredentials.institutionSlug, institutionCredentials.fieldKey],
        set: {
          scheme: sql`excluded.scheme`,
          keyId: sql`excluded.key_id`,
          sealed: sql`excluded.sealed`,
          updatedAt: sql`now()`,
        },
      });
    return true;
  });
}

/** Delete the given fields, or every field of the institution; the number
 *  deleted. */
export async function deleteSealedFields(
  institutionSlug: string,
  fieldKeys?: readonly string[]
): Promise<number> {
  const where = fieldKeys
    ? and(
        eq(institutionCredentials.institutionSlug, institutionSlug),
        inArray(institutionCredentials.fieldKey, [...fieldKeys])
      )
    : eq(institutionCredentials.institutionSlug, institutionSlug);
  const deleted = await db
    .delete(institutionCredentials)
    .where(where)
    .returning({ fieldKey: institutionCredentials.fieldKey });
  return deleted.length;
}
