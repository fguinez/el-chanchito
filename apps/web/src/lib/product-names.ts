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
