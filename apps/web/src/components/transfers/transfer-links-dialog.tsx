"use client";

import { useMemo, useState } from "react";
import { Link2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { SearchInput } from "@/components/ui/search-input";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";
import { formatCLP, formatPlainDateEs } from "@/lib/utils";
import {
  descriptionTitle,
  matchesTransactionSearch,
  searchTokens,
} from "@/lib/transaction-search";
import { InstitutionLogo } from "@/components/institutions/institution-logo";
import {
  CANDIDATE_WINDOW_DAYS,
  LINK_FIELD,
  TRANSFER_SIDES,
  type TransferProductRef,
  type TransferSide,
} from "@/lib/transfers";

/** What the dialog needs from a transfer as GET /api/transfers lists it. */
export interface LinkableTransferView {
  id: string;
  description: string;
  amount: number;
  transferDate: string;
  fromProduct: TransferProductRef | null;
  toProduct: TransferProductRef | null;
  fromTransactionId: string | null;
  toTransactionId: string | null;
}

/** One side as GET /api/transfers/[id]/candidates returns it. */
interface CandidateSide {
  productId: string | null;
  hasFeed: boolean;
  candidates: {
    id: string;
    transactionDate: string;
    /** The scraped text; `displayDescription` is what to show. */
    description: string;
    displayDescription: string;
    amount: number;
    source: string;
    linked: boolean;
  }[];
}

type Candidates = Record<TransferSide, CandidateSide>;
type Selection = Record<TransferSide, string>;

const SIDE_LABEL: Record<TransferSide, string> = {
  from: "Salida (desde)",
  to: "Entrada (hacia)",
};

/** "" = unlinked. */
function currentSelection(transfer: LinkableTransferView): Selection {
  return {
    from: transfer.fromTransactionId ?? "",
    to: transfer.toTransactionId ?? "",
  };
}

/** "Vincular" button + dialog picking the scraped transaction of each side. */
export function TransferLinksDialog({
  transfer,
  onSaved,
}: {
  transfer: LinkableTransferView;
  onSaved: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [candidates, setCandidates] = useState<Candidates | null>(null);
  const [selection, setSelection] = useState<Selection>(() =>
    currentSelection(transfer)
  );
  const [loadError, setLoadError] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  function handleOpenChange(next: boolean) {
    setOpen(next);
    if (!next) return;
    // Start every opening from the transfer as it is now.
    setSelection(currentSelection(transfer));
    setCandidates(null);
    setLoadError(false);
    setError(null);
    fetch(`/api/transfers/${transfer.id}/candidates`)
      .then((res) => {
        if (!res.ok) throw new Error("failed");
        return res.json();
      })
      .then(setCandidates)
      .catch(() => setLoadError(true));
  }

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      const body = Object.fromEntries(
        TRANSFER_SIDES.map((side) => [LINK_FIELD[side], selection[side] || null])
      );
      const res = await fetch(`/api/transfers/${transfer.id}/links`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      if (res.status === 404) {
        setError("Este movimiento ya no existe; recarga la página.");
        return;
      }
      if (!res.ok) {
        const data = await res.json().catch(() => null);
        setError(data?.error ?? "No se pudo vincular el movimiento");
        return;
      }
      setOpen(false);
      onSaved();
    } catch {
      setError("No se pudo vincular el movimiento");
    } finally {
      setSaving(false);
    }
  }

  const products: Record<TransferSide, TransferProductRef | null> = {
    from: transfer.fromProduct,
    to: transfer.toProduct,
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button type="button" variant="outline" size="xs" title="Vincular transacciones">
          <Link2 />
          Vincular
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Vincular transacciones</DialogTitle>
          <DialogDescription>
            Elige el cargo y el abono que importaron los scrapers para{" "}
            <span className="font-medium">{transfer.description}</span> (
            {formatCLP(transfer.amount)}, {formatPlainDateEs(transfer.transferDate)}).
            Las transacciones vinculadas dejan de sumar en Gastos e Ingresos.
            Solo aparecen las del mismo monto exacto, fechadas hasta{" "}
            {CANDIDATE_WINDOW_DAYS} días antes o después.
          </DialogDescription>
        </DialogHeader>

        {loadError ? (
          <p className="text-sm text-destructive">
            No se pudieron cargar las transacciones candidatas.
          </p>
        ) : !candidates ? (
          <p className="text-sm text-muted-foreground">Cargando...</p>
        ) : (
          <div className="space-y-5">
            {TRANSFER_SIDES.map((side) => (
              <SidePicker
                key={side}
                side={side}
                product={products[side]}
                data={candidates[side]}
                value={selection[side]}
                onChange={(id) =>
                  setSelection((prev) => ({ ...prev, [side]: id }))
                }
                name={`transfer-${transfer.id}-${side}`}
              />
            ))}
          </div>
        )}

        {error && <p className="text-sm text-destructive">{error}</p>}

        <DialogFooter>
          <Button
            type="button"
            onClick={handleSave}
            disabled={saving || !candidates}
          >
            {saving ? "Guardando..." : "Guardar"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** One side's radio list, with a search over its candidates' original and
 *  displayed descriptions. A selection the search hides stays selected. */
function SidePicker({
  side,
  product,
  data,
  value,
  onChange,
  name,
}: {
  side: TransferSide;
  product: TransferProductRef | null;
  data: CandidateSide;
  value: string;
  onChange: (id: string) => void;
  name: string;
}) {
  const [search, setSearch] = useState("");
  const tokens = useMemo(() => searchTokens(search), [search]);
  const shown = data.candidates.filter((c) =>
    matchesTransactionSearch(tokens, c.displayDescription, c.description)
  );
  const selectedHidden =
    data.candidates.some((c) => c.id === value) &&
    !shown.some((c) => c.id === value);

  return (
    <fieldset className="space-y-2">
      <legend className="text-sm font-medium">
        {SIDE_LABEL[side]}
        {product && (
          <span className="ml-1 font-normal text-muted-foreground">
            ·{" "}
            <span className="inline-flex items-center gap-1 align-bottom">
              <InstitutionLogo slug={product.institutionSlug} size="sm" />
              {product.institutionName}
            </span>{" "}
            · {product.name}
          </span>
        )}
      </legend>
      {!product ? (
        <p className="text-sm text-muted-foreground">
          Este movimiento no tiene producto en este lado.
        </p>
      ) : (
        <div className="space-y-1">
          {data.candidates.length > 0 && (
            <SearchInput
              className="mb-2 w-full sm:max-w-xs"
              inputClassName="h-8 text-sm"
              aria-label={`Buscar en ${SIDE_LABEL[side].toLowerCase()} por descripción`}
              value={search}
              onValueChange={setSearch}
            />
          )}
          <label className="flex items-center gap-2 text-sm">
            <input
              type="radio"
              name={name}
              checked={value === ""}
              onChange={() => onChange("")}
            />
            <span className="text-muted-foreground">Sin vincular</span>
          </label>
          {shown.map((c) => (
            <label key={c.id} className="flex items-center gap-2 text-sm">
              <input
                type="radio"
                name={name}
                checked={value === c.id}
                onChange={() => onChange(c.id)}
              />
              <span className="w-24 shrink-0 tabular-nums">
                {formatPlainDateEs(c.transactionDate)}
              </span>
              <span
                className="min-w-0 flex-1 truncate"
                title={descriptionTitle(c.displayDescription, c.description)}
              >
                {c.displayDescription}
              </span>
              <span
                className={`tabular-nums ${
                  c.amount < 0 ? "text-red-600" : "text-green-600"
                }`}
              >
                {c.amount < 0 ? "-" : "+"}
                {formatCLP(Math.abs(c.amount))}
              </span>
            </label>
          ))}
          {data.candidates.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              {data.hasFeed
                ? "No hay transacciones que calcen con este movimiento."
                : "Este producto no tiene transacciones; déjalo sin vincular y marca el movimiento como resuelto a mano."}
            </p>
          ) : (
            shown.length === 0 && (
              <p className="text-sm text-muted-foreground">
                Sin resultados para «{search.trim()}».
              </p>
            )
          )}
          {selectedHidden && (
            <p className="text-xs text-muted-foreground">
              La transacción elegida no aparece con esta búsqueda, pero sigue
              elegida.
            </p>
          )}
        </div>
      )}
    </fieldset>
  );
}
