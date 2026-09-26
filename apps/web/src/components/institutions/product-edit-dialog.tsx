"use client";

import { useState } from "react";
import { Pencil } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Separator } from "@/components/ui/separator";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { normalizeSlug } from "@/lib/db/slug";
import {
  DISPLAY_ORDER_LIMIT,
  PRODUCT_NAME_MAX,
  PRODUCT_SLUG_MAX,
} from "@/lib/management";
import {
  displayProductName,
  productIdentityChips,
  type ApiInstitution,
  type InstitutionProduct,
} from "@/components/institutions/shared";
import {
  SELECT_CLASS,
  patchJson,
} from "@/components/institutions/management-shared";

interface PatchResponse {
  institutionSlug: string;
  product: { slug: string };
}

/** Ids of every product hanging (directly or not) from `rootId`. */
function descendantIds(
  rootId: string,
  products: InstitutionProduct[]
): Set<string> {
  const out = new Set<string>();
  let frontier = [rootId];
  while (frontier.length > 0) {
    const next = products
      .filter((p) => p.parentProductId && frontier.includes(p.parentProductId))
      .map((p) => p.id)
      .filter((id) => !out.has(id));
    next.forEach((id) => out.add(id));
    frontier = next;
  }
  return out;
}

/** A picker label that tells apart products sharing a display name: the
 *  currency when not CLP, identity chips (account, brand, last 4). */
function parentOptionLabel(
  product: InstitutionProduct,
  institutionName: string
): string {
  const extras = [
    ...(product.currency !== "CLP" ? [product.currency] : []),
    ...productIdentityChips(product),
  ];
  const name = displayProductName(product, institutionName);
  return extras.length > 0 ? `${name} (${extras.join(" · ")})` : name;
}

/**
 * "Editar" button + dialog for one product: display name, parent product,
 * display order and active flag in one form, and the slug in a separate one,
 * since changing it moves the product's URL. `onSaved` gets the product's
 * slug after the save (the new one when it changed).
 */
export function ProductEditDialog({
  institution,
  product,
  onSaved,
}: {
  institution: { slug: string; name: string };
  product: InstitutionProduct;
  onSaved: (slug: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [siblings, setSiblings] = useState<InstitutionProduct[] | null>(null);

  const [name, setName] = useState(product.name);
  const [parentId, setParentId] = useState(product.parentProductId ?? "");
  const [displayOrder, setDisplayOrder] = useState(String(product.displayOrder));
  const [isActive, setIsActive] = useState(product.isActive);
  const [detailsError, setDetailsError] = useState<string | null>(null);
  const [savingDetails, setSavingDetails] = useState(false);

  const [slugInput, setSlugInput] = useState(product.slug);
  const [slugError, setSlugError] = useState<string | null>(null);
  const [savingSlug, setSavingSlug] = useState(false);

  const productUrl = `/api/institutions/${institution.slug}/products/${product.slug}`;

  function handleOpenChange(next: boolean) {
    setOpen(next);
    if (!next) return;
    // Start every opening from the product as it is now.
    setName(product.name);
    setParentId(product.parentProductId ?? "");
    setDisplayOrder(String(product.displayOrder));
    setIsActive(product.isActive);
    setSlugInput(product.slug);
    setDetailsError(null);
    setSlugError(null);
    setSiblings(null);
    fetch(`/api/institutions/${institution.slug}`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data: { institution: ApiInstitution } | null) =>
        setSiblings(data?.institution.products ?? [])
      )
      .catch(() => setSiblings([]));
  }

  // A descendant as parent would loop; the API refuses it too.
  const excluded = siblings
    ? descendantIds(product.id, siblings)
    : new Set<string>();
  const parentOptions = (siblings ?? []).filter(
    (p) => p.id !== product.id && !excluded.has(p.id)
  );
  // Keep the current parent selectable even before the siblings load.
  const parentMissing =
    product.parentProductId != null &&
    !parentOptions.some((p) => p.id === product.parentProductId);

  const shownAs = displayProductName({ ...product, name: name.trim() }, institution.name);
  const cannotDeactivate = product.isActive && product.currentBalance == null;

  async function saveDetails(event: React.FormEvent) {
    event.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) {
      setDetailsError("El nombre es obligatorio");
      return;
    }
    const order = Number(displayOrder);
    if (
      !displayOrder.trim() ||
      !Number.isInteger(order) ||
      Math.abs(order) > DISPLAY_ORDER_LIMIT
    ) {
      setDetailsError("El orden debe ser un número entero");
      return;
    }

    const body: Record<string, unknown> = {};
    if (trimmed !== product.name) body.name = trimmed;
    if ((parentId || null) !== product.parentProductId) {
      body.parentProductId = parentId || null;
    }
    if (order !== product.displayOrder) body.displayOrder = order;
    if (isActive !== product.isActive) body.isActive = isActive;
    if (Object.keys(body).length === 0) {
      setOpen(false);
      return;
    }

    setSavingDetails(true);
    setDetailsError(null);
    const result = await patchJson<PatchResponse>(productUrl, body, {
      isActive:
        "Un producto sin saldo no se puede desactivar: desaparecería del panel.",
    });
    setSavingDetails(false);
    if (!result.ok) {
      setDetailsError(result.error);
      return;
    }
    setOpen(false);
    onSaved(result.data.product.slug);
  }

  const normalizedSlug = normalizeSlug(slugInput);
  const slugUnchanged = normalizedSlug === product.slug;
  // Old slugs may exceed the cap (V014's disambiguation suffix); only a new
  // one is held to it.
  const slugTooLong =
    !slugUnchanged &&
    normalizedSlug != null &&
    normalizedSlug.length > PRODUCT_SLUG_MAX;

  async function saveSlug(event: React.FormEvent) {
    event.preventDefault();
    if (normalizedSlug == null || slugTooLong || slugUnchanged) return;
    setSavingSlug(true);
    setSlugError(null);
    const result = await patchJson<PatchResponse>(
      productUrl,
      { slug: normalizedSlug },
      { slug: `Ese slug ya lo usa otro producto de ${institution.name}.` }
    );
    setSavingSlug(false);
    if (!result.ok) {
      setSlugError(result.error);
      return;
    }
    setOpen(false);
    onSaved(result.data.product.slug);
  }

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <Pencil className="h-4 w-4" />
          Editar
        </Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Editar producto</DialogTitle>
          <DialogDescription>
            El tipo y la moneda los define la institución: los scrapers
            reconocen el producto por ellos, así que no se pueden cambiar.
          </DialogDescription>
        </DialogHeader>

        <form onSubmit={saveDetails} className="space-y-4">
          <div className="space-y-1.5">
            <label htmlFor="product-name" className="text-sm font-medium">
              Nombre
            </label>
            <Input
              id="product-name"
              value={name}
              maxLength={PRODUCT_NAME_MAX}
              onChange={(e) => setName(e.target.value)}
            />
            {name.trim() && shownAs !== name.trim() && (
              <p className="text-xs text-muted-foreground">
                Se muestra como “{shownAs}”.
              </p>
            )}
            <p className="text-xs text-muted-foreground">
              Cambiar el nombre no cambia el slug ni la URL.
            </p>
          </div>

          <div className="space-y-1.5">
            <label htmlFor="product-parent" className="text-sm font-medium">
              Vinculado a
            </label>
            <select
              id="product-parent"
              className={SELECT_CLASS}
              value={parentId}
              disabled={siblings == null}
              onChange={(e) => setParentId(e.target.value)}
            >
              <option value="">Ninguno</option>
              {parentMissing && (
                <option value={product.parentProductId!}>
                  (vínculo actual)
                </option>
              )}
              {parentOptions.map((p) => (
                <option key={p.id} value={p.id}>
                  {parentOptionLabel(p, institution.name)}
                </option>
              ))}
            </select>
            <p className="text-xs text-muted-foreground">
              Por ejemplo, una tarjeta de débito vinculada a su cuenta
              corriente.
            </p>
          </div>

          <div className="space-y-1.5">
            <label
              htmlFor="product-order"
              className="block text-sm font-medium"
            >
              Orden
            </label>
            <Input
              id="product-order"
              type="number"
              step={1}
              className="w-32"
              value={displayOrder}
              onChange={(e) => setDisplayOrder(e.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              Los productos con un número menor aparecen primero.
            </p>
          </div>

          <div className="space-y-1.5">
            <label className="flex items-center gap-2 text-sm font-medium">
              <input
                type="checkbox"
                className="h-4 w-4 accent-primary"
                checked={isActive}
                disabled={cannotDeactivate}
                onChange={(e) => setIsActive(e.target.checked)}
              />
              Producto activo
            </label>
            <p className="text-xs text-muted-foreground">
              {cannotDeactivate
                ? "Un producto sin saldo no se puede desactivar: desaparecería del panel."
                : "Los scrapers dejan de actualizar el saldo de un producto inactivo (sus movimientos se siguen importando). Su último saldo sigue sumando al patrimonio y los monitores que lo usan quedan sin datos."}
            </p>
          </div>

          {detailsError && (
            <p className="text-sm text-destructive">{detailsError}</p>
          )}
          <DialogFooter>
            <Button type="submit" disabled={savingDetails}>
              {savingDetails ? "Guardando…" : "Guardar cambios"}
            </Button>
          </DialogFooter>
        </form>

        <Separator />

        <form onSubmit={saveSlug} className="space-y-3">
          <div className="space-y-1.5">
            <label htmlFor="product-slug" className="text-sm font-medium">
              Slug
            </label>
            <Input
              id="product-slug"
              value={slugInput}
              onChange={(e) => setSlugInput(e.target.value)}
            />
            <p className="text-xs break-all text-muted-foreground">
              URL: /institutions/{institution.slug}/
              <span className="font-medium text-foreground">
                {normalizedSlug ?? "…"}
              </span>
            </p>
            <p className="text-xs text-muted-foreground">
              Cambiar el slug cambia la URL de este producto y deja de
              funcionar la anterior. Los monitores no se ven afectados.
            </p>
          </div>
          {normalizedSlug == null && (
            <p className="text-sm text-destructive">
              El slug necesita al menos una letra o un número.
            </p>
          )}
          {slugTooLong && (
            <p className="text-sm text-destructive">
              El slug puede tener hasta {PRODUCT_SLUG_MAX} caracteres.
            </p>
          )}
          {slugError && <p className="text-sm text-destructive">{slugError}</p>}
          <DialogFooter>
            <Button
              type="submit"
              variant="outline"
              disabled={
                savingSlug ||
                normalizedSlug == null ||
                slugTooLong ||
                slugUnchanged
              }
            >
              {savingSlug ? "Cambiando…" : "Cambiar slug"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
