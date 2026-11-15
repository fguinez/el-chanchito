import type { NextRequest } from "next/server";
import { z } from "zod";
import { failureBody, requireJsonContentType } from "@/lib/api/validation";
import {
  fetchControlCredentialStatus,
  reloadControlCredentials,
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
  checkCredentialValue,
  credentialField,
  credentialSpec,
} from "@/lib/credentials/registry";
import { CREDENTIAL_SCHEME, sealCredential } from "@/lib/credentials/seal";

export const dynamic = "force-dynamic";

// Next 16: dynamic segment params arrive as a Promise on the context arg.
type Context = { params: Promise<{ slug: string }> };

// Nothing in this file logs a request body, a value or a sealed blob, and no
// response or error message repeats one.

const NO_KEY_MESSAGE =
  "El servicio de scrapers aún no ha publicado su clave de cifrado. " +
  "Inicia el stack completo con `make dev` y vuelve a intentarlo.";

const bodySchema = z.object({ fields: z.record(z.string(), z.string()) }).strict();

function unknownInstitution() {
  return noStoreJson({ error: "Institución desconocida" }, 404);
}

/** The body as text, refusing an oversized one before and after reading. */
async function readBoundedJson(
  request: NextRequest
): Promise<{ ok: true; body: unknown } | { ok: false; status: number; error: string }> {
  const declared = Number(request.headers.get("content-length") ?? "0");
  if (declared > MAX_CREDENTIAL_BODY_BYTES) {
    return { ok: false, status: 413, error: "Cuerpo demasiado grande" };
  }
  const text = await request.text();
  if (new TextEncoder().encode(text).byteLength > MAX_CREDENTIAL_BODY_BYTES) {
    return { ok: false, status: 413, error: "Cuerpo demasiado grande" };
  }
  try {
    return { ok: true, body: JSON.parse(text) };
  } catch {
    return { ok: false, status: 400, error: "Invalid JSON body" };
  }
}

/** Tell the scraper service; true/false when it answered, null when not. */
async function applyToService(): Promise<boolean | null> {
  const reload = await reloadControlCredentials();
  return reload ? reload.applied : null;
}

/**
 * PUT /api/credentials/[slug]: seal and store some or all of an
 * institution's fields, e.g. `{ "fields": { "password": "..." } }` to
 * re-enter only the password. Unknown fields are refused. Answers which
 * field keys were saved and whether the scraper service applied them
 * (`applied`: null when it could not be reached; it picks them up on its
 * next start).
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
    const checked = checkCredentialValue(field, raw);
    if (!checked.ok) {
      return noStoreJson(failureBody({ error: checked.error, field: `fields.${key}` }), 400);
    }
    values.push({ fieldKey: key, value: checked.value });
  }

  const key = await readSealingKey();
  if (!key) return noStoreJson({ error: NO_KEY_MESSAGE }, 503);
  // When the service can be asked, make sure it can open what we seal: it
  // must hold a usable key, and the very one published.
  const control = await fetchControlCredentialStatus();
  if (control && (!control.key_available || control.key_id !== key.keyId)) {
    return noStoreJson(
      {
        error:
          "El servicio de scrapers no puede usar la clave publicada. " +
          "Revisa su log (local/scrapers-dev.log) y reinícialo con `make dev`.",
      },
      503
    );
  }

  let sealed: SealedField[];
  try {
    sealed = await Promise.all(
      values.map(async ({ fieldKey, value }) => ({
        fieldKey,
        sealed: await sealCredential(key, slug, fieldKey, value),
      }))
    );
  } catch {
    return noStoreJson(
      { error: "No se pudo cifrar con la clave publicada. Reinicia el stack con `make dev`." },
      503
    );
  }
  await upsertSealedFields(slug, CREDENTIAL_SCHEME, key.keyId, sealed);

  return noStoreJson({
    saved: sealed.map((field) => field.fieldKey),
    applied: await applyToService(),
  });
}

/**
 * DELETE /api/credentials/[slug][?field=key]: forget one stored field, or
 * all of the institution's. The scraper service falls back to its local
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
  const deleted = await deleteSealedFields(slug, fieldKey);
  return noStoreJson({ deleted, applied: deleted > 0 ? await applyToService() : null });
}
