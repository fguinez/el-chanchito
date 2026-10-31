"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import { SortableTableHead } from "@/components/ui/sortable-table-head";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { SELECT_CLASS } from "@/components/ui/native-select";
import { BudgetDonut, KIND_COLORS } from "@/components/budget/budget-donut";
import { FixedExpenseEditDialog } from "@/components/fixed-expenses/fixed-expense-edit-dialog";
import { useSortableData } from "@/lib/use-sortable-data";
import { cn, formatCLP, formatPercent } from "@/lib/utils";
import { FIXED_EXPENSE_KINDS, type FixedExpenseKind } from "@/lib/db/schema";
import {
  FIXED_EXPENSE_KIND_LABELS,
  fixedExpensePersonalAmount,
  fixedExpenseStatus,
  fixedExpenseTotals,
  formatActiveWindow,
  parseFixedExpenseForm,
  ratioInput,
  sharedRatioValue,
  sortByStatus,
  type FixedExpense,
} from "@/lib/fixed-expenses";
import {
  budgetDraft,
  budgetSummary,
  formatIncomeInput,
  incomeShare,
  parseIncomeInput,
} from "@/lib/budget";
import { formatLocalDate } from "@/lib/monitors/dates";
import { ChevronDown, Trash2 } from "lucide-react";

const STATUS_BADGES = {
  scheduled: { label: "Programado", variant: "outline" },
  ended: { label: "Finalizado", variant: "secondary" },
} as const;

const NAME_PLACEHOLDERS: Record<FixedExpenseKind, string> = {
  fixed: "Ej: Arriendo",
  estimated: "Ej: Supermercado",
  savings: "Ej: Fondo de emergencia",
};

/** Wait after the last keystroke before saving the income. */
const INCOME_SAVE_DELAY_MS = 600;

type SaveState = "idle" | "load-error" | "saving" | "saved" | "error";

type ItemSortKey =
  | "item"
  | "tipo"
  | "total"
  | "personal"
  | "ingreso"
  | "compartido";

function putIncome(monthlyIncome: number, keepalive = false) {
  return fetch("/api/budget-settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ monthlyIncome }),
    keepalive,
  });
}

/**
 * The monthly income field's state: what the field shows, the last valid
 * value (what the chart uses while the text is not a number), and saving
 * with a debounce, at once on blur, and on leaving the page.
 */
function useMonthlyIncome() {
  const [text, setText] = useState("");
  const [income, setIncome] = useState(0);
  const [loaded, setLoaded] = useState(false);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  // The last value sent (or loaded): a save of the same value is skipped.
  const requested = useRef<number | null>(null);
  const pending = useRef<{ timer: ReturnType<typeof setTimeout>; value: number } | null>(
    null
  );
  const sequence = useRef(0);

  useEffect(() => {
    let active = true;
    fetch("/api/budget-settings")
      .then((res) => (res.ok ? res.json() : Promise.reject(res)))
      .then((data: { monthlyIncome: number }) => {
        if (!active) return;
        requested.current = data.monthlyIncome;
        setIncome(data.monthlyIncome);
        setText(data.monthlyIncome > 0 ? formatIncomeInput(data.monthlyIncome) : "");
      })
      .catch(() => {
        if (active) setSaveState("load-error");
      })
      .finally(() => {
        if (active) setLoaded(true);
      });
    return () => {
      active = false;
    };
  }, []);

  const cancelPending = () => {
    if (pending.current) clearTimeout(pending.current.timer);
    pending.current = null;
  };

  const save = useCallback(async (value: number) => {
    cancelPending();
    if (value === requested.current) return;
    requested.current = value;
    const id = ++sequence.current;
    setSaveState("saving");
    try {
      const res = await putIncome(value);
      if (!res.ok) throw new Error(`PUT /api/budget-settings: ${res.status}`);
      if (id === sequence.current) setSaveState("saved");
    } catch {
      // Let the next edit or blur retry the same value.
      if (requested.current === value) requested.current = null;
      if (id === sequence.current) setSaveState("error");
    }
  }, []);

  // Leaving the page with a save still waiting sends it anyway.
  useEffect(
    () => () => {
      if (pending.current) {
        const { value } = pending.current;
        cancelPending();
        putIncome(value, true).catch(() => {});
      }
    },
    []
  );

  const onChange = (next: string) => {
    setText(next);
    const value = parseIncomeInput(next);
    cancelPending();
    if (value == null) return;
    setIncome(value);
    pending.current = {
      value,
      timer: setTimeout(() => void save(value), INCOME_SAVE_DELAY_MS),
    };
  };

  const onBlur = () => {
    const value = parseIncomeInput(text);
    if (value == null) return;
    setText(value > 0 ? formatIncomeInput(value) : "");
    void save(value);
  };

  const invalid = parseIncomeInput(text) == null;
  return { text, income, loaded, saveState, invalid, onChange, onBlur };
}

function SaveStatus({ state, invalid }: { state: SaveState; invalid: boolean }) {
  if (invalid) {
    return (
      <span className="text-destructive dark:text-destructive-foreground">
        Escribe un monto en pesos, sin decimales
      </span>
    );
  }
  switch (state) {
    case "saving":
      return <span>Guardando…</span>;
    case "saved":
      return <span>Guardado</span>;
    case "load-error":
      return (
        <span className="text-destructive dark:text-destructive-foreground">
          No se pudo cargar el ingreso guardado
        </span>
      );
    case "error":
      return (
        <span className="text-destructive dark:text-destructive-foreground">
          No se pudo guardar
        </span>
      );
    default:
      return <span>Se guarda automáticamente</span>;
  }
}

function KindBadge({ kind }: { kind: FixedExpenseKind }) {
  return (
    <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-sm">
      <span
        aria-hidden
        className="size-2.5 shrink-0 rounded-sm"
        style={{ backgroundColor: KIND_COLORS[kind] }}
      />
      {FIXED_EXPENSE_KIND_LABELS[kind]}
    </span>
  );
}

export default function BudgetPage() {
  const monthlyIncome = useMonthlyIncome();
  const income = monthlyIncome.income;

  const [expenses, setExpenses] = useState<FixedExpense[]>([]);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<FixedExpenseKind>("fixed");
  const [amount, setAmount] = useState("");
  const [isShared, setIsShared] = useState(false);
  const [sharedRatio, setSharedRatio] = useState(ratioInput(null));
  const [activeFrom, setActiveFrom] = useState("");
  const [activeTo, setActiveTo] = useState("");
  const [showOptions, setShowOptions] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const loadExpenses = () => {
    fetch("/api/fixed-expenses")
      .then((res) => res.json())
      .then(setExpenses)
      .catch(console.error);
  };

  useEffect(() => {
    loadExpenses();
  }, []);

  const handleAdd = async (event: React.FormEvent) => {
    event.preventDefault();
    const parsed = parseFixedExpenseForm({
      name,
      kind,
      amount,
      isShared,
      sharedRatio,
      activeFrom,
      activeTo,
    });
    if (!parsed.ok) {
      setFormError(parsed.error);
      return;
    }

    setSaving(true);
    setFormError(null);
    try {
      const res = await fetch("/api/fixed-expenses", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(parsed.body),
      });

      if (res.ok) {
        setName("");
        setAmount("");
        setIsShared(false);
        setActiveFrom("");
        setActiveTo("");
        loadExpenses();
      } else {
        const data = await res.json().catch(() => null);
        setFormError(data?.error ?? "No se pudo agregar el item");
      }
    } catch {
      setFormError("No se pudo agregar el item");
    } finally {
      setSaving(false);
    }
  };

  const handleDelete = async (id: string) => {
    await fetch("/api/fixed-expenses", {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
    loadExpenses();
  };

  // Vigencia is decided on the browser's local day, never the UTC date.
  const today = formatLocalDate(new Date());
  // While the add form holds a usable amount, the chart already counts it.
  const draft = budgetDraft({
    name,
    kind,
    amount,
    isShared,
    sharedRatio,
    activeFrom,
    activeTo,
  });
  const summary = budgetSummary(expenses, income, today, draft);
  const totals = fixedExpenseTotals(expenses, today);
  const optionsOpen = showOptions || isShared || activeFrom !== "" || activeTo !== "";

  const registeredLabel =
    expenses.length === 1 ? "1 item" : `${expenses.length} items`;
  const activeLabel =
    totals.activeCount === 1
      ? "1 vigente hoy"
      : `${totals.activeCount} vigentes hoy`;

  const activeShare = useCallback(
    (expense: FixedExpense): number | null =>
      fixedExpenseStatus(expense, today) === "active"
        ? incomeShare(fixedExpensePersonalAmount(expense), income)
        : null,
    [today, income]
  );

  const getValue = useCallback(
    (expense: FixedExpense, key: ItemSortKey): string | number | null => {
      switch (key) {
        case "item":
          return expense.name;
        case "tipo":
          return FIXED_EXPENSE_KINDS.indexOf(expense.kind);
        case "total":
          return expense.amount;
        case "personal":
          return fixedExpensePersonalAmount(expense);
        case "ingreso":
          return activeShare(expense);
        case "compartido":
          // "Compartido" renders a share % (or "No", i.e. 0%): sort numerically.
          return expense.isShared ? sharedRatioValue(expense.sharedRatio) : 0;
      }
    },
    [activeShare]
  );

  // Unsorted, rows group by kind, active ones first within each kind.
  const grouped = useMemo(
    () =>
      sortByStatus(expenses, today).sort(
        (a, b) =>
          FIXED_EXPENSE_KINDS.indexOf(a.kind) - FIXED_EXPENSE_KINDS.indexOf(b.kind)
      ),
    [expenses, today]
  );
  const { sorted, sort, toggleSort } = useSortableData(grouped, getValue);
  // Bridge the generic header's string key to our typed key union.
  const handleSort = (key: string) => toggleSort(key as ItemSortKey);
  const sortProps = (key: ItemSortKey) => ({
    columnKey: key,
    active: sort?.key === key,
    direction: sort?.key === key ? sort.direction : undefined,
    onSort: handleSort,
  });

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-2xl font-bold">Presupuesto mensual</h2>
        <p className="text-sm text-muted-foreground">En qué se va tu sueldo</p>
      </div>

      {/* Income and its split */}
      <Card>
        <CardContent className="grid gap-8 lg:grid-cols-[16rem_minmax(0,1fr)]">
          <div className="space-y-5">
            <div className="space-y-1.5">
              <label htmlFor="monthly-income" className="block text-sm font-medium">
                Ingreso mensual
              </label>
              <div className="relative">
                <span className="pointer-events-none absolute inset-y-0 left-3 flex items-center text-sm text-muted-foreground">
                  $
                </span>
                <Input
                  id="monthly-income"
                  inputMode="numeric"
                  autoComplete="off"
                  placeholder="0"
                  className="pl-7 text-lg tabular-nums"
                  value={monthlyIncome.text}
                  disabled={!monthlyIncome.loaded}
                  aria-invalid={monthlyIncome.invalid || undefined}
                  aria-describedby="monthly-income-status"
                  onChange={(e) => monthlyIncome.onChange(e.target.value)}
                  onBlur={monthlyIncome.onBlur}
                />
              </div>
              <p
                id="monthly-income-status"
                aria-live="polite"
                className="text-xs text-muted-foreground"
              >
                <SaveStatus
                  state={monthlyIncome.saveState}
                  invalid={monthlyIncome.invalid}
                />
              </p>
            </div>

            <dl className="space-y-3 text-sm">
              <div>
                <dt className="text-muted-foreground">Asignado</dt>
                <dd className="font-semibold tabular-nums">
                  {formatCLP(summary.allocated)}
                  {summary.allocatedShare != null && (
                    <span className="ml-2 font-normal text-muted-foreground">
                      {formatPercent(summary.allocatedShare)} del ingreso
                    </span>
                  )}
                </dd>
              </div>
              <div>
                <dt className="text-muted-foreground">
                  {summary.deficit > 0 && income > 0 ? "Déficit" : "Sobra"}
                </dt>
                <dd
                  className={cn(
                    "font-semibold tabular-nums",
                    summary.deficit > 0 &&
                      income > 0 &&
                      "text-destructive dark:text-destructive-foreground"
                  )}
                >
                  {income === 0
                    ? "Sin ingreso"
                    : formatCLP(summary.deficit > 0 ? summary.deficit : summary.remaining)}
                </dd>
              </div>
            </dl>
          </div>

          <BudgetDonut summary={summary} draftName={draft?.name} />
        </CardContent>
      </Card>

      {/* Add form */}
      <Card>
        <CardHeader>
          <CardTitle>Agregar item</CardTitle>
          <CardDescription>
            El gráfico muestra el item mientras lo escribes; se guarda al
            agregarlo.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form noValidate onSubmit={handleAdd} className="space-y-3">
            <div className="flex flex-wrap items-end gap-3">
              <div className="min-w-48 flex-1">
                <label
                  htmlFor="item-name"
                  className="mb-1 block text-sm text-muted-foreground"
                >
                  Nombre
                </label>
                <Input
                  id="item-name"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder={NAME_PLACEHOLDERS[kind]}
                />
              </div>
              <div className="w-48">
                <label
                  htmlFor="item-kind"
                  className="mb-1 block text-sm text-muted-foreground"
                >
                  Tipo
                </label>
                <select
                  id="item-kind"
                  className={SELECT_CLASS}
                  value={kind}
                  onChange={(e) => setKind(e.target.value as FixedExpenseKind)}
                >
                  {FIXED_EXPENSE_KINDS.map((option) => (
                    <option key={option} value={option}>
                      {FIXED_EXPENSE_KIND_LABELS[option]}
                    </option>
                  ))}
                </select>
              </div>
              <div className="w-40">
                <label
                  htmlFor="item-amount"
                  className="mb-1 block text-sm text-muted-foreground"
                >
                  Monto (CLP)
                </label>
                <Input
                  id="item-amount"
                  type="number"
                  value={amount}
                  onChange={(e) => setAmount(e.target.value)}
                  placeholder="1000000"
                />
              </div>
              <Button type="submit" disabled={saving}>
                {saving ? "..." : "Agregar"}
              </Button>
            </div>

            <button
              type="button"
              aria-expanded={optionsOpen}
              aria-controls="item-options"
              onClick={() => setShowOptions(!optionsOpen)}
              className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
            >
              <ChevronDown
                className={cn("h-3.5 w-3.5 transition-transform", optionsOpen && "rotate-180")}
              />
              Compartido y vigencia
            </button>
            {optionsOpen && (
              <div id="item-options" className="flex flex-wrap items-end gap-3">
                <div className="flex h-9 items-center gap-2">
                  <input
                    type="checkbox"
                    id="item-shared"
                    checked={isShared}
                    onChange={(e) => setIsShared(e.target.checked)}
                    className="h-4 w-4"
                  />
                  <label htmlFor="item-shared" className="text-sm">
                    Compartido
                  </label>
                </div>
                {isShared && (
                  <div className="w-24">
                    <label
                      htmlFor="item-ratio"
                      className="mb-1 block text-sm text-muted-foreground"
                    >
                      Ratio
                    </label>
                    <Input
                      id="item-ratio"
                      type="number"
                      step="0.01"
                      min="0"
                      max="1"
                      value={sharedRatio}
                      onChange={(e) => setSharedRatio(e.target.value)}
                    />
                  </div>
                )}
                <div className="w-40">
                  <label
                    htmlFor="item-active-from"
                    className="mb-1 block text-sm text-muted-foreground"
                  >
                    Vigente desde
                  </label>
                  <Input
                    id="item-active-from"
                    type="date"
                    value={activeFrom}
                    onChange={(e) => setActiveFrom(e.target.value)}
                  />
                </div>
                <div className="w-40">
                  <label
                    htmlFor="item-active-to"
                    className="mb-1 block text-sm text-muted-foreground"
                  >
                    Vigente hasta
                  </label>
                  <Input
                    id="item-active-to"
                    type="date"
                    value={activeTo}
                    onChange={(e) => setActiveTo(e.target.value)}
                  />
                </div>
              </div>
            )}
            {formError && <p className="text-sm text-destructive">{formError}</p>}
          </form>
        </CardContent>
      </Card>

      {/* Table */}
      <Card>
        <CardHeader>
          <CardTitle>Items del presupuesto</CardTitle>
          <CardDescription>
            {registeredLabel}, {activeLabel}. Total vigente antes de compartir:{" "}
            {formatCLP(totals.full)}.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {expenses.length === 0 ? (
            <p className="text-muted-foreground">
              Todavía no hay items. Agrega uno arriba.
            </p>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableTableHead label="Item" {...sortProps("item")} />
                  <SortableTableHead label="Tipo" {...sortProps("tipo")} />
                  <SortableTableHead
                    label="Monto total"
                    align="right"
                    {...sortProps("total")}
                  />
                  <SortableTableHead
                    label="Monto personal"
                    align="right"
                    {...sortProps("personal")}
                  />
                  <SortableTableHead
                    label="% del ingreso"
                    align="right"
                    {...sortProps("ingreso")}
                  />
                  <SortableTableHead
                    label="Compartido"
                    align="right"
                    {...sortProps("compartido")}
                  />
                  <TableHead>Vigencia</TableHead>
                  <TableHead className="w-16"></TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {sorted.map((expense) => {
                  const ratio = sharedRatioValue(expense.sharedRatio);
                  const personal = fixedExpensePersonalAmount(expense);
                  const status = fixedExpenseStatus(expense, today);
                  const badge = status === "active" ? null : STATUS_BADGES[status];
                  const share = activeShare(expense);
                  return (
                    <TableRow
                      key={expense.id}
                      className={cn(badge && "text-muted-foreground")}
                    >
                      <TableCell className="font-medium">
                        <span className="flex items-center gap-2">
                          {expense.name}
                          {badge && (
                            <Badge variant={badge.variant}>{badge.label}</Badge>
                          )}
                        </span>
                      </TableCell>
                      <TableCell>
                        <KindBadge kind={expense.kind} />
                      </TableCell>
                      <TableCell className="text-right">
                        {formatCLP(expense.amount)}
                      </TableCell>
                      <TableCell className="text-right">
                        {formatCLP(personal)}
                      </TableCell>
                      <TableCell className="text-right tabular-nums">
                        {share == null ? (
                          <span className="text-muted-foreground">-</span>
                        ) : (
                          formatPercent(share)
                        )}
                      </TableCell>
                      <TableCell className="text-right">
                        {expense.isShared
                          ? `${Math.round(ratio * 100)}%`
                          : "No"}
                      </TableCell>
                      <TableCell>{formatActiveWindow(expense)}</TableCell>
                      <TableCell>
                        <div className="flex items-center gap-3">
                          <FixedExpenseEditDialog
                            expense={expense}
                            onSaved={loadExpenses}
                          />
                          <button
                            onClick={() => handleDelete(expense.id)}
                            aria-label="Eliminar"
                            className="text-muted-foreground hover:text-destructive"
                          >
                            <Trash2 className="h-4 w-4" />
                          </button>
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
    </div>
  );
}
