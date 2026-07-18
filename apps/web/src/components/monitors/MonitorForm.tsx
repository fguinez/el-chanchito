"use client";

// Shared builder for /monitors/new and /monitors/[id]/edit: equation editor
// in display syntax (institution:producto:campo) with a reference picker,
// threshold presets that compile to expressions, and a debounced live
// preview against POST /api/monitors/preview.

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { AlertTriangle, Trash2 } from "lucide-react";
import { METRIC_FIELDS } from "@chanchito/product-model";
import type { ProductKind } from "@chanchito/product-model";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { cn, formatAmount } from "@/lib/utils";
import {
  SEVERITY_LABELS,
  StatusBadge,
  referencesProduct,
  type ApiThreshold,
  type Comparator,
  type MonitorEvaluation,
  type ThresholdSeverity,
} from "./shared";

const PREVIEW_DEBOUNCE_MS = 500;

const COMPARATORS: Comparator[] = ["<", "<=", ">", ">=", "=", "!="];

// Native <select> styled to match the Input primitive.
const SELECT_CLASS =
  "h-9 w-full rounded-md border border-input bg-transparent px-3 py-1 text-sm shadow-xs transition-[color,box-shadow] outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-input/30";

type ThresholdMode = "fixed" | "percent" | "ramp" | "advanced";

/** One threshold being authored. Every preset keeps its own inputs so
 *  switching modes back and forth never loses what was typed. */
type ThresholdDraft = {
  mode: ThresholdMode;
  amount: string; // fixed
  ref: string; // percent: product reference in display syntax
  percent: string; // percent
  base: string; // ramp
  step: string; // ramp
  advanced: string; // advanced (free-form expression)
};

const EMPTY_THRESHOLD: ThresholdDraft = {
  mode: "fixed",
  amount: "",
  ref: "",
  percent: "",
  base: "",
  step: "",
  advanced: "",
};

/** Compile a draft to expression source; null while its inputs are
 *  incomplete (the preview waits, the submit rejects with a message). */
function compileThreshold(draft: ThresholdDraft): string | null {
  switch (draft.mode) {
    case "fixed":
      return draft.amount.trim() === "" ? null : draft.amount.trim();
    case "percent": {
      if (draft.ref.trim() === "" || draft.percent.trim() === "") return null;
      const pct = Number(draft.percent);
      if (!Number.isFinite(pct)) return null;
      // toPrecision trims float artifacts (70.1 -> 0.701, not 0.7010000000000001).
      return `${draft.ref.trim()} * ${Number((pct / 100).toPrecision(12))}`;
    }
    case "ramp": {
      if (draft.base.trim() === "" || draft.step.trim() === "") return null;
      return `${draft.base.trim()} - ${draft.step.trim()} * (DAY_OF_MONTH() - 1)`;
    }
    case "advanced":
      // Untrimmed so API error positions match the textarea content.
      return draft.advanced.trim() === "" ? null : draft.advanced;
  }
}

type FormError = { error: string; field?: string; position?: number };

type ErrorTarget =
  | "name"
  | "currency"
  | "expression"
  | "alert"
  | "warning"
  | "general";

/** Map the API's `field` (thresholds are sent alert-first) to a form spot. */
function errorTarget(field: string | undefined): ErrorTarget {
  if (field === "name") return "name";
  if (field === "currency") return "currency";
  if (field === "expression") return "expression";
  if (field?.startsWith("thresholds[0]")) return "alert";
  if (field?.startsWith("thresholds[1]")) return "warning";
  return "general";
}

/** The offending line of `source` with a caret under `position` (0-based
 *  offset into the whole source). */
function caretBlock(source: string, position: number): string {
  const clamped = Math.max(0, Math.min(position, source.length));
  const lineStart = source.lastIndexOf("\n", clamped - 1) + 1;
  let lineEnd = source.indexOf("\n", clamped);
  if (lineEnd === -1) lineEnd = source.length;
  const line = source.slice(lineStart, lineEnd);
  return `${line}\n${" ".repeat(clamped - lineStart)}^`;
}

function ExpressionErrorNote({
  error,
  source,
}: {
  error: FormError;
  /** When provided (the user typed this exact source), render a caret. */
  source?: string;
}) {
  const showCaret = error.position != null && source != null;
  return (
    <div className="mt-1 space-y-1 text-sm text-destructive">
      <p>
        {error.error}
        {error.position != null && ` (en la posición ${error.position})`}
      </p>
      {showCaret && (
        <pre className="overflow-x-auto rounded-md bg-muted p-2 font-mono text-xs whitespace-pre text-foreground">
          {caretBlock(source, error.position!)}
        </pre>
      )}
    </div>
  );
}

// Picker data, built from GET /api/institutions (active products only).
interface PickerProduct {
  slug: string;
  name: string;
  kind: ProductKind;
  currency: string;
}
interface PickerInstitution {
  slug: string;
  name: string;
  products: PickerProduct[];
}
interface InstitutionsResponse {
  institutions: {
    slug: string;
    name: string;
    products: {
      slug: string;
      name: string;
      kind: ProductKind;
      currency: string;
      isActive: boolean;
    }[];
  }[];
}

/** Which input a picked reference lands in. */
type InsertTarget =
  | "expression"
  | "alert"
  | "warning"
  | "alert-ref"
  | "warning-ref";

type PreviewResult = {
  monitor: {
    displayExpression: string;
    thresholds: ApiThreshold[];
  };
  evaluation: MonitorEvaluation;
};

// Detail response fields the edit mode needs to seed the form.
interface MonitorDetail {
  name: string;
  description: string | null;
  currency: string;
  displayExpression: string;
  thresholds: ApiThreshold[];
  display: { chart: "line" | "stat"; show_margin: boolean };
  isActive: boolean;
}

function ThresholdEditor({
  severity,
  draft,
  onChange,
  onRemove,
  error,
  advancedRef,
  onAdvancedFocus,
  onRefFocus,
}: {
  severity: ThresholdSeverity;
  draft: ThresholdDraft;
  onChange: (draft: ThresholdDraft) => void;
  onRemove?: () => void;
  error: FormError | null;
  advancedRef: React.RefObject<HTMLTextAreaElement | null>;
  onAdvancedFocus: () => void;
  onRefFocus: () => void;
}) {
  const set = (patch: Partial<ThresholdDraft>) =>
    onChange({ ...draft, ...patch });

  return (
    <div className="space-y-3 rounded-md border p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-sm font-medium">
          Umbral de {SEVERITY_LABELS[severity]}
        </span>
        <div className="flex items-center gap-2">
          <select
            className={cn(SELECT_CLASS, "h-8 w-auto")}
            value={draft.mode}
            onChange={(e) => set({ mode: e.target.value as ThresholdMode })}
          >
            <option value="fixed">Monto fijo</option>
            <option value="percent">Porcentaje de un producto</option>
            <option value="ramp">Rampa mensual</option>
            <option value="advanced">Avanzado</option>
          </select>
          {onRemove && (
            <Button
              variant="ghost"
              size="icon-sm"
              onClick={onRemove}
              title="Quitar umbral"
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          )}
        </div>
      </div>

      {draft.mode === "fixed" && (
        <div>
          <label className="mb-1 block text-sm text-muted-foreground">
            Monto
          </label>
          <Input
            type="number"
            value={draft.amount}
            onChange={(e) => set({ amount: e.target.value })}
            placeholder="1000000"
          />
        </div>
      )}

      {draft.mode === "percent" && (
        <div className="grid gap-3 md:grid-cols-2">
          <div>
            <label className="mb-1 block text-sm text-muted-foreground">
              Referencia
            </label>
            <Input
              className="font-mono"
              value={draft.ref}
              onFocus={onRefFocus}
              onChange={(e) => set({ ref: e.target.value })}
              placeholder="institucion:producto:campo"
            />
          </div>
          <div>
            <label className="mb-1 block text-sm text-muted-foreground">
              Porcentaje (%)
            </label>
            <Input
              type="number"
              value={draft.percent}
              onChange={(e) => set({ percent: e.target.value })}
              placeholder="70"
            />
          </div>
        </div>
      )}

      {draft.mode === "ramp" && (
        <div className="grid gap-3 md:grid-cols-2">
          <div>
            <label className="mb-1 block text-sm text-muted-foreground">
              Monto base
            </label>
            <Input
              type="number"
              value={draft.base}
              onChange={(e) => set({ base: e.target.value })}
              placeholder="1000000"
            />
          </div>
          <div>
            <label className="mb-1 block text-sm text-muted-foreground">
              Descuento por día
            </label>
            <Input
              type="number"
              value={draft.step}
              onChange={(e) => set({ step: e.target.value })}
              placeholder="30000"
            />
          </div>
          <p className="text-xs text-muted-foreground md:col-span-2">
            Parte en el monto base el día 1 y baja cada día; se reinicia con
            cada mes: base - descuento * (DAY_OF_MONTH() - 1).
          </p>
        </div>
      )}

      {draft.mode === "advanced" && (
        <div>
          <label className="mb-1 block text-sm text-muted-foreground">
            Expresión
          </label>
          <Textarea
            ref={advancedRef}
            rows={2}
            className="font-mono"
            value={draft.advanced}
            onFocus={onAdvancedFocus}
            onChange={(e) => set({ advanced: e.target.value })}
            placeholder="1000000 - 30000 * (DAY_OF_MONTH() - 1)"
          />
        </div>
      )}

      {error && (
        <ExpressionErrorNote
          error={error}
          source={draft.mode === "advanced" ? draft.advanced : undefined}
        />
      )}
    </div>
  );
}

export function MonitorForm({ monitorId }: { monitorId?: string }) {
  const router = useRouter();
  const isEdit = monitorId != null;

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [currency, setCurrency] = useState("CLP");
  const [expression, setExpression] = useState("");
  const [comparator, setComparator] = useState<Comparator>("<");
  const [alertDraft, setAlertDraft] = useState<ThresholdDraft>({
    ...EMPTY_THRESHOLD,
  });
  const [warningDraft, setWarningDraft] = useState<ThresholdDraft | null>(null);
  const [chart, setChart] = useState<"line" | "stat">("line");
  const [showMargin, setShowMargin] = useState(true);
  const [isActive, setIsActive] = useState(true);

  const [loading, setLoading] = useState(isEdit);
  const [loadError, setLoadError] = useState(false);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<FormError | null>(null);
  const [preview, setPreview] = useState<PreviewResult | null>(null);

  const [institutionsData, setInstitutionsData] = useState<
    PickerInstitution[] | null
  >(null);
  const [pickInst, setPickInst] = useState("");
  const [pickProd, setPickProd] = useState("");
  const [pickField, setPickField] = useState("current_balance");
  const [insertTarget, setInsertTarget] = useState<InsertTarget>("expression");

  const expressionRef = useRef<HTMLTextAreaElement | null>(null);
  const alertAdvancedRef = useRef<HTMLTextAreaElement | null>(null);
  const warningAdvancedRef = useRef<HTMLTextAreaElement | null>(null);

  // Edit mode: seed the form from the stored monitor. Thresholds load into
  // "Avanzado" showing their display-form expressions.
  useEffect(() => {
    if (monitorId == null) return;
    let cancelled = false;
    fetch(`/api/monitors/${monitorId}`)
      .then((res) => {
        if (!res.ok) throw new Error("failed");
        return res.json();
      })
      .then((data: MonitorDetail) => {
        if (cancelled) return;
        setName(data.name);
        setDescription(data.description ?? "");
        setCurrency(data.currency);
        setExpression(data.displayExpression);
        const alertT = data.thresholds.find((t) => t.severity === "alert");
        const warningT = data.thresholds.find((t) => t.severity === "warning");
        if (alertT) {
          setComparator(alertT.comparator);
          setAlertDraft({
            ...EMPTY_THRESHOLD,
            mode: "advanced",
            advanced: alertT.displayExpression,
          });
        }
        setWarningDraft(
          warningT
            ? {
                ...EMPTY_THRESHOLD,
                mode: "advanced",
                advanced: warningT.displayExpression,
              }
            : null
        );
        setChart(data.display.chart);
        setShowMargin(data.display.show_margin);
        setIsActive(data.isActive);
        setLoading(false);
      })
      .catch(() => {
        if (!cancelled) setLoadError(true);
      });
    return () => {
      cancelled = true;
    };
  }, [monitorId]);

  // Reference picker + currency options come from the product catalog.
  useEffect(() => {
    fetch("/api/institutions")
      .then((res) => {
        if (!res.ok) throw new Error("failed");
        return res.json();
      })
      .then((data: InstitutionsResponse) => {
        const mapped = data.institutions
          .map((inst) => ({
            slug: inst.slug,
            name: inst.name,
            products: inst.products
              .filter((p) => p.isActive)
              .map((p) => ({
                slug: p.slug,
                name: p.name,
                kind: p.kind,
                currency: p.currency,
              })),
          }))
          .filter((inst) => inst.products.length > 0);
        setInstitutionsData(mapped);
      })
      .catch(() => setInstitutionsData([]));
  }, []);

  // Cascade selections resolve to the first option when unset or stale.
  const selectedInst =
    institutionsData?.find((i) => i.slug === pickInst) ??
    institutionsData?.[0] ??
    null;
  const selectedProd =
    selectedInst?.products.find((p) => p.slug === pickProd) ??
    selectedInst?.products[0] ??
    null;
  const fieldOptions = selectedProd
    ? ["current_balance", ...Object.keys(METRIC_FIELDS[selectedProd.kind])]
    : [];
  const effectiveField = fieldOptions.includes(pickField)
    ? pickField
    : "current_balance";
  const pickedReference =
    selectedInst && selectedProd
      ? `${selectedInst.slug}:${selectedProd.slug}:${effectiveField}`
      : null;

  const currencyOptions = useMemo(() => {
    const set = new Set<string>();
    for (const inst of institutionsData ?? []) {
      for (const p of inst.products) set.add(p.currency);
    }
    set.add(currency);
    set.delete("CLP");
    return ["CLP", ...[...set].sort()];
  }, [institutionsData, currency]);

  function insertIntoTextarea(
    el: HTMLTextAreaElement | null,
    text: string,
    setValue: (value: string) => void
  ) {
    if (!el) return;
    const start = el.selectionStart ?? el.value.length;
    const end = el.selectionEnd ?? start;
    setValue(el.value.slice(0, start) + text + el.value.slice(end));
    requestAnimationFrame(() => {
      el.focus();
      const pos = start + text.length;
      el.setSelectionRange(pos, pos);
    });
  }

  /** Insert the picked reference into the last-focused expression input. */
  function insertReference() {
    if (!pickedReference) return;
    if (insertTarget === "alert-ref") {
      setAlertDraft((p) => ({ ...p, ref: pickedReference }));
      return;
    }
    if (insertTarget === "warning-ref") {
      setWarningDraft((p) => (p ? { ...p, ref: pickedReference } : p));
      return;
    }
    if (insertTarget === "alert" && alertDraft.mode === "advanced") {
      insertIntoTextarea(alertAdvancedRef.current, pickedReference, (v) =>
        setAlertDraft((p) => ({ ...p, advanced: v }))
      );
      return;
    }
    if (insertTarget === "warning" && warningDraft?.mode === "advanced") {
      insertIntoTextarea(warningAdvancedRef.current, pickedReference, (v) =>
        setWarningDraft((p) => (p ? { ...p, advanced: v } : p))
      );
      return;
    }
    insertIntoTextarea(expressionRef.current, pickedReference, setExpression);
  }

  // Debounced live preview: any form change reposts the compiled monitor to
  // /api/monitors/preview; 400s land on the offending field.
  useEffect(() => {
    if (loading) return;
    const alertExpr = compileThreshold(alertDraft);
    const warningExpr = warningDraft ? compileThreshold(warningDraft) : null;
    if (
      expression.trim() === "" ||
      alertExpr == null ||
      (warningDraft != null && warningExpr == null)
    ) {
      setPreview(null);
      setFormError(null);
      return;
    }
    const body = {
      name: name.trim() === "" ? "Vista previa" : name.trim(),
      description: description.trim() === "" ? null : description.trim(),
      currency,
      expression,
      thresholds: [
        { severity: "alert", comparator, expression: alertExpr },
        ...(warningExpr != null
          ? [{ severity: "warning", comparator, expression: warningExpr }]
          : []),
      ],
      display: { chart, show_margin: showMargin },
    };
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const res = await fetch("/api/monitors/preview", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: controller.signal,
        });
        const data = await res.json();
        if (res.ok) {
          setPreview({ monitor: data.monitor, evaluation: data.evaluation });
          setFormError(null);
        } else {
          setPreview(null);
          setFormError({
            error: data.error ?? "Expresión inválida",
            field: data.field,
            position: data.position,
          });
        }
      } catch {
        // Aborted (a newer edit superseded this one) or network error.
      }
    }, PREVIEW_DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [
    loading,
    name,
    description,
    currency,
    expression,
    comparator,
    alertDraft,
    warningDraft,
    chart,
    showMargin,
  ]);

  async function handleSubmit() {
    if (name.trim() === "") {
      setFormError({ error: "El nombre es obligatorio", field: "name" });
      return;
    }
    if (expression.trim() === "") {
      setFormError({
        error: "La expresión es obligatoria",
        field: "expression",
      });
      return;
    }
    const alertExpr = compileThreshold(alertDraft);
    if (alertExpr == null) {
      setFormError({
        error: "El umbral de alerta está incompleto",
        field: "thresholds[0].expression",
      });
      return;
    }
    const warningExpr = warningDraft ? compileThreshold(warningDraft) : null;
    if (warningDraft != null && warningExpr == null) {
      setFormError({
        error: "El umbral de advertencia está incompleto",
        field: "thresholds[1].expression",
      });
      return;
    }

    setSaving(true);
    setFormError(null);
    try {
      const body = {
        name: name.trim(),
        description: description.trim() === "" ? null : description.trim(),
        currency,
        expression,
        thresholds: [
          { severity: "alert", comparator, expression: alertExpr },
          ...(warningExpr != null
            ? [{ severity: "warning", comparator, expression: warningExpr }]
            : []),
        ],
        display: { chart, show_margin: showMargin },
        ...(isEdit && { isActive }),
      };
      const res = await fetch(
        isEdit ? `/api/monitors/${monitorId}` : "/api/monitors",
        {
          method: isEdit ? "PUT" : "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }
      );
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        setFormError({
          error: data.error ?? "No se pudo guardar el monitor",
          field: data.field,
          position: data.position,
        });
        return;
      }
      router.push(`/monitors/${isEdit ? monitorId : data.id}`);
    } catch {
      setFormError({ error: "No se pudo guardar el monitor" });
    } finally {
      setSaving(false);
    }
  }

  const target = formError ? errorTarget(formError.field) : null;
  const nameError = target === "name" ? formError : null;
  const currencyError = target === "currency" ? formError : null;
  const expressionError = target === "expression" ? formError : null;
  const alertError = target === "alert" ? formError : null;
  const warningError = target === "warning" ? formError : null;
  const generalError = target === "general" ? formError : null;

  const exactComparator = comparator === "=" || comparator === "!=";
  const exactOnMetrics =
    exactComparator &&
    [
      expression,
      compileThreshold(alertDraft) ?? "",
      warningDraft ? (compileThreshold(warningDraft) ?? "") : "",
    ].some(referencesProduct);

  if (loadError) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Editar monitor</h2>
        <p className="text-muted-foreground">No se pudo cargar el monitor.</p>
      </div>
    );
  }
  if (loading) {
    return (
      <div className="space-y-6">
        <h2 className="text-2xl font-bold">Editar monitor</h2>
        <p className="text-muted-foreground">Cargando...</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 className="text-2xl font-bold">
            {isEdit ? "Editar monitor" : "Nuevo monitor"}
          </h2>
          <p className="text-sm text-muted-foreground">
            Define una ecuación sobre tus productos y los umbrales que la
            vigilan.
          </p>
        </div>
        <Button asChild variant="outline" size="sm">
          <Link href={isEdit ? `/monitors/${monitorId}` : "/monitors"}>
            Volver
          </Link>
        </Button>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Definición</CardTitle>
        </CardHeader>
        <CardContent className="grid gap-3 md:grid-cols-2">
          <div>
            <label className="mb-1 block text-sm text-muted-foreground">
              Nombre
            </label>
            <Input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Deuda tarjetas vs cuenta"
            />
            {nameError && (
              <p className="mt-1 text-sm text-destructive">{nameError.error}</p>
            )}
          </div>
          <div>
            <label className="mb-1 block text-sm text-muted-foreground">
              Moneda
            </label>
            <select
              className={SELECT_CLASS}
              value={currency}
              onChange={(e) => setCurrency(e.target.value)}
            >
              {currencyOptions.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
            {currencyError && (
              <p className="mt-1 text-sm text-destructive">
                {currencyError.error}
              </p>
            )}
          </div>
          <div className="md:col-span-2">
            <label className="mb-1 block text-sm text-muted-foreground">
              Descripción (opcional)
            </label>
            <Input
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="Qué vigila este monitor"
            />
          </div>
          <div>
            <label className="mb-1 block text-sm text-muted-foreground">
              Gráfico
            </label>
            <select
              className={SELECT_CLASS}
              value={chart}
              onChange={(e) => setChart(e.target.value as "line" | "stat")}
            >
              <option value="line">Línea histórica</option>
              <option value="stat">Valor actual</option>
            </select>
          </div>
          <div className="flex flex-wrap items-end gap-4 pb-1">
            <label className="flex items-center gap-2 text-sm">
              <input
                type="checkbox"
                className="h-4 w-4 accent-primary"
                checked={showMargin}
                onChange={(e) => setShowMargin(e.target.checked)}
              />
              Mostrar margen
            </label>
            {isEdit && (
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  className="h-4 w-4 accent-primary"
                  checked={isActive}
                  onChange={(e) => setIsActive(e.target.checked)}
                />
                Monitor activo
              </label>
            )}
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Ecuación</CardTitle>
          <CardDescription>
            Referencias en formato institucion:producto:campo, números y
            operadores + - * /, además de DAY_OF_MONTH() y DAYS_IN_MONTH().
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div>
            <label className="mb-1 block text-sm text-muted-foreground">
              Expresión
            </label>
            <Textarea
              ref={expressionRef}
              rows={3}
              className="font-mono"
              value={expression}
              onFocus={() => setInsertTarget("expression")}
              onChange={(e) => setExpression(e.target.value)}
              placeholder="banchile:cuenta_corriente:balance - banchile:tarjeta_clp:owed"
            />
            {expressionError && (
              <ExpressionErrorNote error={expressionError} source={expression} />
            )}
          </div>

          <div>
            <label className="mb-1 block text-sm text-muted-foreground">
              Comparador
            </label>
            <div className="flex items-center gap-3">
              <select
                className={cn(SELECT_CLASS, "w-24")}
                value={comparator}
                onChange={(e) => setComparator(e.target.value as Comparator)}
              >
                {COMPARATORS.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
              <p className="text-xs text-muted-foreground">
                El estado pasa a alerta cuando la expresión cumple esta
                comparación contra el umbral.
              </p>
            </div>
          </div>

          {/* Reference picker: institution -> product -> metric field. */}
          <div className="space-y-2 rounded-md border bg-muted/30 p-3">
            <p className="text-sm font-medium">Insertar referencia</p>
            {institutionsData == null ? (
              <p className="text-sm text-muted-foreground">Cargando...</p>
            ) : institutionsData.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                No hay productos disponibles para referenciar.
              </p>
            ) : (
              <>
                <div className="grid gap-2 md:grid-cols-[1fr_1fr_1fr_auto]">
                  <select
                    className={SELECT_CLASS}
                    value={selectedInst?.slug ?? ""}
                    onChange={(e) => setPickInst(e.target.value)}
                  >
                    {institutionsData.map((inst) => (
                      <option key={inst.slug} value={inst.slug}>
                        {inst.name}
                      </option>
                    ))}
                  </select>
                  <select
                    className={SELECT_CLASS}
                    value={selectedProd?.slug ?? ""}
                    onChange={(e) => setPickProd(e.target.value)}
                  >
                    {(selectedInst?.products ?? []).map((p) => (
                      <option key={p.slug} value={p.slug}>
                        {p.name} ({p.currency})
                      </option>
                    ))}
                  </select>
                  <select
                    className={SELECT_CLASS}
                    value={effectiveField}
                    onChange={(e) => setPickField(e.target.value)}
                  >
                    {fieldOptions.map((f) => (
                      <option key={f} value={f}>
                        {f}
                      </option>
                    ))}
                  </select>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={insertReference}
                    disabled={!pickedReference}
                  >
                    Insertar
                  </Button>
                </div>
                {pickedReference && (
                  <p className="font-mono text-xs text-muted-foreground">
                    {pickedReference}
                  </p>
                )}
                <p className="text-xs text-muted-foreground">
                  Se inserta en el último campo de expresión que tocaste.
                </p>
              </>
            )}
          </div>

          <div className="space-y-3">
            <ThresholdEditor
              severity="alert"
              draft={alertDraft}
              onChange={setAlertDraft}
              error={alertError}
              advancedRef={alertAdvancedRef}
              onAdvancedFocus={() => setInsertTarget("alert")}
              onRefFocus={() => setInsertTarget("alert-ref")}
            />
            {warningDraft ? (
              <ThresholdEditor
                severity="warning"
                draft={warningDraft}
                onChange={setWarningDraft}
                onRemove={() => setWarningDraft(null)}
                error={warningError}
                advancedRef={warningAdvancedRef}
                onAdvancedFocus={() => setInsertTarget("warning")}
                onRefFocus={() => setInsertTarget("warning-ref")}
              />
            ) : (
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setWarningDraft({ ...EMPTY_THRESHOLD })}
              >
                Agregar umbral de advertencia
              </Button>
            )}
          </div>
        </CardContent>
      </Card>

      {exactOnMetrics && (
        <div className="flex items-start gap-3 rounded-md border border-amber-200 bg-amber-50 p-3 dark:border-amber-900 dark:bg-amber-950">
          <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" />
          <p className="text-sm text-amber-800 dark:text-amber-200">
            La comparación exacta (= o !=) es frágil con valores de productos,
            que suelen ser números con decimales flotantes. Considera usar
            &lt;= o &gt;=. Además, el margen no aplica con estos comparadores.
          </p>
        </div>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Vista previa</CardTitle>
          <CardDescription>
            Evaluación con los valores actuales de tus productos.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {preview ? (
            <div className="space-y-3">
              <div className="flex flex-wrap items-center gap-3">
                <StatusBadge status={preview.evaluation.status} />
                <span className="text-2xl font-bold tabular-nums">
                  {preview.evaluation.value != null ? (
                    formatAmount(currency, preview.evaluation.value)
                  ) : (
                    <span className="text-muted-foreground">sin dato</span>
                  )}
                </span>
              </div>
              <div className="space-y-1 text-sm">
                {preview.evaluation.thresholds.map((t, i) => (
                  <div
                    key={t.severity}
                    className="flex flex-wrap items-baseline gap-2"
                  >
                    <span className="text-muted-foreground">
                      Umbral de {SEVERITY_LABELS[t.severity]} hoy:
                    </span>
                    <span className="tabular-nums">
                      {t.value != null
                        ? formatAmount(currency, t.value)
                        : "sin dato"}
                    </span>
                    {preview.monitor.thresholds[i] && (
                      <span className="font-mono text-xs text-muted-foreground">
                        {t.comparator}{" "}
                        {preview.monitor.thresholds[i].displayExpression}
                      </span>
                    )}
                  </div>
                ))}
              </div>
              {preview.evaluation.margin != null && (
                <p className="text-sm font-medium tabular-nums">
                  Margen: {formatAmount(currency, preview.evaluation.margin)}
                </p>
              )}
              {preview.evaluation.noDataReason && (
                <p className="text-xs text-muted-foreground">
                  {preview.evaluation.noDataReason}
                </p>
              )}
            </div>
          ) : formError ? (
            <p className="text-sm text-destructive">{formError.error}</p>
          ) : (
            <p className="text-sm text-muted-foreground">
              Completa la expresión y el umbral de alerta para ver la vista
              previa.
            </p>
          )}
        </CardContent>
      </Card>

      <div className="flex flex-wrap items-center gap-3">
        <Button onClick={handleSubmit} disabled={saving}>
          {saving
            ? "Guardando..."
            : isEdit
              ? "Guardar cambios"
              : "Crear monitor"}
        </Button>
        <Button asChild variant="outline">
          <Link href={isEdit ? `/monitors/${monitorId}` : "/monitors"}>
            Cancelar
          </Link>
        </Button>
        {generalError && (
          <p className="text-sm text-destructive">{generalError.error}</p>
        )}
      </div>
    </div>
  );
}
