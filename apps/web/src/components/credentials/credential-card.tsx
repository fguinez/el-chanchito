"use client";

import { useId, useState } from "react";
import { Eye, EyeOff, Info } from "lucide-react";
import { InstitutionLogo } from "@/components/institutions/institution-logo";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardFooter,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  FIELD_STATE_LABELS,
  bindingGroups,
  bindingProblem,
  checkCredentialValue,
  fieldState,
  type CredentialFieldSpec,
  type CredentialFieldStatus,
  type FieldState,
  type InstitutionCredentialSpec,
  type InstitutionCredentialStatus,
} from "@/lib/credentials/registry";
import { cn, formatDateTimeEs } from "@/lib/utils";

const STATE_BADGE: Record<FieldState, "default" | "secondary" | "destructive" | "outline"> = {
  stored: "default",
  fallback: "secondary",
  needs_reentry: "destructive",
  missing: "outline",
  unknown: "outline",
};

type Message = { kind: "ok" | "error"; text: string } | null;

/** The Spanish note for what the scraper service did with a change. */
function appliedNote(applied: boolean | null): string {
  if (applied === true) return "El servicio de scrapers ya las está usando.";
  if (applied === false) {
    return "El servicio de scrapers no pudo leerlas; se aplicarán cuando reinicie.";
  }
  return "El servicio de scrapers no respondió; se aplicarán cuando arranque.";
}

/** Registry notes mark commands with backticks; render them as code. */
function withCode(text: string) {
  return text
    .split("`")
    .map((part, i) => (i % 2 === 1 ? <code key={i}>{part}</code> : part));
}

async function errorText(response: Response, fallback: string): Promise<string> {
  try {
    const body = await response.json();
    return typeof body?.error === "string" ? body.error : fallback;
  } catch {
    return fallback;
  }
}

/** One institution: field states, an always-empty form, save and delete.
 *  Stored values never come back from the API, so nothing is prefilled. */
export function CredentialCard({
  spec,
  status,
  canSave,
  onChanged,
}: {
  spec: InstitutionCredentialSpec;
  status: InstitutionCredentialStatus | undefined;
  canSave: boolean;
  onChanged: () => void;
}) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [revealed, setRevealed] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<Message>(null);
  const formId = useId();

  const fieldStatus = (key: string): CredentialFieldStatus | undefined =>
    status?.fields.find((field) => field.key === key);
  const anyStored = spec.fields.some((field) => {
    const s = fieldStatus(field.key);
    return s?.stored || s?.needsReentry;
  });
  const typed = Object.entries(values).filter(([, value]) => value !== "");

  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (typed.length === 0 || busy) return;
    const fields: Record<string, string> = {};
    for (const [key, raw] of typed) {
      const field = spec.fields.find((f) => f.key === key);
      if (!field) continue;
      // Known-value lists are the server's call (it reads the opt-out env).
      const checked = checkCredentialValue(field, raw, true);
      if (!checked.ok) {
        setMessage({ kind: "error", text: checked.error });
        return;
      }
      fields[key] = checked.value;
    }
    const grouping = bindingProblem(spec, Object.keys(fields));
    if (grouping) {
      setMessage({ kind: "error", text: grouping });
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      const response = await fetch(`/api/credentials/${spec.slug}`, {
        method: "PUT",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ fields }),
        cache: "no-store",
      });
      if (!response.ok) {
        setMessage({ kind: "error", text: await errorText(response, "No se pudo guardar.") });
        return;
      }
      const body = (await response.json()) as {
        saved: string[];
        applied: boolean | null;
        triggered?: string[];
      };
      // Drop what was typed as soon as it is stored.
      setValues({});
      setRevealed({});
      setMessage({
        kind: "ok",
        text:
          `Guardado (${body.saved.length} ${body.saved.length === 1 ? "campo" : "campos"}). ` +
          appliedNote(body.applied) +
          (body.triggered && body.triggered.length > 0
            ? " El scraper quedó activo y ya empezó una primera lectura; revisa su resultado en Instituciones."
            : ""),
      });
      onChanged();
    } catch {
      setMessage({ kind: "error", text: "No se pudo contactar al servidor." });
    } finally {
      setBusy(false);
    }
  }

  async function remove(field?: CredentialFieldSpec) {
    const group = field ? bindingGroups(spec).find((keys) => keys.includes(field.key)) : undefined;
    const groupLabels = group
      ?.map((key) => spec.fields.find((f) => f.key === key)?.labelEs ?? key)
      .join(", ");
    const question = field
      ? group
        ? `¿Borrar ${groupLabels} de ${spec.labelEs}? Se guardan juntos, así que se borran juntos. El scraper volverá a usar el almacén local, si tiene uno.`
        : `¿Borrar ${field.labelEs} de ${spec.labelEs}? El scraper volverá a usar el almacén local, si tiene uno.`
      : `¿Borrar todas las credenciales guardadas de ${spec.labelEs}? El scraper volverá a usar el almacén local, si tiene uno.`;
    if (busy || !window.confirm(question)) return;
    setBusy(true);
    setMessage(null);
    try {
      const query = field ? `?field=${encodeURIComponent(field.key)}` : "";
      const response = await fetch(`/api/credentials/${spec.slug}${query}`, {
        method: "DELETE",
        cache: "no-store",
      });
      if (!response.ok) {
        setMessage({ kind: "error", text: await errorText(response, "No se pudo borrar.") });
        return;
      }
      const body = (await response.json()) as { applied: boolean | null };
      setMessage({ kind: "ok", text: `Borrado. ${appliedNote(body.applied)}` });
      onChanged();
    } catch {
      setMessage({ kind: "error", text: "No se pudo contactar al servidor." });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center gap-3">
          <InstitutionLogo slug={spec.logoSlug} />
          <div className="min-w-0 flex-1">
            <CardTitle className="flex flex-wrap items-center gap-2">
              {spec.labelEs}
              {status?.enabled === true && <Badge variant="secondary">Scraper activo</Badge>}
              {status?.enabled === false && <Badge variant="outline">Scraper inactivo</Badge>}
            </CardTitle>
            {spec.noteEs && (
              <CardDescription className="mt-1 flex gap-1.5">
                <Info className="mt-0.5 size-3.5 shrink-0" aria-hidden />
                <span>{withCode(spec.noteEs)}</span>
              </CardDescription>
            )}
          </div>
        </div>
      </CardHeader>

      {spec.fields.length > 0 && (
        <>
          <CardContent>
            <form id={formId} onSubmit={save} className="space-y-4" autoComplete="off">
              {spec.fields.map((field) => {
                const s = fieldStatus(field.key);
                const state = s ? fieldState(s) : "unknown";
                const inputId = `${formId}-${field.key}`;
                const secret = field.kind === "secret";
                const shown = Boolean(revealed[field.key]);
                return (
                  <div key={field.key} className="space-y-1.5">
                    <div className="flex flex-wrap items-center gap-2">
                      <label htmlFor={inputId} className="text-sm font-medium">
                        {field.labelEs}
                      </label>
                      <Badge variant={STATE_BADGE[state]}>
                        {state === "missing" && !field.required
                          ? "Opcional, vacío"
                          : FIELD_STATE_LABELS[state]}
                      </Badge>
                      {s?.updatedAt && (
                        <span className="text-xs text-muted-foreground">
                          Actualizado {formatDateTimeEs(s.updatedAt)}
                        </span>
                      )}
                      {(s?.stored || s?.needsReentry) && (
                        <Button
                          type="button"
                          variant="ghost"
                          size="xs"
                          className="ml-auto text-muted-foreground"
                          disabled={busy}
                          onClick={() => remove(field)}
                        >
                          Borrar
                        </Button>
                      )}
                    </div>
                    <div className="relative">
                      <Input
                        id={inputId}
                        name={`${spec.slug}-${field.key}`}
                        type={secret && !shown ? "password" : "text"}
                        inputMode={field.inputMode}
                        autoComplete={secret ? "new-password" : "off"}
                        autoCapitalize="off"
                        autoCorrect="off"
                        spellCheck={false}
                        maxLength={field.maxLength}
                        placeholder={
                          field.placeholder ??
                          (state === "stored" ? "Escribe uno nuevo para reemplazarlo" : "")
                        }
                        value={values[field.key] ?? ""}
                        onChange={(event) =>
                          setValues((prev) => ({ ...prev, [field.key]: event.target.value }))
                        }
                        disabled={!canSave || busy}
                        className={cn(secret && "pr-10")}
                      />
                      {secret && (
                        <button
                          type="button"
                          className="absolute inset-y-0 right-0 flex w-9 items-center justify-center text-muted-foreground hover:text-foreground"
                          aria-label={shown ? "Ocultar lo que escribes" : "Mostrar lo que escribes"}
                          aria-pressed={shown}
                          onClick={() =>
                            setRevealed((prev) => ({ ...prev, [field.key]: !prev[field.key] }))
                          }
                        >
                          {shown ? <EyeOff className="size-4" /> : <Eye className="size-4" />}
                        </button>
                      )}
                    </div>
                    {(field.helpEs || field.defaultValue) && (
                      <p className="text-xs text-muted-foreground">
                        {field.helpEs}
                        {field.defaultValue && (
                          <>
                            {field.helpEs ? " " : ""}
                            Habitual: <code>{field.defaultValue}</code>
                          </>
                        )}
                      </p>
                    )}
                  </div>
                );
              })}
            </form>
          </CardContent>
          <CardFooter className="flex flex-wrap items-center gap-2">
            <Button type="submit" form={formId} disabled={!canSave || busy || typed.length === 0}>
              {busy ? "Guardando…" : "Guardar"}
            </Button>
            {anyStored && (
              <Button type="button" variant="outline" disabled={busy} onClick={() => remove()}>
                Borrar todo
              </Button>
            )}
            {message && (
              <p
                role={message.kind === "error" ? "alert" : "status"}
                className={cn(
                  "basis-full text-sm",
                  message.kind === "error" ? "text-destructive" : "text-muted-foreground"
                )}
              >
                {message.text}
              </p>
            )}
          </CardFooter>
        </>
      )}
    </Card>
  );
}
