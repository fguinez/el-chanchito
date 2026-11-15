import { fetchControlCredentialStatus, fetchEnabledScrapers } from "@/lib/credentials/control";
import { listCredentialMeta, readSealingKey } from "@/lib/credentials/db";
import { buildCredentialsOverview, noStoreJson } from "@/lib/credentials/overview";

export const dynamic = "force-dynamic";

/**
 * GET /api/credentials: per institution and field, whether a credential is
 * stored here, needs re-entry, or comes from the scraper service's local
 * secret store or env, and when it was last saved. Never a value: the sealed
 * column is not even selected. With the scraper service down the stored
 * state still shows and the fallback reads as unknown.
 */
export async function GET() {
  const [key, meta, control, enabledScrapers] = await Promise.all([
    readSealingKey(),
    listCredentialMeta(),
    fetchControlCredentialStatus(),
    fetchEnabledScrapers(),
  ]);
  return noStoreJson(buildCredentialsOverview({ key, meta, control, enabledScrapers }));
}
