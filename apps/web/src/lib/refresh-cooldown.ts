// The scraper service refuses to re-trigger a scraper inside its manual-refresh
// cooldown (BanChile: when a run would need a second full login too soon, since
// the bank throttles logins). `POST /refresh/{slug}` then answers 429 and
// `POST /refresh` skips it; both list it as
// `skipped: [{ slug, retry_after_seconds }]`. This turns that into the notice
// the Instituciones pages show instead of a generic error.

import { institutionLabel } from "@/lib/institution-labels";

interface SkippedRefresh {
  slug: string;
  retryAfterSeconds: number;
}

function skippedRefreshes(body: unknown): SkippedRefresh[] {
  const skipped = (body as { skipped?: unknown } | null)?.skipped;
  if (!Array.isArray(skipped)) return [];
  return skipped.flatMap((entry) => {
    const slug = entry?.slug;
    const seconds = entry?.retry_after_seconds;
    return typeof slug === "string" && Number.isFinite(seconds)
      ? [{ slug, retryAfterSeconds: seconds as number }]
      : [];
  });
}

function minutesLabel(seconds: number): string {
  const minutes = Math.max(1, Math.ceil(seconds / 60));
  return minutes === 1 ? "1 minuto" : `${minutes} minutos`;
}

/**
 * The Spanish notice for a refresh response's cooled-down scrapers, or null
 * when nothing was skipped. A 429 always yields a notice, even with a body
 * that doesn't say which scraper or for how long.
 */
export function refreshCooldownNotice(
  status: number,
  body: unknown
): string | null {
  const skipped = skippedRefreshes(body);
  if (skipped.length === 0) {
    return status === 429
      ? "Esta institución se consultó hace poco; inténtalo de nuevo en unos minutos."
      : null;
  }
  return skipped
    .map(
      ({ slug, retryAfterSeconds }) =>
        `${institutionLabel(slug)} se consultó hace poco; para evitar el bloqueo ` +
        `del banco, se podrá actualizar de nuevo en ${minutesLabel(retryAfterSeconds)}.`
    )
    .join(" ");
}
