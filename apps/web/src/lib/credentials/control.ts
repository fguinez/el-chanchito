// The scraper service's /credentials endpoints, reached through
// SCRAPER_CONTROL_URL like the refresh routes. Every call is best effort:
// null means the service is not configured, not reachable, or answered with
// something unexpected. Responses carry booleans and a key id only.

import { z } from "zod";

const TIMEOUT_MS = 5000;

const fieldStatus = z.object({
  fallback: z.boolean(),
  stored: z.boolean(),
  needs_reentry: z.boolean(),
});

const statusSchema = z.object({
  key_available: z.boolean(),
  key_id: z.string().nullable(),
  // ok, starting, key_mismatch, key_unavailable or pending_unresolved.
  state: z.string().optional(),
  // The database seals to another service's key that owns stored rows.
  key_mismatch: z.boolean().optional(),
  max_age_days: z.number().int().nonnegative().optional(),
  rotation_failed_at: z.string().nullable().optional(),
  rotation_error: z.string().nullable().optional(),
  fields: z.record(z.string(), fieldStatus),
});

export type ControlCredentialStatus = z.infer<typeof statusSchema>;

const reloadSchema = z.object({
  applied: z.boolean(),
  added: z.array(z.string()).default([]),
  scrapers: z.array(z.string()),
});

export type ControlReload = z.infer<typeof reloadSchema>;

const scrapersSchema = z.object({ scrapers: z.array(z.string()) });

function controlUrl(path: string): string | null {
  const base = process.env.SCRAPER_CONTROL_URL;
  return base ? `${base.replace(/\/$/, "")}${path}` : null;
}

async function call<S extends z.ZodType>(
  path: string,
  method: "GET" | "POST",
  schema: S
): Promise<z.output<S> | null> {
  const url = controlUrl(path);
  if (!url) return null;
  try {
    const res = await fetch(url, {
      method,
      // The service refuses a /credentials POST not labelled JSON.
      headers: method === "POST" ? { "content-type": "application/json" } : undefined,
      body: method === "POST" ? "{}" : undefined,
      signal: AbortSignal.timeout(TIMEOUT_MS),
      cache: "no-store",
    });
    if (!res.ok) return null;
    const parsed = schema.safeParse(await res.json());
    return parsed.success ? parsed.data : null;
  } catch {
    return null;
  }
}

export function fetchControlCredentialStatus() {
  return call("/credentials/status", "GET", statusSchema);
}

export async function fetchEnabledScrapers(): Promise<string[] | null> {
  return (await call("/scrapers", "GET", scrapersSchema))?.scrapers ?? null;
}

/** Ask the service to re-read the stored credentials and rebuild its
 *  scrapers. Null when it could not be asked. */
export function reloadControlCredentials() {
  return call("/credentials/reload", "POST", reloadSchema);
}

/** Run a newly enabled scraper now through the regular refresh endpoint, so
 *  its cooldown and in-flight guards apply. True when the service accepted. */
export async function triggerControlRefresh(slug: string): Promise<boolean> {
  const url = controlUrl(`/refresh/${encodeURIComponent(slug)}`);
  if (!url) return false;
  try {
    const res = await fetch(url, {
      method: "POST",
      signal: AbortSignal.timeout(TIMEOUT_MS),
      cache: "no-store",
    });
    return res.status === 202;
  } catch {
    return false;
  }
}
