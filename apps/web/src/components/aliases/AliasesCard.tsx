"use client";

import { useCallback, useState } from "react";
import { FlaskConical, Plus } from "lucide-react";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Table,
  TableBody,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { useSortableData } from "@/lib/use-sortable-data";
import {
  ALIAS_MAX_LENGTH,
  ALIAS_PRIORITY_MAX,
  ALIAS_PRIORITY_MIN,
  MATCH_DESCRIPTION_MAX_LENGTH,
  MATCH_TYPES,
  type MatchType,
} from "@/lib/aliases";
import { AliasPreview } from "./AliasPreview";
import { AliasRow } from "./AliasRow";
import {
  MATCH_TYPE_LABELS,
  PRIORITY_ERROR,
  SELECT_CLASS,
  parsePriority,
  plural,
  readApiError,
  type ApiAlias,
} from "./shared";

type AliasSortKey = "descripcion" | "tipo" | "alias" | "prioridad";

/** The order aliases apply in: priority desc, then oldest first, then id. */
function byApplicationOrder(a: ApiAlias, b: ApiAlias): number {
  if (a.priority !== b.priority) return b.priority - a.priority;
  if (a.createdAt !== b.createdAt) return a.createdAt < b.createdAt ? -1 : 1;
  return a.id < b.id ? -1 : 1;
}

type PreviewTarget = { matchDescription: string; matchType: MatchType };

function AddAliasForm({
  version,
  initialMatchDescription,
  initialMatchType,
  onCreated,
}: {
  version: number;
  initialMatchDescription: string;
  initialMatchType: MatchType;
  onCreated: () => void;
}) {
  const [matchDescription, setMatchDescription] = useState(
    initialMatchDescription
  );
  const [matchType, setMatchType] = useState<MatchType>(initialMatchType);
  const [alias, setAlias] = useState("");
  const [priority, setPriority] = useState("0");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [preview, setPreview] = useState<PreviewTarget | null>(null);

  function handleTest() {
    if (!matchDescription.trim()) {
      setError("Escribe una descripción para probarla");
      return;
    }
    setError(null);
    setPreview({ matchDescription: matchDescription.trim(), matchType });
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const parsedPriority = parsePriority(priority);
    if (!matchDescription.trim()) {
      setError("La descripción es obligatoria");
      return;
    }
    if (!alias.trim()) {
      setError("El alias es obligatorio");
      return;
    }
    if (parsedPriority === null) {
      setError(PRIORITY_ERROR);
      return;
    }
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const res = await fetch("/api/aliases", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          matchDescription,
          matchType,
          alias,
          priority: parsedPriority,
        }),
      });
      if (!res.ok) {
        setError(await readApiError(res, "No se pudo crear el alias"));
        return;
      }
      setMatchDescription("");
      setAlias("");
      setPriority("0");
      setMatchType("exact");
      setPreview(null);
      setNotice("Alias agregado. Ya se aplica a las transacciones que coinciden.");
      onCreated();
    } catch {
      setError("No se pudo crear el alias");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-3">
      <form onSubmit={handleSubmit}>
        <div className="flex flex-wrap items-end gap-3">
          <div className="min-w-48 flex-1">
            <label
              htmlFor="new-alias-match"
              className="mb-1 block text-sm text-muted-foreground"
            >
              Descripción a coincidir
            </label>
            <Input
              id="new-alias-match"
              value={matchDescription}
              maxLength={MATCH_DESCRIPTION_MAX_LENGTH}
              onChange={(e) => {
                setMatchDescription(e.target.value);
                setError(null);
                setNotice(null);
              }}
              placeholder="Ej: COMPRA WEB 1234"
            />
          </div>
          <div className="w-48">
            <label
              htmlFor="new-alias-type"
              className="mb-1 block text-sm text-muted-foreground"
            >
              Tipo
            </label>
            <select
              id="new-alias-type"
              value={matchType}
              onChange={(e) => setMatchType(e.target.value as MatchType)}
              className={SELECT_CLASS}
            >
              {MATCH_TYPES.map((type) => (
                <option key={type} value={type}>
                  {MATCH_TYPE_LABELS[type]}
                </option>
              ))}
            </select>
          </div>
          <div className="min-w-40 flex-1">
            <label
              htmlFor="new-alias-alias"
              className="mb-1 block text-sm text-muted-foreground"
            >
              Alias
            </label>
            <Input
              id="new-alias-alias"
              value={alias}
              maxLength={ALIAS_MAX_LENGTH}
              onChange={(e) => {
                setAlias(e.target.value);
                setError(null);
                setNotice(null);
              }}
              placeholder="Ej: Supermercado del barrio"
            />
          </div>
          <div className="w-28">
            <label
              htmlFor="new-alias-priority"
              className="mb-1 block text-sm text-muted-foreground"
            >
              Prioridad
            </label>
            <Input
              id="new-alias-priority"
              type="number"
              step={1}
              min={ALIAS_PRIORITY_MIN}
              max={ALIAS_PRIORITY_MAX}
              value={priority}
              onChange={(e) => setPriority(e.target.value)}
            />
          </div>
          <Button type="button" variant="outline" onClick={handleTest}>
            <FlaskConical className="h-4 w-4" />
            Probar
          </Button>
          <Button type="submit" disabled={saving}>
            <Plus className="h-4 w-4" />
            {saving ? "Guardando..." : "Agregar"}
          </Button>
        </div>
        {error && <p className="mt-1 text-sm text-destructive">{error}</p>}
        {notice && <p className="mt-1 text-sm text-muted-foreground">{notice}</p>}
      </form>
      {preview !== null && (
        <AliasPreview
          key={`${preview.matchType}:${preview.matchDescription}`}
          matchDescription={preview.matchDescription}
          matchType={preview.matchType}
          version={version}
          onClose={() => setPreview(null)}
        />
      )}
    </div>
  );
}

export function AliasesCard({
  aliases,
  version,
  initialMatchDescription,
  initialMatchType,
  onChange,
}: {
  aliases: ApiAlias[];
  version: number;
  initialMatchDescription: string;
  initialMatchType: MatchType;
  onChange: () => void;
}) {
  const sortedByApplication = [...aliases].sort(byApplicationOrder);

  const getValue = useCallback(
    (alias: ApiAlias, key: AliasSortKey): string | number | null => {
      switch (key) {
        case "descripcion":
          return alias.matchDescription;
        case "tipo":
          return alias.matchType;
        case "alias":
          return alias.alias;
        case "prioridad":
          return alias.priority;
      }
    },
    []
  );
  const { sorted, sort, toggleSort } = useSortableData(
    sortedByApplication,
    getValue
  );
  // Bridge the generic header's string key to our typed key union.
  const handleSort = (key: string) => toggleSort(key as AliasSortKey);
  const sortProps = (key: AliasSortKey) => ({
    columnKey: key,
    active: sort?.key === key,
    direction: sort?.key === key ? sort.direction : undefined,
    onSort: handleSort,
  });

  return (
    <Card>
      <CardHeader>
        <CardTitle>Alias</CardTitle>
        <CardDescription className="space-y-2">
          <p>
            Un alias coincide cuando la descripción original de la transacción es
            igual (coincidencia exacta) o la contiene (contiene), sin importar
            mayúsculas o minúsculas. Si coinciden varios, gana el de mayor
            prioridad y, con igual prioridad, el más antiguo.
          </p>
          <p>
            Los alias se aplican al momento de mostrar: editar o eliminar uno
            actualiza las listas de inmediato, sin un paso de &quot;aplicar&quot;.
            La descripción original nunca se pierde.
          </p>
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <AddAliasForm
          version={version}
          initialMatchDescription={initialMatchDescription}
          initialMatchType={initialMatchType}
          onCreated={onChange}
        />
        {aliases.length === 0 ? (
          <p className="text-muted-foreground">No hay alias. Agrega uno arriba.</p>
        ) : (
          <div className="space-y-2">
            <p className="text-sm text-muted-foreground">
              {plural(aliases.length, "alias", "alias")}
              {!sort && ", en el orden en que se aplican"}.
            </p>
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableTableHead
                    label="Descripción"
                    {...sortProps("descripcion")}
                  />
                  <SortableTableHead label="Tipo" {...sortProps("tipo")} />
                  <SortableTableHead label="Alias" {...sortProps("alias")} />
                  <SortableTableHead
                    label="Prioridad"
                    align="right"
                    className="w-32"
                    {...sortProps("prioridad")}
                  />
                  <TableHead className="w-72"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sorted.map((alias) => (
                  <AliasRow
                    key={alias.id}
                    alias={alias}
                    version={version}
                    onChange={onChange}
                  />
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
