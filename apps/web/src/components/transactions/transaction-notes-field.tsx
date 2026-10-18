"use client";

import { useState, type KeyboardEvent, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { TRANSACTION_NOTES_MAX_LENGTH } from "@/lib/transactions";

const SAVE_FAILED = "No se pudo guardar la nota.";

/** Focuses the box with the caret after the text. Module level so the ref is
 *  stable and only runs when the box mounts, not on every keystroke. */
function focusAtEnd(element: HTMLTextAreaElement | null) {
  if (!element) return;
  element.focus();
  element.setSelectionRange(element.value.length, element.value.length);
}

/**
 * The detail panel's "Notas": the saved note, and an inline box to add, edit
 * or clear it through PATCH /api/transactions/[id] (an empty box removes it;
 * Cmd/Ctrl+Enter saves). `renderSection` lays the section out in the panel's
 * own style with `action` in its header. Key it by the transaction id so a
 * draft does not follow the panel to another row; `onSaved` runs after a save
 * so the panel reloads the row.
 */
export function TransactionNotesField({
  transactionId,
  notes,
  onSaved,
  renderSection,
}: {
  transactionId: string;
  notes: string | null;
  onSaved: () => void;
  renderSection: (action: ReactNode, children: ReactNode) => ReactNode;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The last saved note, shown until the panel's reload replaces `notes`, so
  // the previous note does not flash back while the row is refetched; dropped
  // once the reload lands.
  const [saved, setSaved] = useState<{
    value: string | null;
    replaced: string | null;
  } | null>(null);
  if (saved && saved.replaced !== notes) setSaved(null);
  const shown = saved && saved.replaced === notes ? saved.value : notes;

  const startEditing = () => {
    setDraft(shown ?? "");
    setError(null);
    setEditing(true);
  };

  const save = async () => {
    const next = draft.trim() || null;
    if (next === shown) {
      setEditing(false);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const res = await fetch(
        `/api/transactions/${encodeURIComponent(transactionId)}`,
        {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ notes: next }),
        }
      );
      if (!res.ok) {
        const body = await res.json().catch(() => null);
        setError(
          body?.field === "notes"
            ? `La nota puede tener hasta ${TRANSACTION_NOTES_MAX_LENGTH} caracteres.`
            : SAVE_FAILED
        );
        return;
      }
      setSaved({ value: next, replaced: notes });
      setEditing(false);
      onSaved();
    } catch {
      setError(SAVE_FAILED);
    } finally {
      setSaving(false);
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      if (!saving) void save();
    }
  };

  if (!editing) {
    return renderSection(
      <Button
        variant="outline"
        size="xs"
        onClick={startEditing}
        aria-label={shown ? "Editar nota" : "Agregar nota"}
      >
        {shown ? "Editar" : "Agregar"}
      </Button>,
      shown ? (
        <p className="text-sm break-words whitespace-pre-wrap">{shown}</p>
      ) : (
        <p className="text-sm text-muted-foreground">Sin notas</p>
      )
    );
  }

  return renderSection(
    null,
    <div className="space-y-2">
      <Textarea
        ref={focusAtEnd}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={onKeyDown}
        maxLength={TRANSACTION_NOTES_MAX_LENGTH}
        rows={4}
        placeholder="Escribe una nota"
        aria-label="Nota"
        readOnly={saving}
      />
      <p className="text-xs text-muted-foreground">
        Deja vacío para quitar la nota. Guarda con Ctrl+Enter o ⌘+Enter.
      </p>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      <div className="flex gap-2">
        <Button size="sm" onClick={() => void save()} disabled={saving}>
          {saving ? "Guardando..." : "Guardar"}
        </Button>
        <Button
          size="sm"
          variant="outline"
          onClick={() => setEditing(false)}
          disabled={saving}
        >
          Cancelar
        </Button>
      </div>
    </div>
  );
}
