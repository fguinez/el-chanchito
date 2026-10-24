"use client";

import { useCallback, useEffect, useRef, useState } from "react";
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
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Button } from "@/components/ui/button";
import { RULE_KEYWORD_MAX_LENGTH, trimKeyword } from "@/lib/categories";
import type { CategorySuggestionGroup } from "@/lib/category-suggestions-db";
import { formatPlainDateEs } from "@/lib/utils";
import { CategoryLabel, plural, readApiError, type ApiCategory } from "./shared";

type Action = "apply" | "always" | "dismiss";

/**
 * "Sugerencias": uncategorized transactions grouped by description, each with
 * the category the categorized history suggests. Nothing is applied on its
 * own: "Aplicar a estas N" categorizes those rows once (marked manual),
 * "Aplicar siempre" creates the exact rule for the description (as the
 * transaction panel does) and "Ignorar" stops suggesting that category for
 * it. `version` reloads the list when the page reloads; `onChange` tells the
 * page that categories changed.
 */
export function SuggestionsCard({
  categories,
  version,
  onChange,
}: {
  categories: ApiCategory[];
  version: number;
  onChange: () => void;
}) {
  const [suggestions, setSuggestions] = useState<CategorySuggestionGroup[] | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<{ text: string; error: boolean } | null>(null);
  const loadSeq = useRef(0);

  const load = useCallback(async () => {
    const seq = ++loadSeq.current;
    try {
      const res = await fetch("/api/categories/suggestions");
      if (!res.ok) throw new Error("failed");
      const data: { suggestions: CategorySuggestionGroup[] } = await res.json();
      if (seq !== loadSeq.current) return;
      setSuggestions(data.suggestions);
      setLoadError(false);
    } catch {
      if (seq !== loadSeq.current) return;
      setLoadError(true);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load, version]);

  const categoriesById = new Map(categories.map((c) => [c.id, c]));

  async function act(group: CategorySuggestionGroup, action: Action) {
    setBusy(group.sampleTransactionId);
    setMessage(null);
    const payload = JSON.stringify({
      description: group.description,
      categoryId: group.categoryId,
    });
    const request =
      action === "always"
        ? fetch(
            `/api/transactions/${encodeURIComponent(group.sampleTransactionId)}/category-rule`,
            {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              // The sample row may have been recategorized since the list
              // loaded: leave it alone then, the rule is still saved.
              body: JSON.stringify({
                categoryId: group.categoryId,
                onlyIfUncategorized: true,
              }),
            }
          )
        : fetch(`/api/categories/suggestions/${action}`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: payload,
          });
    try {
      const res = await request;
      if (!res.ok) {
        setMessage({
          text: await readApiError(res, "No se pudo aplicar la sugerencia", {
            404: "La transacción ya no existe",
          }),
          error: true,
        });
        await load();
        return;
      }
      const data = await res.json().catch(() => null);
      if (action === "dismiss") {
        setMessage({ text: "Sugerencia ignorada.", error: false });
        await load();
        return;
      }
      const count =
        (data?.updatedCount ?? 0) +
        (action === "always" && data?.transactionUpdated ? 1 : 0);
      if (action === "apply" && count === 0) {
        setMessage({
          text: "Ya no hay transacciones sin categoría con esta descripción",
          error: false,
        });
        await load();
        return;
      }
      setMessage({
        text: `${plural(count, "transacción categorizada", "transacciones categorizadas")} en ${group.categoryName}${
          action === "always" ? "; regla exacta guardada" : ""
        }.`,
        error: false,
      });
      // The page reload bumps `version`, which reloads this list too.
      onChange();
    } catch {
      setMessage({ text: "No se pudo aplicar la sugerencia", error: true });
    } finally {
      setBusy(null);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Sugerencias</CardTitle>
        <CardDescription>
          Categorías sugeridas para las transacciones sin categoría, según cómo
          categorizaste las que tienen una descripción igual o parecida. Nada
          se aplica solo: &quot;Aplicar a estas N&quot; categoriza esas
          transacciones una vez, &quot;Aplicar siempre&quot; además crea una
          regla exacta para la descripción, e &quot;Ignorar&quot; deja de
          sugerir esa categoría para ella.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        {message && (
          <p
            role="status"
            className={message.error ? "text-sm text-destructive" : "text-sm text-muted-foreground"}
          >
            {message.text}
          </p>
        )}
        {loadError && !suggestions ? (
          <p className="text-muted-foreground">
            No se pudieron cargar las sugerencias.
          </p>
        ) : !suggestions ? (
          <p className="text-muted-foreground">Cargando...</p>
        ) : suggestions.length === 0 ? (
          <p className="text-muted-foreground">
            No hay sugerencias por ahora.
          </p>
        ) : (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Descripción</TableHead>
                <TableHead className="text-right">Transacciones</TableHead>
                <TableHead>Sugerencia</TableHead>
                <TableHead className="w-80"></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {suggestions.map((group) => {
                const category = categoriesById.get(group.categoryId);
                const tooLong =
                  trimKeyword(group.description).length > RULE_KEYWORD_MAX_LENGTH;
                const disabled = busy !== null;
                return (
                  <TableRow key={`${group.sampleTransactionId}:${group.categoryId}`}>
                    <TableCell className="max-w-72 whitespace-normal break-words">
                      {group.description}
                      <span className="block text-xs text-muted-foreground">
                        Última: {formatPlainDateEs(group.latestDate)}
                      </span>
                    </TableCell>
                    <TableCell className="text-right tabular-nums">
                      {group.count.toLocaleString("es-CL")}
                    </TableCell>
                    <TableCell className="whitespace-normal">
                      {category ? (
                        <CategoryLabel category={category} />
                      ) : (
                        group.categoryName
                      )}
                      <span className="block text-xs text-muted-foreground">
                        {group.reason}
                      </span>
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-wrap justify-end gap-1.5">
                        <Button
                          size="xs"
                          disabled={disabled}
                          onClick={() => act(group, "apply")}
                        >
                          {group.count === 1
                            ? "Aplicar a esta"
                            : `Aplicar a estas ${group.count.toLocaleString("es-CL")}`}
                        </Button>
                        <Button
                          size="xs"
                          variant="outline"
                          disabled={disabled || tooLong}
                          onClick={() => act(group, "always")}
                          title={
                            tooLong
                              ? `La descripción supera los ${RULE_KEYWORD_MAX_LENGTH} caracteres de una regla`
                              : "Crea una regla exacta para esta descripción: las transacciones con ella, actuales y futuras, toman esta categoría"
                          }
                        >
                          Aplicar siempre
                        </Button>
                        <Button
                          size="xs"
                          variant="ghost"
                          disabled={disabled}
                          onClick={() => act(group, "dismiss")}
                        >
                          Ignorar
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        )}
      </CardContent>
    </Card>
  );
}
