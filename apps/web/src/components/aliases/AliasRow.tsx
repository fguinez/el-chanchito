"use client";

import { useState } from "react";
import { Check, FlaskConical, Pencil, Trash2, X } from "lucide-react";
import { TableCell, TableRow } from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  ALIAS_MAX_LENGTH,
  ALIAS_PRIORITY_MAX,
  ALIAS_PRIORITY_MIN,
  MATCH_DESCRIPTION_MAX_LENGTH,
  MATCH_TYPES,
  type MatchType,
} from "@/lib/aliases";
import { AliasPreview } from "./AliasPreview";
import {
  CURRENCY_OPTIONS,
  MATCH_TYPE_LABELS,
  PRIORITY_ERROR,
  SELECT_CLASS,
  formatAmountCondition,
  parsePriority,
  readAmountCondition,
  readApiError,
  type ApiAlias,
} from "./shared";

const ALIAS_GONE_MESSAGE = "El alias ya no existe";

type AliasDraft = {
  matchDescription: string;
  matchType: MatchType;
  alias: string;
  priority: string;
  amountMin: string;
  amountMax: string;
  currency: string;
};

/** The match under test, or null when the preview is closed. */
type PreviewTarget = {
  matchDescription: string;
  matchType: MatchType;
  amountMin: number | null;
  amountMax: number | null;
  currency: string | null;
};

export function AliasRow({
  alias,
  version,
  onChange,
}: {
  alias: ApiAlias;
  version: number;
  onChange: () => void;
}) {
  // null while viewing; the fields being edited otherwise.
  const [draft, setDraft] = useState<AliasDraft | null>(null);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [preview, setPreview] = useState<PreviewTarget | null>(null);

  function startEditing() {
    setError(null);
    setDraft({
      matchDescription: alias.matchDescription,
      matchType: alias.matchType,
      alias: alias.alias,
      priority: String(alias.priority),
      amountMin: alias.amountMin === null ? "" : String(alias.amountMin),
      amountMax: alias.amountMax === null ? "" : String(alias.amountMax),
      currency: alias.currency ?? "",
    });
  }

  function stopEditing() {
    setDraft(null);
    setError(null);
  }

  function handleTest() {
    const matchDescription = (
      draft?.matchDescription ?? alias.matchDescription
    ).trim();
    if (!matchDescription) {
      setError("Escribe una descripción para probarla");
      return;
    }
    const condition = draft
      ? readAmountCondition(draft.amountMin, draft.amountMax, draft.currency)
      : {
          amountMin: alias.amountMin,
          amountMax: alias.amountMax,
          currency: alias.currency,
        };
    if ("error" in condition) {
      setError(condition.error);
      return;
    }
    setPreview({
      matchDescription,
      matchType: draft?.matchType ?? alias.matchType,
      ...condition,
    });
  }

  async function handleSave() {
    if (!draft) return;
    const matchDescription = draft.matchDescription.trim();
    const aliasText = draft.alias.trim();
    const priority = parsePriority(draft.priority);
    if (!matchDescription) {
      setError("La descripción es obligatoria");
      return;
    }
    if (!aliasText) {
      setError("El alias es obligatorio");
      return;
    }
    if (priority === null) {
      setError(PRIORITY_ERROR);
      return;
    }
    const condition = readAmountCondition(
      draft.amountMin,
      draft.amountMax,
      draft.currency
    );
    if ("error" in condition) {
      setError(condition.error);
      return;
    }
    const patch: Record<string, string | number | null> = {};
    if (matchDescription !== alias.matchDescription) {
      patch.matchDescription = matchDescription;
    }
    if (draft.matchType !== alias.matchType) patch.matchType = draft.matchType;
    if (aliasText !== alias.alias) patch.alias = aliasText;
    if (priority !== alias.priority) patch.priority = priority;
    if (condition.amountMin !== alias.amountMin) {
      patch.amountMin = condition.amountMin;
    }
    if (condition.amountMax !== alias.amountMax) {
      patch.amountMax = condition.amountMax;
    }
    if (condition.currency !== alias.currency) patch.currency = condition.currency;
    if (Object.keys(patch).length === 0) {
      stopEditing();
      return;
    }

    setSaving(true);
    setError(null);
    try {
      const res = await fetch(`/api/aliases/${alias.id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(patch),
      });
      if (!res.ok) {
        setError(
          await readApiError(res, "No se pudo guardar el alias", {
            404: ALIAS_GONE_MESSAGE,
          })
        );
        return;
      }
      stopEditing();
      onChange();
    } catch {
      setError("No se pudo guardar el alias");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (
      deleting ||
      !window.confirm(
        `¿Eliminar el alias "${alias.alias}"?\n\nLas transacciones volverán a mostrar su descripción original.`
      )
    ) {
      return;
    }
    setDeleting(true);
    setError(null);
    try {
      const res = await fetch(`/api/aliases/${alias.id}`, { method: "DELETE" });
      if (!res.ok) {
        setError(
          await readApiError(res, "No se pudo eliminar el alias", {
            404: ALIAS_GONE_MESSAGE,
          })
        );
        return;
      }
      onChange();
    } catch {
      setError("No se pudo eliminar el alias");
    } finally {
      setDeleting(false);
    }
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Enter" && !saving) handleSave();
    if (e.key === "Escape") stopEditing();
  }

  return (
    <>
      <TableRow>
        <TableCell>
          {draft ? (
            <Input
              aria-label="Descripción a coincidir"
              value={draft.matchDescription}
              maxLength={MATCH_DESCRIPTION_MAX_LENGTH}
              onChange={(e) =>
                setDraft({ ...draft, matchDescription: e.target.value })
              }
              onKeyDown={onKeyDown}
              className="max-w-64"
              autoFocus
            />
          ) : (
            <span className="font-mono text-sm break-all">
              {alias.matchDescription}
            </span>
          )}
          {error && (
            <p className="mt-1 max-w-64 text-sm whitespace-normal text-destructive">
              {error}
            </p>
          )}
        </TableCell>
        <TableCell>
          {draft ? (
            <select
              aria-label="Tipo de coincidencia"
              value={draft.matchType}
              onChange={(e) =>
                setDraft({ ...draft, matchType: e.target.value as MatchType })
              }
              className={`${SELECT_CLASS} max-w-48`}
            >
              {MATCH_TYPES.map((type) => (
                <option key={type} value={type}>
                  {MATCH_TYPE_LABELS[type]}
                </option>
              ))}
            </select>
          ) : (
            MATCH_TYPE_LABELS[alias.matchType]
          )}
        </TableCell>
        <TableCell>
          {draft ? (
            <div className="flex flex-wrap items-center gap-1">
              <Input
                aria-label="Monto mínimo"
                type="number"
                step="any"
                inputMode="decimal"
                placeholder="mín"
                value={draft.amountMin}
                onChange={(e) =>
                  setDraft({ ...draft, amountMin: e.target.value })
                }
                onKeyDown={onKeyDown}
                className="w-24"
              />
              <Input
                aria-label="Monto máximo"
                type="number"
                step="any"
                inputMode="decimal"
                placeholder="máx"
                value={draft.amountMax}
                onChange={(e) =>
                  setDraft({ ...draft, amountMax: e.target.value })
                }
                onKeyDown={onKeyDown}
                className="w-24"
              />
              <select
                aria-label="Moneda de la condición"
                value={draft.currency}
                onChange={(e) =>
                  setDraft({ ...draft, currency: e.target.value })
                }
                className={`${SELECT_CLASS} w-24`}
              >
                <option value="">Sin</option>
                {CURRENCY_OPTIONS.map((code) => (
                  <option key={code} value={code}>
                    {code}
                  </option>
                ))}
              </select>
            </div>
          ) : (
            <span className="text-sm text-muted-foreground tabular-nums">
              {formatAmountCondition(alias)}
            </span>
          )}
        </TableCell>
        <TableCell>
          {draft ? (
            <Input
              aria-label="Alias"
              value={draft.alias}
              maxLength={ALIAS_MAX_LENGTH}
              onChange={(e) => setDraft({ ...draft, alias: e.target.value })}
              onKeyDown={onKeyDown}
              className="max-w-56"
            />
          ) : (
            alias.alias
          )}
        </TableCell>
        <TableCell className="text-right tabular-nums">
          {draft ? (
            <Input
              aria-label="Prioridad del alias"
              type="number"
              step={1}
              min={ALIAS_PRIORITY_MIN}
              max={ALIAS_PRIORITY_MAX}
              value={draft.priority}
              onChange={(e) => setDraft({ ...draft, priority: e.target.value })}
              onKeyDown={onKeyDown}
              className="ml-auto w-24 text-right"
            />
          ) : (
            alias.priority
          )}
        </TableCell>
        <TableCell>
          <div className="flex justify-end gap-1">
            <Button variant="ghost" size="sm" onClick={handleTest}>
              <FlaskConical className="h-4 w-4" />
              Probar
            </Button>
            {draft ? (
              <>
                <Button size="sm" onClick={handleSave} disabled={saving}>
                  <Check className="h-4 w-4" />
                  {saving ? "Guardando..." : "Guardar"}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={stopEditing}
                  disabled={saving}
                >
                  <X className="h-4 w-4" />
                  Cancelar
                </Button>
              </>
            ) : (
              <>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Editar alias ${alias.alias}`}
                  title="Editar"
                  onClick={startEditing}
                >
                  <Pencil />
                </Button>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label={`Eliminar alias ${alias.alias}`}
                  title="Eliminar"
                  onClick={handleDelete}
                  disabled={deleting}
                  className="text-muted-foreground hover:text-destructive"
                >
                  <Trash2 />
                </Button>
              </>
            )}
          </div>
        </TableCell>
      </TableRow>
      {preview !== null && (
        <TableRow className="hover:bg-transparent">
          <TableCell colSpan={6} className="whitespace-normal">
            <AliasPreview
              key={`${preview.matchType}:${preview.matchDescription}`}
              matchDescription={preview.matchDescription}
              matchType={preview.matchType}
              amountMin={preview.amountMin}
              amountMax={preview.amountMax}
              currency={preview.currency}
              version={version}
              onClose={() => setPreview(null)}
            />
          </TableCell>
        </TableRow>
      )}
    </>
  );
}
