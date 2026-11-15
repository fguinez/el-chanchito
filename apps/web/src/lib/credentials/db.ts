// Data access for the sealed credentials (V048). The read path selects the
// metadata columns only: no query here ever returns `sealed` to a caller.

import { and, eq } from "drizzle-orm";
import { sql } from "drizzle-orm";
import { db } from "@/lib/db";
import { credentialKeys, institutionCredentials } from "@/lib/db/schema";
import type { SealingKey } from "./seal";

export async function readSealingKey(): Promise<SealingKey | null> {
  const rows = await db
    .select({
      keyId: credentialKeys.keyId,
      algorithm: credentialKeys.algorithm,
      publicKey: credentialKeys.publicKey,
    })
    .from(credentialKeys)
    .where(eq(credentialKeys.id, 1))
    .limit(1);
  return rows[0] ?? null;
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

/** Insert or replace the given fields of one institution, in one statement. */
export async function upsertSealedFields(
  institutionSlug: string,
  scheme: string,
  keyId: string,
  fields: SealedField[]
): Promise<void> {
  if (fields.length === 0) return;
  await db
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
}

/** Delete one field, or every field of the institution; the number deleted. */
export async function deleteSealedFields(
  institutionSlug: string,
  fieldKey?: string
): Promise<number> {
  const where = fieldKey
    ? and(
        eq(institutionCredentials.institutionSlug, institutionSlug),
        eq(institutionCredentials.fieldKey, fieldKey)
      )
    : eq(institutionCredentials.institutionSlug, institutionSlug);
  const deleted = await db
    .delete(institutionCredentials)
    .where(where)
    .returning({ fieldKey: institutionCredentials.fieldKey });
  return deleted.length;
}
