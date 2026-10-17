import { KIND_INFO, type ProductKind } from "@chanchito/product-model";

/**
 * A meaningful label for the Producto column. Scraped products are auto-named
 * "Institution - kind" (e.g. "Buda - crypto (ETH)"), so a raw name carries no
 * more information than the Tipo badge. When that's the case we fall back to
 * the currency for crypto (CLP / ETH / BTC …) and to the friendly kind label
 * otherwise; anything a human named stays untouched.
 */
export function displayProductName(
  product: { name: string; kind: ProductKind; currency: string },
  institutionName: string
): string {
  const prefix = `${institutionName} - `;
  const cleaned = (
    product.name.startsWith(prefix)
      ? product.name.slice(prefix.length)
      : product.name
  ).trim();

  // The auto-name embeds the institution's name at creation time, so after an
  // institution rename the prefix no longer matches: recognize the suffix.
  const autoNamed = new RegExp(` - ${product.kind}( \\(.*\\))?$`).test(
    product.name
  );
  const isGeneric =
    autoNamed ||
    cleaned === product.kind ||
    cleaned.startsWith(`${product.kind} (`);
  if (isGeneric || !cleaned) {
    if (product.kind === "crypto") return product.currency;
    return KIND_INFO[product.kind].labelEs;
  }
  return cleaned;
}

/**
 * What the edit dialog's Nombre field sends as `name`: undefined for no
 * change, null to reset to the default name, or the new name. An empty field,
 * or one holding exactly the default's display label (the placeholder), means
 * "use the default", so typing the placeholder never pins a rename.
 */
export function nameFieldPatch(
  input: string,
  product: { name: string; defaultName: string },
  defaultLabel: string
): string | null | undefined {
  const trimmed = input.trim();
  if (!trimmed || trimmed === defaultLabel) {
    return product.name !== product.defaultName ? null : undefined;
  }
  return trimmed !== product.name ? trimmed : undefined;
}

/** Keys that accept the placeholder as a starting point, like a suggestion. */
const FILL_KEYS = new Set(["ArrowRight", "End"]);

/**
 * Whether a double-click (no `key`) or a key press on the edit dialog's
 * Nombre field should fill it with the default's display label. Only an empty
 * field fills, so a double-click on typed text keeps its word selection.
 */
export function fillsNameFromDefault(value: string, key?: string): boolean {
  if (value !== "") return false;
  return key === undefined || FILL_KEYS.has(key);
}
