// Display names for the scraper service's institution slugs, which is all it
// reports (scraper runs, refresh responses).

const INSTITUTION_LABELS: Record<string, string> = {
  fintual: "Fintual",
  buda: "Buda",
  banchile: "Banco de Chile",
  mach: "MACH",
  mercadopago: "MercadoPago",
  tenpo: "Tenpo",
  bci_lider: "BCI Lider",
  _legacy_composite: "Email (legacy)",
};

/** The display name for `slug`, or the slug itself when it has none. */
export function institutionLabel(slug: string): string {
  return INSTITUTION_LABELS[slug] ?? slug;
}
