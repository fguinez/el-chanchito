import type { NextRequest } from "next/server";
import { z } from "zod";
import { failureBody, requireJsonContentType } from "@/lib/api/validation";
import {
  KEY_CHANGED_MESSAGE,
  SAVE_BLOCKED_MESSAGES,
  saveBlocker,
} from "@/lib/credentials/availability";
import {
  fetchControlCredentialStatus,
  reloadControlCredentials,
  triggerControlRefresh,
} from "@/lib/credentials/control";
import {
  deleteSealedFields,
  readSealingKey,
  upsertSealedFields,
  type SealedField,
} from "@/lib/credentials/db";
import { noStoreJson } from "@/lib/credentials/overview";
import {
  MAX_CREDENTIAL_BODY_BYTES,
  bindingGroups,
  bindingProblem,
  checkCredentialValue,
  credentialField,
  credentialSpec,
} from "@/lib/credentials/registry";
import { CREDENTIAL_SCHEME, bindingDigest, sealCredential } from "@/lib/credentials/seal";

export const dynamic = "force-dynamic";

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ slug: string }> };

// Nothing in this file logs a request body, a value or a sealed blob, and no
// response or error message repeats one.

const bodySchema = z.object({ fields: z.record(z.string(), z.string()) }).strict();

function unknownInstitution() {
  return noStoreJson({ error: "Institución desconocida" }, 404);
}

const TOO_LARGE = { ok: false as const, status: 413, error: "Cuerpo demasiado grande" };

/** The body as JSON, never buffering more than the cap: a declared length
 *  over it is refused up front, and the stream is cut as soon as it passes. */
async function readBoundedJson(
  request: NextRequest
): Promise<{ ok: true; body: unknown } | { ok: false; status: number; error: string }> {
  const declared = Number(request.headers.get("content-length") ?? "0");
  if (declared > MAX_CREDENTIAL_BODY_BYTES) return TOO_LARGE;
  const chunks: Uint8Array[] = [];
  let size = 0;
  if (request.body) {
    const reader = request.body.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > MAX_CREDENTIAL_BODY_BYTES) {
        await reader.cancel();
        return TOO_LARGE;
      }
      chunks.push(value);
    }
  }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    return { ok: true, body: JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) };
  } catch {
    return { ok: false, status: 400, error: "El cuerpo no es JSON válido." };
  }
}

function envFlag(name: string | null): boolean {
  if (!name) return false;
  const value = (process.env[name] ?? "").trim().toLowerCase();
  return !["", "0", "false", "no", "off"].includes(value);
}

/** Tell the scraper service; `applied` is true/false when it answered and
 *  null when not. A scraper this change enabled is run right away through the
 *  refresh endpoint (its cooldown and in-flight guards apply); `triggered`
 *  lists the ones the service accepted. */
async function applyToService(): Promise<{ applied: boolean | null; triggered: string[] }> {
  const reload = await reloadControlCredentials();
  if (!reload) return { applied: null, triggered: [] };
  const triggered: string[] = [];
  for (const slug of reload.added) {
    if (await triggerControlRefresh(slug)) triggered.push(slug);
  }
  return { applied: reload.applied, triggered };
}

/**
 * PUT /api/credentials/[slug]: seal and store some or all of an
 * institution's fields, e.g. `{ "fields": { "password": "..." } }` to
 * re-enter only the password. Unknown fields are refused. Answers which
 * field keys were saved and whether the scraper service applied them
 * (`applied`: null when it could not be reached; it picks them up on its
 * next start). Saving needs the running service to hold the active key:
 * otherwise 503 with `reason` (see lib/credentials/availability), and 409
 * `key_changed` if the key rotated twice under the request.
 */
export async function PUT(request: NextRequest, { params }: Context) {
  const { slug } = await params;
  const spec = credentialSpec(slug);
  if (!spec) return unknownInstitution();
  if (spec.fields.length === 0) {
    return noStoreJson(
      failureBody({ error: `${spec.labelEs} no usa credenciales guardadas.` }),
      400
    );
  }

  const unsupported = requireJsonContentType(request);
  if (unsupported) {
    unsupported.headers.set("cache-control", "no-store");
    return unsupported;
  }
  const read = await readBoundedJson(request);
  if (!read.ok) return noStoreJson({ error: read.error }, read.status);
  const parsed = bodySchema.safeParse(read.body);
  if (!parsed.success) {
    return noStoreJson(
      failureBody({ error: "Se espera { fields: { campo: valor } }", field: "fields" }),
      400
    );
  }

  const entries = Object.entries(parsed.data.fields);
  if (entries.length === 0) {
    return noStoreJson(failureBody({ error: "No hay campos que guardar", field: "fields" }), 400);
  }
  const values: { fieldKey: string; value: string }[] = [];
  for (const [key, raw] of entries) {
    const field = credentialField(spec, key);
    if (!field) {
      return noStoreJson(
        failureBody({ error: "Campo desconocido para esta institución", field: `fields.${key}` }),
        400
      );
    }
    const checked = checkCredentialValue(field, raw, envFlag(field.allowCustomEnv));
    if (!checked.ok) {
      return noStoreJson(failureBody({ error: checked.error, field: `fields.${key}` }), 400);
    }
    values.push({ fieldKey: key, value: checked.value });
  }

  const grouping = bindingProblem(
    spec,
    values.map((v) => v.fieldKey)
  );
  if (grouping) return noStoreJson(failureBody({ error: grouping, field: "fields" }), 400);

  const byKey = new Map(values.map((v) => [v.fieldKey, v.value]));
  // A rotation between reading the key and storing makes the store refuse
  // (the key is retired); read the new key and seal once more.
  for (let attempt = 0; attempt < 2; attempt++) {
    const key = await readSealingKey();
    // Seal only to a key the running service confirms it holds: the active
    // row alone could be stale, another service's or planted.
    const blocked = saveBlocker(key, await fetchControlCredentialStatus());
    if (blocked || !key) {
      return noStoreJson({ error: SAVE_BLOCKED_MESSAGES[blocked ?? "no_key"], reason: blocked }, 503);
    }
    let sealed: SealedField[];
    try {
      sealed = await Promise.all(
        values.map(async ({ fieldKey, value }) => {
          const binds = credentialField(spec, fieldKey)?.binds ?? [];
          // bindingProblem guaranteed every bound value is in this request.
          const binding =
            binds.length > 0
              ? await bindingDigest(binds.map((k) => byKey.get(k) ?? ""))
              : undefined;
          return { fieldKey, sealed: await sealCredential(key, slug, fieldKey, value, binding) };
        })
      );
    } catch {
      return noStoreJson(
        { error: "No se pudo cifrar con la clave publicada. Reinicia el servicio de scrapers." },
        503
      );
    }
    if (await upsertSealedFields(slug, CREDENTIAL_SCHEME, key.keyId, sealed)) {
      return noStoreJson({
        saved: sealed.map((field) => field.fieldKey),
        ...(await applyToService()),
      });
    }
  }
  return noStoreJson({ error: KEY_CHANGED_MESSAGE, reason: "key_changed" }, 409);
}

/**
 * DELETE /api/credentials/[slug][?field=key]: forget one stored field (with
 * the rest of its binding group, e.g. the three IMAP fields), or all of the
 * institution's. The scraper service falls back to its local
 * secret store or env for them.
 */
export async function DELETE(request: NextRequest, { params }: Context) {
  const { slug } = await params;
  const spec = credentialSpec(slug);
  if (!spec) return unknownInstitution();
  const fieldKey = request.nextUrl.searchParams.get("field") ?? undefined;
  if (fieldKey !== undefined && !credentialField(spec, fieldKey)) {
    return noStoreJson(
      failureBody({ error: "Campo desconocido para esta institución", field: "field" }),
      400
    );
  }
  // A field in a binding group goes with its whole group: a secret must never
  // outlive, or be paired with, a destination it was not saved with.
  const group = fieldKey
    ? (bindingGroups(spec).find((keys) => keys.includes(fieldKey)) ?? [fieldKey])
    : undefined;
  const deleted = await deleteSealedFields(slug, group);
  const applied = deleted > 0 ? (await applyToService()).applied : null;
  return noStoreJson({ deleted, applied });
}
