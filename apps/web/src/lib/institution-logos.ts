// Never hotlink a logo or use a favicon service: that would tell a third party
// which banks the user has.

/** Where the logo files are served from (not /institutions/, a page route). */
export const INSTITUTION_LOGO_DIR = "/logos/institutions";

export interface InstitutionLogoEntry {
  file: string;
  /** The image carries its own background (an app icon), so it fills the
   *  tile edge to edge instead of sitting padded on white. */
  bleed?: boolean;
}

export const INSTITUTION_LOGOS: Record<string, InstitutionLogoEntry> = {
  // bancochile.cl header isotype, set on the bank's navy (#002464).
  banchile: { file: "banchile.svg", bleed: true },
  // Isotype of the Bci logo served by bci.cl.
  bci: { file: "bci.svg" },
  // liderbci.cl apple-touch-icon.
  bci_lider: { file: "bci_lider.svg", bleed: true },
  // mercadopago.cl favicon.
  mercadopago: { file: "mercadopago.svg" },
  // MACHBANK App Store icon.
  mach: { file: "mach.png", bleed: true },
  // tenpo.cl apple-touch-icon.
  tenpo: { file: "tenpo.png", bleed: true },
  // fintual.cl apple-touch-icon.
  fintual: { file: "fintual.png" },
  // Mark from the blog.buda.com favicon.
  buda: { file: "buda.svg" },
  // santander.com favicon.
  santander: { file: "santander.png" },
  // skipo.com apple-touch-icon.
  skipo: { file: "skipo.png" },
  // bancofalabella.cl isotype.
  banco_falabella: { file: "banco_falabella.svg" },
  // Racional App Store icon.
  racional: { file: "racional.png", bleed: true },
};

export interface ResolvedInstitutionLogo {
  src: string;
  bleed: boolean;
}

/** The logo for `slug`, or null when it has none (manual, csv_import, a
 *  user-created institution). */
export function institutionLogo(
  slug: string
): ResolvedInstitutionLogo | null {
  // hasOwn: a slug like "constructor" must not hit Object.prototype.
  if (!Object.hasOwn(INSTITUTION_LOGOS, slug)) return null;
  const entry = INSTITUTION_LOGOS[slug];
  return {
    src: `${INSTITUTION_LOGO_DIR}/${entry.file}`,
    bleed: entry.bleed ?? false,
  };
}
