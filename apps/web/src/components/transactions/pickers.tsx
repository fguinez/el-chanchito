"use client";

// Small shared data hooks and a product <select>, used by the Gastos manual
// form and the transaction edit form: the categories list (for recategorizing)
// and the active products grouped by institution (for choosing a product).

import { useEffect, useState } from "react";
import { SELECT_CLASS } from "@/components/ui/native-select";

export interface CategoryOption {
  id: string;
  name: string;
}

/** Every category, by name, for a recategorize <select>. Empty on failure. */
export function useCategories(): CategoryOption[] {
  const [categories, setCategories] = useState<CategoryOption[]>([]);
  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/categories", { signal: controller.signal })
      .then((res) => (res.ok ? res.json() : []))
      .then((rows: CategoryOption[]) =>
        setCategories(rows.map((c) => ({ id: c.id, name: c.name })))
      )
      .catch(() => {});
    return () => controller.abort();
  }, []);
  return categories;
}

export interface PickerProduct {
  id: string;
  name: string;
}
export interface PickerInstitution {
  slug: string;
  name: string;
  products: PickerProduct[];
}
interface InstitutionsResponse {
  institutions: {
    slug: string;
    name: string;
    products: {
      id: string;
      name: string;
      currency: string;
      isActive: boolean;
    }[];
  }[];
}

/** Active products grouped by institution, optionally only one currency
 *  (the Gastos form is CLP-only). `error` is true when the fetch failed. */
export function useActiveProducts(currency?: string): {
  institutions: PickerInstitution[];
  error: boolean;
} {
  const [institutions, setInstitutions] = useState<PickerInstitution[]>([]);
  const [error, setError] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    fetch("/api/institutions", { signal: controller.signal })
      .then((res) => {
        if (!res.ok) throw new Error("failed");
        return res.json();
      })
      .then((data: InstitutionsResponse) => {
        setInstitutions(
          data.institutions
            .map((inst) => ({
              slug: inst.slug,
              name: inst.name,
              products: inst.products
                .filter(
                  (p) => p.isActive && (!currency || p.currency === currency)
                )
                .map((p) => ({ id: p.id, name: p.name })),
            }))
            .filter((inst) => inst.products.length > 0)
        );
      })
      .catch(() => setError(true));
    return () => controller.abort();
  }, [currency]);
  return { institutions, error };
}

/** A native product <select>, styled like the Input primitive. */
export function ProductSelect({
  value,
  onChange,
  institutions,
  emptyLabel = "Sin producto",
  id,
}: {
  value: string;
  onChange: (id: string) => void;
  institutions: PickerInstitution[];
  emptyLabel?: string;
  id?: string;
}) {
  return (
    <select
      id={id}
      className={SELECT_CLASS}
      value={value}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="">{emptyLabel}</option>
      {institutions.map((inst) => (
        <optgroup key={inst.slug} label={inst.name}>
          {inst.products.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
            </option>
          ))}
        </optgroup>
      ))}
    </select>
  );
}
