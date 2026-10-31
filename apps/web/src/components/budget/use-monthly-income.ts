"use client";

// The Presupuesto page's "Ingreso mensual" field: what it shows, the last
// valid value (what the chart uses while the text is half typed), and saving
// to /api/budget-settings.
//
// Saves are serialized: one PUT at a time, and when it lands the latest value
// asked for is sent next, so a slow PUT can never overwrite a newer one. A
// save runs 600 ms after the last keystroke, at once on blur, and on leaving
// the page or closing the tab (keepalive). Nothing is ever saved until the
// stored income has loaded, so a failed load cannot wipe it with an empty
// field.

import { useCallback, useEffect, useRef, useState } from "react";
import {
  formatIncomeInput,
  isIncomePrefix,
  parseIncomeInput,
} from "@/lib/budget";

/** Wait after the last keystroke before saving the income. */
export const INCOME_SAVE_DELAY_MS = 600;

export type IncomeStatus =
  | "loading"
  | "load-error"
  | "idle"
  | "dirty"
  | "saving"
  | "saved"
  | "error";

function putIncome(monthlyIncome: number, keepalive: boolean) {
  return fetch("/api/budget-settings", {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ monthlyIncome }),
    keepalive,
  });
}

function display(value: number): string {
  return value > 0 ? formatIncomeInput(value) : "";
}

export function useMonthlyIncome() {
  const [text, setText] = useState("");
  const [income, setIncome] = useState(0);
  const [status, setStatus] = useState<IncomeStatus>("loading");
  const [showError, setShowError] = useState(false);
  /** What the server holds: loaded, then each successful PUT. */
  const saved = useRef<number | null>(null);
  /** The value the save loop sends next. */
  const target = useRef<number | null>(null);
  /** The latest valid value typed; null while the field holds an invalid one. */
  const typed = useRef<number | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const running = useRef(false);
  const unmounted = useRef(false);
  /** The tab is being closed or reloaded (pagehide), so PUTs use keepalive. */
  const leaving = useRef(false);

  const load = useCallback(() => {
    fetch("/api/budget-settings")
      .then((res) => (res.ok ? res.json() : Promise.reject(res)))
      .then((data: { monthlyIncome: number }) => {
        if (unmounted.current) return;
        saved.current = data.monthlyIncome;
        typed.current = data.monthlyIncome;
        setIncome(data.monthlyIncome);
        setText(display(data.monthlyIncome));
        setStatus("idle");
      })
      .catch(() => {
        if (!unmounted.current) setStatus("load-error");
      });
  }, []);

  /** Sends target until the server holds it; one PUT in flight at most. */
  const flush = useCallback(async () => {
    if (running.current) return;
    running.current = true;
    try {
      while (target.current != null && target.current !== saved.current) {
        const value = target.current;
        if (!unmounted.current) setStatus("saving");
        try {
          const res = await putIncome(
            value,
            unmounted.current || leaving.current
          );
          if (!res.ok) throw new Error(`PUT /api/budget-settings: ${res.status}`);
          saved.current = value;
        } catch {
          // target stays unsaved: the next edit, blur or Reintentar retries.
          if (!unmounted.current) setStatus("error");
          return;
        }
      }
      if (!unmounted.current) {
        setStatus(typed.current === saved.current ? "saved" : "dirty");
      }
    } finally {
      running.current = false;
    }
  }, []);

  const cancelTimer = () => {
    if (timer.current) clearTimeout(timer.current);
    timer.current = null;
  };

  const save = useCallback(
    (value: number) => {
      cancelTimer();
      // Never before the stored value is known (see the module comment).
      if (saved.current == null) return;
      target.current = value;
      void flush();
    },
    [flush]
  );

  useEffect(() => {
    unmounted.current = false;
    load();
    // A value typed but not yet sent still goes out (keepalive); behind the
    // PUT in flight, if any, which the loop then follows with it.
    const sendPending = () => {
      cancelTimer();
      if (saved.current != null && typed.current != null) {
        target.current = typed.current;
        void flush();
      }
    };
    // Closing or reloading the tab never unmounts the page.
    const onPageHide = () => {
      leaving.current = true;
      sendPending();
    };
    const onPageShow = () => {
      leaving.current = false;
    };
    window.addEventListener("pagehide", onPageHide);
    window.addEventListener("pageshow", onPageShow);
    return () => {
      window.removeEventListener("pagehide", onPageHide);
      window.removeEventListener("pageshow", onPageShow);
      unmounted.current = true;
      sendPending();
    };
  }, [load, flush]);

  const onChange = (next: string) => {
    setText(next);
    const parsed = parseIncomeInput(next);
    if (!parsed.ok) {
      cancelTimer();
      // Leaving the page now must not save a value the field no longer shows.
      typed.current = null;
      setShowError(parsed.reason === "range" || !isIncomePrefix(next));
      setStatus("dirty");
      return;
    }
    setShowError(false);
    typed.current = parsed.value;
    setIncome(parsed.value);
    if (parsed.value !== saved.current) setStatus("dirty");
    cancelTimer();
    timer.current = setTimeout(() => save(parsed.value), INCOME_SAVE_DELAY_MS);
  };

  const onBlur = () => {
    const parsed = parseIncomeInput(text);
    if (!parsed.ok) {
      setShowError(true);
      return;
    }
    setText(display(parsed.value));
    save(parsed.value);
  };

  /** After a failed load, load again; after a failed save, save again. */
  const retry = () => {
    if (status === "load-error") {
      setStatus("loading");
      load();
    } else if (typed.current != null) {
      save(typed.current);
    }
  };

  const parsed = parseIncomeInput(text);
  const error =
    showError && !parsed.ok
      ? parsed.reason === "range"
        ? "El monto es demasiado grande"
        : "Escribe el monto en pesos, sin decimales (ej: 2.500.000)"
      : null;

  return {
    text,
    income,
    status,
    error,
    disabled: status === "loading" || status === "load-error",
    onChange,
    onBlur,
    retry,
  };
}
