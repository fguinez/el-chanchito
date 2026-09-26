/**
 * `scraper_runs.institution` of the runs made before V008 by the old combined
 * email scraper (MACH, Mercado Pago and Tenpo in one run). It is not an
 * institution, so it has no `institutions` row to take a name from.
 */
export const LEGACY_COMPOSITE_INSTITUTION = "_legacy_composite";

/**
 * Display name of a scraper run's institution: its `institutions.name`
 * (joined in by `GET /api/scrapers`), else the slug itself.
 */
export function scraperRunLabel(run: {
  institution: string;
  institution_name: string | null;
}): string {
  if (run.institution_name) return run.institution_name;
  if (run.institution === LEGACY_COMPOSITE_INSTITUTION) return "Email (legacy)";
  return run.institution;
}
